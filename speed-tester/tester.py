"""Pin a mihomo proxy group to the fastest node by measured throughput.

Every setting comes from the environment and is validated once at startup
(missing/invalid values exit loudly instead of running with silent defaults).
Each sweep takes the TEST_MAX_NODES lowest-latency members (from provider
healthcheck history), downloads TEST_URL through each of them via the local
mixed proxy, measures real bytes/sec, and selects the fastest healthy node
through the Clash API. If no member is measurable the selection is left untouched.
Sweep results persist in SQLite history: nodes with consecutive recent failures
are skipped (cooldown), and on boot the group is restored to the recent best.
"""

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from history import History

# Top-level imports only; stdlib modules have negligible import cost.

SERVICE_NAME = "proxy-pool-speed-tester"
SKIP_NODES = frozenset({"DIRECT", "REJECT", "PASS", "COMPATIBLE"})
CHUNK_BYTES = 65536


class TesterConfig:
    """Validated tester settings. Single source: environment variables."""

    def __init__(self) -> None:
        self.api = _required_str("MIHOMO_API")
        self.proxy = _required_str("PROXY_URL")
        self.group = _required_str("TEST_GROUP")
        self.url = _required_str("TEST_URL")
        self.interval_s = _required_int("TEST_INTERVAL_S", minimum=30)
        self.timeout_s = _required_int("TEST_TIMEOUT_S", minimum=5)
        self.max_bytes = _required_int("TEST_MAX_BYTES", minimum=262144)
        self.max_nodes = _required_int("TEST_MAX_NODES", minimum=3)
        self.history_db = _required_str("HISTORY_DB")
        self.cooldown_fails = _required_int("HISTORY_COOLDOWN_FAILS", minimum=1)
        self.cooldown_h = _required_int("HISTORY_COOLDOWN_H", minimum=1)
        self.history_window_h = _required_int("HISTORY_WINDOW_H", minimum=1)


def _required_str(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"missing required environment variable: {name}")
    return value


def _required_int(name: str, minimum: int) -> int:
    raw = _required_str(name)
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"environment variable {name} must be an integer, got: {raw!r}")
    if value < minimum:
        raise ValueError(f"environment variable {name} must be >= {minimum}, got: {value}")
    return value


def _logger() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        f"%(asctime)s level=%(levelname)s service={SERVICE_NAME} %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logger = logging.getLogger(SERVICE_NAME)
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    return logger


LOG = _logger()


def api_request(config: TesterConfig, method: str, path: str, payload: dict | None = None) -> dict:
    """Call the Clash API directly (never through the proxy)."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{config.api}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=config.timeout_s) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Clash API {method} {path} failed: {exc}") from exc
    return json.loads(body) if body else {}


def group_state(config: TesterConfig) -> tuple[list[str], str]:
    state = api_request(config, "GET", "/proxies")
    try:
        group = state["proxies"][config.group]
        members = [str(n) for n in group["all"]]
        current = str(group["now"])
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"proxy group {config.group!r} not found in Clash API response: {exc}") from exc
    return members, current


def provider_delays(config: TesterConfig) -> dict[str, int | None]:
    """Last known latency per node from provider healthchecks (None = unknown)."""
    try:
        state = api_request(config, "GET", "/providers/proxies")
        providers = state["providers"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"provider list unavailable: {exc}") from exc
    delays: dict[str, int | None] = {}
    try:
        for provider in providers.values():
            for node in provider["proxies"]:
                history = node.get("history", []) or []
                last = history[-1].get("delay") if history else None
                delays[str(node.get("name", ""))] = last if last else None
    except (TypeError, AttributeError, KeyError) as exc:
        raise RuntimeError(f"unexpected provider payload shape: {exc}") from exc
    return delays


def select_node(config: TesterConfig, name: str) -> None:
    api_request(config, "PUT", f"/proxies/{urllib.parse.quote(config.group)}", {"name": name})


def pin_and_measure(config: TesterConfig, node: str) -> float:
    """Pin the group to node, then download TEST_URL. Returns KB/s, or -1 on failure."""
    select_node(config, node)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": config.proxy, "https": config.proxy})
    )
    request = urllib.request.Request(config.url, headers={"User-Agent": SERVICE_NAME})
    received = 0
    started = time.monotonic()
    deadline = started + config.timeout_s
    try:
        with opener.open(request, timeout=config.timeout_s) as response:
            while received < config.max_bytes and time.monotonic() < deadline:
                chunk = response.read(min(CHUNK_BYTES, config.max_bytes - received))
                if not chunk:
                    break
                received += len(chunk)
    except (urllib.error.URLError, OSError) as exc:
        LOG.warning(f"event=probe_failed node={node} error={exc}")
        return -1.0
    elapsed = max(time.monotonic() - started, 0.001)
    return received / 1024.0 / elapsed


def sweep(config: TesterConfig, history: History) -> None:
    run_id = uuid.uuid4().hex[:8]
    members, current = group_state(config)
    candidates = [n for n in members if n not in SKIP_NODES]
    if not candidates:
        LOG.error(f"run={run_id} event=sweep_skipped reason=no_candidates group={config.group}")
        return
    delays = provider_delays(config)
    ranked = sorted(candidates, key=lambda n: (delays.get(n) is None, delays.get(n) or 0))
    shortlist: list[str] = []
    cooled = 0
    for node in ranked:
        if (
            history.consecutive_failures(node, config.cooldown_fails, config.cooldown_h)
            >= config.cooldown_fails
        ):
            cooled += 1
            continue
        shortlist.append(node)
        if len(shortlist) >= config.max_nodes:
            break
    if not shortlist:
        LOG.error(
            f"run={run_id} event=sweep_skipped reason=all_in_cooldown "
            f"pool={len(candidates)} selection_kept={current}"
        )
        return
    LOG.info(
        f"run={run_id} event=sweep_started group={config.group} "
        f"pool={len(candidates)} tested={len(shortlist)} skipped_cooldown={cooled} current={current}"
    )
    speeds: dict[str, float] = {}
    try:
        for node in shortlist:
            speeds[node] = pin_and_measure(config, node)
        history.record_sweep(run_id, speeds)
        history.prune(max(config.history_window_h, config.cooldown_h))
    except RuntimeError as exc:
        LOG.error(f"run={run_id} event=sweep_aborted error={exc} selection_restored={current}")
        try:
            select_node(config, current)
        except RuntimeError as restore_exc:
            LOG.critical(f"run={run_id} event=restore_failed error={restore_exc}")
        return
    healthy = {node: speed for node, speed in speeds.items() if speed > 0}
    if not healthy:
        LOG.error(f"run={run_id} event=sweep_failed reason=all_nodes_unmeasurable selection_kept={current}")
        try:
            select_node(config, current)
        except RuntimeError as exc:
            LOG.critical(f"run={run_id} event=restore_failed error={exc}")
        return
    fastest = max(healthy, key=lambda node: healthy[node])
    summary = ",".join(f"{node}={speed:.0f}KB/s" for node, speed in sorted(healthy.items()))
    if fastest == current:
        LOG.info(f"run={run_id} event=kept node={current} speeds=[{summary}]")
        return
    try:
        select_node(config, fastest)
    except RuntimeError as exc:
        LOG.critical(f"run={run_id} event=rotate_failed to={fastest} error={exc}")
    else:
        LOG.info(f"run={run_id} event=rotated from={current} to={fastest} speeds=[{summary}]")


def restore_best(config: TesterConfig, history: History) -> None:
    """Pin the group to the recent fastest node, if history knows one."""
    try:
        members, current = group_state(config)
        pool = [n for n in members if n not in SKIP_NODES]
        eligible = [
            n
            for n in pool
            if history.consecutive_failures(n, config.cooldown_fails, config.cooldown_h)
            < config.cooldown_fails
        ]
        if len(eligible) < len(pool):
            LOG.info(f"event=restore_cooled skipped={len(pool) - len(eligible)}")
        best = history.best_node(eligible, config.history_window_h)
    except RuntimeError as exc:
        LOG.error(f"event=restore_skipped error={exc}")
        return
    if best is None:
        LOG.info("event=no_history starting_cold=true")
    elif best == current:
        LOG.info(f"event=restored node={best} already_selected=true")
    else:
        try:
            select_node(config, best)
        except RuntimeError as exc:
            LOG.error(f"event=restore_failed error={exc}")
        else:
            LOG.info(f"event=restored node={best} previous={current}")


def main() -> int:
    try:
        config = TesterConfig()
    except ValueError as exc:
        LOG.critical(f"event=bad_config error={exc}")
        return 1
    try:
        history = History(config.history_db)
    except RuntimeError as exc:
        LOG.critical(f"event=bad_history error={exc}")
        return 1
    LOG.info(
        f"event=started group={config.group} interval_s={config.interval_s} "
        f"timeout_s={config.timeout_s} max_bytes={config.max_bytes} max_nodes={config.max_nodes} "
        f"history_db={config.history_db} cooldown_fails={config.cooldown_fails} "
        f"cooldown_h={config.cooldown_h} history_window_h={config.history_window_h}"
    )
    restore_best(config, history)
    while True:
        try:
            sweep(config, history)
        except RuntimeError as exc:
            LOG.error(f"event=sweep_error error={exc}")
        time.sleep(config.interval_s)


if __name__ == "__main__":
    sys.exit(main())
