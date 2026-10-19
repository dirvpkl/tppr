import os
import sys
import tempfile
import unittest
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import prober

SERVICES = """
[dispatcher]
port = 17893

[[services]]
name = "managed"
subscriptions = ["my-vless"]
select = true
username = "managed"
password = "at-least-8-chars"
"""

PROBER = """
[target]
group = "SVC_managed"
provider = "my-vless"
username = "managed"

[probe]
url = "https://example.com/api"
method = "POST"
expected_status = 400
body_file = "BODY_PATH"
"""


def prober_text(body_path: Path, **replacements: str) -> str:
    # TOML basic strings treat backslash as an escape, and a Windows temp path
    # is full of them.
    text = PROBER.replace("BODY_PATH", body_path.as_posix())
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


class ConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.body = self.root / "body.json"
        self.body.write_text('{"a":1}', encoding="utf-8")
        self.prober = self.root / "prober.toml"
        self.prober.write_text(prober_text(self.body), encoding="utf-8")
        self.services = self.root / "services.toml"
        self.services.write_text(SERVICES, encoding="utf-8")
        self.set_env()

    def set_env(self, **extra: str) -> None:
        previous = {
            name: os.environ.get(name)
            for name in (
                "MIHOMO_API",
                "PROXY_HOST",
                "SERVICES_FILE",
                "PROBER_FILE",
                "HEARTBEAT_MAX_AGE_S",
                "CACHE_FILE",
            )
        }
        os.environ.update(
            {
                "MIHOMO_API": "http://controller:9090",
                "PROXY_HOST": "relay",
                "SERVICES_FILE": str(self.services),
                "PROBER_FILE": str(self.prober),
                "HEARTBEAT_MAX_AGE_S": "180",
                "CACHE_FILE": str(self.root / "cache.json"),
            }
        )
        os.environ.update(extra)

        def restore() -> None:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)


class LoadConfigTests(ConfigTestCase):
    def test_builds_proxy_url_from_services_file(self) -> None:
        config = prober.load_config()
        self.assertEqual(
            config.proxy_url, "http://managed:at-least-8-chars@relay:17893"
        )
        self.assertEqual(config.api, "http://controller:9090")
        self.assertEqual(config.expected_status, 400)
        self.assertEqual(config.exclude_countries, ())
        self.assertEqual(config.cache_file, self.root / "cache.json")

    def test_dispatcher_port_follows_services_file(self) -> None:
        self.services.write_text(
            SERVICES.replace("port = 17893", "port = 20000"), encoding="utf-8"
        )
        self.assertTrue(prober.load_config().proxy_url.endswith("@relay:20000"))

    def test_rejects_group_from_another_account(self) -> None:
        self.prober.write_text(
            prober_text(self.body, **{'group = "SVC_managed"': 'group = "SVC_other"'}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "expected SVC_managed"):
            prober.load_config()

    def test_rejects_provider_outside_the_account(self) -> None:
        self.prober.write_text(
            prober_text(self.body, **{'provider = "my-vless"': 'provider = "other"'}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "not in the subscriptions"):
            prober.load_config()

    def test_rejects_unknown_username(self) -> None:
        self.prober.write_text(
            prober_text(self.body, **{'username = "managed"': 'username = "ghost"'}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "no service in"):
            prober.load_config()

    def test_heartbeat_must_outlive_the_interval(self) -> None:
        self.set_env(HEARTBEAT_MAX_AGE_S="5")
        with self.assertRaisesRegex(ValueError, "HEARTBEAT_MAX_AGE_S"):
            prober.load_config()

    def test_rejects_unknown_target_key(self) -> None:
        # A typo in a target key must abort, not silently steer nothing.
        self.prober.write_text(
            prober_text(
                self.body,
                **{'username = "managed"': 'username = "managed"\nsessin = "x"'},
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "unknown keys: sessin"):
            prober.load_config()

    def test_parses_country_denylist(self) -> None:
        self.prober.write_text(
            prober_text(self.body)
            + '\n[filter]\nexclude_countries = ["ru", "CN", "ru"]\n',
            encoding="utf-8",
        )
        self.assertEqual(prober.load_config().exclude_countries, ("RU", "CN"))

    def test_rejects_unknown_filter_key(self) -> None:
        self.prober.write_text(
            prober_text(self.body) + '\n[filter]\nexclude = ["RU"]\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "unknown keys: exclude"):
            prober.load_config()

    def test_rejects_invalid_country_code(self) -> None:
        for code in ("RUS", "ß", ""):
            with self.subTest(code=code):
                self.prober.write_text(
                    prober_text(self.body)
                    + f'\n[filter]\nexclude_countries = ["{code}"]\n',
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "invalid country code"):
                    prober.load_config()


class PickOrderTests(unittest.TestCase):
    def test_probes_now_first_then_round_robin(self) -> None:
        order, cursor = prober.pick_order(["a", "b", "c"], "b", 0)
        self.assertEqual(order, ["b", "a", "c"])
        self.assertEqual(cursor, 0)

    def test_skips_missing_now(self) -> None:
        order, cursor = prober.pick_order(["a", "b", "c"], "gone", 2)
        self.assertEqual(order, ["c", "a", "b"])
        self.assertEqual(cursor, 2)

    def test_empty_candidates(self) -> None:
        self.assertEqual(prober.pick_order([], None, 7), ([], 0))

    def test_normalizes_cursor(self) -> None:
        order, cursor = prober.pick_order(["a", "b"], None, 9)
        self.assertEqual(cursor, 1)
        self.assertEqual(order, ["b", "a"])


class FakeController(prober.Controller):
    def __init__(self, kind: str, now: str, members: list[str]) -> None:
        super().__init__("http://unused:9090")
        self.kind = kind
        self.now = now
        self.members = members
        self.switched: list[str] = []

    def group(self, name: str) -> dict[str, Any]:
        return {"type": self.kind, "now": self.now, "all": self.members}

    def candidates(self, provider: str, group_all: list[str]) -> list[str]:
        return list(group_all)

    def switch(self, group: str, node: str) -> None:
        self.switched.append(node)


def config() -> prober.ProbeConfig:
    return prober.ProbeConfig(
        api="http://c:9090",
        group="SVC_managed",
        provider="my-vless",
        proxy_url="http://u:p@relay:17893",
        probe_url="https://example.com/api",
        method="POST",
        headers=(),
        body=b"{}",
        expected_status=400,
        timeout_s=5,
        interval_s=15,
        heartbeat_max_age_s=180,
        exclude_countries=(),
        cache_file=Path("cache.json"),
    )


class RunRoundTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = prober.VerdictCache(
            path=Path(tmp.name) / "cache.json", day="2026-10-19"
        )

    def run_round(
        self,
        controller: FakeController,
        results: list[tuple[int | None, str]],
        *,
        exclude_countries: tuple[str, ...] = (),
        cursor: int = 0,
    ) -> tuple[int, Any]:
        probe_config = replace(config(), exclude_countries=exclude_countries)
        with mock.patch.object(prober, "probe_once", side_effect=results) as probe:
            cursor = prober.run_round(
                probe_config,
                controller,
                urllib.request.OpenerDirector(),
                cursor,
                self.cache,
            )
        return cursor, probe

    def test_skips_a_node_cached_bad(self) -> None:
        controller = FakeController("Selector", "a", ["a", "b"])
        self.cache.record("a", False)
        cursor, probe = self.run_round(controller, [(400, "ok")])
        self.assertEqual(controller.switched, ["b"])
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(cursor, 0)

    def test_country_filter_skips_denied_node(self) -> None:
        controller = FakeController(
            "Selector", "🇷🇺 YT & DS 4", ["🇷🇺 YT & DS 4", "🇩🇪 Германия 1"]
        )
        cursor, probe = self.run_round(
            controller, [(400, "ok")], exclude_countries=("RU",)
        )
        self.assertEqual(controller.switched, ["🇩🇪 Германия 1"])
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(cursor, 0)

    def test_retries_every_node_when_all_are_cached_bad(self) -> None:
        controller = FakeController("Selector", "a", ["a", "b"])
        self.cache.record("a", False)
        self.cache.record("b", False)
        cursor, probe = self.run_round(controller, [(500, "bad"), (400, "ok")])
        self.assertEqual(controller.switched, ["b"])
        self.assertEqual(probe.call_count, 2)
        self.assertTrue(self.cache.is_bad("a"))
        self.assertFalse(self.cache.is_bad("b"))
        self.assertEqual(cursor, 1)

    def test_nothing_to_probe_when_filter_removes_every_node(self) -> None:
        controller = FakeController("Selector", "🇷🇺 a", ["🇷🇺 a"])
        cursor, probe = self.run_round(
            controller, [(400, "ok")], exclude_countries=("RU",)
        )
        self.assertEqual(controller.switched, [])
        self.assertEqual(probe.call_count, 0)
        self.assertEqual(cursor, 0)


class GroupTypeTests(unittest.TestCase):
    """Mihomo answers "Selector" for a `type: select` group. The original code
    compared against "select" literally and crashed on every round, 2873 times."""

    def test_accepts_controller_spelling(self) -> None:
        controller = FakeController("Selector", "a", ["a", "b"])
        self.assertEqual(prober._check_select_group(controller, config()), ["a", "b"])

    def test_accepts_config_spelling(self) -> None:
        controller = FakeController("select", "a", ["a"])
        self.assertEqual(prober._check_select_group(controller, config()), ["a"])

    def test_rejects_fallback_group(self) -> None:
        controller = FakeController("Fallback", "a", ["a"])
        with self.assertRaisesRegex(ValueError, "only select groups"):
            prober._check_select_group(controller, config())


if __name__ == "__main__":
    unittest.main()
