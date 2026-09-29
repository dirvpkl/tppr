import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gate


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stub_socks5(stop: threading.Event) -> int:
    """Minimal upstream: no-auth SOCKS5 CONNECT, then echo."""
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
            threading.Thread(target=_echo_session, args=(conn,), daemon=True).start()

    def _echo_session(conn: socket.socket) -> None:
        with conn:
            try:
                greet = gate._read_exact(conn, 2)
                if len(greet) != 2:
                    return
                gate._read_exact(conn, greet[1])
                conn.sendall(b"\x05\x00")
                header = gate._read_exact(conn, 4)
                kind = header[3]
                if kind == 0x01:
                    gate._read_exact(conn, 6)
                elif kind == 0x03:
                    size = gate._read_exact(conn, 1)
                    gate._read_exact(conn, (size[0] if size else 0) + 2)
                else:
                    return
                conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                while True:
                    data = conn.recv(65536)
                    if not data:
                        return
                    conn.sendall(data)
            except OSError:
                return

    threading.Thread(target=serve, daemon=True).start()
    return port


def _make_state(
    directory: Path, pool_text: str, port: int
) -> tuple[gate.GateState, gate.PoolSpec]:
    pool_file = directory / "pool-x.yaml"
    pool_file.write_text(pool_text, encoding="utf-8")
    db = sqlite3.connect(str(directory / "hashes.db"), check_same_thread=False)
    db.execute(
        "CREATE TABLE leases(pool TEXT, h TEXT, first_seen INTEGER, ttl INTEGER, "
        "PRIMARY KEY(pool, h))"
    )
    spec = gate.PoolSpec("test", port, (pool_file,))
    return (
        gate.GateState(lock=threading.Lock(), db=db, specs={port: spec}, pool_cache={}),
        spec,
    )


class ValidationTests(unittest.TestCase):
    def test_user_must_be_hex(self) -> None:
        self.assertTrue(gate._check_user("a1b2c3d4"))
        self.assertTrue(gate._check_user("ABCDEF1234567890" * 4))
        self.assertFalse(gate._check_user("random"))
        self.assertFalse(gate._check_user("short"))
        self.assertFalse(gate._check_user("zz-top-88"))

    def test_ttl_bounds(self) -> None:
        self.assertEqual(gate._check_ttl("15"), 15)
        self.assertEqual(gate._check_ttl("2592000"), 2592000)
        self.assertIsNone(gate._check_ttl("14"))
        self.assertIsNone(gate._check_ttl("2592001"))
        self.assertIsNone(gate._check_ttl(""))
        self.assertIsNone(gate._check_ttl("4h"))
        self.assertIsNone(gate._check_ttl("-5"))

    def test_pick_is_deterministic(self) -> None:
        nodes: list[gate.YamlMapping] = [{"name": f"n{i}"} for i in range(10)]
        first = gate._pick_node(nodes, "a1b2c3d4")
        self.assertIs(gate._pick_node(nodes, "a1b2c3d4"), first)
        self.assertIsNot(
            gate._pick_node(nodes, "a1b2c3d4"), gate._pick_node(nodes, "ffffffff")
        )


class LeaseTests(unittest.TestCase):
    def test_first_request_opens_lease(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state, _ = _make_state(Path(raw), "proxies: []\n", 20006)
            try:
                self.assertTrue(gate._lease_ok(state, "test", "a1b2c3d4", 3600))
            finally:
                state.db.close()

    def test_expired_lease_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state, _ = _make_state(Path(raw), "proxies: []\n", 20006)
            state.db.execute(
                "INSERT INTO leases VALUES('test', 'a1b2c3d4', ?, 15)",
                (int(gate._now()) - 16,),
            )
            state.db.commit()
            try:
                self.assertFalse(gate._lease_ok(state, "test", "a1b2c3d4", 3600))
            finally:
                state.db.close()

    def test_first_ttl_wins(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state, _ = _make_state(Path(raw), "proxies: []\n", 20006)
            self.assertTrue(gate._lease_ok(state, "test", "a1b2c3d4", 3600))
            try:
                row = state.db.execute(
                    "SELECT ttl FROM leases WHERE pool='test' AND h='a1b2c3d4'"
                ).fetchone()
                self.assertEqual(int(row[0]), 3600)
            finally:
                state.db.close()


class RoundTripTests(unittest.TestCase):
    def _serve_gate(self, state: gate.GateState, spec: gate.PoolSpec) -> int:
        server = gate.socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), gate.HashGateHandler, bind_and_activate=False
        )
        server.allow_reuse_address = True
        server.daemon_threads = True
        server.state = state  # type: ignore[attr-defined]
        server.spec = spec  # type: ignore[attr-defined]
        server.server_bind()
        server.server_activate()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return int(server.server_address[1])

    def test_socks5_echo(self) -> None:
        stop = threading.Event()
        upstream = _stub_socks5(stop)
        try:
            with tempfile.TemporaryDirectory() as raw:
                directory = Path(raw)
                pool = (
                    "proxies:\n"
                    "  - name: stub\n"
                    "    type: socks5\n"
                    "    server: 127.0.0.1\n"
                    f"    port: {upstream}\n"
                )
                state, spec = _make_state(directory, pool, 20006)
                gate_port = self._serve_gate(state, spec)
                try:
                    sock = socket.create_connection(("127.0.0.1", gate_port), timeout=5)
                    with sock:
                        sock.sendall(b"\x05\x01\x00")
                        self.assertEqual(gate._read_exact(sock, 2), b"\x05\x02")
                        user, word = b"a1b2c3d4", b"3600"
                        sock.sendall(
                            b"\x01"
                            + bytes([len(user)])
                            + user
                            + bytes([len(word)])
                            + word
                        )
                        self.assertEqual(gate._read_exact(sock, 2), b"\x01\x00")
                        sock.sendall(b"\x05\x01\x00\x03\x07example\x00\x50")
                        reply = gate._read_exact(sock, 10)
                        self.assertEqual(reply[1:2], b"\x00")
                        sock.sendall(b"hello-gate")
                        self.assertEqual(gate._read_exact(sock, 10), b"hello-gate")
                finally:
                    state.db.close()
        finally:
            stop.set()

    def test_expired_hash_rejected(self) -> None:
        stop = threading.Event()
        upstream = _stub_socks5(stop)
        try:
            with tempfile.TemporaryDirectory() as raw:
                directory = Path(raw)
                pool = (
                    "proxies:\n"
                    "  - name: stub\n"
                    "    type: socks5\n"
                    "    server: 127.0.0.1\n"
                    f"    port: {upstream}\n"
                )
                state, spec = _make_state(directory, pool, 20006)
                gate_port = self._serve_gate(state, spec)
                try:
                    sock = socket.create_connection(("127.0.0.1", gate_port), timeout=5)
                    with sock:
                        sock.sendall(b"\x05\x01\x00")
                        gate._read_exact(sock, 2)
                        user, word = b"deadbeef", b"15"
                        sock.sendall(
                            b"\x01"
                            + bytes([len(user)])
                            + user
                            + bytes([len(word)])
                            + word
                        )
                        # plant an expired lease, then retry on a fresh connection
                        state.db.execute(
                            "INSERT INTO leases VALUES('test', 'deadbeef', ?, 15)",
                            (int(gate._now()) - 16,),
                        )
                        state.db.commit()
                    retry = socket.create_connection(
                        ("127.0.0.1", gate_port), timeout=5
                    )
                    with retry:
                        retry.sendall(b"\x05\x01\x00")
                        retry.recv(2)
                        retry.sendall(
                            b"\x01"
                            + bytes([len(user)])
                            + user
                            + bytes([len(word)])
                            + word
                        )
                        self.assertEqual(gate._read_exact(retry, 2), b"\x01\x01")
                finally:
                    state.db.close()
        finally:
            stop.set()


if __name__ == "__main__":
    unittest.main()
