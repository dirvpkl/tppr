"""Append dispatcher accounts to the gitignored services.toml.

One port serves every account; only the name, username and password differ.
Passwords are written into services.toml and nowhere else.
"""

from __future__ import annotations

import argparse
import re
import secrets
import string
import sys
import tomllib
from pathlib import Path

PASSWORD_ALPHABET = string.ascii_letters + string.digits
ACCOUNT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
DISPATCHER_HEADER = "[dispatcher]"


def _account_names(base: str, count: int, taken: set[str]) -> list[str]:
    """base, base1, base2, ... skipping names already present in services.toml."""
    names: list[str] = []
    candidate = base
    suffix = 0
    while len(names) < count:
        if candidate in taken:
            suffix += 1
            candidate = f"{base}{suffix}"
            continue
        taken.add(candidate)
        names.append(candidate)
    return names


def _existing_service_names(path: Path) -> set[str]:
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    return {str(service["name"]) for service in document.get("services", [])}


def _dispatcher_port(text: str) -> int | None:
    match = re.search(r"(?m)^\[dispatcher\]\s*$", text)
    if match is None:
        return None
    port = re.search(r"(?m)^port\s*=\s*(\d+)\s*$", text[match.end() :])
    if port is None:
        raise ValueError("[dispatcher] block in services.toml has no port")
    return int(port.group(1))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate dispatcher accounts in services.toml"
    )
    parser.add_argument("--services", type=Path, default=Path("services.toml"))
    parser.add_argument(
        "--count", type=int, required=True, help="how many accounts to append"
    )
    parser.add_argument(
        "--name",
        required=True,
        help="first account name; later ones get a numeric suffix",
    )
    parser.add_argument(
        "--port",
        type=int,
        required=True,
        help="dispatcher port shared by every account",
    )
    parser.add_argument(
        "--provider",
        action="append",
        required=True,
        help="free provider used as fallback; repeat for several",
    )
    parser.add_argument(
        "--password-length", type=int, default=16, help="generated password length"
    )
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be at least 1")
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    if not 8 <= args.password_length <= 128:
        parser.error("--password-length must be between 8 and 128")
    if not ACCOUNT_NAME_RE.fullmatch(args.name):
        parser.error("--name contains unsupported characters")
    if not args.services.is_file():
        parser.error(f"{args.services} does not exist; copy services.example.toml")

    text = args.services.read_text(encoding="utf-8")
    current_port = _dispatcher_port(text)
    if current_port is not None and current_port != args.port:
        raise SystemExit(
            f"services.toml already exposes the dispatcher on port {current_port}, "
            f"not {args.port}; edit it by hand or pass --port {current_port}"
        )

    names = _account_names(
        args.name, args.count, _existing_service_names(args.services)
    )
    providers = ", ".join(f'"{provider}"' for provider in args.provider)
    blocks = []
    for name in names:
        password = "".join(
            secrets.choice(PASSWORD_ALPHABET) for _ in range(args.password_length)
        )
        blocks.append(
            "\n[[services]]\n"
            f'name = "{name}"\n'
            f"subscriptions = [{providers}]\n"
            f'username = "{name}"\n'
            f'password = "{password}"\n'
        )
    if current_port is None:
        blocks.insert(0, f"{DISPATCHER_HEADER}\nport = {args.port}\n\n")

    args.services.write_text(
        text.rstrip("\n") + "\n" + "".join(blocks), encoding="utf-8"
    )
    print(
        f"appended {len(names)} accounts ({names[0]}..{names[-1]}) to {args.services} "
        f"on dispatcher port {args.port}; passwords are stored only in that file"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
