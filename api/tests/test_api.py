import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import api

HEALTH = """[health]
url = "https://example.com/health"
interval = 60
timeout = 5000
max_failed_times = 3
expected_status = 204

[dispatcher]
port = 20000

[[services]]
name = "seed"
subscriptions = ["extra"]
username = "seed"
password = "0123456789abcdef"
"""


def _request(
    method: str, base: str, path: str, token: str | None, body: object = None
) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body).encode()
    request = Request(f"{base}{path}", data=data, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.services = self.root / "services.toml"
        self.services.write_text(HEALTH, encoding="utf-8")
        self.config = api.AppConfig(
            token="test-token",
            services_path=self.services,
            custom_path=self.root / "mine.yaml",
            generated_path=self.root / "generated.yaml",
            worker_url="http://worker:8080",
            hash_db_path=self.root / "hashes.db",
            listen="127.0.0.1",
            port=0,
        )
        self.state = api.AppState(lock=threading.Lock(), config=self.config)
        self.server = api.ThreadingHTTPServer(
            ("127.0.0.1", 0), api.ApiHandler, bind_and_activate=False
        )
        self.server.allow_reuse_address = True
        self.server.daemon_threads = True
        self.server.state = self.state  # type: ignore[attr-defined]
        self.server.server_bind()
        self.server.server_activate()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._worker = api._worker_pools
        api._worker_pools = lambda url: {"extra": 10}  # type: ignore[assignment]  # noqa: E731

    def tearDown(self) -> None:
        api._worker_pools = self._worker
        self.server.shutdown()
        self.directory.cleanup()

    def test_unauthorized(self) -> None:
        status, payload = _request("GET", self.base, "/status", None)
        self.assertEqual(status, 401)
        status, _ = _request("GET", self.base, "/status", "wrong")
        self.assertEqual(status, 401)

    def test_index(self) -> None:
        status, payload = _request("GET", self.base, "/", "test-token")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_status_shape(self) -> None:
        status, payload = _request("GET", self.base, "/status", "test-token")
        self.assertEqual(status, 200)
        self.assertEqual(payload["services_total"], 1)
        self.assertEqual(payload["pools"], {"extra": 10})
        self.assertEqual(payload["leases"], {})
        self.assertTrue(payload["config_stale"])

    def test_create_lists_without_password(self) -> None:
        status, created = _request(
            "POST",
            self.base,
            "/accounts",
            "test-token",
            {
                "name": "acc01",
                "subscriptions": ["extra"],
                "username": "acc01",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["name"], "acc01")
        self.assertEqual(len(str(created["password"])), 16)
        self.assertTrue(created["reload_required"])
        status, listed = _request("GET", self.base, "/accounts", "test-token")
        self.assertEqual(status, 200)
        accounts = cast(list[object], listed["accounts"])
        self.assertEqual(len(accounts), 2)
        first = cast(dict[str, object], accounts[0])
        self.assertTrue(first["has_password"])
        self.assertNotIn("password", first)

    def test_create_duplicate_conflicts(self) -> None:
        body: dict[str, object] = {
            "name": "acc01",
            "subscriptions": ["extra"],
            "username": "acc01",
        }
        _request("POST", self.base, "/accounts", "test-token", body)
        status, payload = _request("POST", self.base, "/accounts", "test-token", body)
        self.assertEqual(status, 409)
        self.assertIn("duplicate", str(payload["error"]))

    def test_create_rejects_bad_input(self) -> None:
        status, _ = _request(
            "POST",
            self.base,
            "/accounts",
            "test-token",
            {"name": "acc01", "subscriptions": ["missing"]},
        )
        self.assertEqual(status, 400)
        status, _ = _request(
            "POST",
            self.base,
            "/accounts",
            "test-token",
            {
                "name": "acc02",
                "subscriptions": ["extra"],
                "username": "acc02",
                "password": "short",
            },
        )
        self.assertEqual(status, 400)
        status, _ = _request(
            "POST",
            self.base,
            "/accounts",
            "test-token",
            {"name": "acc03", "primary": "ghost", "lock_proxy": True},
        )
        self.assertEqual(status, 400)

    def test_delete_account(self) -> None:
        _request(
            "POST",
            self.base,
            "/accounts",
            "test-token",
            {"name": "acc01", "subscriptions": ["extra"], "username": "acc01"},
        )
        status, payload = _request("DELETE", self.base, "/accounts/acc01", "test-token")
        self.assertEqual(status, 200)
        self.assertTrue(payload["reload_required"])
        status, _ = _request("DELETE", self.base, "/accounts/acc01", "test-token")
        self.assertEqual(status, 404)
        status, _ = _request("DELETE", self.base, "/accounts/nope", "test-token")
        self.assertEqual(status, 404)

    def test_leases(self) -> None:
        db = sqlite3.connect(str(self.config.hash_db_path))
        db.execute(
            "CREATE TABLE leases(pool TEXT, h TEXT, first_seen INTEGER, ttl INTEGER, "
            "PRIMARY KEY(pool, h))"
        )
        db.execute("INSERT INTO leases VALUES('free', 'abc123', 1000, 3600)")
        db.commit()
        db.close()
        status, payload = _request("GET", self.base, "/leases", "test-token")
        self.assertEqual(status, 200)
        leases = cast(list[object], payload["leases"])
        self.assertEqual(len(leases), 1)
        first_lease = cast(dict[str, object], leases[0])
        self.assertEqual(first_lease["user"], "abc123")
        status, payload = _request("GET", self.base, "/leases?pool=free", "test-token")
        self.assertEqual(len(cast(list[object], payload["leases"])), 1)
        status, payload = _request(
            "DELETE", self.base, "/leases/free/abc123", "test-token"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["revoked"])
        status, _ = _request("DELETE", self.base, "/leases/free/abc123", "test-token")
        self.assertEqual(status, 404)

    def test_leases_missing_db(self) -> None:
        status, payload = _request("GET", self.base, "/leases", "test-token")
        self.assertEqual(status, 200)
        self.assertEqual(payload["leases"], [])
        status, _ = _request("DELETE", self.base, "/leases/free/abc123", "test-token")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
