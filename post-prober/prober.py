"""Steer one Mihomo select group with a real POST probe.

Every interval the prober sends the configured POST request through the
group's current node (via the dispatcher) and expects one exact status
code. A node that answers anything else is abandoned immediately: the
prober switches the group to the next candidate and probes again without
waiting. When no candidate answers as expected, the group stays where it
is and the next round retries from the top.

Config comes from two files (both mounted read-only): the probe file
carries the target group and the request shape, services.toml carries the
dispatcher port and password for the probed account. Wiring (controller
address, proxy host, file paths) comes from the environment. Anything
misconfigured fails loudly at startup; per-candidate transport errors are
verdicts (try the next node), not crashes.

Each completed round touches a heartbeat file so the compose healthcheck can
tell "running" from "stuck".
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SERVICE_NAME = "tppr-post-prober"
SKIP_NODES = frozenset({"DIRECT", "REJECT", "PASS", "COMPATIBLE"})
# Mihomo reports a `type: select` group as "Selector"; services.toml spells the
# same thing lowercase. Both spellings are accepted, case-insensitively.
SELECT_GROUP_TYPES = frozenset({"select", "selector"})
MIN_INTERVAL_S = 5
MAX_INTERVAL_S = 3600
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 120
MAX_BODY_BYTES = 65536
STARTUP_ATTEMPTS = 30
STARTUP_WAIT_S = 5
MAX_CONTROLLER_FAILURES = 5
HEARTBEAT_FILE = "/tmp/post-prober-heartbeat"

log = logging.getLogger(SERVICE_NAME)


@dataclass(frozen=True)
class ProbeConfig:
    api: str
    group: str
    provider: str
    proxy_url: str
    probe_url: str
    method: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    expected_status: int
    timeout_s: int
    interval_s: int
    heartbeat_max_age_s: int


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} environment variable is required")
    return value


def _required_int_env(name: str, minimum: int, maximum: int) -> int:
    raw = _required_env(name)
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got: {raw}") from None
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _non_empty(mapping: dict[str, object], key: str, section: str) -> str:
    value = mapping.get(key)
    if type(value) is not str or not value.strip():
        raise ValueError(f"{section}.{key} must be a non-empty string")
    return value.strip()


def _optional_int(
    mapping: dict[str, object], key: str, default: int, minimum: int, maximum: int
) -> int:
    if key not in mapping:
        return default
    value = mapping[key]
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
    return value


def _read_toml(path: Path, label: str) -> dict[str, object]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {label} {path}: {exc}") from exc
    try:
        return tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {label} {path}: {exc}") from exc


def _table(
    value: object, label: str, allowed: frozenset[str] | None = None
) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be a table")
    table: dict[str, object] = value  # type: ignore[assignment]
    if allowed is not None:
        unknown = sorted(set(table) - allowed)
        if unknown:
            raise ValueError(f"{label} has unknown keys: {', '.join(unknown)}")
    return table


@dataclass(frozen=True)
class TargetService:
    name: str
    password: str
    subscriptions: tuple[str, ...]
    dispatcher_port: int


def _target_service(
    document: dict[str, object], username: str, path: Path
) -> TargetService:
    """Resolve the probed account: its password, its providers, and the
    dispatcher port. All three come from services.toml, so moving the
    dispatcher or renaming a service can never silently desync the prober."""
    dispatcher = _table(document.get("dispatcher"), "dispatcher")
    raw_port = dispatcher.get("port")
    if type(raw_port) is not int:
        raise ValueError(f"dispatcher.port must be an integer in {path}")
    raw_services = document.get("services", [])
    if type(raw_services) is not list:
        raise ValueError(f"services must be an array in {path}")
    for raw_service in raw_services:
        service = _table(raw_service, "services entry")
        if service.get("username") != username:
            continue
        password = service.get("password")
        if type(password) is not str or not password:
            raise ValueError(f"service {username} has no password in {path}")
        raw_subscriptions = service.get("subscriptions", [])
        if type(raw_subscriptions) is not list:
            raise ValueError(f"service {username}.subscriptions must be an array")
        subscriptions = tuple(item for item in raw_subscriptions if type(item) is str)
        return TargetService(
            name=_non_empty(service, "name", "service"),
            password=password,
            subscriptions=subscriptions,
            dispatcher_port=raw_port,
        )
    raise ValueError(f"username {username} has no service in {path}")


def load_config() -> ProbeConfig:
    api = _required_env("MIHOMO_API").rstrip("/")
    proxy_host = _required_env("PROXY_HOST")
    services_path = Path(_required_env("SERVICES_FILE"))
    prober_path = Path(_required_env("PROBER_FILE"))
    prober_doc = _read_toml(prober_path, "prober config")
    target = _table(
        prober_doc.get("target"), "target", frozenset({"group", "provider", "username"})
    )
    group = _non_empty(target, "group", "target")
    provider = _non_empty(target, "provider", "target")
    username = _non_empty(target, "username", "target")

    services_doc = _read_toml(services_path, "services file")
    service = _target_service(services_doc, username, services_path)
    if group != f"SVC_{service.name}":
        raise ValueError(
            f"target.group {group} does not belong to account {username}: "
            f"expected SVC_{service.name}"
        )
    if provider not in service.subscriptions:
        raise ValueError(
            f"target.provider {provider} is not in the subscriptions of "
            f"{username} ({', '.join(service.subscriptions) or 'none'})"
        )

    probe = _table(
        prober_doc.get("probe"),
        "probe",
        frozenset(
            {
                "url",
                "method",
                "headers",
                "body_file",
                "expected_status",
                "timeout_s",
                "interval_s",
            }
        ),
    )
    probe_url = _non_empty(probe, "url", "probe")
    parsed_url = urllib.parse.urlparse(probe_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ValueError("probe.url must be an HTTP(S) URL")
    method = _non_empty(probe, "method", "probe")
    if not method.isupper() or not method.isalpha():
        raise ValueError("probe.method must be an uppercase token like POST")
    expected = probe.get("expected_status")
    if type(expected) is not int or not 100 <= expected <= 599:
        raise ValueError("probe.expected_status must be a status code")
    timeout = _optional_int(probe, "timeout_s", 15, MIN_TIMEOUT_S, MAX_TIMEOUT_S)
    interval = _optional_int(probe, "interval_s", 15, MIN_INTERVAL_S, MAX_INTERVAL_S)
    raw_headers = probe.get("headers", {})
    if type(raw_headers) is not dict:
        raise ValueError("probe.headers must be a table")
    headers = tuple((str(name), str(value)) for name, value in raw_headers.items())
    raw_body_file = probe.get("body_file")
    if type(raw_body_file) is not str or not raw_body_file.strip():
        raise ValueError("probe.body_file must be a non-empty string")
    body_file = raw_body_file.strip()
    try:
        body = Path(body_file).read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read probe body {body_file}: {exc}") from exc
    if not body:
        raise ValueError(f"probe body {body_file} is empty")

    quoted = urllib.parse.quote(service.password, safe="")
    return ProbeConfig(
        api=api,
        group=group,
        provider=provider,
        proxy_url=f"http://{username}:{quoted}@{proxy_host}:{service.dispatcher_port}",
        probe_url=probe_url,
        method=method,
        headers=headers,
        body=body,
        expected_status=expected,
        timeout_s=timeout,
        interval_s=interval,
        heartbeat_max_age_s=_required_int_env(
            "HEARTBEAT_MAX_AGE_S", interval + 1, 86400
        ),
    )


def pick_order(
    candidates: list[str], now: str | None, cursor: int
) -> tuple[list[str], int]:
    """Probe `now` first (no flap when it is good), then round-robin from
    the cursor. Returns the probe order and the normalized cursor."""
    if not candidates:
        return [], 0
    cursor = cursor % len(candidates)
    order = [now] if now in candidates else []
    for offset in range(len(candidates)):
        name = candidates[(cursor + offset) % len(candidates)]
        if name != now:
            order.append(name)
    return order, cursor


class Controller:
    def __init__(self, api: str) -> None:
        self.api = api

    def _get(self, path: str) -> dict[str, Any]:
        # The controller has no published schema, so payloads are typed as
        # Any here and narrowed once at each call site below.
        with urllib.request.urlopen(self.api + path, timeout=30) as response:
            payload = json.load(response)
        if type(payload) is not dict:
            raise ValueError(f"unexpected payload at {path}")
        return payload

    def group(self, name: str) -> dict[str, Any]:
        return self._get("/proxies/" + urllib.parse.quote(name, safe=""))

    def candidates(self, provider: str, group_all: list[str]) -> list[str]:
        """Group members that belong to `provider`, minus the built-ins.
        Queries the single provider, not the full provider map: the map is
        megabytes of every pool node, this is one subscription."""
        payload = self._get(
            "/providers/proxies/" + urllib.parse.quote(provider, safe="")
        )
        raw_nodes = payload.get("proxies", [])
        if type(raw_nodes) is not list:
            raise ValueError(f"unexpected provider payload for {provider}")
        wanted = {
            node.get("name")
            for node in raw_nodes
            if type(node) is dict and type(node.get("name")) is str
        }
        return [name for name in group_all if name in wanted and name not in SKIP_NODES]

    def switch(self, group: str, node: str) -> None:
        payload = json.dumps({"name": node}).encode()
        request = urllib.request.Request(
            self.api + "/proxies/" + urllib.parse.quote(group, safe=""),
            data=payload,
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30):
            pass


def _group_members(group: dict[str, Any]) -> list[str]:
    raw_all = group.get("all", [])
    if type(raw_all) is not list:
        raise ValueError("group payload has no member list")
    return [name for name in raw_all if type(name) is str]


def _check_select_group(controller: Controller, config: ProbeConfig) -> list[str]:
    group = controller.group(config.group)
    kind = group.get("type")
    if not isinstance(kind, str) or kind.lower() not in SELECT_GROUP_TYPES:
        raise ValueError(
            f"group {config.group} is {kind}, the prober steers only select groups"
        )
    return _group_members(group)


def probe_once(
    config: ProbeConfig, opener: urllib.request.OpenerDirector
) -> tuple[int | None, str]:
    request = urllib.request.Request(
        config.probe_url,
        data=config.body,
        method=config.method,
        headers=dict(config.headers),
    )
    try:
        with opener.open(request, timeout=config.timeout_s) as response:
            response.read(MAX_BODY_BYTES)
            return response.status, "ok"
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(MAX_BODY_BYTES)[:200].decode("utf-8", errors="replace")
        except OSError:
            detail = ""
        return exc.code, detail or "http error"
    except OSError as exc:
        return None, f"transport: {exc}"


def run_round(
    config: ProbeConfig,
    controller: Controller,
    opener: urllib.request.OpenerDirector,
    cursor: int,
) -> int:
    group_all = _check_select_group(controller, config)
    candidates = controller.candidates(config.provider, group_all)
    if not candidates:
        log.warning("no candidates in provider %s yet, retrying", config.provider)
        return cursor
    now = controller.group(config.group).get("now")
    order, cursor = pick_order(candidates, now if type(now) is str else None, cursor)
    for name in order:
        if name != now:
            try:
                controller.switch(config.group, name)
                now = name
            except OSError as exc:
                log.warning("switch to %s failed: %s", name, exc)
                continue
        code, detail = probe_once(config, opener)
        if code == config.expected_status:
            log.info("picked %s, probe answered %s", name, code)
            return candidates.index(name)
        log.warning("node %s answered %s (%s), rerolling", name, code, detail)
    log.warning("no candidate answered %s, staying put", config.expected_status)
    return cursor


def _touch_heartbeat() -> None:
    try:
        Path(HEARTBEAT_FILE).touch()
    except OSError as exc:
        log.warning("cannot update heartbeat: %s", exc)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    log_file = os.environ.get("LOG_FILE", "").strip()
    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10_485_760, backupCount=5
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logging.getLogger().addHandler(file_handler)
    config = load_config()
    controller = Controller(config.api)
    for _ in range(STARTUP_ATTEMPTS):
        try:
            group_all = _check_select_group(controller, config)
            if controller.candidates(config.provider, group_all):
                break
        except OSError as exc:
            log.info("controller not ready yet: %s", exc)
        time.sleep(STARTUP_WAIT_S)
    else:
        raise ValueError(f"provider {config.provider} never served candidates")
    log.info(
        "steering %s over %s, expecting %s from %s every %ss",
        config.group,
        config.provider,
        config.expected_status,
        config.method,
        config.interval_s,
    )
    proxy_handler = urllib.request.ProxyHandler(
        {"http": config.proxy_url, "https": config.proxy_url}
    )
    opener = urllib.request.build_opener(proxy_handler)
    failures = 0
    cursor = 0
    while True:
        try:
            cursor = run_round(config, controller, opener, cursor)
            failures = 0
        except (OSError, ValueError) as exc:
            failures += 1
            log.warning(
                "controller round failed (%s/%s): %s",
                failures,
                MAX_CONTROLLER_FAILURES,
                exc,
            )
            if failures >= MAX_CONTROLLER_FAILURES:
                raise
        _touch_heartbeat()
        time.sleep(config.interval_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
