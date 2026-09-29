"""Hash-routed proxy gateway.

Listens on one TCP port per pool (SOCKS5 and HTTP CONNECT on the same port).
Authentication doubles as routing and expiry:

- username: hex hash selecting the upstream node, ``int(hash, 16) % len(nodes)``
- password: TTL in seconds, digits only, ``MIN_TTL_S``..``MAX_TTL_S``

The first request records ``first_seen`` in SQLite; every later connection is
rejected once ``now > first_seen + TTL``. The countdown starts at the first
request, not at creation. There is no password to guess: any hash is a valid
username, and each hash deterministically pins one exit node while it lives.

Upstream nodes come from the pool files and must be HTTP or SOCKS5 proxies;
the gateway forwards with plain sockets and cannot speak vless/vmess/ss.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import logging
import os
import re
import select
import socket
import socketserver
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml  # type: ignore[import-untyped]

SERVICE_NAME = "proxy-pool-hash-gate"
HASH_RE = re.compile(r"[0-9a-fA-F]{8,64}\Z")
TTL_RE = re.compile(r"[0-9]{1,10}\Z")
MIN_TTL_S = 15
MAX_TTL_S = 30 * 24 * 3600
MIN_PORT = 1
MAX_PORT = 65535
IDLE_TIMEOUT_S = 300
CONNECT_TIMEOUT_S = 15
FORWARD_TYPES = ("http", "socks5")

TomlTable = dict[str, object]
YamlMapping = dict[str, object]

LOG = logging.getLogger(SERVICE_NAME)


def _now() -> float:
    return time.time()


@dataclass(frozen=True)
class PoolSpec:
    name: str
    port: int
    files: tuple[Path, ...]


@dataclass
class GateState:
    lock: threading.Lock
    db: sqlite3.Connection
    specs: dict[int, PoolSpec]
    pool_cache: dict[str, tuple[float, list[YamlMapping]]]


def _env_port(name: str) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw.isdigit():
        raise ValueError(f"{name} must be a port number")
    port = int(raw)
    if port < 1024 or port > MAX_PORT:
        raise ValueError(f"{name} must be between 1024 and {MAX_PORT}")
    return port


def _load_pool_files(paths: tuple[Path, ...]) -> list[YamlMapping]:
    nodes: list[YamlMapping] = []
    seen: set[tuple[str, str, int]] = set()
    for path in paths:
        if not path.is_file():
            LOG.warning("pool file missing, skipped: %s", path)
            continue
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            LOG.warning("pool file unreadable, skipped: %s (%s)", path, exc)
            continue
        if type(document) is not dict:
            continue
        raw_proxies = cast(TomlTable, document).get("proxies", [])
        if type(raw_proxies) is not list:
            continue
        for raw_proxy in cast(list[object], raw_proxies):
            if type(raw_proxy) is not dict:
                continue
            proxy = cast(YamlMapping, raw_proxy)
            kind = proxy.get("type")
            server = proxy.get("server")
            node_port = proxy.get("port")
            if kind not in FORWARD_TYPES:
                continue
            if type(server) is not str or not server.strip():
                continue
            if type(node_port) is not int or not MIN_PORT <= node_port <= MAX_PORT:
                continue
            key = (str(kind), server.strip(), node_port)
            if key in seen:
                continue
            seen.add(key)
            nodes.append(dict(proxy))
    return nodes


def _pool_nodes(state: GateState, spec: PoolSpec) -> list[YamlMapping]:
    now = _now()
    cached = state.pool_cache.get(spec.name)
    newest = 0.0
    for path in spec.files:
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0.0
        newest = max(newest, modified)
    if cached is not None and cached[0] >= newest:
        return cached[1]
    nodes = _load_pool_files(spec.files)
    with state.lock:
        state.pool_cache[spec.name] = (now, nodes)
    LOG.info("pool %s loaded %d forwardable nodes", spec.name, len(nodes))
    return nodes


def _check_user(user: str) -> bool:
    return HASH_RE.fullmatch(user) is not None


def _check_ttl(password: str) -> int | None:
    if TTL_RE.fullmatch(password) is None:
        return None
    ttl = int(password)
    if ttl < MIN_TTL_S or ttl > MAX_TTL_S:
        return None
    return ttl


def _lease_ok(state: GateState, pool: str, user: str, ttl: int) -> bool:
    now = _now()
    with state.lock:
        row = state.db.execute(
            "SELECT first_seen, ttl FROM leases WHERE pool = ? AND h = ?",
            (pool, user.lower()),
        ).fetchone()
        if row is None:
            state.db.execute(
                "INSERT INTO leases(pool, h, first_seen, ttl) VALUES(?, ?, ?, ?)",
                (pool, user.lower(), int(now), ttl),
            )
            state.db.execute(
                "DELETE FROM leases WHERE first_seen + ttl < ?",
                (int(now),),
            )
            state.db.commit()
            return True
        first_seen = int(row[0])
        stored_ttl = int(row[1])
    return now <= first_seen + stored_ttl


def _pick_node(nodes: list[YamlMapping], user: str) -> YamlMapping:
    return nodes[int(user, 16) % len(nodes)]


def _connect_upstream(node: YamlMapping, host: str, port: int) -> socket.socket:
    kind = str(node.get("type"))
    server = str(node["server"])
    node_port = int(cast(int, node["port"]))
    username = node.get("username")
    password = node.get("password")
    sock = socket.create_connection((server, node_port), timeout=CONNECT_TIMEOUT_S)
    try:
        if kind == "http":
            request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            if type(username) is str and type(password) is str:
                token = base64.b64encode(f"{username}:{password}".encode()).decode()
                request += f"Proxy-Authorization: Basic {token}\r\n"
            request += "\r\n"
            sock.sendall(request.encode())
            response = _read_http_head(sock)
            if not response.startswith("HTTP/1.1 200") and not response.startswith(
                "HTTP/1.0 200"
            ):
                raise OSError(f"upstream http rejected: {response.splitlines()[0]}")
        else:
            has_creds = type(username) is str and type(password) is str
            sock.sendall(b"\x05\x02\x00\x02" if has_creds else b"\x05\x01\x00")
            choice = _read_exact(sock, 2)
            if len(choice) != 2 or choice[0] != 0x05:
                raise OSError("upstream socks5 handshake failed")
            if choice[1] == 0x02:
                user = str(username).encode()
                word = str(password).encode()
                sock.sendall(
                    b"\x01" + bytes([len(user)]) + user + bytes([len(word)]) + word
                )
                auth = _read_exact(sock, 2)
                if len(auth) != 2 or auth[1] != 0x00:
                    raise OSError("upstream socks5 auth failed")
            elif choice[1] != 0x00:
                raise OSError("upstream socks5 needs unsupported auth")
            try:
                packed = socket.inet_aton(host)
                connect = b"\x05\x01\x00\x01" + packed + port.to_bytes(2, "big")
            except OSError:
                encoded = host.encode()
                connect = (
                    b"\x05\x01\x00\x03"
                    + bytes([len(encoded)])
                    + encoded
                    + port.to_bytes(2, "big")
                )
            sock.sendall(connect)
            reply = _read_exact(sock, 4)
            if len(reply) != 4 or reply[1] != 0x00:
                raise OSError("upstream socks5 connect failed")
            _drain_socks5_address(sock, reply[3])
    except OSError:
        sock.close()
        raise
    return sock


def _read_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    sock.settimeout(CONNECT_TIMEOUT_S)
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _read_http_head(sock: socket.socket) -> str:
    data = b""
    sock.settimeout(CONNECT_TIMEOUT_S)
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:
            raise OSError("upstream http head too large")
    return data.decode("latin-1")


def _drain_socks5_address(sock: socket.socket, kind: int) -> None:
    if kind == 0x01:
        _read_exact(sock, 4 + 2)
    elif kind == 0x03:
        length = _read_exact(sock, 1)
        _read_exact(sock, (length[0] if length else 0) + 2)
    elif kind == 0x04:
        _read_exact(sock, 16 + 2)
    else:
        raise OSError("upstream socks5 bad address type")


def _relay(left: socket.socket, right: socket.socket) -> None:
    threads = [
        threading.Thread(target=_copy, args=(left, right)),
        threading.Thread(target=_copy, args=(right, left)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def _copy(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            ready, _, _ = select.select([source], [], [], IDLE_TIMEOUT_S)
            if not ready:
                break
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        try:
            source.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            target.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _basic_credentials(head: str) -> tuple[str, str] | None:
    for line in head.split("\r\n"):
        if line.lower().startswith("proxy-authorization: basic "):
            try:
                decoded = base64.b64decode(line.split(" ", 2)[2].strip()).decode(
                    "utf-8", errors="strict"
                )
            except (binascii.Error, ValueError, UnicodeDecodeError):
                return None
            if ":" not in decoded:
                return None
            user, _, password = decoded.partition(":")
            return user, password
    return None


class HashGateHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        state = cast(GateState, self.server.state)  # type: ignore[attr-defined]
        spec = cast(PoolSpec, self.server.spec)  # type: ignore[attr-defined]
        client = cast(socket.socket, self.request)
        client.settimeout(CONNECT_TIMEOUT_S)
        try:
            first = client.recv(1, socket.MSG_PEEK)
        except OSError:
            return
        if first == b"\x05":
            self._handle_socks5(client, state, spec)
        else:
            self._handle_http(client, state, spec)

    def _authorize(
        self, state: GateState, spec: PoolSpec, user: str, password: str
    ) -> YamlMapping | None:
        if not _check_user(user):
            return None
        ttl = _check_ttl(password)
        if ttl is None:
            return None
        nodes = _pool_nodes(state, spec)
        if not nodes:
            LOG.warning("pool %s has no forwardable nodes", spec.name)
            return None
        node = _pick_node(nodes, user)
        if not _lease_ok(state, spec.name, user, ttl):
            LOG.info(
                "expired credential pool=%s user=%s...",
                spec.name,
                user[:8],
            )
            return None
        return node

    def _handle_socks5(
        self, client: socket.socket, state: GateState, spec: PoolSpec
    ) -> None:
        greet = _read_exact(client, 2)
        if len(greet) != 2 or greet[0] != 0x05:
            return
        _read_exact(client, greet[1])
        client.sendall(b"\x05\x02")
        version = _read_exact(client, 1)
        if version != b"\x01":
            return
        user_length = _read_exact(client, 1)
        user = _read_exact(client, user_length[0] if user_length else 0)
        word_length = _read_exact(client, 1)
        word = _read_exact(client, word_length[0] if word_length else 0)
        try:
            username = user.decode("utf-8")
            password = word.decode("utf-8")
        except UnicodeDecodeError:
            client.sendall(b"\x01\x01")
            return
        node = self._authorize(state, spec, username, password)
        if node is None:
            client.sendall(b"\x01\x01")
            return
        client.sendall(b"\x01\x00")
        header = _read_exact(client, 4)
        if len(header) != 4 or header[1] != 0x01:
            return
        host, port = self._read_target(client, header)
        if host is None or port is None:
            return
        try:
            upstream = _connect_upstream(node, host, port)
        except OSError as exc:
            LOG.info("upstream failed pool=%s: %s", spec.name, exc)
            client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
            return
        client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        _relay(client, upstream)

    def _read_target(
        self, client: socket.socket, header: bytes
    ) -> tuple[str | None, int | None]:
        kind = header[3]
        if kind == 0x01:
            raw = _read_exact(client, 6)
            if len(raw) != 6:
                return None, None
            return socket.inet_ntoa(raw[:4]), int.from_bytes(raw[4:], "big")
        if kind == 0x03:
            length = _read_exact(client, 1)
            if not length:
                return None, None
            raw = _read_exact(client, length[0] + 2)
            if len(raw) != length[0] + 2:
                return None, None
            return raw[:-2].decode("latin-1"), int.from_bytes(raw[-2:], "big")
        if kind == 0x04:
            raw = _read_exact(client, 18)
            if len(raw) != 18:
                return None, None
            return socket.inet_ntop(socket.AF_INET6, raw[:16]), int.from_bytes(
                raw[16:], "big"
            )
        return None, None

    def _handle_http(
        self, client: socket.socket, state: GateState, spec: PoolSpec
    ) -> None:
        try:
            head = _read_http_head(client)
        except OSError:
            return
        lines = head.split("\r\n")
        if not lines or not lines[0].upper().startswith("CONNECT "):
            client.sendall(
                b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n"
            )
            return
        try:
            _, target, _ = lines[0].split(" ", 2)
            host, _, port_text = target.rpartition(":")
            port = int(port_text)
        except ValueError:
            return
        credentials = _basic_credentials(head)
        if credentials is None:
            client.sendall(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b'Proxy-Authenticate: Basic realm="hash-gate"\r\n'
                b"Content-Length: 0\r\n\r\n"
            )
            return
        username, password = credentials
        node = self._authorize(state, spec, username, password)
        if node is None:
            client.sendall(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b"Content-Length: 0\r\n\r\n"
            )
            return
        try:
            upstream = _connect_upstream(node, host, port)
        except OSError as exc:
            LOG.info("upstream failed pool=%s: %s", spec.name, exc)
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        _relay(client, upstream)


def _open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), check_same_thread=False)
    db.execute(
        "CREATE TABLE IF NOT EXISTS leases("
        "pool TEXT NOT NULL, h TEXT NOT NULL, "
        "first_seen INTEGER NOT NULL, ttl INTEGER NOT NULL, "
        "PRIMARY KEY(pool, h))"
    )
    db.commit()
    return db


def run(
    specs: list[PoolSpec],
    db_path: Path,
) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s level=%(levelname)s service=%(name)s %(message)s",
    )
    state = GateState(
        lock=threading.Lock(),
        db=_open_db(db_path),
        specs={spec.port: spec for spec in specs},
        pool_cache={},
    )
    servers: list[socketserver.ThreadingTCPServer] = []
    for spec in specs:
        server = socketserver.ThreadingTCPServer(
            ("0.0.0.0", spec.port), HashGateHandler, bind_and_activate=False
        )
        server.allow_reuse_address = True
        server.daemon_threads = True
        server.state = state  # type: ignore[attr-defined]
        server.spec = spec  # type: ignore[attr-defined]
        server.server_bind()
        server.server_activate()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        LOG.info(
            "hash gate listening pool=%s port=%d files=%d",
            spec.name,
            spec.port,
            len(spec.files),
        )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Hash-routed proxy gateway")
    parser.add_argument("--db", type=Path, default=Path("/data/hashes.db"))
    parser.add_argument("--pool-dir", type=Path, default=Path("/pools"))
    parser.add_argument("--custom-file", type=Path, default=Path("/custom/mine.yaml"))
    parser.add_argument("--remote-file", type=Path, default=Path("/pools/free.yaml"))
    args = parser.parse_args()
    try:
        pool_files = tuple(sorted(args.pool_dir.glob("pool-*.yaml")))
        specs = [
            PoolSpec(
                "all",
                _env_port("HASH_GATE_ALL_PORT"),
                (args.custom_file, *pool_files, args.remote_file),
            ),
            PoolSpec("free", _env_port("HASH_GATE_FREE_PORT"), pool_files),
            PoolSpec("custom", _env_port("HASH_GATE_CUSTOM_PORT"), (args.custom_file,)),
        ]
    except ValueError as exc:
        parser.error(str(exc))
    return run(specs, args.db)


if __name__ == "__main__":
    raise SystemExit(main())
