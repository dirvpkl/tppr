import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate

HEALTH = """
[health]
url = "https://example.com/health"
interval = 60
timeout = 5000
max_failed_times = 3
expected_status = 204
"""

BASE = (
    "\nproxy-providers:\n  free-vless:\n    type: http\n"
    "    url: https://example.com/free\n\n"
    "proxy-groups:\n  - name: POOL\n    type: select\n    use: [free-vless]\n"
    "  - name: TEST_POOL\n    type: select\n    use: [free-vless]\n\n"
    "rules:\n  - MATCH,DIRECT\n\nprofile:\n  store-selected: true\n"
)

SUBSCRIPTION = """
[[subscriptions]]
name = "extra"
url = "https://example.com/extra"
interval = 21600
"""

CUSTOM = {
    "proxy-a": {"name": "proxy-a", "type": "ss", "server": "a.example", "port": 8388},
    "proxy-b": {"name": "proxy-b", "type": "ss", "server": "b.example", "port": 8388},
}


class GenerateTests(unittest.TestCase):
    def write_services(self, directory: Path, body: str) -> Path:
        path = directory / "services.toml"
        path.write_text(HEALTH + body, encoding="utf-8")
        return path

    def load(self, body: str, custom_names: set[str] | None = None):
        declarations = "" if "[[subscriptions]]" in body else SUBSCRIPTION
        with tempfile.TemporaryDirectory() as raw_directory:
            path = self.write_services(Path(raw_directory), declarations + body)
            return generate._load_services(path, custom_proxy_names=custom_names)

    def test_renders_fixed_ports_and_ordered_fallbacks(self) -> None:
        health, parsed, subscriptions, dispatcher, global_pools = self.load(
            """
[[services]]
            name = "telegram"
port = 20001
primary = "proxy-a"
fallback = ["proxy-b"]
""",
            set(CUSTOM),
        )
        rendered = generate.render_config(
            BASE, parsed, subscriptions, health, CUSTOM, dispatcher
        )
        override = generate.render_compose_override(parsed, dispatcher, global_pools)
        self.assertIn('name: "SVC_telegram"', rendered)
        self.assertIn("port: 20001", rendered)
        self.assertLess(rendered.index('"proxy-a"'), rendered.index('"proxy-b"'))
        self.assertIn('      - "0.0.0.0:20001:20001"', override)

    def test_renders_subscription_provider_and_service_use(self) -> None:
        body = """
[[services]]
name = "telegram"
port = 20001
subscriptions = ["extra"]
"""
        health, parsed, subscriptions, dispatcher, _ = self.load(body)
        rendered = generate.render_config(
            BASE, parsed, subscriptions, health, {}, dispatcher
        )
        self.assertIn('  "extra":\n', rendered)
        self.assertIn('    url: "https://example.com/extra"', rendered)
        self.assertIn("    path: ./providers/extra.yaml", rendered)
        self.assertIn('    use:\n      - "extra"', rendered)
        self.assertIn("      timeout: 5000", rendered)
        self.assertIn("    health-check:\n      enable: true", rendered)

    def test_rejects_unknown_service_subscription(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown provider"):
            self.load("""
[[services]]
name = "telegram"
port = 20001
subscriptions = ["missing"]
""")

    def test_renders_dispatcher_authentication_and_user_rules(self) -> None:
        health, parsed, subscriptions, dispatcher, _ = self.load(
            """
[dispatcher]
port = 20000

[[services]]
name = "telegram"
primary = "proxy-a"
subscriptions = ["extra"]
username = "telegram"
password = "secret123"
""",
            set(CUSTOM),
        )
        rendered = generate.render_config(
            BASE, parsed, subscriptions, health, CUSTOM, dispatcher
        )
        override = generate.render_compose_override(parsed, dispatcher, None)
        self.assertIn('  - name: "dispatcher"', rendered)
        self.assertIn("    port: 20000", rendered)
        self.assertIn('      - username: "telegram"', rendered)
        self.assertIn('        password: "secret123"', rendered)
        self.assertIn("  - IN-USER,telegram,SVC_telegram", rendered)
        self.assertLess(rendered.index("IN-USER"), rendered.index("MATCH,DIRECT"))
        self.assertIn('      - "0.0.0.0:20000:20000"', override)

    def test_rejects_dispatcher_without_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires at least one service"):
            self.load(
                """
[dispatcher]
port = 20000

[[services]]
name = "telegram"
port = 20001
subscriptions = ["extra"]
""",
                set(CUSTOM),
            )

    def test_pinned_proxy_falls_back_to_free_pool(self) -> None:
        health, parsed, subscriptions, dispatcher, _ = self.load(
            """
[[subscriptions]]
name = "my-vless"
url = "https://example.com/subscription"
interval = 21600

[[services]]
name = "acc01"
port = 20001
primary = "proxy-a"
subscriptions = ["my-vless"]
""",
            set(CUSTOM),
        )
        rendered = generate.render_config(
            BASE, parsed, subscriptions, health, CUSTOM, dispatcher
        )
        group = rendered[rendered.index('name: "SVC_acc01"') :]
        self.assertIn('      - "proxy-a"\n', group)
        self.assertIn('    use:\n      - "my-vless"\n', group)
        self.assertFalse(parsed[0].lock_proxy)

    def test_lock_proxy_forbids_fallback_sources(self) -> None:
        with self.assertRaisesRegex(ValueError, "lock_proxy forbids"):
            self.load(
                """
[[services]]
name = "acc01"
port = 20001
primary = "proxy-a"
subscriptions = ["extra"]
lock_proxy = true
""",
                set(CUSTOM),
            )

    def test_requires_fallback_source_unless_locked(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a fallback source"):
            self.load(
                """
[[services]]
name = "acc01"
port = 20001
primary = "proxy-a"
""",
                set(CUSTOM),
            )

    def test_rejects_unknown_primary_proxy(self) -> None:
        with self.assertRaisesRegex(ValueError, "not defined in the custom proxy"):
            self.load(
                """
[[services]]
name = "acc01"
port = 20001
primary = "typo-name"
subscriptions = ["extra"]
""",
                set(CUSTOM),
            )

    def test_service_without_port_requires_dispatcher_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires port or dispatcher"):
            self.load("""
[[services]]
name = "acc01"
subscriptions = ["extra"]
""")

    def test_rejects_short_password(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 8 characters"):
            self.load("""
[dispatcher]
port = 20000

[[services]]
name = "acc01"
port = 20001
subscriptions = ["extra"]
username = "acc01"
password = "short"
""")

    def test_renders_global_pool_groups_and_ports(self) -> None:
        health, parsed, subscriptions, dispatcher, global_pools = self.load(
            """
[global_pools]
free_port = 17891
custom_port = 17892

[dispatcher]
port = 17893

[[services]]
name = "acc01"
primary = "proxy-a"
subscriptions = ["extra"]
username = "acc01"
password = "secret123"
""",
            set(CUSTOM),
        )
        rendered = generate.render_config(
            BASE,
            parsed,
            subscriptions,
            health,
            CUSTOM,
            dispatcher,
            None,
            global_pools,
        )
        override = generate.render_compose_override(parsed, dispatcher, global_pools)
        self.assertIn('  - name: "FREE"', rendered)
        self.assertIn('  - name: "CUSTOM"', rendered)
        self.assertIn('    proxy: "FREE"', rendered)
        self.assertIn('    proxy: "CUSTOM"', rendered)
        for port in ("17891", "17892", "17893"):
            self.assertIn(f'      - "0.0.0.0:{port}:{port}"', override)
        self.assertNotIn("20001", override)

    def test_renders_custom_proxies_inline(self) -> None:
        health, parsed, subscriptions, dispatcher, _ = self.load(
            """
[global_pools]
custom_port = 17892

[[services]]
name = "acc01"
port = 20001
primary = "proxy-a"
lock_proxy = true
""",
            set(CUSTOM),
        )
        rendered = generate.render_config(
            BASE, parsed, subscriptions, health, CUSTOM, dispatcher
        )
        self.assertIn("proxies:\n- name: proxy-a", rendered)
        self.assertLess(
            rendered.index("proxies:\n- name: proxy-a"),
            rendered.index("proxy-providers:"),
        )
        self.assertIn(
            "  - name: POOL\n    type: select\n"
            "    use: [free-vless, extra]\n"
            '    proxies:\n      - "proxy-a"\n      - "proxy-b"',
            rendered,
        )
        self.assertIn(
            "  - name: TEST_POOL\n    type: select\n"
            "    use: [free-vless, extra]\n"
            '    proxies:\n      - "proxy-a"',
            rendered,
        )

    def test_renders_local_pool_provider(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            pool_path = directory / "pool-sources.toml"
            pool_path.write_text(
                '[worker]\nendpoint = "http://pool-worker:8080"\n'
                'refresh_interval_s = 900\n\n[[pools]]\nname = "telegram"\n'
                'sources = ["source"]\nmax_nodes = 100\nshared = false\n',
                encoding="utf-8",
            )
            pools = generate._load_pools(pool_path)
            services = self.write_services(
                directory,
                """
[[services]]
name = "telegram"
port = 20001
subscriptions = ["telegram"]
""",
            )
            health, parsed, subscriptions, dispatcher, _ = generate._load_services(
                services, {pool.name for pool in pools}
            )
            rendered = generate.render_config(
                BASE, parsed, subscriptions, health, {}, dispatcher, pools
            )
        self.assertIn('  "telegram":\n', rendered)
        self.assertIn(
            '    url: "http://pool-worker:8080/pools/telegram.yaml"', rendered
        )
        self.assertIn("    path: ./providers/pool-telegram.yaml", rendered)

    def test_generate_embeds_custom_proxy_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            services = self.write_services(
                directory,
                """
[global_pools]
custom_port = 17892

[[services]]
name = "acc01"
port = 20001
primary = "proxy-a"
lock_proxy = true
""",
            )
            base_path = directory / "config.base.yaml"
            base_path.write_text(BASE, encoding="utf-8")
            custom_path = directory / "providers" / "mine.yaml"
            custom_path.parent.mkdir()
            custom_path.write_text(
                "proxies:\n  - name: proxy-a\n    type: ss\n"
                "    server: a.example\n    port: 8388\n",
                encoding="utf-8",
            )
            config_path = directory / "generated" / "config.yaml"
            override_path = directory / "docker-compose.override.yml"
            generate.generate(
                services,
                base_path,
                config_path,
                override_path,
                directory / "missing-pools.toml",
                custom_path,
            )
            rendered = config_path.read_text(encoding="utf-8")
            override = override_path.read_text(encoding="utf-8")
        self.assertIn("proxies:\n- name: proxy-a", rendered)
        self.assertNotIn("type: file", rendered)
        self.assertLess(
            rendered.index("proxies:\n- name: proxy-a"),
            rendered.index("proxy-providers:"),
        )
        self.assertIn("  free-vless:\n    type: http", rendered)
        self.assertIn('      - "0.0.0.0:17892:17892"', override)

    def test_rejects_reserved_service_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "reserved by Mihomo"):
            self.load("""
[[services]]
name = "one"
port = 7890
subscriptions = ["extra"]
""")

    def test_rejects_duplicate_ports(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicates another listener"):
            self.load("""
[[services]]
name = "one"
port = 20001
subscriptions = ["extra"]

[[services]]
name = "two"
port = 20001
subscriptions = ["extra"]
""")

    def test_rejects_invalid_proxy_lists(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate proxy"):
            self.load(
                """
[[services]]
name = "one"
port = 20001
primary = "proxy-a"
fallback = ["proxy-a"]
lock_proxy = true
""",
                set(CUSTOM),
            )

    def test_renders_load_balance_group(self) -> None:
        health, parsed, subscriptions, dispatcher, _ = self.load("""
[[services]]
name = "random"
port = 20001
subscriptions = ["extra"]
balance = "round-robin"
""")
        rendered = generate.render_config(
            BASE, parsed, subscriptions, health, {}, dispatcher
        )
        self.assertIn('  - name: "SVC_random"\n    type: load-balance', rendered)
        self.assertIn("    strategy: round-robin", rendered)
        self.assertEqual(parsed[0].balance, "round-robin")

    def test_rejects_balance_with_primary(self) -> None:
        with self.assertRaisesRegex(ValueError, "balance rotates the whole pool"):
            self.load(
                """
[[services]]
name = "one"
port = 20001
primary = "proxy-a"
subscriptions = ["extra"]
balance = "round-robin"
""",
                set(CUSTOM),
            )

    def test_rejects_balance_without_subscriptions(self) -> None:
        with self.assertRaisesRegex(ValueError, "balance requires"):
            self.load("""
[[services]]
name = "one"
port = 20001
balance = "round-robin"
""")

    def test_rejects_unknown_balance_strategy(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be one of"):
            self.load("""
[[services]]
name = "one"
port = 20001
subscriptions = ["extra"]
balance = "lottery"
""")


if __name__ == "__main__":
    unittest.main()
