"""Hash-routed proxy gateway.

Listens on one TCP port per pool (SOCKS5 and HTTP CONNECT on the same port).
Every connection exits through a uniformly random pool node; authentication
only gates lifetime:

- username: any name identifying the credential, 1-64 chars of
  ``[A-Za-z0-9_.-]`` (a hex hash works fine too, it carries no meaning)
- password: TTL in seconds, digits only, ``MIN_TTL_S``..``MAX_TTL_S``

The first request records ``first_seen`` in SQLite; every later connection is
rejected once ``now > first_seen + TTL``. There is no password to guess and no
stable exit: rotation comes from picking a fresh node per connection. A dead
pick is retried transparently against other random nodes of the same pool.

Upstream nodes come from the pool files and must be HTTP or SOCKS5 proxies;
the gateway forwards with plain sockets and cannot speak vless/vmess/ss.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import logging
import os
import random
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

from common.upstream import connect_upstream, read_exact, read_http_head

SERVICE_NAME = "proxy-pool-hash-gate"
USER_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}\Z")
TTL_RE = re.compile(r"[0-9]{1,10}\Z")
MIN_TTL_S = 5
MAX_TTL_S = 30 * 24 * 3600
UPSTREAM_ATTEMPTS = 5
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
    return USER_RE.fullmatch(user) is not None


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
            (pool, user),
        ).fetchone()
        if row is None:
            state.db.execute(
                "INSERT INTO leases(pool, h, first_seen, ttl) VALUES(?, ?, ?, ?)",
                (pool, user, int(now), ttl),
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


def _connect_pool(
    nodes: list[YamlMapping], host: str, port: int, pool: str
) -> socket.socket:
    """Connect through up to UPSTREAM_ATTEMPTS distinct random nodes.

    Candidates are drawn without replacement, so a retry never repeats the
    node that just failed and never walks the pool in a fixed order.
    """
    last_error: OSError | None = None
    for node in random.sample(nodes, min(len(nodes), UPSTREAM_ATTEMPTS)):
        try:
            return connect_upstream(node, host, port, CONNECT_TIMEOUT_S)
        except OSError as exc:
            last_error = exc
            LOG.debug(
                "upstream failed pool=%s node=%s: %s", pool, node.get("name"), exc
            )
    raise OSError(f"all upstream attempts failed in pool {pool}") from last_error


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
    ) -> list[YamlMapping] | None:
        if not _check_user(user):
            return None
        ttl = _check_ttl(password)
        if ttl is None:
            return None
        nodes = _pool_nodes(state, spec)
        if not nodes:
            LOG.warning("pool %s has no forwardable nodes", spec.name)
            return None
        if not _lease_ok(state, spec.name, user, ttl):
            LOG.info(
                "expired credential pool=%s user=%s...",
                spec.name,
                user[:8],
            )
            return None
        return nodes

    def _handle_socks5(
        self, client: socket.socket, state: GateState, spec: PoolSpec
    ) -> None:
        greet = read_exact(client, 2, CONNECT_TIMEOUT_S)
        if len(greet) != 2 or greet[0] != 0x05:
            return
        read_exact(client, greet[1], CONNECT_TIMEOUT_S)
        client.sendall(b"\x05\x02")
        version = read_exact(client, 1, CONNECT_TIMEOUT_S)
        if version != b"\x01":
            return
        user_length = read_exact(client, 1, CONNECT_TIMEOUT_S)
        user = read_exact(
            client, user_length[0] if user_length else 0, CONNECT_TIMEOUT_S
        )
        word_length = read_exact(client, 1, CONNECT_TIMEOUT_S)
        word = read_exact(
            client, word_length[0] if word_length else 0, CONNECT_TIMEOUT_S
        )
        try:
            username = user.decode("utf-8")
            password = word.decode("utf-8")
        except UnicodeDecodeError:
            client.sendall(b"\x01\x01")
            return
        nodes = self._authorize(state, spec, username, password)
        if nodes is None:
            client.sendall(b"\x01\x01")
            return
        client.sendall(b"\x01\x00")
        header = read_exact(client, 4, CONNECT_TIMEOUT_S)
        if len(header) != 4 or header[1] != 0x01:
            return
        host, port = self._read_target(client, header)
        if host is None or port is None:
            return
        try:
            upstream = _connect_pool(nodes, host, port, spec.name)
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
            raw = read_exact(client, 6, CONNECT_TIMEOUT_S)
            if len(raw) != 6:
                return None, None
            return socket.inet_ntoa(raw[:4]), int.from_bytes(raw[4:], "big")
        if kind == 0x03:
            length = read_exact(client, 1, CONNECT_TIMEOUT_S)
            if not length:
                return None, None
            raw = read_exact(client, length[0] + 2, CONNECT_TIMEOUT_S)
            if len(raw) != length[0] + 2:
                return None, None
            return raw[:-2].decode("latin-1"), int.from_bytes(raw[-2:], "big")
        if kind == 0x04:
            raw = read_exact(client, 18, CONNECT_TIMEOUT_S)
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
            head = read_http_head(client, CONNECT_TIMEOUT_S)
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
        nodes = self._authorize(state, spec, username, password)
        if nodes is None:
            client.sendall(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b"Content-Length: 0\r\n\r\n"
            )
            return
        try:
            upstream = _connect_pool(nodes, host, port, spec.name)
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
