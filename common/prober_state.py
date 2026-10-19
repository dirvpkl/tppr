"""Shared prober state: country denylist and the daily node-verdict cache.

The POST prober writes both; the Telegram rotation bot reads the same filter
file and the same cache, so a manual rotation cannot land on a denied country
or on a node the prober already marked bad today. The cache resets at
00:00 UTC. Stdlib only; no sibling imports, so every service image can carry
this module without pulling in probe/upstream.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

REGIONAL_INDICATOR_BASE = 0x1F1E6
COUNTRY_CODE_RE = re.compile(r"[A-Za-z]{2}\Z")


def _utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def country_code(name: str) -> str | None:
    """Return the ISO code from the flag prefix of a provider node name.

    Mihomo's provider payload has no country field, but subscription names are
    written as ``🇩🇪 Германия 1``. A flag is two adjacent regional indicator
    symbols, each mapping to A-Z. Nodes without a flag return None and are
    never denied: a denylist must not drop nodes just because they are
    unlabelled.
    """
    letters: list[str] = []
    for char in name:
        value = ord(char)
        if REGIONAL_INDICATOR_BASE <= value <= REGIONAL_INDICATOR_BASE + 25:
            letters.append(chr(ord("A") + value - REGIONAL_INDICATOR_BASE))
            if len(letters) == 2:
                return "".join(letters)
        elif letters:
            break
    return None


def parse_country_codes(value: object, label: str) -> tuple[str, ...]:
    """Validate a TOML array of two-letter country codes, uppercase and unique."""
    if type(value) is not list:
        raise ValueError(f"{label} must be an array of two-letter country codes")
    codes: list[str] = []
    for item in value:
        if type(item) is not str or COUNTRY_CODE_RE.fullmatch(item) is None:
            raise ValueError(f"{label} contains an invalid country code: {item!r}")
        code = item.upper()
        if code not in codes:
            codes.append(code)
    return tuple(codes)


def filter_candidates(
    candidates: list[str], exclude_countries: tuple[str, ...]
) -> list[str]:
    """Drop candidates whose flag prefix is denied; unlabelled ones stay."""
    if not exclude_countries:
        return list(candidates)
    excluded = frozenset(exclude_countries)
    return [name for name in candidates if country_code(name) not in excluded]


def load_excluded_countries(prober_path: Path) -> tuple[str, ...]:
    """Read ``[filter] exclude_countries`` from the prober TOML.

    That file is the single source for the denylist; the bot reads it instead
    of carrying a second copy.
    """
    try:
        raw = prober_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read prober config {prober_path}: {exc}") from exc
    try:
        document = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in prober config {prober_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"prober config {prober_path} is not a table")
    table = document.get("filter", {})
    if not isinstance(table, dict):
        raise ValueError("prober filter must be a table")
    unknown = sorted(set(table) - {"exclude_countries"})
    if unknown:
        raise ValueError(f"prober filter has unknown keys: {', '.join(unknown)}")
    return parse_country_codes(
        table.get("exclude_countries", []), "filter.exclude_countries"
    )


@dataclass(frozen=True)
class CandidateSelection:
    """A candidate list ready to probe, plus why some were left out."""

    to_probe: tuple[str, ...]
    skipped_bad: int
    all_bad_retry: bool


def eligible_candidates(
    candidates: list[str], exclude_countries: tuple[str, ...], cache: "VerdictCache"
) -> CandidateSelection:
    """Country-allowed candidates minus the ones cached bad today.

    When every allowed candidate is cached bad, returns them all with
    ``all_bad_retry`` set, so a fully failed pool gets one more scan instead of
    stalling until midnight.
    """
    allowed = filter_candidates(candidates, exclude_countries)
    fresh = [name for name in allowed if not cache.is_bad(name)]
    if fresh:
        return CandidateSelection(tuple(fresh), len(allowed) - len(fresh), False)
    return CandidateSelection(tuple(allowed), 0, bool(allowed))


@dataclass
class VerdictCache:
    """Daily node verdicts, persisted so a restart does not rescan the pool.

    A cache is an optimisation, not a contract: an unreadable or malformed file
    is discarded with a warning instead of aborting a steering loop that still
    has candidates to probe.
    """

    path: Path
    day: str
    verdicts: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "VerdictCache":
        cache = cls(path=path, day=_utc_day())
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.info("cache %s not found yet, starting empty", path)
            return cache
        except OSError as exc:
            log.warning("cache %s unreadable, starting empty: %s", path, exc)
            return cache
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("cache %s is not valid JSON, starting empty: %s", path, exc)
            return cache
        if not isinstance(document, dict):
            log.warning("cache %s is not an object, starting empty", path)
            return cache
        stored_day = document.get("day")
        if stored_day != cache.day:
            log.info(
                "cache day %s is not today %s, discarding it", stored_day, cache.day
            )
            return cache
        raw_nodes = document.get("nodes", {})
        if not isinstance(raw_nodes, dict):
            log.warning("cache %s nodes are not an object, starting empty", path)
            return cache
        for name, verdict in raw_nodes.items():
            if type(name) is str and type(verdict) is bool:
                cache.verdicts[name] = verdict
        bad = sum(1 for verdict in cache.verdicts.values() if not verdict)
        log.info(
            "cache %s: %d verdicts (%d bad) for UTC day %s",
            path,
            len(cache.verdicts),
            bad,
            cache.day,
        )
        return cache

    def rollover(self) -> None:
        today = _utc_day()
        if today == self.day:
            return
        log.info(
            "UTC day changed from %s to %s, clearing %d cached verdicts",
            self.day,
            today,
            len(self.verdicts),
        )
        self.day = today
        self.verdicts.clear()
        self.save()

    def is_bad(self, node: str) -> bool:
        return self.verdicts.get(node) is False

    def record(self, node: str, ok: bool) -> None:
        if self.verdicts.get(node) is ok:
            return
        self.verdicts[node] = ok
        self.save()

    def save(self) -> None:
        document = {"day": self.day, "nodes": self.verdicts}
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(
                json.dumps(document, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temp, self.path)
        except OSError as exc:
            log.warning("cannot write cache %s: %s", self.path, exc)
