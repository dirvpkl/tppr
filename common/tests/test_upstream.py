import socket
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import upstream


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stub_http(expect_auth: str | None, stop: threading.Event) -> int:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.5)
    port = int(listener.getsockname()[1])

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    def handle(conn: socket.socket) -> None:
        with conn:
            try:
                head = upstream.read_http_head(conn, 5.0)
                if expect_auth is not None and expect_auth not in head:
                    conn.sendall(b"HTTP/1.1 407 Required\r\nContent-Length: 0\r\n\r\n")
                    return
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                return

    threading.Thread(target=serve, daemon=True).start()
    return port


def _stub_silent_http(stop: threading.Event) -> int:
    """Upstream that reads the CONNECT head and closes without answering.

    The TCP-connectable-but-not-a-proxy shape that fills a scraped pool. The
    head is drained first so the close is a graceful FIN rather than an RST,
    which is what leaves the caller with an empty response instead of a reset.
    """
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.5)
    port = int(listener.getsockname()[1])

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    def handle(conn: socket.socket) -> None:
        with conn:
            try:
                upstream.read_http_head(conn, 5.0)
            except OSError:
                return

    threading.Thread(target=serve, daemon=True).start()
    return port


def _stub_socks5(stop: threading.Event) -> int:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.5)
    port = int(listener.getsockname()[1])

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    def handle(conn: socket.socket) -> None:
        with conn:
            try:
                greet = upstream.read_exact(conn, 2, 5.0)
                if len(greet) != 2:
                    return
                upstream.read_exact(conn, greet[1], 5.0)
                conn.sendall(b"\x05\x00")
                header = upstream.read_exact(conn, 4, 5.0)
                kind = header[3]
                if kind == 0x01:
                    upstream.read_exact(conn, 6, 5.0)
                elif kind == 0x03:
                    size = upstream.read_exact(conn, 1, 5.0)
                    upstream.read_exact(conn, (size[0] if size else 0) + 2, 5.0)
                else:
                    return
                conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            except OSError:
                return

    threading.Thread(target=serve, daemon=True).start()
    return port


class UpstreamTests(unittest.TestCase):
    def test_http_no_auth(self) -> None:
        stop = threading.Event()
        port = _stub_http(None, stop)
        try:
            node: dict[str, object] = {
                "type": "http",
                "server": "127.0.0.1",
                "port": port,
            }
            sock = upstream.connect_upstream(node, "example.com", 443, 5.0)
            sock.close()
        finally:
            stop.set()

    def test_http_with_auth(self) -> None:
        stop = threading.Event()
        port = _stub_http("dXNlcjpwYXNz", stop)
        try:
            node: dict[str, object] = {
                "type": "http",
                "server": "127.0.0.1",
                "port": port,
                "username": "user",
                "password": "pass",
            }
            sock = upstream.connect_upstream(node, "example.com", 443, 5.0)
            sock.close()
        finally:
            stop.set()

    def test_http_rejected(self) -> None:
        stop = threading.Event()
        port = _stub_http("dXNlcjpwYXNz", stop)
        try:
            node: dict[str, object] = {
                "type": "http",
                "server": "127.0.0.1",
                "port": port,
                "username": "user",
                "password": "wrong",
            }
            with self.assertRaises(OSError):
                upstream.connect_upstream(node, "example.com", 443, 5.0)
        finally:
            stop.set()

    def test_http_closed_without_head(self) -> None:
        # Must stay an OSError: the pool picks a random node per connection and
        # treats OSError as "this node is dead, try the next one". Any other
        # exception type escapes the retry loop and kills the request.
        stop = threading.Event()
        port = _stub_silent_http(stop)
        try:
            node: dict[str, object] = {
                "type": "http",
                "server": "127.0.0.1",
                "port": port,
            }
            with self.assertRaises(OSError):
                upstream.connect_upstream(node, "example.com", 443, 5.0)
        finally:
            stop.set()

    def test_socks5_no_auth(self) -> None:
        stop = threading.Event()
        port = _stub_socks5(stop)
        try:
            node: dict[str, object] = {
                "type": "socks5",
                "server": "127.0.0.1",
                "port": port,
            }
            sock = upstream.connect_upstream(node, "example.com", 443, 5.0)
            sock.close()
        finally:
            stop.set()

    def test_socks5_refused(self) -> None:
        node: dict[str, object] = {
            "type": "socks5",
            "server": "127.0.0.1",
            "port": _free_port(),
        }
        with self.assertRaises(OSError):
            upstream.connect_upstream(node, "example.com", 443, 5.0)

    def test_bad_node_rejected(self) -> None:
        with self.assertRaises(ValueError):
            upstream.connect_upstream({"type": "vless"}, "example.com", 443, 5.0)
        with self.assertRaises(ValueError):
            upstream.connect_upstream({"type": "http"}, "example.com", 443, 5.0)
        with self.assertRaises(ValueError):
            upstream.connect_upstream(
                {"type": "http", "server": "127.0.0.1", "port": 99999},
                "example.com",
                443,
                5.0,
            )


if __name__ == "__main__":
    unittest.main()
