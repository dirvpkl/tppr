#!/usr/bin/env python3
"""who_uses.py — debug helper: who belongs to which profile/service.

Prints service -> primary node -> upstream server:port plus usage counts.
Never prints credentials. Read-only: touches nothing.
Usage: python scripts/who_uses.py [--service NAME]
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse_services(path):
    t = path.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^(\[\[services\]\]\s*)$", t)
    out = []
    for i in range(1, len(parts), 2):
        body = parts[i + 1]
        nm = re.search(r'^name = "([^"]+)"', body, re.M)
        prim = re.search(r'^primary = "([^"]+)"', body, re.M)
        subs = re.search(r"^subscriptions = (\[[^\]]*\])", body, re.M)
        sel = re.search(r"^select = true", body, re.M)
        if nm:
            out.append(
                {
                    "name": nm.group(1),
                    "primary": prim.group(1) if prim else "-",
                    "subs": subs.group(1) if subs else "-",
                    "select": bool(sel),
                }
            )
    return out


def parse_nodes(path):
    t = path.read_text(encoding="utf-8").splitlines()
    nodes = {}
    cur = None
    for line in t:
        m = re.match(r'\s*- name: "([^"]+)"', line)
        if m:
            cur = m.group(1)
            nodes[cur] = {}
            continue
        if cur:
            for k in ("type", "server", "port"):
                m2 = re.match(r"\s*%s: (\S+)" % k, line)
                if m2:
                    nodes[cur][k] = m2.group(1)
        if re.match(r"\s*- name:", line):
            cur = None
    return nodes


def main() -> int:
    only = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--service" else None
    services = parse_services(ROOT / "services.toml")
    nodes = parse_nodes(ROOT / "mihomo" / "providers" / "mine.yaml")
    usage: dict[str, int] = {}
    for s in services:
        if s["primary"] != "-":
            usage[s["primary"]] = usage.get(s["primary"], 0) + 1
    for s in services:
        if only and s["name"] != only:
            continue
        n = nodes.get(s["primary"], {})
        upstream = "%s:%s" % (n.get("server", "?"), n.get("port", "?")) if n else "?"
        flag = " [select-steered]" if s["select"] else ""
        print("%-16s -> %-16s (%s)%s" % (s["name"], s["primary"], upstream, flag))
    print("\nshared upstream endpoints (services per server:port):")
    by_upstream: dict[str, list[str]] = {}
    for s in services:
        if s["primary"] == "-":
            continue
        n = nodes.get(s["primary"], {})
        up = "%s:%s" % (n.get("server", "?"), n.get("port", "?"))
        by_upstream.setdefault(up, []).append(s["name"])
    for up, names in sorted(by_upstream.items(), key=lambda kv: -len(kv[1])):
        if len(names) > 1:
            print("  %s x%d: %s" % (up, len(names), ", ".join(names)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
