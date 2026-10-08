"""Select and benchmark mihomo proxy nodes in bounded parallel batches.

The production proxy group is never used as a test scratchpad. The tester
selects nodes only in an isolated group exposed through a dedicated internal
listener, then updates the production group after a successful measurement.
Latency probes run concurrently in batches and stop once enough good
candidates are found; only those candidates receive throughput tests.
"""

import concurrent.futures
import http.client
import json
import logging
import logging.handlers
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

from history import History

SERVICE_NAME = "tppr-speed-tester"
SKIP_NODES = frozenset({"DIRECT", "REJECT", "PASS", "COMPATIBLE"})
CHUNK_BYTES = 65536
MAX_PROBE_BATCH_SIZE = 100
MAX_PROBE_BATCHES = 3
MAX_PROBE_WORKERS = 100
MAX_FAILOVER_GOOD_CANDIDATES = 1
MAX_SWEEP_GOOD_CANDIDATES = 5
MAX_SPEED_NODES = 5


@dataclass(frozen=True)
class ProbeResult:
    node: str
    delay_ms: int | None
    error: str | None


class TesterConfig:
    """Validated tester settings. Single source: environment variables."""

    def __init__(self) -> None:
        self.api = _required_str("MIHOMO_API")
        self.proxy = _required_str("PROXY_URL")
        self.group = _required_str("TEST_GROUP")
        self.production_group = _required_str("PRODUCTION_GROUP")
        self.provider_names = tuple(
            name.strip()
            for name in _required_str("PROVIDER_NAMES").split(",")
            if name.strip()
        )
        if not self.provider_names:
            raise ValueError("PROVIDER_NAMES must contain at least one provider")
        self.url = _required_str("TEST_URL")
        self.health_url = _required_str("HEALTH_URL")
        self.health_interval_s = _required_int("HEALTH_INTERVAL_S", minimum=5)
        self.health_timeout_ms = _required_int("HEALTH_TIMEOUT_MS", minimum=500)
        self.max_delay_ms = _required_int("MAX_DELAY_MS", minimum=1)
        self.probe_batch_size = _required_int("PROBE_BATCH_SIZE", minimum=1)
        if self.probe_batch_size > MAX_PROBE_BATCH_SIZE:
            raise ValueError(
                f"PROBE_BATCH_SIZE must be <= {MAX_PROBE_BATCH_SIZE}, "
                f"got: {self.probe_batch_size}"
            )
        self.probe_workers = _required_int("PROBE_WORKERS", minimum=1)
        if self.probe_workers > MAX_PROBE_WORKERS:
            raise ValueError(
                f"PROBE_WORKERS must be <= {MAX_PROBE_WORKERS}, "
                f"got: {self.probe_workers}"
            )
        self.max_probe_batches = _required_int("MAX_PROBE_BATCHES", minimum=1)
        if self.max_probe_batches > MAX_PROBE_BATCHES:
            raise ValueError(
                f"MAX_PROBE_BATCHES must be <= {MAX_PROBE_BATCHES}, "
                f"got: {self.max_probe_batches}"
            )
        self.failover_good_candidates = _required_int(
            "FAILOVER_GOOD_CANDIDATES", minimum=1
        )
        if self.failover_good_candidates > MAX_FAILOVER_GOOD_CANDIDATES:
            raise ValueError(
                f"FAILOVER_GOOD_CANDIDATES must be <= "
                f"{MAX_FAILOVER_GOOD_CANDIDATES}, got: "
                f"{self.failover_good_candidates}"
            )
        self.sweep_good_candidates = _required_int("SWEEP_GOOD_CANDIDATES", minimum=1)
        if self.sweep_good_candidates > MAX_SWEEP_GOOD_CANDIDATES:
            raise ValueError(
                f"SWEEP_GOOD_CANDIDATES must be <= {MAX_SWEEP_GOOD_CANDIDATES}, "
                f"got: {self.sweep_good_candidates}"
            )
        self.test_interval_s = _required_int("TEST_INTERVAL_S", minimum=30)
        self.test_timeout_s = _required_int("TEST_TIMEOUT_S", minimum=5)
        self.test_max_bytes = _required_int("TEST_MAX_BYTES", minimum=262144)
        self.test_max_nodes = _required_int("TEST_MAX_NODES", minimum=1)
        if self.test_max_nodes > MAX_SPEED_NODES:
            raise ValueError(
                f"TEST_MAX_NODES must be <= {MAX_SPEED_NODES}, "
                f"got: {self.test_max_nodes}"
            )
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
        raise ValueError(
            f"environment variable {name} must be an integer, got: {raw!r}"
        )
    if value < minimum:
        raise ValueError(
            f"environment variable {name} must be >= {minimum}, got: {value}"
        )
    return value


def _logger() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            f"%(asctime)s level=%(levelname)s service={SERVICE_NAME} %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logger = logging.getLogger(SERVICE_NAME)
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    log_file = os.environ.get("LOG_FILE", "").strip()
    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10_485_760, backupCount=5
        )
        file_handler.setFormatter(handler.formatter)
        logger.addHandler(file_handler)
    return logger


LOG = _logger()


def _api_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api_request(
    config: TesterConfig,
    method: str,
    path: str,
    payload: dict | None = None,
    timeout_s: float | None = None,
) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{config.api}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with _api_opener().open(
            request,
            timeout=(
                timeout_s if timeout_s is not None else config.health_timeout_ms / 1000
            ),
        ) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Clash API {method} {path} returned {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise RuntimeError(f"Clash API {method} {path} failed: {exc}") from exc
    try:
        return json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Clash API {method} {path} returned invalid JSON: {exc}"
        ) from exc


def group_state(config: TesterConfig, group: str) -> tuple[list[str], str]:
    state = api_request(
        config,
        "GET",
        f"/proxies/{urllib.parse.quote(group, safe='')}",
        timeout_s=config.health_timeout_ms / 1000,
    )
    try:
        proxy_group = state
        members = [str(node) for node in proxy_group["all"]]
        current = str(proxy_group["now"])
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            f"proxy group {group!r} returned an invalid payload: {exc}"
        ) from exc
    return members, current


def available_nodes(config: TesterConfig, allowed_nodes: set[str]) -> dict[str, str]:
    available: dict[str, str] = {}
    for provider_name in config.provider_names:
        provider = api_request(
            config,
            "GET",
            f"/providers/proxies/{urllib.parse.quote(provider_name, safe='')}",
            timeout_s=config.health_timeout_ms / 1000,
        )
        try:
            nodes = provider["proxies"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(
                f"provider {provider_name!r} returned an invalid payload: {exc}"
            ) from exc
        for node in nodes:
            name = str(node["name"])
            if name in SKIP_NODES:
                continue
            if name not in allowed_nodes:
                continue
            if name in available:
                raise RuntimeError(f"proxy name belongs to multiple providers: {name}")
            available[name] = provider_name
    missing = sorted(allowed_nodes - SKIP_NODES - available.keys())
    if missing:
        preview = ", ".join(missing[:5])
        raise RuntimeError(
            f"selector members missing from configured providers: "
            f"count={len(missing)} nodes={preview}"
        )
    return available


def select_node(config: TesterConfig, group: str, name: str) -> None:
    api_request(
        config,
        "PUT",
        f"/proxies/{urllib.parse.quote(group, safe='')}",
        {"name": name},
        timeout_s=config.health_timeout_ms / 1000,
    )


def probe_node(config: TesterConfig, node: str, provider: str) -> ProbeResult:
    query = urllib.parse.urlencode(
        {"url": config.health_url, "timeout": config.health_timeout_ms}
    )
    path = (
        f"/providers/proxies/{urllib.parse.quote(provider, safe='')}/"
        f"{urllib.parse.quote(node, safe='')}/healthcheck?{query}"
    )
    try:
        response = api_request(
            config, "GET", path, timeout_s=config.health_timeout_ms / 1000 + 1
        )
        return ProbeResult(node=node, delay_ms=int(response["delay"]), error=None)
    except (RuntimeError, KeyError, TypeError, ValueError) as exc:
        return ProbeResult(node=node, delay_ms=None, error=str(exc))


def find_good_candidates(
    config: TesterConfig,
    history: History,
    nodes: dict[str, str],
    excluded: set[str],
    target_good: int,
) -> list[ProbeResult]:
    eligible = [
        (node, provider)
        for node, provider in nodes.items()
        if node not in SKIP_NODES
        and node not in excluded
        and history.consecutive_failures(
            node, config.cooldown_fails, config.cooldown_h * 3600
        )
        < config.cooldown_fails
    ]
    random.shuffle(eligible)
    good: list[ProbeResult] = []
    batches = 0
    tested = 0
    for start in range(0, len(eligible), config.probe_batch_size):
        if batches >= config.max_probe_batches:
            break
        batch = eligible[start : start + config.probe_batch_size]
        batches += 1
        tested += len(batch)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=config.probe_workers
        ) as pool:
            futures = [
                pool.submit(probe_node, config, node, provider)
                for node, provider in batch
            ]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if (
                    result.delay_ms is not None
                    and result.delay_ms <= config.max_delay_ms
                ):
                    good.append(result)
                    if len(good) >= target_good:
                        break
        if len(good) >= target_good:
            break
    LOG.info(
        f"event=candidate_scan batches={batches} tested={tested} good={len(good)} "
        f"target={target_good} max_delay_ms={config.max_delay_ms} "
        f"max_batches={config.max_probe_batches}"
    )
    return sorted(good, key=lambda result: result.delay_ms or config.max_delay_ms + 1)


def _download_speed(config: TesterConfig, node: str) -> float:
    select_node(config, config.group, node)
    _, selected = group_state(config, config.group)
    if selected != node:
        raise RuntimeError(f"test group did not pin {node!r}; selected {selected!r}")

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": config.proxy, "https": config.proxy})
    )
    request = urllib.request.Request(config.url, headers={"User-Agent": SERVICE_NAME})
    received = 0
    expected_bytes: int | None = None
    started = time.monotonic()
    deadline = started + config.test_timeout_s
    try:
        with opener.open(request, timeout=config.test_timeout_s) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    expected_bytes = int(content_length)
                except ValueError:
                    LOG.warning(
                        f"event=download_invalid_length node={node} "
                        f"content_length={content_length!r}"
                    )
                    return -1.0
            while received < config.test_max_bytes and time.monotonic() < deadline:
                chunk = response.read(
                    min(CHUNK_BYTES, config.test_max_bytes - received)
                )
                if not chunk:
                    break
                received += len(chunk)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        LOG.warning(f"event=download_failed node={node} error={exc}")
        return -1.0
    elapsed = time.monotonic() - started
    required_bytes = min(expected_bytes or config.test_max_bytes, config.test_max_bytes)
    if received < required_bytes:
        LOG.warning(
            f"event=download_truncated node={node} bytes={received} "
            f"expected={required_bytes}"
        )
        return -1.0
    if received == 0 or elapsed <= 0:
        LOG.warning(
            f"event=download_empty node={node} bytes={received} seconds={elapsed:.3f}"
        )
        return -1.0
    return received / 1024.0 / elapsed


def _set_production(config: TesterConfig, node: str) -> None:
    select_node(config, config.production_group, node)
    _, selected = group_state(config, config.production_group)
    if selected != node:
        raise RuntimeError(
            f"production group did not select {node!r}; selected {selected!r}"
        )


def sweep(config: TesterConfig, history: History) -> None:
    run_id = uuid.uuid4().hex[:8]
    production_members, current = group_state(config, config.production_group)
    test_members, _ = group_state(config, config.group)
    allowed_nodes = set(production_members) & set(test_members)
    providers = available_nodes(config, allowed_nodes)
    candidates = find_good_candidates(
        config,
        history,
        providers,
        {current},
        config.sweep_good_candidates,
    )
    current_result = (
        ProbeResult(node=current, delay_ms=None, error=None)
        if current in providers
        else None
    )
    if current_result is not None:
        candidates = [current_result, *candidates]
    if not candidates:
        LOG.warning(f"run={run_id} event=sweep_no_candidate production={current}")
        return

    shortlist = []
    seen: set[str] = set()
    for result in candidates:
        if result.node not in seen:
            shortlist.append(result.node)
            seen.add(result.node)
        if len(shortlist) >= config.test_max_nodes:
            break
    speeds: dict[str, float] = {}
    for node in shortlist:
        speeds[node] = _download_speed(config, node)
    history.record_sweep(run_id, speeds)
    history.prune(max(config.history_window_h, config.cooldown_h))

    healthy = {node: speed for node, speed in speeds.items() if speed > 0}
    if not healthy:
        LOG.warning(
            f"run={run_id} event=sweep_no_healthy_candidate production={current}"
        )
        return
    fastest = max(healthy, key=lambda node: healthy[node])
    summary = ",".join(
        f"{node}={speed:.0f}KB/s" for node, speed in sorted(healthy.items())
    )
    _set_production(config, fastest)
    if fastest == current:
        LOG.info(f"run={run_id} event=kept node={fastest} speeds=[{summary}]")
    else:
        LOG.info(
            f"run={run_id} event=rotated from={current} to={fastest} speeds=[{summary}]"
        )


def ensure_healthy(config: TesterConfig, history: History) -> None:
    production_members, current = group_state(config, config.production_group)
    test_members, _ = group_state(config, config.group)
    allowed_nodes = set(production_members) & set(test_members)
    providers = available_nodes(config, allowed_nodes)
    if current not in production_members or current not in providers:
        LOG.warning(f"event=production_missing node={current}")
    else:
        result = probe_node(config, current, providers[current])
        if result.delay_ms is not None:
            return
        LOG.warning(f"event=production_unhealthy node={current} error={result.error}")
    candidates = find_good_candidates(
        config, history, providers, {current}, config.failover_good_candidates
    )
    speeds: dict[str, float] = {}
    for candidate in candidates[: config.test_max_nodes]:
        try:
            speed = _download_speed(config, candidate.node)
        except RuntimeError as exc:
            LOG.warning(
                f"event=failover_probe_failed node={candidate.node} error={exc}"
            )
            speed = -1.0
        speeds[candidate.node] = speed
        if speed > 0:
            break
    if not speeds:
        LOG.error(f"event=failover_failed node={current}")
        return
    history.record_sweep(f"failover-{uuid.uuid4().hex[:8]}", speeds)
    healthy = [node for node, speed in speeds.items() if speed > 0]
    if not healthy:
        LOG.error(f"event=failover_failed node={current}")
        return
    fastest = max(healthy, key=lambda node: speeds[node])
    speed = speeds[fastest]
    _set_production(config, fastest)
    LOG.warning(
        f"event=failover from={current} to={fastest} "
        f"delay_ms={next(result.delay_ms for result in candidates if result.node == fastest)} "
        f"speed={speed:.0f}KB/s"
    )


def prepare_production(config: TesterConfig) -> None:
    members, current = group_state(config, config.production_group)
    if current in members:
        LOG.info(f"event=startup_keep node={current}")
        return
    LOG.warning(f"event=startup_stale node={current} available={len(members)}")


def main() -> int:
    try:
        config = TesterConfig()
        if config.group != "TEST_POOL" or config.production_group != "POOL":
            raise ValueError(
                "TEST_GROUP must be TEST_POOL and PRODUCTION_GROUP must be POOL "
                "because mihomo's speed-test listener is wired to TEST_POOL"
            )
    except ValueError as exc:
        LOG.critical(f"event=bad_config error={exc}")
        return 1
    try:
        history = History(config.history_db)
    except RuntimeError as exc:
        LOG.critical(f"event=bad_history error={exc}")
        return 1

    LOG.info(
        f"event=started test_group={config.group} production_group={config.production_group} "
        f"health_interval_s={config.health_interval_s} probe_batch_size={config.probe_batch_size} "
        f"probe_workers={config.probe_workers} "
        f"failover_good_candidates={config.failover_good_candidates} "
        f"sweep_good_candidates={config.sweep_good_candidates} "
        f"test_interval_s={config.test_interval_s} test_max_nodes={config.test_max_nodes}"
    )
    try:
        prepare_production(config)
    except RuntimeError as exc:
        LOG.critical(f"event=startup_failed error={exc}")
        return 1
    next_test = time.monotonic()
    while True:
        try:
            ensure_healthy(config, history)
        except RuntimeError as exc:
            LOG.error(f"event=health_error error={exc}")
        now = time.monotonic()
        if now >= next_test:
            try:
                sweep(config, history)
            except RuntimeError as exc:
                LOG.error(f"event=sweep_error error={exc}")
            next_test = time.monotonic() + config.test_interval_s
        time.sleep(config.health_interval_s)


if __name__ == "__main__":
    sys.exit(main())
