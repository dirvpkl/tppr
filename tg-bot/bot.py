"""Primitive Telegram bot: /start rotates one Mihomo select group.

Long-polls the Bot API for updates. A /start message moves TARGET_GROUP to
the next eligible member (round-robin over the controller's order) and replies
with old -> new node names, or with the error text when rotation fails.
Eligible means country-allowed per the prober's ``[filter]`` and not cached bad
for the current UTC day, so a manual rotation cannot undo the prober's work.
Anything that is not /start is ignored. No ports are published: the bot only
makes outbound HTTPS calls (Telegram API, Mihomo controller).

Secrets: TELEGRAM_BOT_TOKEN comes from the environment; the dispatcher
account password comes from the mounted services.toml. Neither is ever
logged (request URLs carrying them are never printed).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from common.logutil import bound_correlation_id, configure_logging
from common.prober_state import (
    VerdictCache,
    eligible_candidates,
    load_excluded_countries,
)

SERVICE_NAME = "tppr-tg-bot"
POLL_TIMEOUT_S = 25
REQUEST_TIMEOUT_S = 40
RETRY_WAIT_S = 5
MAX_REPLY_CHARS = 4000

LOG = logging.getLogger(SERVICE_NAME)


def _getenv(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"missing required environment variable: {name}")
    return value


class BotConfig:
    """Validated bot settings. Single source: environment variables."""

    def __init__(self) -> None:
        self.token = _getenv("TELEGRAM_BOT_TOKEN")
        self.api = _getenv("MIHOMO_API").rstrip("/")
        self.group = _getenv("TARGET_GROUP")
        self.services_file = _getenv("BOT_SERVICES_FILE")
        self.username = _getenv("BOT_USERNAME")
        self.prober_file = _getenv("BOT_PROBER_FILE")
        self.cache_file = Path(_getenv("BOT_CACHE_FILE"))
        # Same sources as the prober: one denylist, one verdict cache.
        self.excluded_countries = load_excluded_countries(Path(self.prober_file))
        proxy_url = _dispatcher_proxy_url(self.services_file, self.api, self.username)
        # Explicit openers: Telegram leaves only through the dispatcher
        # (paid exit); the controller stays direct. No environment routing,
        # so nothing can silently fall back to direct.
        self.tg_opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
        self.direct_opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )


def _dispatcher_proxy_url(services_file: str, api: str, username: str) -> str:
    """Dispatcher proxy URL for this bot's account.

    Single source is the mounted services.toml (dispatcher host/port and the
    account password all come from there, nothing is duplicated). The
    assembled URL is used by the opener only and never logged.
    """
    try:
        with open(services_file, "rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise ValueError(f"cannot read services file: {exc}")
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid services TOML: {exc}")
    if not isinstance(document, dict):
        raise ValueError("services file is not a table")
    dispatcher = document.get("dispatcher")
    port = dispatcher.get("port") if isinstance(dispatcher, dict) else None
    if not isinstance(port, int):
        raise ValueError("services file has no dispatcher port")
    host = urllib.parse.urlsplit(api).hostname or ""
    if not host:
        raise ValueError("controller API URL has no host")
    services = document.get("services")
    if not isinstance(services, list):
        raise ValueError("services file has no services list")
    for service in services:
        if isinstance(service, dict) and service.get("username") == username:
            password = service.get("password", "")
            if not isinstance(password, str) or not password:
                raise ValueError("dispatcher account has no password")
            quoted = (
                f"{urllib.parse.quote(username, safe='')}"
                f":{urllib.parse.quote(password, safe='')}"
            )
            return f"http://{quoted}@{host}:{port}"
    raise ValueError("dispatcher account not found in services file")


def _telegram(config: BotConfig, method: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{config.token}/{method}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with config.tg_opener.open(request, timeout=REQUEST_TIMEOUT_S) as response:
            envelope = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"telegram {method} failed: HTTP {exc.code} {detail}")
    except OSError as exc:
        raise RuntimeError(f"telegram {method} failed: {exc}")
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        raise RuntimeError(f"telegram {method} failed: {envelope!r}"[:200])
    return envelope


def get_updates(config: BotConfig, offset: int) -> list[dict]:
    envelope = _telegram(
        config,
        "getUpdates",
        {"offset": offset, "timeout": POLL_TIMEOUT_S},
    )
    updates = envelope.get("result", [])
    if not isinstance(updates, list):
        raise RuntimeError("telegram getUpdates returned no list")
    return updates


def send_message(config: BotConfig, chat_id: int, text: str) -> None:
    _telegram(
        config,
        "sendMessage",
        {"chat_id": chat_id, "text": text[:MAX_REPLY_CHARS]},
    )


def group_state(config: BotConfig, group: str) -> tuple[list[str], str]:
    request = urllib.request.Request(
        f"{config.api}/proxies/{urllib.parse.quote(group, safe='')}"
    )
    try:
        with config.direct_opener.open(request, timeout=REQUEST_TIMEOUT_S) as response:
            state = json.loads(response.read().decode("utf-8"))
    except OSError as exc:
        raise RuntimeError(f"controller GET group {group!r} failed: {exc}")
    try:
        members = [str(node) for node in state["all"]]
        current = str(state["now"])
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            f"controller group {group!r} returned an invalid payload: {exc}"
        )
    return members, current


def select_node(config: BotConfig, group: str, name: str) -> None:
    request = urllib.request.Request(
        f"{config.api}/proxies/{urllib.parse.quote(group, safe='')}",
        data=json.dumps({"name": name}).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with config.direct_opener.open(request, timeout=REQUEST_TIMEOUT_S):
            pass
    except OSError as exc:
        raise RuntimeError(f"controller PUT group {group!r} failed: {exc}")


def pick_next(members: list[str], current: str) -> str:
    """Next member after current in controller order, wrapping around."""
    if not members:
        raise ValueError("group has no members to rotate to")
    try:
        index = members.index(current)
    except ValueError:
        return members[0]
    return members[(index + 1) % len(members)]


def rotation_members(config: BotConfig, members: list[str]) -> list[str]:
    """Members a /start may rotate to.

    The prober's denylist and today's verdict cache apply here too: a manual
    rotation must not land on a denied country or on a node the prober already
    found broken. The cache is reloaded per rotation because the prober writes
    it every round.
    """
    cache = VerdictCache.load(config.cache_file)
    selection = eligible_candidates(members, config.excluded_countries, cache)
    if not selection.to_probe:
        raise ValueError(f"country filter excludes every member of {config.group}")
    if selection.all_bad_retry:
        LOG.warning(
            "all %d rotatable members are cached bad, retrying them",
            len(selection.to_probe),
        )
    elif selection.skipped_bad:
        LOG.info(
            "rotation skipped %d cached-bad members",
            selection.skipped_bad,
        )
    return list(selection.to_probe)


def rotate_group(config: BotConfig) -> tuple[str, str]:
    """Move the group to the next eligible node; return (old, new). Fail loud."""
    members, current = group_state(config, config.group)
    target = pick_next(rotation_members(config, members), current)
    select_node(config, config.group, target)
    _, selected = group_state(config, config.group)
    if selected != target:
        raise RuntimeError(f"group did not select {target!r}; selected {selected!r}")
    return current, target


def handle_update(config: BotConfig, update: object) -> tuple[int, str] | None:
    """Reply (chat_id, text) for /start, None for anything else.

    ``update`` is boundary data straight from Telegram; it is validated once
    here (``object`` in, malformed values ignored) rather than trusted.
    """
    if not isinstance(update, dict):
        return None
    message = update.get("message") or update.get("edited_message") or {}
    if not isinstance(message, dict):
        return None
    text = str(message.get("text") or "").strip()
    command = text.split(" ", 1)[0]
    if command != "/start" and not command.startswith("/start@"):
        return None
    chat = message.get("chat") or {}
    if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
        return None
    try:
        old, new = rotate_group(config)
    except (RuntimeError, ValueError, OSError) as exc:
        return chat["id"], f"rotation of {config.group} failed: {exc}"
    return chat["id"], f"rotated {config.group}: {old} -> {new}"


def main() -> int:
    configure_logging(SERVICE_NAME)
    try:
        config = BotConfig()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    LOG.info("bot polling for /start, group=%s", config.group)
    LOG.info(
        "rotation honours prober filter %s and verdict cache %s",
        ", ".join(config.excluded_countries) or "none",
        config.cache_file,
    )
    LOG.info("egress via dispatcher (paid exit, no pool fallback)")
    offset = 0
    while True:
        try:
            updates = get_updates(config, offset)
        except (RuntimeError, OSError) as exc:
            LOG.warning("poll failed: %s", exc)
            time.sleep(RETRY_WAIT_S)
            continue
        for update in updates:
            if isinstance(update, dict) and isinstance(update.get("update_id"), int):
                offset = max(offset, update["update_id"] + 1)
            with bound_correlation_id():
                try:
                    reply = handle_update(config, update)
                except (RuntimeError, ValueError, OSError) as exc:
                    LOG.warning("update failed: %s", exc)
                    continue
                if reply is None:
                    continue
                chat_id, text = reply
                try:
                    send_message(config, chat_id, text)
                except (RuntimeError, OSError) as exc:
                    LOG.warning("send failed: %s", exc)
        if not updates:
            continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
