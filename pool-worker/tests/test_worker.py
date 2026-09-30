import dataclasses
import socket
import sys
import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker

from common import probe


def _read_head(conn: socket.socket) -> bytes:
    head = b""
    conn.settimeout(5.0)
    while b"\r\n\r\n" not in head:
        chunk = conn.recv(4096)
        if not chunk:
            break
        head += chunk
    return head


def _check_settings(
    raw: Mapping[str, object] | None = None,
    timeout_s: int = 5,
    workers: int = 4,
) -> worker.CheckSettings:
    table: worker.TomlTable = {"url": "https://example.com/health"}
    if raw is not None:
        table.update(raw)
    return worker.CheckSettings(
        True,
        probe.parse_probe_spec(table),
        probe.parse_expect_spec(None),
        timeout_s,
        workers,
    )


class WorkerTests(unittest.TestCase):
    def config(self) -> worker.WorkerConfig:
        return worker.WorkerConfig(
            subconv_url="http://subconv:8080",
            output_dir=Path("providers"),
            refresh_interval_s=900,
            request_timeout_s=30,
            retry_backoff_s=5,
            max_attempts=3,
            max_workers=2,
            check=worker.CheckSettings(False, None, probe.ExpectSpec(), 8, 10),
            sources=(
                worker.Source("one", "https://example.com/one"),
                worker.Source("two", "https://example.com/two"),
            ),
            pools=(
                worker.Pool("alpha", ("one", "two"), 10, False),
                worker.Pool("beta", ("one", "two"), 10, False),
            ),
            listen="0.0.0.0",
            port=8080,
        )

    def test_fingerprint_ignores_display_name(self) -> None:
        first = {
            "name": "Node A",
            "type": "vless",
            "server": "example.com",
            "port": 443,
        }
        second = {
            "name": "Node B",
            "type": "vless",
            "server": "example.com",
            "port": 443,
        }
        self.assertEqual(worker._fingerprint(first), worker._fingerprint(second))

    def test_deduplicates_and_partitions_deterministically(self) -> None:
        proxy_a: worker.YamlMapping = {
            "name": "A",
            "type": "vless",
            "server": "a.example",
            "port": 443,
            "uuid": "6f0c1a2b-3c4d-5e6f-8a9b-0c1d2e3f4a5b",
        }
        proxy_b: worker.YamlMapping = {
            "name": "B",
            "type": "ss",
            "server": "b.example",
            "port": 8388,
            "cipher": "aes-128-gcm",
            "password": "secret",
        }
        duplicate: worker.YamlMapping = {"name": "A duplicate"}
        duplicate.update(
            {key: value for key, value in proxy_a.items() if key != "name"}
        )
        source_proxies: dict[str, list[worker.YamlMapping]] = {
            "one": [proxy_a, proxy_b],
            "two": [duplicate],
        }
        config = self.config()
        nodes, dropped = worker._merge_sources(config, source_proxies)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(dropped, 0)
        buckets = worker._pool_nodes(config, nodes)
        self.assertEqual(sum(len(pool_nodes) for pool_nodes in buckets.values()), 2)
        self.assertEqual(worker._pool_nodes(config, nodes), buckets)

    def test_shared_pool_receives_duplicate_once_per_pool(self) -> None:
        config = worker.WorkerConfig(
            subconv_url="http://subconv:8080",
            output_dir=Path("providers"),
            refresh_interval_s=900,
            request_timeout_s=30,
            retry_backoff_s=5,
            max_attempts=3,
            max_workers=2,
            check=worker.CheckSettings(False, None, probe.ExpectSpec(), 8, 10),
            sources=(worker.Source("one", "https://example.com/one"),),
            pools=(
                worker.Pool("alpha", ("one",), 10, True),
                worker.Pool("beta", ("one",), 10, True),
            ),
            listen="0.0.0.0",
            port=8080,
        )
        proxy = {
            "name": "node",
            "type": "ss",
            "server": "a.example",
            "port": 8388,
            "cipher": "aes-128-gcm",
            "password": "secret",
        }
        nodes, dropped = worker._merge_sources(config, {"one": [proxy]})
        self.assertEqual(dropped, 0)
        buckets = worker._pool_nodes(config, nodes)
        self.assertEqual(len(buckets["alpha"]), 1)
        self.assertEqual(len(buckets["beta"]), 1)

    def test_failed_source_does_not_block_other_pools(self) -> None:
        config = self.config()
        proxy: worker.YamlMapping = {
            "name": "A",
            "type": "ss",
            "server": "a.example",
            "port": 8388,
            "cipher": "aes-128-gcm",
            "password": "secret",
        }

        def fake_fetch_all(
            worker_config: worker.WorkerConfig,
        ) -> tuple[dict[str, list[worker.YamlMapping]], dict[str, str]]:
            self.assertEqual(worker_config.subconv_url, config.subconv_url)
            return {"one": [proxy]}, {"two": "source two failed after 3 attempts"}

        original = worker._fetch_all
        worker._fetch_all = fake_fetch_all  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as directory:
                local = dataclasses.replace(config, output_dir=Path(directory))
                state = worker.WorkerState(lock=threading.Lock())
                self.assertTrue(worker.refresh(local, state))
                self.assertTrue(state.ready)
                self.assertEqual(
                    state.source_errors, {"two": "source two failed after 3 attempts"}
                )
                self.assertEqual(state.source_nodes, {"one": 1})
                self.assertEqual(state.unique_nodes, 1)
                written = {
                    path.name: path.stat().st_size
                    for path in Path(directory).glob("pool-*.yaml")
                }
                snapshots = state.snapshots or {}
                self.assertEqual(
                    written,
                    {
                        f"pool-{pool.name}.yaml": len(snapshots[pool.name])
                        for pool in config.pools
                    },
                )
                self.assertIn(b'"status": "degraded"', worker._health_payload(state))
        finally:
            worker._fetch_all = original

    def test_drops_nodes_mihomo_cannot_parse(self) -> None:
        config = self.config()
        good: worker.YamlMapping = {
            "name": "good",
            "type": "vless",
            "server": "a.example",
            "port": 443,
            "uuid": "6f0c1a2b-3c4d-5e6f-8a9b-0c1d2e3f4a5b",
        }
        bad_key: worker.YamlMapping = {
            "name": "bad-ss",
            "type": "ss",
            "server": "b.example",
            "port": 8388,
            "cipher": "2022-blake3-chacha20-poly1305",
            "password": "not base64!!",
        }
        bad_uuid: worker.YamlMapping = {
            "name": "bad-vless",
            "type": "vless",
            "server": "c.example",
            "port": 443,
            "uuid": "not-a-uuid",
        }
        nodes, dropped = worker._merge_sources(
            config, {"one": [good, bad_key, bad_uuid], "two": []}
        )
        self.assertEqual(dropped, 2)
        self.assertEqual(list(nodes), [worker._fingerprint(good)])

    def test_drops_ss_plugin_without_mode(self) -> None:
        config = self.config()
        good: worker.YamlMapping = {
            "name": "good-obfs",
            "type": "ss",
            "server": "a.example",
            "port": 8388,
            "cipher": "aes-128-gcm",
            "password": "secret",
            "plugin": "v2ray-plugin",
            "plugin-opts": {"mode": "websocket", "host": "example.com"},
        }
        bad: worker.YamlMapping = {
            "name": "bad-obfs",
            "type": "ss",
            "server": "b.example",
            "port": 8388,
            "cipher": "aes-128-gcm",
            "password": "secret",
            "plugin": "v2ray-plugin",
            "plugin-opts": {"tls": True},
        }
        nodes, dropped = worker._merge_sources(config, {"one": [good, bad]})
        self.assertEqual(dropped, 1)
        self.assertEqual(len(nodes), 1)

    def test_all_sources_failed_keeps_previous_snapshot(self) -> None:
        config = self.config()
        original = worker._fetch_all
        worker._fetch_all = lambda worker_config: ({}, {"one": "boom", "two": "boom"})  # type: ignore[assignment]
        try:
            state = worker.WorkerState(lock=threading.Lock())
            with self.assertRaises(RuntimeError):
                worker.refresh(config, state)
            self.assertFalse(state.ready)
            self.assertIsNone(state.snapshots)
        finally:
            worker._fetch_all = original

    def test_check_config_defaults_to_disabled(self) -> None:
        document: worker.TomlTable = {
            "worker": {
                "subconv_url": "http://subconv:8080",
                "output_dir": "providers",
                "refresh_interval_s": 900,
                "request_timeout_s": 30,
                "retry_backoff_s": 5,
                "max_attempts": 3,
                "listen": "0.0.0.0",
                "port": 8080,
            },
            "sources": [{"name": "s", "url": "https://example.com/s"}],
            "pools": [{"name": "x", "sources": ["s"], "max_nodes": 1, "shared": False}],
        }
        config = worker._parse_config(document)
        self.assertFalse(config.check.enabled)

    def test_check_config_parses(self) -> None:
        document: worker.TomlTable = {
            "worker": {
                "subconv_url": "http://subconv:8080",
                "output_dir": "providers",
                "refresh_interval_s": 900,
                "request_timeout_s": 30,
                "retry_backoff_s": 5,
                "max_attempts": 3,
                "listen": "0.0.0.0",
                "port": 8080,
            },
            "sources": [{"name": "s", "url": "https://example.com/s"}],
            "pools": [{"name": "x", "sources": ["s"], "max_nodes": 1, "shared": False}],
            "check": {
                "url": "https://example.com/health",
                "timeout_s": 5,
                "workers": 10,
            },
        }
        config = worker._parse_config(document)
        self.assertTrue(config.check.enabled)
        self.assertEqual(
            config.check,
            worker.CheckSettings(
                True,
                probe.parse_probe_spec({"url": "https://example.com/health"}),
                probe.parse_expect_spec(None),
                5,
                10,
            ),
        )

    def test_check_config_rejects_bad_url(self) -> None:
        document: worker.TomlTable = {
            "worker": {
                "subconv_url": "http://subconv:8080",
                "output_dir": "providers",
                "refresh_interval_s": 900,
                "request_timeout_s": 30,
                "retry_backoff_s": 5,
                "max_attempts": 3,
                "listen": "0.0.0.0",
                "port": 8080,
            },
            "sources": [{"name": "s", "url": "https://example.com/s"}],
            "pools": [{"name": "x", "sources": ["s"], "max_nodes": 1, "shared": False}],
            "check": {"url": "socks5://example.com:1080"},
        }
        with self.assertRaisesRegex(ValueError, "HTTP"):
            worker._parse_config(document)

    def test_check_all_keeps_live_nodes(self) -> None:
        stop = threading.Event()
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.5)
        upstream = int(listener.getsockname()[1])

        def serve() -> None:
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except OSError:
                    continue
                threading.Thread(target=handle, args=(conn,), daemon=True).start()

        def handle(conn: socket.socket) -> None:
            with conn:
                try:
                    conn.settimeout(5.0)
                    connect = _read_head(conn)
                    if not connect.startswith(b"CONNECT "):
                        return
                    conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    inner = _read_head(conn)
                    if not inner.startswith(b"GET "):
                        return
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    return

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            config = dataclasses.replace(
                self.config(),
                check=_check_settings(
                    {"url": f"http://127.0.0.1:{upstream}/health"},
                    timeout_s=5,
                    workers=4,
                ),
            )
            live_proxy: worker.YamlMapping = {
                "name": "live",
                "type": "http",
                "server": "127.0.0.1",
                "port": upstream,
            }
            dead_proxy: worker.YamlMapping = {
                "name": "dead",
                "type": "http",
                "server": "127.0.0.1",
                "port": 1,
            }
            other_proxy: worker.YamlMapping = {
                "name": "other",
                "type": "vless",
                "server": "x.example",
                "port": 443,
                "uuid": "6f0c1a2b-3c4d-5e6f-8a9b-0c1d2e3f4a5b",
            }
            nodes: dict[str, worker.Node] = {}
            for proxy in (live_proxy, dead_proxy, other_proxy):
                fingerprint = worker._fingerprint(proxy)
                nodes[fingerprint] = worker.Node(dict(proxy), fingerprint, {"one"})
            live, checked, dead = worker._check_all(config, nodes)
            self.assertEqual(checked, 3)
            self.assertEqual(dead, 1)
            self.assertEqual(len(live), 2)
            names = {node.proxy["name"] for node in live.values()}
            self.assertEqual(names, {"live", "other"})
        finally:
            stop.set()
            listener.close()

    def test_check_all_enforces_expect_status(self) -> None:
        stop = threading.Event()
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.5)
        upstream = int(listener.getsockname()[1])

        def serve() -> None:
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except OSError:
                    continue
                threading.Thread(target=handle, args=(conn,), daemon=True).start()

        def handle(conn: socket.socket) -> None:
            with conn:
                try:
                    conn.settimeout(5.0)
                    if not _read_head(conn).startswith(b"CONNECT "):
                        return
                    conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    if not _read_head(conn).startswith(b"GET "):
                        return
                    conn.sendall(b"HTTP/1.1 500 Broken\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    return

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            target = f"http://127.0.0.1:{upstream}/health"
            node_proxy: worker.YamlMapping = {
                "name": "flaky",
                "type": "http",
                "server": "127.0.0.1",
                "port": upstream,
            }
            fingerprint = worker._fingerprint(node_proxy)
            nodes = {fingerprint: worker.Node(dict(node_proxy), fingerprint, {"one"})}
            lax = dataclasses.replace(
                self.config(),
                check=worker.CheckSettings(
                    True,
                    probe.parse_probe_spec({"url": target}),
                    probe.parse_expect_spec(None),
                    5,
                    4,
                ),
            )
            live, _, dead = worker._check_all(lax, dict(nodes))
            self.assertEqual(dead, 0)
            self.assertEqual(len(live), 1)
            strict = dataclasses.replace(
                self.config(),
                check=worker.CheckSettings(
                    True,
                    probe.parse_probe_spec({"url": target}),
                    probe.parse_expect_spec({"status": [200]}),
                    5,
                    4,
                ),
            )
            live, _, dead = worker._check_all(strict, dict(nodes))
            self.assertEqual(dead, 1)
            self.assertEqual(len(live), 0)
        finally:
            stop.set()
            listener.close()

    def test_check_config_rejects_bad_probe(self) -> None:
        for check in (
            {"url": "https://example.com/health", "method": "get"},
            {"url": "https://example.com/health", "method": "GE T"},
            {"url": "https://example.com/health", "headers": ["X-A"]},
            {"url": "https://example.com/health", "body": 5},
            {"url": "https://example.com/health", "expect": {"status": [99]}},
            {
                "url": "https://example.com/health",
                "expect": {"body": {"regex": "(broken"}},
            },
        ):
            document: worker.TomlTable = {
                "worker": {
                    "subconv_url": "http://subconv:8080",
                    "output_dir": "providers",
                    "refresh_interval_s": 900,
                    "request_timeout_s": 30,
                    "retry_backoff_s": 5,
                    "max_attempts": 3,
                    "listen": "0.0.0.0",
                    "port": 8080,
                },
                "sources": [{"name": "s", "url": "https://example.com/s"}],
                "pools": [
                    {"name": "x", "sources": ["s"], "max_nodes": 1, "shared": False}
                ],
                "check": check,
            }
            with self.assertRaises(ValueError, msg=str(check)):
                worker._parse_config(document)

    def test_partial_sources_below_majority_keeps_snapshot(self) -> None:
        config = dataclasses.replace(
            self.config(),
            sources=(
                worker.Source("one", "https://example.com/one"),
                worker.Source("two", "https://example.com/two"),
                worker.Source("three", "https://example.com/three"),
            ),
        )
        original = worker._fetch_all
        worker._fetch_all = lambda worker_config: (  # type: ignore[assignment]
            {
                "one": [
                    {
                        "name": "A",
                        "type": "ss",
                        "server": "a.example",
                        "port": 8388,
                        "cipher": "aes-128-gcm",
                        "password": "secret",
                    }
                ]
            },
            {"two": "boom", "three": "boom"},
        )
        try:
            state = worker.WorkerState(lock=threading.Lock())
            with self.assertRaisesRegex(RuntimeError, "only 1/3 sources"):
                worker.refresh(config, state)
            self.assertFalse(state.ready)
        finally:
            worker._fetch_all = original


if __name__ == "__main__":
    unittest.main()
