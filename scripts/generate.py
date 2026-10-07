from __future__ import annotations

import argparse
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

# Reads the local custom-proxy file that services.toml pins proxies from.
import yaml  # type: ignore[import-untyped]

MAX_SERVICES = 1000
BALANCE_STRATEGIES = ("round-robin", "consistent-hashing")
MAX_SUBSCRIPTIONS = 100
MIN_SERVICE_PORT = 1024
MAX_SERVICE_PORT = 65535
MIN_PASSWORD_LENGTH = 8
MIN_SUBSCRIPTION_INTERVAL = 60
MAX_SUBSCRIPTION_INTERVAL = 604800
SERVICE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}\Z")
DISPATCHER_USER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
RESERVED_SERVICE_NAMES = frozenset(
    {
        "POOL",
        "TEST_POOL",
        "FREE",
        "CUSTOM",
        "PROXY",
        "DIRECT",
        "REJECT",
        "PASS",
        "COMPATIBLE",
    }
)
RESERVED_PROVIDER_NAMES = frozenset({"free", "free-vless", "mine"})
RESERVED_SERVICE_PORTS = frozenset({7890, 7891, 9090})
FREE_GROUP = "FREE"
CUSTOM_GROUP = "CUSTOM"


@dataclass(frozen=True)
class HealthSettings:
    url: str
    interval: int
    timeout: int
    max_failed_times: int
    expected_status: int


@dataclass(frozen=True)
class Subscription:
    name: str
    url: str
    interval: int
    health: HealthSettings | None


@dataclass(frozen=True)
class Dispatcher:
    port: int


@dataclass(frozen=True)
class LocalPool:
    name: str
    endpoint: str
    interval: int


@dataclass(frozen=True)
class GlobalPools:
    free_port: int | None
    custom_port: int | None


@dataclass(frozen=True)
class Service:
    name: str
    port: int | None
    primary: str | None
    fallback: tuple[str, ...]
    subscriptions: tuple[str, ...]
    username: str | None
    password: str | None
    lock_proxy: bool
    balance: str | None
    select: bool


TomlTable = dict[str, object]


def _table(value: object, field: str) -> TomlTable:
    if type(value) is not dict:
        raise ValueError(f"{field} must be a table")
    return cast(TomlTable, value)


def _list(value: object, field: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"{field} must be an array")
    return cast(list[object], value)


def _text(table: TomlTable, field: str) -> str:
    value = table.get(field)
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return cast(str, value).strip()


def _reject_unknown_keys(table: TomlTable, allowed: frozenset[str], label: str) -> None:
    """A typo in a config key must abort the run, not be silently ignored —
    a health check that quietly keeps the global default is a wrong config
    that looks right."""
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown keys: {', '.join(unknown)}")


def _integer(table: TomlTable, field: str, minimum: int, maximum: int) -> int:
    value = table.get(field)
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    integer = cast(int, value)
    if integer < minimum or integer > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return integer


def _optional_integer(
    table: TomlTable, field: str, default: int, minimum: int, maximum: int
) -> int:
    if field not in table:
        return default
    return _integer(table, field, minimum, maximum)


def _optional_port(table: TomlTable, field: str) -> int | None:
    if field not in table:
        return None
    port = _integer(table, field, MIN_SERVICE_PORT, MAX_SERVICE_PORT)
    if port in RESERVED_SERVICE_PORTS:
        raise ValueError(f"{field} is reserved by Mihomo: {port}")
    return port


def _load_custom_proxies(path: Path) -> dict[str, TomlTable]:
    if not path.is_file():
        return {}
    try:
        raw_document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"cannot read custom proxies {path}: {exc}") from exc
    if type(raw_document) is not dict:
        raise ValueError(f"custom proxies file {path} must be a mapping")
    raw_proxies = cast(TomlTable, raw_document).get("proxies", [])
    if type(raw_proxies) is not list:
        raise ValueError(f"{path} must contain a proxies list")
    proxies: dict[str, TomlTable] = {}
    for index, raw_proxy in enumerate(cast(list[object], raw_proxies)):
        field = f"{path}: proxies[{index}]"
        if type(raw_proxy) is not dict:
            raise ValueError(f"{field} must be a mapping")
        proxy = cast(TomlTable, raw_proxy)
        name = proxy.get("name")
        if type(name) is not str or not cast(str, name).strip():
            raise ValueError(f"{field}.name must be a non-empty string")
        if cast(str, name).strip() in RESERVED_SERVICE_NAMES:
            raise ValueError(f"{field}.name is reserved: {name}")
        if type(proxy.get("type")) is not str:
            raise ValueError(f"{field}.type must be a non-empty string")
        if cast(str, name).strip() in proxies:
            raise ValueError(f"duplicate custom proxy name: {name}")
        proxies[cast(str, name).strip()] = proxy
    return proxies


def _load_services(
    path: Path,
    local_provider_names: set[str] | None = None,
    custom_proxy_names: set[str] | None = None,
) -> tuple[
    HealthSettings,
    list[Service],
    list[Subscription],
    Dispatcher | None,
    GlobalPools | None,
]:
    try:
        with path.open("rb") as handle:
            document = cast(TomlTable, tomllib.load(handle))
    except OSError as exc:
        raise ValueError(f"cannot read services file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {path}: {exc}") from exc

    health = _table(document.get("health"), "health")
    settings = HealthSettings(
        url=_text(health, "url"),
        interval=_integer(health, "interval", 1, 86400),
        timeout=_integer(health, "timeout", 500, 120000),
        max_failed_times=_integer(health, "max_failed_times", 1, 100),
        expected_status=_integer(health, "expected_status", 100, 599),
    )

    raw_subscriptions = _list(document.get("subscriptions", []), "subscriptions")
    if len(raw_subscriptions) > MAX_SUBSCRIPTIONS:
        raise ValueError(f"subscriptions supports at most {MAX_SUBSCRIPTIONS} entries")

    subscriptions: list[Subscription] = []
    subscription_names: set[str] = set()
    for index, raw_subscription in enumerate(raw_subscriptions):
        field = f"subscriptions[{index}]"
        table = _table(raw_subscription, field)
        name = _text(table, "name")
        if not SERVICE_NAME_RE.fullmatch(name):
            raise ValueError(f"{field}.name contains unsupported characters")
        if name in RESERVED_PROVIDER_NAMES:
            raise ValueError(f"{field}.name is reserved: {name}")
        if name in subscription_names:
            raise ValueError(f"duplicate subscription name: {name}")
        url = _text(table, "url")
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError(f"{field}.url must be an HTTP(S) URL")
        interval = _integer(
            table,
            "interval",
            MIN_SUBSCRIPTION_INTERVAL,
            MAX_SUBSCRIPTION_INTERVAL,
        )
        _reject_unknown_keys(
            table, frozenset({"name", "url", "interval", "health"}), field
        )
        raw_health = table.get("health")
        health_override: HealthSettings | None = None
        if raw_health is not None:
            health_table = _table(raw_health, f"{field}.health")
            _reject_unknown_keys(
                health_table,
                frozenset(
                    {
                        "url",
                        "interval",
                        "timeout",
                        "max_failed_times",
                        "expected_status",
                    }
                ),
                f"{field}.health",
            )
            health_override = HealthSettings(
                url=_text(health_table, "url"),
                interval=_optional_integer(
                    health_table, "interval", settings.interval, 1, 86400
                ),
                timeout=_optional_integer(
                    health_table, "timeout", settings.timeout, 500, 120000
                ),
                max_failed_times=_optional_integer(
                    health_table, "max_failed_times", settings.max_failed_times, 1, 100
                ),
                expected_status=_optional_integer(
                    health_table,
                    "expected_status",
                    settings.expected_status,
                    100,
                    599,
                ),
            )
        subscription_names.add(name)
        subscriptions.append(Subscription(name, url, interval, health_override))

    known_provider_names = subscription_names | (local_provider_names or set())
    known_custom_names = custom_proxy_names or set()
    dispatcher: Dispatcher | None = None
    raw_dispatcher = document.get("dispatcher")
    if raw_dispatcher is not None:
        dispatcher_table = _table(raw_dispatcher, "dispatcher")
        dispatcher_port = _integer(
            dispatcher_table, "port", MIN_SERVICE_PORT, MAX_SERVICE_PORT
        )
        if dispatcher_port in RESERVED_SERVICE_PORTS:
            raise ValueError(
                f"dispatcher.port is reserved by Mihomo: {dispatcher_port}"
            )
        dispatcher = Dispatcher(dispatcher_port)

    global_pools: GlobalPools | None = None
    raw_global_pools = document.get("global_pools")
    if raw_global_pools is not None:
        global_table = _table(raw_global_pools, "global_pools")
        global_pools = GlobalPools(
            free_port=_optional_port(global_table, "free_port"),
            custom_port=_optional_port(global_table, "custom_port"),
        )

    claimed_ports: set[int] = set()
    if dispatcher is not None:
        claimed_ports.add(dispatcher.port)
    if global_pools is not None:
        for field, port in (
            ("global_pools.free_port", global_pools.free_port),
            ("global_pools.custom_port", global_pools.custom_port),
        ):
            if port is None:
                continue
            if port in claimed_ports:
                raise ValueError(f"{field} duplicates another listener port: {port}")
            claimed_ports.add(port)

    raw_services = _list(document.get("services", []), "services")
    if len(raw_services) > MAX_SERVICES:
        raise ValueError(
            f"services has {len(raw_services)} entries but the generator allows at "
            f"most MAX_SERVICES = {MAX_SERVICES}. If that is really what you want, "
            "raise MAX_SERVICES at the top of scripts/generate.py and reload."
        )

    services: list[Service] = []
    names: set[str] = set()
    usernames: set[str] = set()
    for index, raw_service in enumerate(raw_services):
        field = f"services[{index}]"
        table = _table(raw_service, field)
        name = _text(table, "name")
        if not SERVICE_NAME_RE.fullmatch(name):
            raise ValueError(f"{field}.name contains unsupported characters")
        if name.upper() in RESERVED_SERVICE_NAMES:
            raise ValueError(f"{field}.name is reserved: {name}")
        if name in names:
            raise ValueError(f"duplicate service name: {name}")
        port = _optional_port(table, "port")
        if port is not None:
            if port in claimed_ports:
                raise ValueError(
                    f"{field}.port duplicates another listener port: {port}"
                )
            claimed_ports.add(port)

        raw_primary = table.get("primary")
        primary = None if raw_primary is None else _text(table, "primary")
        if primary is not None and primary not in known_custom_names:
            raise ValueError(
                f"{field}.primary is not defined in the custom proxy file: {primary}"
            )
        raw_fallback = _list(table.get("fallback", []), f"{field}.fallback")
        fallback: list[str] = []
        for fallback_index, raw_proxy in enumerate(raw_fallback):
            if type(raw_proxy) is not str or not raw_proxy.strip():
                raise ValueError(
                    f"{field}.fallback[{fallback_index}] must be a non-empty string"
                )
            proxy = cast(str, raw_proxy).strip()
            if proxy == primary or proxy in fallback:
                raise ValueError(f"{field}.fallback contains a duplicate proxy")
            fallback.append(proxy)
        if primary is None and fallback:
            raise ValueError(f"{field}.fallback requires {field}.primary")

        raw_service_subscriptions = _list(
            table.get("subscriptions", []), f"{field}.subscriptions"
        )
        service_subscriptions: list[str] = []
        for subscription_index, raw_subscription_name in enumerate(
            raw_service_subscriptions
        ):
            if (
                type(raw_subscription_name) is not str
                or not raw_subscription_name.strip()
            ):
                raise ValueError(
                    f"{field}.subscriptions[{subscription_index}] must be a non-empty string"
                )
            subscription_name = cast(str, raw_subscription_name).strip()
            if subscription_name not in known_provider_names:
                raise ValueError(
                    f"{field}.subscriptions references an unknown provider: "
                    f"{subscription_name}"
                )
            if subscription_name in service_subscriptions:
                raise ValueError(
                    f"{field}.subscriptions contains a duplicate subscription: "
                    f"{subscription_name}"
                )
            service_subscriptions.append(subscription_name)

        raw_username = table.get("username")
        raw_password = table.get("password")
        if (raw_username is None) != (raw_password is None):
            raise ValueError(
                f"{field}.username and {field}.password must be set together"
            )
        username = None if raw_username is None else _text(table, "username")
        password = None if raw_password is None else _text(table, "password")
        if username is not None and not DISPATCHER_USER_RE.fullmatch(username):
            raise ValueError(f"{field}.username contains unsupported characters")
        if password is not None and len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"{field}.password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        if username is not None and username in usernames:
            raise ValueError(f"duplicate dispatcher username: {username}")
        if username is not None:
            usernames.add(username)

        if port is None and username is None:
            raise ValueError(f"{field} requires port or dispatcher credentials")

        raw_balance = table.get("balance")
        balance = None if raw_balance is None else _text(table, "balance")
        if balance is not None and balance not in BALANCE_STRATEGIES:
            raise ValueError(
                f"{field}.balance must be one of {', '.join(BALANCE_STRATEGIES)}"
            )
        if balance is not None and not service_subscriptions:
            raise ValueError(f"{field}.balance requires {field}.subscriptions")
        if primary is None and not service_subscriptions:
            raise ValueError(f"{field} requires primary or subscriptions")

        lock_proxy = table.get("lock_proxy", False)
        if type(lock_proxy) is not bool:
            raise ValueError(f"{field}.lock_proxy must be a boolean")
        lock_proxy = cast(bool, lock_proxy)
        if lock_proxy and (fallback or service_subscriptions):
            raise ValueError(
                f"{field}.lock_proxy forbids {field}.fallback and {field}.subscriptions"
            )
        if (
            not lock_proxy
            and primary is not None
            and not (fallback or service_subscriptions)
        ):
            raise ValueError(
                f"{field} requires a fallback source; set {field}.lock_proxy = true "
                "to pin the primary proxy"
            )

        if balance is not None and (primary is not None or fallback or lock_proxy):
            raise ValueError(
                f"{field}.balance rotates the whole pool, so it forbids "
                f"{field}.primary, {field}.fallback and {field}.lock_proxy"
            )

        raw_select = table.get("select", False)
        if type(raw_select) is not bool:
            raise ValueError(f"{field}.select must be a boolean")
        select_flag = cast(bool, raw_select)
        if select_flag and not service_subscriptions:
            raise ValueError(f"{field}.select requires {field}.subscriptions")
        if select_flag and (
            primary is not None or fallback or lock_proxy or balance is not None
        ):
            raise ValueError(
                f"{field}.select is steered externally, so it forbids "
                f"{field}.primary, {field}.fallback, {field}.lock_proxy and "
                f"{field}.balance"
            )

        names.add(name)
        services.append(
            Service(
                name,
                port,
                primary,
                tuple(fallback),
                tuple(service_subscriptions),
                username,
                password,
                lock_proxy,
                balance,
                select_flag,
            )
        )
    if dispatcher is None and usernames:
        raise ValueError("dispatcher credentials require a [dispatcher] block")
    if dispatcher is not None and not usernames:
        raise ValueError("[dispatcher] requires at least one service username/password")
    if global_pools is not None:
        if global_pools.free_port is not None and not known_provider_names:
            raise ValueError(
                "global_pools.free_port needs at least one subscription or pool"
            )
        if global_pools.custom_port is not None and not known_custom_names:
            raise ValueError("global_pools.custom_port needs at least one custom proxy")
    return settings, services, subscriptions, dispatcher, global_pools


def _load_pools(path: Path) -> list[LocalPool]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            document = cast(TomlTable, tomllib.load(handle))
    except OSError as exc:
        raise ValueError(f"cannot read pool config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {path}: {exc}") from exc

    worker = _table(document.get("worker"), "worker")
    endpoint = _text(worker, "endpoint")
    parsed_endpoint = urlparse(endpoint)
    if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.netloc:
        raise ValueError("worker.endpoint must be an HTTP(S) URL")
    interval = _integer(
        worker,
        "refresh_interval_s",
        MIN_SUBSCRIPTION_INTERVAL,
        MAX_SUBSCRIPTION_INTERVAL,
    )
    raw_pools = _list(document.get("pools", []), "pools")
    if not raw_pools:
        raise ValueError("pool config must contain at least one [[pools]] entry")

    pools: list[LocalPool] = []
    names: set[str] = set()
    for index, raw_pool in enumerate(raw_pools):
        field = f"pools[{index}]"
        table = _table(raw_pool, field)
        name = _text(table, "name")
        if not SERVICE_NAME_RE.fullmatch(name):
            raise ValueError(f"{field}.name contains unsupported characters")
        if name in RESERVED_PROVIDER_NAMES:
            raise ValueError(f"{field}.name is reserved: {name}")
        if name in names:
            raise ValueError(f"duplicate pool name: {name}")
        names.add(name)
        pools.append(LocalPool(name, endpoint.rstrip("/"), interval))
    return pools


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _service_group(service: Service, health: HealthSettings) -> str:
    if service.select:
        lines = [
            f"  - name: {_quote(f'SVC_{service.name}')}",
            "    type: select",
            "    use:",
        ]
        lines.extend(
            f"      - {_quote(subscription)}" for subscription in service.subscriptions
        )
        lines.append("    empty-fallback: REJECT")
        return "\n".join(lines) + "\n"
    group_type = "load-balance" if service.balance is not None else "fallback"
    lines = [
        f"  - name: {_quote(f'SVC_{service.name}')}",
        f"    type: {group_type}",
    ]
    if service.balance is not None:
        lines.append(f"    strategy: {service.balance}")
    if service.primary is not None:
        lines.append("    proxies:")
        lines.append(f"      - {_quote(service.primary)}")
        lines.extend(f"      - {_quote(proxy)}" for proxy in service.fallback)
    if service.subscriptions:
        lines.append("    use:")
        lines.extend(
            f"      - {_quote(subscription)}" for subscription in service.subscriptions
        )
    lines.extend(
        [
            f"    url: {_quote(health.url)}",
            f"    interval: {health.interval}",
            "    lazy: true",
            f"    timeout: {health.timeout}",
            f"    max-failed-times: {health.max_failed_times}",
            f"    expected-status: {health.expected_status}",
            "    empty-fallback: REJECT",
        ]
    )
    return "\n".join(lines) + "\n"


def _service_listener(service: Service) -> str:
    return (
        "\n".join(
            [
                f"  - name: {_quote(f'svc-{service.name}')}",
                "    type: mixed",
                "    listen: 0.0.0.0",
                f"    port: {service.port}",
                f"    proxy: {_quote(f'SVC_{service.name}')}",
            ]
        )
        + "\n"
    )


def _group_listener(name: str, group: str, port: int) -> str:
    return (
        "\n".join(
            [
                f"  - name: {_quote(name)}",
                "    type: mixed",
                "    listen: 0.0.0.0",
                f"    port: {port}",
                f"    proxy: {_quote(group)}",
            ]
        )
        + "\n"
    )


def _select_group(name: str, lines: list[str]) -> str:
    return (
        "\n".join(
            [
                f"  - name: {_quote(name)}",
                "    type: select",
                *lines,
                "    empty-fallback: REJECT",
            ]
        )
        + "\n"
    )


def _dispatcher_listener(dispatcher: Dispatcher, services: list[Service]) -> str:
    lines = [
        '  - name: "dispatcher"',
        "    type: mixed",
        "    listen: 0.0.0.0",
        f"    port: {dispatcher.port}",
        "    users:",
    ]
    for service in services:
        if service.username is not None and service.password is not None:
            lines.extend(
                [
                    "      - username: " + _quote(service.username),
                    "        password: " + _quote(service.password),
                ]
            )
    return "\n".join(lines) + "\n"


def _dispatcher_rules(services: list[Service]) -> str:
    return "".join(
        f"  - IN-USER,{service.username},SVC_{service.name}\n"
        for service in services
        if service.username is not None
    )


def _insert_before(text: str, anchor: str, addition: str, label: str) -> str:
    if text.count(anchor) != 1:
        raise ValueError(f"base config must contain one {label} anchor")
    return text.replace(anchor, addition + anchor, 1)


def _insert_after(text: str, anchor: str, addition: str, label: str) -> str:
    if text.count(anchor) != 1:
        raise ValueError(f"base config must contain one {label} anchor")
    return text.replace(anchor, anchor + addition, 1)


def _set_global_group_sources(
    text: str, free_providers: list[str], custom_names: list[str]
) -> str:
    pattern = re.compile(r"(?m)^    use: \[[^\]\n]*\]$")
    if len(pattern.findall(text)) != 2:
        raise ValueError("base config must contain POOL and TEST_POOL select groups")
    lines = [f"    use: [{', '.join(free_providers)}]"]
    if custom_names:
        lines.append("    proxies:")
        lines.extend(f"      - {_quote(name)}" for name in custom_names)
    return pattern.sub(lambda _: "\n".join(lines), text)


def _service_health(
    service: Service,
    health_by_provider: dict[str, HealthSettings],
    default: HealthSettings,
) -> HealthSettings:
    distinct = list(
        dict.fromkeys(health_by_provider[name] for name in service.subscriptions)
    )
    if len(distinct) > 1:
        raise ValueError(
            f"services {service.name} mixes providers with different health "
            "checks; give the custom-health subscription its own service"
        )
    return distinct[0] if distinct else default


def render_config(
    base: str,
    services: list[Service],
    subscriptions: list[Subscription],
    health: HealthSettings,
    custom_proxies: dict[str, TomlTable],
    dispatcher: Dispatcher | None = None,
    pools: list[LocalPool] | None = None,
    global_pools: GlobalPools | None = None,
) -> str:
    rendered = base
    free_providers = (
        ["free-vless"]
        + [subscription.name for subscription in subscriptions]
        + [pool.name for pool in pools or []]
    )
    custom_names = list(custom_proxies)
    health_by_provider: dict[str, HealthSettings] = {
        name: health
        for name in [s.name for s in subscriptions] + [p.name for p in pools or []]
    }
    for subscription in subscriptions:
        if subscription.health is not None:
            health_by_provider[subscription.name] = subscription.health
    if custom_names:
        rendered = _set_global_group_sources(rendered, free_providers, custom_names)

    groups = "\n" + "".join(
        _service_group(
            service,
            (
                health
                if service.select
                else _service_health(service, health_by_provider, health)
            ),
        )
        for service in services
    )
    if global_pools is not None:
        if global_pools.free_port is not None:
            groups += _select_group(
                FREE_GROUP, [f"    use: [{', '.join(free_providers)}]"]
            )
        if global_pools.custom_port is not None:
            groups += _select_group(
                CUSTOM_GROUP,
                ["    proxies:", *[f"      - {_quote(n)}" for n in custom_names]],
            )
    if groups.strip():
        rendered = _insert_before(rendered, "\nrules:\n", groups, "rules")
    if dispatcher is not None:
        rendered = _insert_after(
            rendered,
            "\nrules:\n",
            _dispatcher_rules(services),
            "rules",
        )
    if services or dispatcher is not None or global_pools is not None:
        listeners = "".join(
            _service_listener(service)
            for service in services
            if service.port is not None
        )
        if dispatcher is not None:
            listeners = _dispatcher_listener(dispatcher, services) + listeners
        if global_pools is not None:
            if global_pools.free_port is not None:
                listeners += _group_listener(
                    "free-pool", FREE_GROUP, global_pools.free_port
                )
            if global_pools.custom_port is not None:
                listeners += _group_listener(
                    "custom-pool", CUSTOM_GROUP, global_pools.custom_port
                )
        rendered = _insert_before(rendered, "\nprofile:\n", "\n" + listeners, "profile")

    if custom_proxies:
        rendered = _insert_before(
            rendered,
            "\nproxy-providers:\n",
            "\n"
            + yaml.safe_dump(
                {"proxies": list(custom_proxies.values())},
                allow_unicode=True,
                sort_keys=False,
            ),
            "proxy-providers",
        )

    provider_blocks: list[str] = []
    for subscription in subscriptions:
        provider_health = subscription.health or health
        provider_blocks.append(
            "\n".join(
                [
                    f"  {_quote(subscription.name)}:",
                    "    type: http",
                    f"    url: {_quote(subscription.url)}",
                    f"    path: ./providers/{subscription.name}.yaml",
                    f"    interval: {subscription.interval}",
                    "    health-check:",
                    "      enable: true",
                    f"      url: {_quote(provider_health.url)}",
                    f"      interval: {provider_health.interval}",
                    f"      timeout: {provider_health.timeout}",
                    "      lazy: true",
                    f"      max-failed-times: {provider_health.max_failed_times}",
                    f"      expected-status: {provider_health.expected_status}",
                    "",
                ]
            )
        )
    for pool in pools or []:
        provider_blocks.append(
            "\n".join(
                [
                    f"  {_quote(pool.name)}:",
                    "    type: http",
                    f"    url: {_quote(f'{pool.endpoint}/pools/{pool.name}.yaml')}",
                    f"    path: ./providers/pool-{pool.name}.yaml",
                    f"    interval: {pool.interval}",
                    "    health-check:",
                    "      enable: true",
                    f"      url: {_quote(health.url)}",
                    f"      interval: {health.interval}",
                    f"      timeout: {health.timeout}",
                    "      lazy: true",
                    f"      max-failed-times: {health.max_failed_times}",
                    f"      expected-status: {health.expected_status}",
                    "",
                ]
            )
        )
    if provider_blocks:
        rendered = _insert_before(
            rendered,
            "\nproxy-groups:\n",
            "\n" + "\n".join(provider_blocks) + "\n",
            "proxy-groups",
        )
    return rendered


def render_compose_override(
    services: list[Service],
    dispatcher: Dispatcher | None = None,
    global_pools: GlobalPools | None = None,
) -> str:
    ports: list[int] = []
    if dispatcher is not None:
        ports.append(dispatcher.port)
    if global_pools is not None:
        ports.extend(
            port
            for port in (global_pools.free_port, global_pools.custom_port)
            if port is not None
        )
    ports.extend(service.port for service in services if service.port is not None)
    if not ports:
        return "services: {}\n"
    lines = ["services:", "  tppr-mihomo-relay:", "    ports:"]
    lines.extend(f'      - "0.0.0.0:{port}:{port}"' for port in ports)
    return "\n".join(lines) + "\n"


def generate(
    services_path: Path,
    base_path: Path,
    config_path: Path,
    override_path: Path,
    pools_path: Path = Path("pool-sources.toml"),
    custom_path: Path = Path("mihomo/providers/mine.yaml"),
) -> None:
    pools = _load_pools(pools_path)
    custom_proxies = _load_custom_proxies(custom_path)
    health, services, subscriptions, dispatcher, global_pools = _load_services(
        services_path, {pool.name for pool in pools}, set(custom_proxies)
    )
    subscription_names = {subscription.name for subscription in subscriptions}
    pool_names = {pool.name for pool in pools}
    overlap = subscription_names & pool_names
    if overlap:
        raise ValueError(
            f"provider name is used by both subscription and pool: {overlap.pop()}"
        )
    try:
        base = base_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read base config {base_path}: {exc}") from exc
    config_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        render_config(
            base,
            services,
            subscriptions,
            health,
            custom_proxies,
            dispatcher,
            pools,
            global_pools,
        ),
        encoding="utf-8",
    )
    override_path.write_text(
        render_compose_override(services, dispatcher, global_pools), encoding="utf-8"
    )
    print(f"generated {config_path} and {override_path} ({len(services)} services)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate per-service Mihomo ports")
    parser.add_argument("--services", type=Path, default=Path("services.toml"))
    parser.add_argument("--base", type=Path, default=Path("mihomo/config.base.yaml"))
    parser.add_argument(
        "--config", type=Path, default=Path("mihomo/generated/config.yaml")
    )
    parser.add_argument(
        "--override", type=Path, default=Path("docker-compose.override.yml")
    )
    parser.add_argument("--pools", type=Path, default=Path("pool-sources.toml"))
    parser.add_argument(
        "--custom", type=Path, default=Path("mihomo/providers/mine.yaml")
    )
    args = parser.parse_args()
    try:
        generate(
            args.services,
            args.base,
            args.config,
            args.override,
            args.pools,
            args.custom,
        )
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
