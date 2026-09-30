from __future__ import annotations

import socket
import ssl
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import probe

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEST_CERT = str(FIXTURES / "test-cert.pem")
TEST_KEY = str(FIXTURES / "test-key.pem")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_request(conn: socket.socket) -> tuple[bytes, bytes]:
    """Read one request head; leftover bytes after the head terminator belong
    to the body (a single segment usually carries both) and are returned."""
    data = b""
    conn.settimeout(5.0)
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:
            raise OSError("head too large")
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _read_head(conn: socket.socket) -> bytes:
    """Head-only reader for CONNECT handshakes and bodyless requests, where
    nothing follows the terminator by protocol."""
    return _read_request(conn)[0]


def _parse_request(head: bytes, body: bytes) -> dict[str, object]:
    lines = head.decode("latin-1").split("\r\n")
    method, path, _ = lines[0].split(" ", 2)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return {"method": method, "path": path, "headers": headers, "body": body}


class EchoServer:
    """Canned HTTP(S) origin. Records the last request it served."""

    def __init__(
        self,
        status: int = 200,
        headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"ok",
        delay_s: float = 0.0,
        use_tls: bool = False,
    ) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.delay_s = delay_s
        self.use_tls = use_tls
        self.requests: list[dict[str, object]] = []
        self._stop = threading.Event()
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.5)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> EchoServer:
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._listener.close()

    def _serve(self) -> None:
        context = None
        if self.use_tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(TEST_CERT, TEST_KEY)
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            raw: socket.socket = conn
            if context is not None:
                try:
                    raw = context.wrap_socket(conn, server_side=True)
                except OSError:
                    conn.close()
                    continue
            threading.Thread(target=self._handle, args=(raw,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                head, body = _read_request(conn)
                if not head:
                    return
                length = 0
                for line in head.decode("latin-1").split("\r\n")[1:]:
                    name, _, value = line.partition(":")
                    if (
                        name.strip().lower() == "content-length"
                        and value.strip().isdigit()
                    ):
                        length = int(value.strip())
                while len(body) < length:
                    chunk = conn.recv(min(4096, length - len(body)))
                    if not chunk:
                        break
                    body += chunk
                self.requests.append(_parse_request(head, body))
                if self.delay_s:
                    time.sleep(self.delay_s)
                conn.sendall(
                    f"HTTP/1.1 {self.status} X\r\n".encode()
                    + b"".join(f"{k}: {v}\r\n".encode() for k, v in self.headers)
                    + f"Content-Length: {len(self.body)}\r\n\r\n".encode()
                    + self.body
                )
            except OSError:
                return


def _splice(first: socket.socket, second: socket.socket, stop: threading.Event) -> None:
    first.settimeout(0.5)
    second.settimeout(0.5)
    while not stop.is_set():
        try:
            chunk = first.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            return
        if not chunk:
            return
        try:
            second.sendall(chunk)
        except OSError:
            return


class ConnectProxy:
    """Minimal HTTP CONNECT proxy splicing bytes to a fixed target."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self.target_host = target_host
        self.target_port = target_port
        self._stop = threading.Event()
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.5)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> ConnectProxy:
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._listener.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        with client:
            try:
                head = _read_head(client)
                if not head.decode("latin-1").split("\r\n")[0].startswith("CONNECT "):
                    return
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                upstream = socket.create_connection(
                    (self.target_host, self.target_port), timeout=5.0
                )
            except OSError:
                return
            with upstream:
                first = threading.Thread(
                    target=_splice, args=(client, upstream, self._stop), daemon=True
                )
                second = threading.Thread(
                    target=_splice, args=(upstream, client, self._stop), daemon=True
                )
                first.start()
                second.start()
                first.join(timeout=10.0)
                second.join(timeout=10.0)


def _http_node(port: int) -> dict[str, object]:
    return {"name": "n", "type": "http", "server": "127.0.0.1", "port": port}


def _probe_for(server: EchoServer, **overrides: object) -> probe.ProbeSpec:
    base: dict[str, object] = {"url": f"http://127.0.0.1:{server.port}/"}
    base.update(overrides)
    return probe.parse_probe_spec(base)


class ProbeParseTests(unittest.TestCase):
    def test_probe_defaults(self) -> None:
        spec = probe.parse_probe_spec({"url": "https://example.com/health"})
        self.assertEqual(spec.method, "GET")
        self.assertEqual(spec.host, "example.com")
        self.assertEqual(spec.port, 443)
        self.assertTrue(spec.use_tls)
        self.assertEqual(spec.path, "/health")
        self.assertEqual(spec.headers, ())
        self.assertEqual(spec.cookies, ())
        self.assertEqual(spec.body, b"")
        self.assertIsNone(spec.tls_cafile)

    def test_probe_full_shape(self) -> None:
        spec = probe.parse_probe_spec(
            {
                "url": "http://example.com:8080/a?b=c",
                "method": "PATCH",
                "headers": {"X-A": "1"},
                "cookies": {"s": "abc"},
                "body": '{"x":1}',
                "tls_cafile": TEST_CERT,
            }
        )
        self.assertEqual(spec.method, "PATCH")
        self.assertFalse(spec.use_tls)
        self.assertEqual(spec.port, 8080)
        self.assertEqual(spec.path, "/a?b=c")
        self.assertEqual(spec.headers, (("X-A", "1"),))
        self.assertEqual(spec.cookies, (("s", "abc"),))
        self.assertEqual(spec.body, '{"x":1}'.encode())
        self.assertEqual(spec.tls_cafile, TEST_CERT)

    def test_probe_rejects_bad_shapes(self) -> None:
        for raw in (
            {"url": "socks5://example.com:1080"},
            {"url": "https://"},
            {"url": "https://example.com", "method": "get"},
            {"url": "https://example.com", "method": "GE T"},
            {"url": "https://example.com", "headers": ["x"]},
            {
                "url": "https://example.com",
                "headers": {"Cookie": "a=b"},
                "cookies": {"s": "abc"},
            },
            {"url": "https://example.com", "body": 5},
            {"url": "https://example.com", "tls_cafile": "/nonexistent.pem"},
        ):
            with self.assertRaises(ValueError, msg=str(raw)):
                probe.parse_probe_spec(raw)

    def test_expect_defaults_to_anything_goes(self) -> None:
        expect = probe.parse_expect_spec(None)
        self.assertEqual(expect, probe.ExpectSpec())

    def test_expect_full_shape(self) -> None:
        expect = probe.parse_expect_spec(
            {
                "status": [200, "2xx", 400],
                "headers": [
                    {"name": "Content-Type", "contains": "json"},
                    {"name": "X-Mode", "equals": "live"},
                    {"name": "X-Id", "regex": "^[0-9]+$"},
                ],
                "body": {"contains": "ok", "regex": "ok"},
                "cookies": [{"name": "uid"}, {"name": "role", "equals": "admin"}],
                "max_latency_ms": 1500,
            }
        )
        self.assertEqual(expect.status, (200, "2xx", 400))
        self.assertEqual(len(expect.headers), 3)
        self.assertEqual(expect.cookies[1], probe.CookieRule("role", "admin"))
        self.assertEqual(expect.max_latency_ms, 1500)

    def test_expect_rejects_bad_shapes(self) -> None:
        cases: tuple[object, ...] = (
            {"status": [99]},
            {"status": ["3x"]},
            {"status": ["ok"]},
            {"headers": [{"name": "X-A"}]},
            {"headers": [{"name": "X-A", "equals": "1", "contains": "1"}]},
            {"headers": [{"name": "not a token!", "equals": "1"}]},
            {"headers": [{"name": "X-A", "regex": "(broken"}]},
            {"body": {}},
            {"cookies": [{"name": "uid", "equals": 5}]},
            {"max_latency_ms": 0},
            {"max_latency_ms": "fast"},
        )
        for raw in cases:
            with self.assertRaises(ValueError, msg=str(raw)):
                probe.parse_expect_spec(raw)


class ProbeExchangeTests(unittest.TestCase):
    def test_sends_configured_request(self) -> None:
        server = EchoServer().start()
        proxy = ConnectProxy("127.0.0.1", server.port).start()
        try:
            spec = probe.parse_probe_spec(
                {
                    "url": f"http://127.0.0.1:{server.port}/ping?x=1",
                    "method": "PATCH",
                    "headers": {"X-Test": "yes"},
                    "cookies": {"s": "abc"},
                    "body": "payload",
                }
            )
            result = probe.probe_node(
                _http_node(proxy.port), spec, probe.parse_expect_spec(None), 5.0
            )
            self.assertEqual(result.verdict, probe.LIVE)
            self.assertEqual(result.status, 200)
            request = server.requests[0]
            self.assertEqual(request["method"], "PATCH")
            self.assertEqual(request["path"], "/ping?x=1")
            headers = cast(dict[str, str], request["headers"])
            self.assertEqual(headers["x-test"], "yes")
            self.assertEqual(headers["cookie"], "s=abc")
            self.assertEqual(request["body"], b"payload")
        finally:
            proxy.close()
            server.close()

    def test_unexpected_status_is_dead_but_expected_400_is_live(self) -> None:
        server = EchoServer(status=400, body=b"nope").start()
        proxy = ConnectProxy("127.0.0.1", server.port).start()
        try:
            spec = _probe_for(server)
            node = _http_node(proxy.port)
            strict = probe.probe_node(node, spec, probe.parse_expect_spec(None), 5.0)
            self.assertEqual(strict.verdict, probe.LIVE)
            picky = probe.probe_node(
                node, spec, probe.parse_expect_spec({"status": [200]}), 5.0
            )
            self.assertEqual(picky.verdict, probe.DEAD)
            self.assertEqual(picky.status, 400)
            self.assertIn("status 400", picky.reason)
            allowed = probe.probe_node(
                node, spec, probe.parse_expect_spec({"status": [200, 400]}), 5.0
            )
            self.assertEqual(allowed.verdict, probe.LIVE)
            ranged = probe.probe_node(
                node, spec, probe.parse_expect_spec({"status": ["4xx"]}), 5.0
            )
            self.assertEqual(ranged.verdict, probe.LIVE)
        finally:
            proxy.close()
            server.close()

    def test_header_body_cookie_expectations(self) -> None:
        server = EchoServer(
            headers=(
                ("Content-Type", "application/json"),
                ("Set-Cookie", "uid=42; Path=/"),
            ),
            body=b'{"ok":true}',
        ).start()
        proxy = ConnectProxy("127.0.0.1", server.port).start()
        try:
            spec = _probe_for(server)
            node = _http_node(proxy.port)
            good = probe.parse_expect_spec(
                {
                    "status": [200],
                    "headers": [{"name": "Content-Type", "contains": "json"}],
                    "body": {"contains": '"ok":true', "regex": r"\{.*\}"},
                    "cookies": [{"name": "uid", "equals": "42"}],
                }
            )
            self.assertEqual(
                probe.probe_node(node, spec, good, 5.0).verdict, probe.LIVE
            )
            bad = probe.parse_expect_spec(
                {
                    "status": [200],
                    "headers": [{"name": "X-Missing", "equals": "1"}],
                    "body": {"contains": "nope"},
                    "cookies": [{"name": "uid", "equals": "7"}],
                }
            )
            result = probe.probe_node(node, spec, bad, 5.0)
            self.assertEqual(result.verdict, probe.DEAD)
        finally:
            proxy.close()
            server.close()

    def test_latency_limit(self) -> None:
        server = EchoServer(delay_s=0.5).start()
        proxy = ConnectProxy("127.0.0.1", server.port).start()
        try:
            spec = _probe_for(server)
            result = probe.probe_node(
                _http_node(proxy.port),
                spec,
                probe.parse_expect_spec({"max_latency_ms": 50}),
                5.0,
            )
            self.assertEqual(result.verdict, probe.DEAD)
            self.assertIn("latency", result.reason)
        finally:
            proxy.close()
            server.close()

    def test_unsupported_type_is_skipped(self) -> None:
        spec = probe.parse_probe_spec({"url": "https://example.com/"})
        result = probe.probe_node(
            {"name": "n", "type": "vless", "server": "x", "port": 443},
            spec,
            probe.parse_expect_spec(None),
            5.0,
        )
        self.assertEqual(result.verdict, probe.SKIPPED)

    def test_refused_tunnel_is_dead(self) -> None:
        spec = probe.parse_probe_spec({"url": "https://example.com/"})
        result = probe.probe_node(
            {"name": "n", "type": "http", "server": "127.0.0.1", "port": 1},
            spec,
            probe.parse_expect_spec(None),
            2.0,
        )
        self.assertEqual(result.verdict, probe.DEAD)
        self.assertIn("tunnel:", result.reason)

    def test_body_is_capped(self) -> None:
        big = b"y" * (probe.MAX_BODY_BYTES + 100)
        first, peer = socket.socketpair()
        try:
            server_done = threading.Event()

            def serve() -> None:
                with peer:
                    try:
                        _read_head(peer)
                        peer.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Length: "
                            + str(len(big)).encode()
                            + b"\r\n\r\n"
                            + big
                        )
                    except OSError:
                        pass
                    finally:
                        server_done.set()

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            spec = probe.parse_probe_spec({"url": "http://example.com/"})
            response = probe.probe_over_socket(first, spec, 5.0)
            self.assertEqual(len(response.body), probe.MAX_BODY_BYTES)
            server_done.wait(timeout=5.0)
        finally:
            first.close()

    def test_tls_through_proxy(self) -> None:
        server = EchoServer(use_tls=True).start()
        proxy = ConnectProxy("127.0.0.1", server.port).start()
        try:
            spec = probe.parse_probe_spec(
                {
                    "url": f"https://localhost:{server.port}/",
                    "tls_cafile": TEST_CERT,
                }
            )
            result = probe.probe_node(
                _http_node(proxy.port), spec, probe.parse_expect_spec(None), 5.0
            )
            self.assertEqual(result.verdict, probe.LIVE)
            self.assertEqual(result.status, 200)
        finally:
            proxy.close()
            server.close()


if __name__ == "__main__":
    unittest.main()
