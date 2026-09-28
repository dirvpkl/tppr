from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import logging
import re
import threading
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.error import URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import yaml  # type: ignore[import-untyped]

SERVICE_NAME = "proxy-pool-pool-worker"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z")
MAX_SOURCES = 100
MAX_POOLS = 100
MIN_INTERVAL = 60
MAX_INTERVAL = 86400
MIN_TIMEOUT = 5
MAX_TIMEOUT = 120
MIN_BACKOFF = 1
MAX_BACKOFF = 60
MIN_ATTEMPTS = 1
MAX_ATTEMPTS = 10
MIN_NODES = 1
MAX_NODES = 1000
# Mihomo rejects the whole provider on an unknown shadowsocks cipher, so the
# worker filters against the exact set Mihomo implements. Note that the legacy
# name `chacha20-poly1305` is absent on purpose: Mihomo only has the ietf one.
SS_CIPHERS = frozenset(
    {
        "none",
        "aes-128-gcm",
        "aes-192-gcm",
        "aes-256-gcm",
        "chacha20-ietf-poly1305",
        "xchacha20-ietf-poly1305",
        "xchacha20",
        "aes-128-ctr",
        "aes-192-ctr",
        "aes-256-ctr",
        "aes-128-cfb",
        "aes-192-cfb",
        "aes-256-cfb",
        "rc4-md5",
        "chacha20",
        "chacha20-ietf",
        "2022-blake3-aes-128-gcm",
        "2022-blake3-aes-256-gcm",
        "2022-blake3-chacha20-poly1305",
        "2022-blake3-chacha8-poly1305",
    }
)
SS2022_CIPHERS = frozenset(
    name for name in SS_CIPHERS if name.startswith("2022-blake3")
)
MIN_WORKERS = 1
MAX_WORKERS = 16
DEFAULT_WORKERS = 4
DEFAULT_LISTEN = "0.0.0.0"
DEFAULT_PORT = 8080

TomlTable = dict[str, object]
YamlMapping = dict[str, object]

LOG = logging.getLogger(SERVICE_NAME)


@dataclass(frozen=True)
class Source:
    name: str
    url: str


@dataclass(frozen=True)
class Pool:
    name: str
    sources: tuple[str, ...]
    max_nodes: int
    shared: bool


@dataclass(frozen=True)
class WorkerConfig:
    subconv_url: str
    output_dir: Path
    refresh_interval_s: int
    request_timeout_s: int
    retry_backoff_s: int
    max_attempts: int
    sources: tuple[Source, ...]
    pools: tuple[Pool, ...]
    max_workers: int
    listen: str
    port: int


@dataclass
class Node:
    proxy: YamlMapping
    fingerprint: str
    sources: set[str]


@dataclass
class WorkerState:
    lock: threading.Lock
    ready: bool = False
    snapshots: dict[str, bytes] | None = None
    source_errors: dict[str, str] = field(default_factory=dict)
    source_nodes: dict[str, int] = field(default_factory=dict)
    pool_nodes: dict[str, int] = field(default_factory=dict)
    unique_nodes: int = 0
    dropped_nodes: int = 0
    last_refresh: str = ""


def _table(value: object, field: str) -> TomlTable:
    if type(value) is not dict:
        raise ValueError(f"{field} must be a table")
    return cast(TomlTable, value)


def _list(value: object, field: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"{field} must be an array")
    return cast(list[object], value)


def _text(table: TomlTable, field: str) -> str:
    value = table.get(field)
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return cast(str, value).strip()


def _integer(table: TomlTable, field: str, minimum: int, maximum: int) -> int:
    value = table.get(field)
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    integer = cast(int, value)
    if integer < minimum or integer > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return integer


def _boolean(table: TomlTable, field: str) -> bool:
    value = table.get(field)
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return cast(bool, value)


def _http_url(value: str, field: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field} must be an HTTP(S) URL")
    return value


def _load_config(path: Path) -> WorkerConfig:
    try:
        with path.open("rb") as handle:
            document = cast(TomlTable, tomllib.load(handle))
    except OSError as exc:
        raise ValueError(f"cannot read pool config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {path}: {exc}") from exc
    return _parse_config(document)


def _parse_config(document: TomlTable) -> WorkerConfig:
    worker = _table(document.get("worker"), "worker")
    subconv_url = _http_url(_text(worker, "subconv_url"), "worker.subconv_url")
    output_dir = Path(_text(worker, "output_dir"))
    refresh_interval_s = _integer(
        worker, "refresh_interval_s", MIN_INTERVAL, MAX_INTERVAL
    )
    request_timeout_s = _integer(worker, "request_timeout_s", MIN_TIMEOUT, MAX_TIMEOUT)
    retry_backoff_s = _integer(worker, "retry_backoff_s", MIN_BACKOFF, MAX_BACKOFF)
    max_attempts = _integer(worker, "max_attempts", MIN_ATTEMPTS, MAX_ATTEMPTS)
    raw_workers = worker.get("max_workers", DEFAULT_WORKERS)
    if type(raw_workers) is not int:
        raise ValueError("worker.max_workers must be an integer")
    max_workers = cast(int, raw_workers)
    if max_workers < MIN_WORKERS or max_workers > MAX_WORKERS:
        raise ValueError(
            f"worker.max_workers must be between {MIN_WORKERS} and {MAX_WORKERS}"
        )
    listen = worker.get("listen", DEFAULT_LISTEN)
    if type(listen) is not str or not cast(str, listen).strip():
        raise ValueError("worker.listen must be a non-empty string")
    port = _integer(worker, "port", 1, 65535)

    raw_sources = _list(document.get("sources", []), "sources")
    if len(raw_sources) > MAX_SOURCES:
        raise ValueError(f"sources supports at most {MAX_SOURCES} entries")
    sources: list[Source] = []
    source_names: set[str] = set()
    for index, raw_source in enumerate(raw_sources):
        field = f"sources[{index}]"
        table = _table(raw_source, field)
        name = _text(table, "name")
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"{field}.name contains unsupported characters")
        if name in source_names:
            raise ValueError(f"duplicate source name: {name}")
        source_names.add(name)
        sources.append(Source(name, _http_url(_text(table, "url"), f"{field}.url")))

    raw_pools = _list(document.get("pools", []), "pools")
    if len(raw_pools) > MAX_POOLS:
        raise ValueError(f"pools supports at most {MAX_POOLS} entries")
    pools: list[Pool] = []
    pool_names: set[str] = set()
    referenced_sources: set[str] = set()
    for index, raw_pool in enumerate(raw_pools):
        field = f"pools[{index}]"
        table = _table(raw_pool, field)
        name = _text(table, "name")
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"{field}.name contains unsupported characters")
        if name in pool_names:
            raise ValueError(f"duplicate pool name: {name}")
        raw_pool_sources = _list(table.get("sources", []), f"{field}.sources")
        pool_sources: list[str] = []
        for source_index, raw_source_name in enumerate(raw_pool_sources):
            if type(raw_source_name) is not str or not raw_source_name.strip():
                raise ValueError(
                    f"{field}.sources[{source_index}] must be a non-empty string"
                )
            source_name = cast(str, raw_source_name).strip()
            if source_name not in source_names:
                raise ValueError(
                    f"{field}.sources references an unknown source: {source_name}"
                )
            if source_name in pool_sources:
                raise ValueError(
                    f"{field}.sources contains a duplicate source: {source_name}"
                )
            pool_sources.append(source_name)
            referenced_sources.add(source_name)
        if not pool_sources:
            raise ValueError(f"{field}.sources must not be empty")
        max_nodes = _integer(table, "max_nodes", MIN_NODES, MAX_NODES)
        shared = _boolean(table, "shared")
        pool_names.add(name)
        pools.append(Pool(name, tuple(pool_sources), max_nodes, shared))

    if not pools:
        raise ValueError("pools must contain at least one entry")
    unused_sources = source_names - referenced_sources
    if unused_sources:
        raise ValueError(f"unassigned sources: {', '.join(sorted(unused_sources))}")

    return WorkerConfig(
        subconv_url=subconv_url.rstrip("/"),
        output_dir=output_dir,
        refresh_interval_s=refresh_interval_s,
        request_timeout_s=request_timeout_s,
        retry_backoff_s=retry_backoff_s,
        max_attempts=max_attempts,
        sources=tuple(sources),
        pools=tuple(pools),
        max_workers=max_workers,
        listen=cast(str, listen).strip(),
        port=port,
    )


def _proxy_list(payload: object) -> list[YamlMapping]:
    if type(payload) is not dict:
        raise ValueError("SubConv response must be a mapping")
    raw_proxies = cast(YamlMapping, payload).get("proxies")
    if type(raw_proxies) is not list:
        raise ValueError("SubConv response is missing a proxies list")
    proxies: list[YamlMapping] = []
    for raw_proxy in cast(list[object], raw_proxies):
        if type(raw_proxy) is not dict:
            raise ValueError("SubConv returned a non-mapping proxy")
        proxy = cast(YamlMapping, raw_proxy)
        if type(proxy.get("name")) is not str or type(proxy.get("type")) is not str:
            raise ValueError("SubConv returned a proxy without name or type")
        proxies.append(dict(proxy))
    return proxies


def _is_usable(proxy: YamlMapping) -> bool:
    """Reject nodes Mihomo refuses to parse.

    Mihomo drops an entire proxy-provider when a single node fails to
    initialize, so a single malformed free node empties the whole pool.
    """
    server = proxy.get("server")
    port = proxy.get("port")
    kind = proxy.get("type")
    if type(server) is not str or not server.strip():
        return False
    if type(port) is not int or not 1 <= port <= 65535:
        return False
    if type(kind) is not str or not kind.strip():
        return False
    if kind in {"ss", "trojan"}:
        password = proxy.get("password")
        if type(password) is not str or not password:
            return False
    if kind == "ss":
        cipher = proxy.get("cipher")
        if type(cipher) is not str or cipher.lower() not in SS_CIPHERS:
            return False
        if cipher.lower() in SS2022_CIPHERS:
            try:
                if not base64.b64decode(str(proxy["password"]), validate=True):
                    return False
            except (binascii.Error, ValueError):
                return False
    if kind == "snell" and not proxy.get("psk"):
        return False
    if kind in {"vmess", "vless"}:
        try:
            uuid.UUID(str(proxy.get("uuid", "")))
        except (AttributeError, TypeError, ValueError):
            return False
    return True


def _fetch_source(source: Source, config: WorkerConfig) -> list[YamlMapping]:
    query = urlencode({"url": source.url})
    url = f"{config.subconv_url}/provider?{query}"
    request = Request(url, headers={"User-Agent": "proxy-pool-pool-worker"})
    attempts = config.max_attempts
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=config.request_timeout_s) as response:
                body = response.read().decode("utf-8", errors="replace")
            try:
                payload = yaml.safe_load(body)
            except yaml.YAMLError as exc:
                raise ValueError(f"source {source.name} returned invalid YAML") from exc
            return _proxy_list(payload)
        except (OSError, URLError, TimeoutError, ValueError) as exc:
            if attempt == attempts:
                raise RuntimeError(
                    f"source {source.name} failed after {attempts} attempts"
                ) from exc
            time.sleep(config.retry_backoff_s * attempt)
    raise RuntimeError(f"source {source.name} failed")


def _fetch_all(
    config: WorkerConfig,
) -> tuple[dict[str, list[YamlMapping]], dict[str, str]]:
    proxies: dict[str, list[YamlMapping]] = {}
    errors: dict[str, str] = {}
    workers = min(config.max_workers, len(config.sources))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = {
            source.name: executor.submit(_fetch_source, source, config)
            for source in config.sources
        }
    for source in config.sources:
        try:
            proxies[source.name] = results[source.name].result()
        except RuntimeError as exc:
            errors[source.name] = str(exc)
            LOG.error("source unavailable: %s", exc)
    return proxies, errors


def _fingerprint(proxy: YamlMapping) -> str:
    identity = {
        key: value
        for key, value in proxy.items()
        if key not in {"name", "id", "remark"}
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _score(fingerprint: str, pool_name: str) -> int:
    digest = hashlib.sha256(f"{fingerprint}:{pool_name}".encode()).hexdigest()
    return int(digest[:16], 16)


def _merge_sources(
    config: WorkerConfig,
    source_proxies: dict[str, list[YamlMapping]],
) -> tuple[dict[str, Node], int]:
    nodes: dict[str, Node] = {}
    dropped = 0
    for source in config.sources:
        for proxy in source_proxies.get(source.name, []):
            if not _is_usable(proxy):
                dropped += 1
                continue
            fingerprint = _fingerprint(proxy)
            node = nodes.get(fingerprint)
            if node is None:
                nodes[fingerprint] = Node(dict(proxy), fingerprint, {source.name})
            else:
                node.sources.add(source.name)
    if dropped:
        LOG.warning("dropped %d unusable nodes", dropped)
    return nodes, dropped


def _node_name(pool: Pool, node: Node) -> str:
    raw_name = node.proxy.get("name")
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw_name)).strip("-")
    if not label:
        label = "node"
    return f"{pool.name}-{node.fingerprint[:8]}-{label}"[:120]


def _pool_nodes(config: WorkerConfig, nodes: dict[str, Node]) -> dict[str, list[Node]]:
    buckets: dict[str, list[Node]] = {pool.name: [] for pool in config.pools}
    for node in nodes.values():
        eligible = [
            pool
            for pool in config.pools
            if any(source in pool.sources for source in node.sources)
        ]
        if not eligible:
            raise ValueError(f"node {node.fingerprint[:12]} is not assigned to a pool")
        targets = (
            eligible
            if any(pool.shared for pool in eligible)
            else [min(eligible, key=lambda pool: _score(node.fingerprint, pool.name))]
        )
        for pool in targets:
            buckets[pool.name].append(node)
    for pool in config.pools:
        buckets[pool.name].sort(
            key=lambda node: (_score(node.fingerprint, pool.name), node.fingerprint)
        )
        buckets[pool.name] = buckets[pool.name][: pool.max_nodes]
    return buckets


def _render_pool(pool: Pool, nodes: list[Node]) -> bytes:
    proxies: list[YamlMapping] = []
    for node in nodes:
        proxy = dict(node.proxy)
        proxy["name"] = _node_name(pool, node)
        proxies.append(proxy)
    document = {"proxies": proxies}
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode("utf-8")


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def refresh(config: WorkerConfig, state: WorkerState) -> bool:
    source_proxies, errors = _fetch_all(config)
    if not source_proxies:
        raise RuntimeError("all sources failed; keeping the previous pool snapshot")
    nodes, dropped = _merge_sources(config, source_proxies)
    if not nodes:
        raise RuntimeError(
            "sources returned no usable nodes; keeping the previous snapshot"
        )
    buckets = _pool_nodes(config, nodes)
    snapshots: dict[str, bytes] = {}
    for pool in config.pools:
        content = _render_pool(pool, buckets[pool.name])
        snapshots[pool.name] = content
        _write_atomic(config.output_dir / f"pool-{pool.name}.yaml", content)
    with state.lock:
        state.snapshots = snapshots
        state.ready = True
        state.source_errors = errors
        state.source_nodes = {
            name: len(items) for name, items in source_proxies.items()
        }
        state.pool_nodes = {name: len(items) for name, items in buckets.items()}
        state.unique_nodes = len(nodes)
        state.dropped_nodes = dropped
        state.last_refresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    LOG.info(
        "refresh complete sources_ok=%d sources_failed=%d unique_nodes=%d "
        "dropped_nodes=%d pools=%d",
        len(source_proxies),
        len(errors),
        len(nodes),
        dropped,
        len(config.pools),
    )
    return True


def _health_payload(state: WorkerState) -> bytes:
    with state.lock:
        payload = {
            "status": "ok" if not state.source_errors else "degraded",
            "ready": state.ready,
            "unique_nodes": state.unique_nodes,
            "dropped_nodes": state.dropped_nodes,
            "sources_ok": len(state.source_nodes),
            "sources_failed": len(state.source_errors),
            "source_nodes": dict(sorted(state.source_nodes.items())),
            "source_errors": dict(sorted(state.source_errors.items())),
            "pool_nodes": dict(sorted(state.pool_nodes.items())),
            "last_refresh": state.last_refresh,
        }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _handler(state: WorkerState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # Mihomo keeps the connection alive; every response must carry its own
        # Content-Length or the client reads an incomplete body and reports EOF.
        protocol_version = "HTTP/1.1"

        def _respond(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/healthz":
                with state.lock:
                    ready = state.ready
                self._respond(
                    200 if ready else 503,
                    _health_payload(state) if ready else b'{"status":"starting"}\n',
                    "application/json; charset=utf-8",
                )
                return
            prefix = "/pools/"
            if path.startswith(prefix) and path.endswith(".yaml"):
                pool_name = path[len(prefix) : -len(".yaml")]
                with state.lock:
                    content = (state.snapshots or {}).get(pool_name)
                if content is None:
                    self._respond(404, b"unknown pool\n", "text/plain; charset=utf-8")
                    return
                self._respond(200, content, "text/yaml; charset=utf-8")
                return
            self._respond(404, b"not found\n", "text/plain; charset=utf-8")

        def log_message(self, format: str, *args: object) -> None:
            LOG.debug("http %s", format % args)

    return Handler


def _serve(config: WorkerConfig, state: WorkerState) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((config.listen, config.port), _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def run(config: WorkerConfig, once: bool) -> int:
    state = WorkerState(lock=threading.Lock())
    if once:
        refresh(config, state)
        return 0
    server = _serve(config, state)
    LOG.info("worker listening on %s:%d", config.listen, config.port)
    try:
        while True:
            try:
                refresh(config, state)
                delay = config.refresh_interval_s
            except Exception as exc:
                LOG.error("refresh failed: %s", exc)
                delay = max(config.retry_backoff_s * config.max_attempts, 30)
            time.sleep(delay)
    finally:
        server.shutdown()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build deduplicated Mihomo pools")
    parser.add_argument(
        "--config", type=Path, default=Path("/config/pool-sources.toml")
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s level=%(levelname)s service=%(name)s %(message)s",
    )
    try:
        config = _load_config(args.config)
        return run(config, args.once)
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
