"""Management REST API for the proxy pool.

Localhost only, Bearer token from the API_TOKEN environment variable. Lets
agents manage accounts, inspect pools and revoke hash-gate leases without
touching TOML files or running scripts.

Mutations edit services.toml (validated before write) and report
reload_required; only reload.ps1 recreates containers. Lease revocation takes
effect immediately because the hash gate checks SQLite on every connection.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import logging.handlers
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

sys.path.insert(0, "/scripts")

import generate  # noqa: E402
from new_accounts import random_password  # noqa: E402

SERVICE_NAME = "tppr-api"
MAX_BODY = 65536
WORKER_TIMEOUT_S = 5
DB_TIMEOUT_S = 5

LOG = logging.getLogger(SERVICE_NAME)


@dataclass(frozen=True)
class AppConfig:
    token: str
    services_path: Path
    custom_path: Path
    generated_path: Path
    worker_url: str
    hash_db_path: Path
    listen: str
    port: int


@dataclass
class AppState:
    lock: threading.Lock
    config: AppConfig


def _load_config() -> AppConfig:
    token = os.environ.get("API_TOKEN", "").strip()
    if not token:
        raise ValueError("API_TOKEN environment variable is required")
    services_path = Path(os.environ.get("SERVICES_PATH", "/config/services.toml"))
    if not services_path.is_file():
        raise ValueError(f"services file not found: {services_path}")
    return AppConfig(
        token=token,
        services_path=services_path,
        custom_path=Path(os.environ.get("CUSTOM_PATH", "/mihomo/providers/mine.yaml")),
        generated_path=Path(
            os.environ.get("GENERATED_PATH", "/mihomo/generated/config.yaml")
        ),
        worker_url=os.environ.get("WORKER_URL", "http://tppr-pool-worker:8080").rstrip(
            "/"
        ),
        hash_db_path=Path(os.environ.get("HASH_DB", "/hashdata/hashes.db")),
        listen=os.environ.get("LISTEN", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
    )


def _worker_pools(worker_url: str) -> dict[str, int] | None:
    try:
        with urlopen(f"{worker_url}/healthz", timeout=WORKER_TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError) as exc:
        LOG.warning("pool worker unreachable: %s", exc)
        return None
    pools = payload.get("pool_nodes", {})
    if type(pools) is not dict:
        return None
    return {str(name): int(count) for name, count in pools.items()}


def _custom_names(custom_path: Path) -> set[str]:
    if not custom_path.is_file():
        return set()
    try:
        import yaml  # type: ignore[import-untyped]

        document = yaml.safe_load(custom_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - foreign file, report and continue empty
        LOG.warning("cannot read custom proxies %s: %s", custom_path, exc)
        return set()
    if type(document) is not dict:
        return set()
    raw = document.get("proxies", [])
    if type(raw) is not list:
        return set()
    return {
        str(proxy["name"])
        for proxy in raw
        if type(proxy) is dict and type(proxy.get("name")) is str
    }


def _service_names(text: str) -> set[str]:
    return set(re.findall(r'(?m)^name = "([^"]+)"\s*$', text))


def _split_blocks(text: str) -> tuple[str, list[str]]:
    """Split services.toml into head text and raw [[services]] blocks."""
    parts = re.split(r"(?m)^\[\[services\]\]\s*$", text)
    return parts[0], parts[1:]


def _validated_write(config: AppConfig, text: str, pool_names: set[str] | None) -> None:
    """Write services.toml only if the new content validates."""
    if pool_names is None:
        raise ValueError("pool worker unreachable, cannot validate pool subscriptions")
    custom_names = _custom_names(config.custom_path)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".toml", delete=False
    ) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    try:
        generate._load_services(
            temporary, local_provider_names=pool_names, custom_proxy_names=custom_names
        )
    finally:
        temporary.unlink(missing_ok=True)
    config.services_path.write_text(text, encoding="utf-8")


def _account_payload(service: generate.Service) -> dict[str, object]:
    return {
        "name": service.name,
        "port": service.port,
        "primary": service.primary,
        "fallback": list(service.fallback),
        "subscriptions": list(service.subscriptions),
        "username": service.username,
        "has_password": service.password is not None,
        "lock_proxy": service.lock_proxy,
        "balance": service.balance,
    }


def _error(status: int, message: str) -> tuple[int, dict[str, object]]:
    return status, {"error": message}


def _load_for_read(
    config: AppConfig,
) -> tuple[list[generate.Service], dict[str, int] | None]:
    """Load services for read paths, naming the real cause on failure."""
    pools = _worker_pools(config.worker_url)
    try:
        _, services, _, _, _ = generate._load_services(
            config.services_path,
            local_provider_names=set(pools or ()),
            custom_proxy_names=_custom_names(config.custom_path),
        )
    except ValueError as exc:
        if pools is None and "unknown provider" in str(exc):
            raise ValueError(
                "pool worker unreachable, cannot verify pool subscriptions"
            ) from exc
        raise
    return services, pools


def _status_code(error: ValueError) -> int:
    text = str(error)
    if "duplicate service name" in text or "duplicate dispatcher username" in text:
        return 409
    return 400


def handle_status(
    state: AppState, body: dict[str, object]
) -> tuple[int, dict[str, object]]:
    config = state.config
    try:
        services, pools = _load_for_read(config)
    except ValueError as exc:
        message = str(exc)
        status = 502 if message.startswith("pool worker unreachable") else 500
        return _error(status, message)
    leases: dict[str, int] = {}
    if config.hash_db_path.is_file():
        try:
            db = sqlite3.connect(str(config.hash_db_path), timeout=DB_TIMEOUT_S)
            try:
                rows = db.execute(
                    "SELECT pool, COUNT(*) FROM leases GROUP BY pool"
                ).fetchall()
                leases = {str(pool): int(count) for pool, count in rows}
            finally:
                db.close()
        except sqlite3.Error as exc:
            LOG.warning("cannot read leases: %s", exc)
    generated = config.generated_path
    stale = True
    if generated.is_file():
        try:
            stale = generated.stat().st_mtime < config.services_path.stat().st_mtime
        except OSError:
            stale = True
    return 200, {
        "services_total": len(services),
        "services_with_credentials": sum(1 for s in services if s.username),
        "pools": pools,
        "pools_unreachable": pools is None,
        "leases": leases,
        "config_stale": stale,
    }


def handle_list_accounts(
    state: AppState, body: dict[str, object]
) -> tuple[int, dict[str, object]]:
    config = state.config
    try:
        services, _ = _load_for_read(config)
    except ValueError as exc:
        message = str(exc)
        status = 502 if message.startswith("pool worker unreachable") else 500
        return _error(status, message)
    return 200, {"accounts": [_account_payload(service) for service in services]}


def _render_block(body: dict[str, object]) -> tuple[str, str | None, str]:
    """Validate one [[services]] payload. Returns (name, password, block)."""
    name = body.get("name")
    if type(name) is not str or not name.strip():
        raise ValueError("field 'name' is required")
    name = name.strip()
    lines = [f'name = "{name}"']
    port = body.get("port")
    if port is not None:
        if type(port) is not int:
            raise ValueError("field 'port' must be an integer")
        lines.append(f"port = {port}")
    for key in ("primary", "balance"):
        value = body.get(key)
        if value is not None:
            if type(value) is not str:
                raise ValueError(f"field '{key}' must be a string")
            lines.append(f'{key} = "{value}"')
    for key in ("fallback", "subscriptions"):
        raw_items = body.get(key, [])
        if type(raw_items) is not list or any(
            type(item) is not str for item in cast(list[object], raw_items)
        ):
            raise ValueError(f"field '{key}' must be an array of strings")
        items = cast(list[str], raw_items)
        if items:
            quoted = ", ".join(f'"{item}"' for item in items)
            lines.append(f"{key} = [{quoted}]")
    lock_proxy = body.get("lock_proxy", False)
    if type(lock_proxy) is not bool:
        raise ValueError("field 'lock_proxy' must be a boolean")
    if lock_proxy:
        lines.append("lock_proxy = true")
    username = body.get("username")
    password: str | None = None
    if username is not None:
        if type(username) is not str:
            raise ValueError("field 'username' must be a string")
        raw_password = body.get("password")
        if raw_password is None:
            password = random_password()
        elif type(raw_password) is not str:
            raise ValueError("field 'password' must be a string")
        else:
            password = raw_password
        lines.append(f'username = "{username}"')
        lines.append(f'password = "{password}"')
    elif body.get("password") is not None:
        raise ValueError("field 'password' requires 'username'")
    return name, password, "\n".join(lines) + "\n"


def handle_create_account(
    state: AppState, body: dict[str, object]
) -> tuple[int, dict[str, object]]:
    config = state.config
    try:
        name, password, block = _render_block(body)
    except ValueError as exc:
        return _error(400, str(exc))
    with state.lock:
        text = config.services_path.read_text(encoding="utf-8")
        if name in _service_names(text):
            return _error(409, f"duplicate service name: {name}")
        pools = _worker_pools(config.worker_url)
        candidate = text.rstrip("\n") + "\n\n[[services]]\n" + block
        try:
            _validated_write(
                config, candidate, set(pools) if pools is not None else None
            )
        except ValueError as exc:
            return _error(_status_code(exc), str(exc))
    created: dict[str, object] = {"name": name, "reload_required": True}
    username = body.get("username")
    if username is not None:
        created["username"] = username
        created["password"] = password
    return 201, created


def handle_delete_account(state: AppState, name: str) -> tuple[int, dict[str, object]]:
    config = state.config
    with state.lock:
        text = config.services_path.read_text(encoding="utf-8")
        head, blocks = _split_blocks(text)
        kept: list[str] = []
        found = False
        for block in blocks:
            match = re.search(r'(?m)^name = "([^"]+)"\s*$', block)
            if match is not None and match.group(1) == name:
                found = True
                continue
            kept.append(block)
        if not found:
            return _error(404, f"unknown account: {name}")
        candidate = head + "".join("[[services]]\n" + block for block in kept)
        pools = _worker_pools(config.worker_url)
        try:
            _validated_write(
                config, candidate, set(pools) if pools is not None else None
            )
        except ValueError as exc:
            return _error(_status_code(exc), str(exc))
    return 200, {"deleted": name, "reload_required": True}


def handle_list_leases(
    state: AppState, query: dict[str, str]
) -> tuple[int, dict[str, object]]:
    config = state.config
    if not config.hash_db_path.is_file():
        return 200, {"leases": []}
    try:
        db = sqlite3.connect(str(config.hash_db_path), timeout=DB_TIMEOUT_S)
        try:
            rows = db.execute(
                "SELECT pool, h, first_seen, ttl FROM leases ORDER BY pool, h"
            ).fetchall()
        finally:
            db.close()
    except sqlite3.Error as exc:
        return _error(500, f"cannot read leases: {exc}")
    now = time.time()
    wanted = query.get("pool")
    leases = []
    for pool, user, first_seen, ttl in rows:
        pool, user = str(pool), str(user)
        if wanted is not None and pool != wanted:
            continue
        expires_in = int(first_seen) + int(ttl) - int(now)
        leases.append(
            {
                "pool": pool,
                "user": user,
                "first_seen": int(first_seen),
                "ttl": int(ttl),
                "expired": expires_in < 0,
                "expires_in_s": expires_in,
            }
        )
    return 200, {"leases": leases}


def handle_delete_lease(
    state: AppState, pool: str, user: str
) -> tuple[int, dict[str, object]]:
    config = state.config
    if not config.hash_db_path.is_file():
        return _error(404, f"unknown lease: {pool}/{user}")
    try:
        db = sqlite3.connect(str(config.hash_db_path), timeout=DB_TIMEOUT_S)
        try:
            cursor = db.execute(
                "DELETE FROM leases WHERE pool = ? AND h = ?", (pool, user)
            )
            db.commit()
            removed = cursor.rowcount
        finally:
            db.close()
    except sqlite3.Error as exc:
        return _error(500, f"cannot delete lease: {exc}")
    if not removed:
        return _error(404, f"unknown lease: {pool}/{user}")
    return 200, {"revoked": True, "pool": pool, "user": user}


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tppr-api"

    def _respond(self, status: int, payload: dict[str, object]) -> None:
        body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        state = self.server.state  # type: ignore[attr-defined]
        presented = self.headers.get("Authorization", "")
        if not presented.startswith("Bearer "):
            return False
        return hmac.compare_digest(presented[7:].strip(), state.config.token)

    def _read_json(self) -> tuple[dict[str, object] | None, str | None]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "invalid Content-Length"
        if size > MAX_BODY:
            return None, "request body too large"
        raw = self.rfile.read(size) if size else b""
        if not raw:
            return {}, None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, "request body must be JSON"
        if type(payload) is not dict:
            return None, "request body must be a JSON object"
        return payload, None

    def _route(self) -> None:
        state = self.server.state  # type: ignore[attr-defined]
        if not self._authorized():
            self._respond(401, {"error": "missing or invalid bearer token"})
            return
        parts = urlsplit(self.path)
        segments = [segment for segment in parts.path.split("/") if segment]
        query: dict[str, str] = {}
        for chunk in parts.query.split("&"):
            if "=" in chunk:
                key, _, value = chunk.partition("=")
                query[key] = value
        method = self.command
        try:
            if method == "GET" and not segments:
                self._respond(
                    200,
                    {
                        "ok": True,
                        "endpoints": [
                            "GET /status",
                            "GET /accounts",
                            "POST /accounts",
                            "DELETE /accounts/{name}",
                            "GET /leases[?pool=]",
                            "DELETE /leases/{pool}/{user}",
                        ],
                    },
                )
            elif method == "GET" and segments == ["status"]:
                self._respond(*handle_status(state, {}))
            elif method == "GET" and segments == ["accounts"]:
                self._respond(*handle_list_accounts(state, {}))
            elif method == "POST" and segments == ["accounts"]:
                payload, error = self._read_json()
                if error is not None:
                    self._respond(400, {"error": error})
                else:
                    self._respond(*handle_create_account(state, payload or {}))
            elif (
                method == "DELETE" and len(segments) == 2 and segments[0] == "accounts"
            ):
                self._respond(*handle_delete_account(state, segments[1]))
            elif method == "GET" and segments == ["leases"]:
                self._respond(*handle_list_leases(state, query))
            elif method == "DELETE" and len(segments) == 3 and segments[0] == "leases":
                self._respond(*handle_delete_lease(state, segments[1], segments[2]))
            else:
                self._respond(404, {"error": "unknown endpoint"})
        except OSError as exc:
            LOG.error("request failed: %s", exc)
            self._respond(500, {"error": "internal error"})

    def do_GET(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def do_DELETE(self) -> None:
        self._route()

    def log_message(self, format: str, *args: object) -> None:
        LOG.debug("http %s", format % args)


def run(config: AppConfig) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s level=%(levelname)s service=%(name)s %(message)s",
    )
    log_file = os.environ.get("LOG_FILE", "").strip()
    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10_485_760, backupCount=5
        )
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s level=%(levelname)s service=%(name)s %(message)s"
            )
        )
        logging.getLogger().addHandler(file_handler)
    state = AppState(lock=threading.Lock(), config=config)
    server = ThreadingHTTPServer((config.listen, config.port), ApiHandler)
    server.daemon_threads = True
    server.state = state  # type: ignore[attr-defined]
    LOG.info("management api listening on %s:%d", config.listen, config.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Management REST API")
    parser.parse_args()
    try:
        return run(_load_config())
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
