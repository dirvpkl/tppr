import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tester
from history import History


class ConfigTests(unittest.TestCase):
    def test_missing_config_fails_loudly(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(ValueError, "MIHOMO_API"),
        ):
            tester.TesterConfig()


class CandidateScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = object.__new__(tester.TesterConfig)
        self.config.probe_batch_size = 100
        self.config.probe_workers = 100
        self.config.max_probe_batches = 1
        self.config.max_delay_ms = 800
        self.config.cooldown_fails = 3
        self.config.cooldown_h = 1
        self.history = History(":memory:")

    def tearDown(self) -> None:
        self.history.close()

    def test_stops_after_first_good_candidate(self) -> None:
        nodes = {f"node-{index}": "provider" for index in range(1000)}
        calls: list[str] = []

        def probe(
            _config: tester.TesterConfig, node: str, _provider: str
        ) -> tester.ProbeResult:
            calls.append(node)
            delay = 100 if int(node.rsplit("-", 1)[1]) < 10 else 2000
            return tester.ProbeResult(node=node, delay_ms=delay, error=None)

        with (
            patch.object(tester, "probe_node", side_effect=probe),
            patch.object(tester.random, "shuffle", lambda values: None),
        ):
            results = tester.find_good_candidates(
                self.config, self.history, nodes, excluded=set(), target_good=1
            )

        self.assertEqual(100, len(calls))
        self.assertEqual(1, len(results))
        self.assertTrue(all(result.delay_ms == 100 for result in results))

    def test_does_not_scan_second_batch(self) -> None:
        nodes = {f"node-{index}": "provider" for index in range(150)}
        calls: list[str] = []

        def probe(
            _config: tester.TesterConfig, node: str, _provider: str
        ) -> tester.ProbeResult:
            calls.append(node)
            index = int(node.rsplit("-", 1)[1])
            return tester.ProbeResult(
                node=node,
                delay_ms=100 if index >= 100 else 2000,
                error=None,
            )

        with (
            patch.object(tester, "probe_node", side_effect=probe),
            patch.object(tester.random, "shuffle", lambda values: None),
        ):
            results = tester.find_good_candidates(
                self.config, self.history, nodes, excluded=set(), target_good=1
            )

        self.assertEqual(100, len(calls))
        self.assertEqual(0, len(results))

    def test_uses_next_batches_until_limit(self) -> None:
        self.config.max_probe_batches = 3
        nodes = {f"node-{index}": "provider" for index in range(400)}
        calls: list[str] = []

        def probe(
            _config: tester.TesterConfig, node: str, _provider: str
        ) -> tester.ProbeResult:
            calls.append(node)
            index = int(node.rsplit("-", 1)[1])
            return tester.ProbeResult(
                node=node,
                delay_ms=100 if index >= 300 else 2000,
                error=None,
            )

        with (
            patch.object(tester, "probe_node", side_effect=probe),
            patch.object(tester.random, "shuffle", lambda values: None),
        ):
            results = tester.find_good_candidates(
                self.config, self.history, nodes, excluded=set(), target_good=1
            )

        self.assertEqual(300, len(calls))
        self.assertEqual(0, len(results))

    def test_probe_batch_size_is_capped(self) -> None:
        environment = {
            "MIHOMO_API": "http://mihomo:9090",
            "PROXY_URL": "http://mihomo:7891",
            "TEST_GROUP": "TEST_POOL",
            "PRODUCTION_GROUP": "POOL",
            "PROVIDER_NAMES": "provider",
            "TEST_URL": "https://example.com/speed",
            "HEALTH_URL": "https://example.com/health",
            "HEALTH_INTERVAL_S": "10",
            "HEALTH_TIMEOUT_MS": "3000",
            "MAX_DELAY_MS": "800",
            "PROBE_BATCH_SIZE": "101",
            "PROBE_WORKERS": "100",
            "MAX_PROBE_BATCHES": "3",
            "FAILOVER_GOOD_CANDIDATES": "1",
            "SWEEP_GOOD_CANDIDATES": "5",
            "TEST_INTERVAL_S": "300",
            "TEST_TIMEOUT_S": "15",
            "TEST_MAX_BYTES": "8388608",
            "TEST_MAX_NODES": "5",
            "HISTORY_DB": ":memory:",
            "HISTORY_COOLDOWN_FAILS": "3",
            "HISTORY_COOLDOWN_H": "1",
            "HISTORY_WINDOW_H": "24",
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            self.assertRaisesRegex(ValueError, "PROBE_BATCH_SIZE"),
        ):
            tester.TesterConfig()

    def test_probe_limits_are_capped(self) -> None:
        environment = {
            "MIHOMO_API": "http://mihomo:9090",
            "PROXY_URL": "http://mihomo:7891",
            "TEST_GROUP": "TEST_POOL",
            "PRODUCTION_GROUP": "POOL",
            "PROVIDER_NAMES": "provider",
            "TEST_URL": "https://example.com/speed",
            "HEALTH_URL": "https://example.com/health",
            "HEALTH_INTERVAL_S": "10",
            "HEALTH_TIMEOUT_MS": "3000",
            "MAX_DELAY_MS": "800",
            "PROBE_BATCH_SIZE": "100",
            "PROBE_WORKERS": "101",
            "MAX_PROBE_BATCHES": "3",
            "FAILOVER_GOOD_CANDIDATES": "1",
            "SWEEP_GOOD_CANDIDATES": "5",
            "TEST_INTERVAL_S": "300",
            "TEST_TIMEOUT_S": "15",
            "TEST_MAX_BYTES": "8388608",
            "TEST_MAX_NODES": "5",
            "HISTORY_DB": ":memory:",
            "HISTORY_COOLDOWN_FAILS": "3",
            "HISTORY_COOLDOWN_H": "1",
            "HISTORY_WINDOW_H": "24",
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            self.assertRaisesRegex(ValueError, "PROBE_WORKERS"),
        ):
            tester.TesterConfig()

    def test_max_probe_batches_is_capped(self) -> None:
        environment = {
            "MIHOMO_API": "http://mihomo:9090",
            "PROXY_URL": "http://mihomo:7891",
            "TEST_GROUP": "TEST_POOL",
            "PRODUCTION_GROUP": "POOL",
            "PROVIDER_NAMES": "provider",
            "TEST_URL": "https://example.com/speed",
            "HEALTH_URL": "https://example.com/health",
            "HEALTH_INTERVAL_S": "10",
            "HEALTH_TIMEOUT_MS": "3000",
            "MAX_DELAY_MS": "800",
            "PROBE_BATCH_SIZE": "100",
            "PROBE_WORKERS": "100",
            "MAX_PROBE_BATCHES": "4",
            "FAILOVER_GOOD_CANDIDATES": "1",
            "SWEEP_GOOD_CANDIDATES": "5",
            "TEST_INTERVAL_S": "300",
            "TEST_TIMEOUT_S": "15",
            "TEST_MAX_BYTES": "8388608",
            "TEST_MAX_NODES": "5",
            "HISTORY_DB": ":memory:",
            "HISTORY_COOLDOWN_FAILS": "3",
            "HISTORY_COOLDOWN_H": "1",
            "HISTORY_WINDOW_H": "24",
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            self.assertRaisesRegex(ValueError, "MAX_PROBE_BATCHES"),
        ):
            tester.TesterConfig()

    def test_available_nodes_filters_to_selector_membership(self) -> None:
        config = object.__new__(tester.TesterConfig)
        config.provider_names = ("provider",)
        config.health_timeout_ms = 3000
        with patch.object(
            tester,
            "api_request",
            return_value={"proxies": [{"name": "allowed"}, {"name": "not-in-group"}]},
        ):
            nodes = tester.available_nodes(config, {"allowed"})
        self.assertEqual({"allowed": "provider"}, nodes)

    def test_sweep_benchmarks_five_total_nodes(self) -> None:
        config = object.__new__(tester.TesterConfig)
        config.production_group = "POOL"
        config.group = "TEST_POOL"
        config.test_max_nodes = 5
        config.history_window_h = 24
        config.cooldown_h = 1
        config.sweep_good_candidates = 5
        history = History(":memory:")
        candidates = [
            tester.ProbeResult(f"candidate-{index}", 100 + index, None)
            for index in range(5)
        ]
        with (
            patch.object(
                tester,
                "group_state",
                side_effect=[
                    (["current", *(result.node for result in candidates)], "current")
                ]
                * 2,
            ),
            patch.object(
                tester,
                "available_nodes",
                return_value={
                    "current": "provider",
                    **{result.node: "provider" for result in candidates},
                },
            ),
            patch.object(
                tester,
                "find_good_candidates",
                return_value=candidates,
            ),
            patch.object(
                tester,
                "_download_speed",
                side_effect=lambda _config, node: 100.0 if node == "current" else 50.0,
            ),
            patch.object(tester, "_set_production") as set_production,
        ):
            tester.sweep(config, history)
        set_production.assert_called_once_with(config, "current")
        history.close()

    def test_incomplete_read_is_a_failed_speed_probe(self) -> None:
        config = object.__new__(tester.TesterConfig)
        config.group = "TEST_POOL"
        config.url = "https://example.com/speed"
        config.proxy = "http://mihomo:7891"
        config.test_max_bytes = 1024
        config.test_timeout_s = 15
        response = patch.object(
            tester.urllib.request,
            "build_opener",
            return_value=MagicMock(
                open=MagicMock(
                    side_effect=tester.http.client.IncompleteRead(b"partial")
                )
            ),
        )
        with (
            patch.object(tester, "select_node"),
            patch.object(tester, "group_state", return_value=([], "candidate")),
            response,
        ):
            speed = tester._download_speed(config, "candidate")
        self.assertEqual(-1.0, speed)

    def test_short_content_length_is_a_failed_speed_probe(self) -> None:
        config = object.__new__(tester.TesterConfig)
        config.group = "TEST_POOL"
        config.url = "https://example.com/speed"
        config.proxy = "http://mihomo:7891"
        config.test_max_bytes = 1024
        config.test_timeout_s = 15
        response = MagicMock()
        response.headers = {"Content-Length": "57"}
        response.read.side_effect = [b"partial", b""]
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with (
            patch.object(tester, "select_node"),
            patch.object(tester, "group_state", return_value=([], "candidate")),
            patch.object(tester.urllib.request, "build_opener", return_value=opener),
        ):
            speed = tester._download_speed(config, "candidate")
        self.assertEqual(-1.0, speed)

    def test_sweep_does_not_switch_without_positive_speed(self) -> None:
        config = object.__new__(tester.TesterConfig)
        config.production_group = "POOL"
        config.group = "TEST_POOL"
        config.test_max_nodes = 5
        config.history_window_h = 24
        config.cooldown_h = 1
        config.sweep_good_candidates = 1
        history = History(":memory:")
        with (
            patch.object(
                tester,
                "group_state",
                side_effect=[(["current", "candidate"], "current")] * 2,
            ),
            patch.object(
                tester,
                "available_nodes",
                return_value={"current": "provider", "candidate": "provider"},
            ),
            patch.object(
                tester,
                "find_good_candidates",
                return_value=[tester.ProbeResult("candidate", 100, None)],
            ),
            patch.object(tester, "_download_speed", return_value=-1.0),
            patch.object(tester, "_set_production") as set_production,
        ):
            tester.sweep(config, history)
        set_production.assert_not_called()
        history.close()

    def test_excludes_cooldown_and_production_node(self) -> None:
        self.history.record_sweep("old", {"dead": -1.0})
        self.history.record_sweep("old", {"dead": -1.0})
        self.history.record_sweep("old", {"dead": -1.0})
        with patch.object(
            tester,
            "probe_node",
            side_effect=lambda _config, node, _provider: tester.ProbeResult(
                node, 100, None
            ),
        ) as probe:
            tester.find_good_candidates(
                self.config,
                self.history,
                {"current": "provider", "dead": "provider", "fresh": "provider"},
                excluded={"current"},
                target_good=1,
            )
        probed = [call.args[1] for call in probe.call_args_list]
        self.assertEqual(["fresh"], probed)


if __name__ == "__main__":
    unittest.main()
