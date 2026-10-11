"""Primitive Telegram bot: /start rotates one Mihomo select group.

Long-polls the Bot API for updates. A /start message moves TARGET_GROUP to
the next member (round-robin over the controller's order) and replies with
old -> new node names, or with the error text when rotation fails. Anything
that is not /start is ignored. No ports are published: the bot only makes
outbound HTTPS calls (Telegram API, Mihomo controller).

Secrets: TELEGRAM_BOT_TOKEN comes from the environment only and is never
logged (request URLs carrying it are never printed).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SERVICE_NAME = "tppr-tg-bot"
POLL_TIMEOUT_S = 25
REQUEST_TIMEOUT_S = 40
RETRY_WAIT_S = 5
MAX_REPLY_CHARS = 4000
LOG_FORMAT = "%(asctime)s level=%(levelname)s service=%(name)s %(message)s"

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


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    log_file = os.environ.get("LOG_FILE", "").strip()
    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10_485_760, backupCount=5
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(file_handler)


def _telegram(config: BotConfig, method: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{config.token}/{method}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
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


def group_state(api: str, group: str) -> tuple[list[str], str]:
    request = urllib.request.Request(
        f"{api}/proxies/{urllib.parse.quote(group, safe='')}"
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
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


def select_node(api: str, group: str, name: str) -> None:
    request = urllib.request.Request(
        f"{api}/proxies/{urllib.parse.quote(group, safe='')}",
        data=json.dumps({"name": name}).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S):
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


def rotate_group(config: BotConfig) -> tuple[str, str]:
    """Move the group to the next node; return (old, new). Fail loud."""
    members, current = group_state(config.api, config.group)
    target = pick_next(members, current)
    select_node(config.api, config.group, target)
    _, selected = group_state(config.api, config.group)
    if selected != target:
        raise RuntimeError(f"group did not select {target!r}; selected {selected!r}")
    return current, target


def handle_update(config: BotConfig, update: dict) -> tuple[int, str] | None:
    """Reply (chat_id, text) for /start, None for anything else."""
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
    _setup_logging()
    try:
        config = BotConfig()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    LOG.info("bot polling for /start, group=%s", config.group)
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
