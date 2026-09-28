import dataclasses
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker


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


if __name__ == "__main__":
    unittest.main()
