"""HTTP-level liveness probing through a proxy tunnel.

`connect_upstream` (common.upstream) only proves TCP: the tunnel opens, but
nothing says the proxy actually serves HTTP through it. This module sends a
real, configurable HTTP request through the tunnel and matches the full
response against expectations: status, headers, body and cookies.

Stdlib only. No module state is mutated per call, so `probe_node` is safe to
call from a thread pool. Node types the prober cannot speak report
"skipped": dropping them would gut pools on a capability gap, not on
evidence. A malformed *node* reports "dead" (evidence about that node);
malformed *config* raises ValueError at parse time and must abort the run.
"""

from __future__ import annotations

import http.client
import re
import socket
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from http.cookies import SimpleCookie
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from common.upstream import connect_upstream

# Verdict literals. Both sides (this module and its callers) reference these,
# never their own copies of the same strings.
LIVE = "live"
DEAD = "dead"
SKIPPED = "skipped"

MIN_STATUS = 100
MAX_STATUS = 599
# Safety bound on how much response body is read for matching. A probe never
# needs megabytes; without a cap a hostile target could exhaust the worker.
MAX_BODY_BYTES = 262144
MIN_LATENCY_MS = 1
MAX_LATENCY_MS = 3600000

METHOD_RE = re.compile(r"[A-Z][A-Z0-9_-]*\Z")
STATUS_CLASS_RE = re.compile(r"[1-5]xx\Z")
TOKEN_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")

# Built once: loading the system CA bundle costs tens of milliseconds and
# every HTTPS probe would otherwise pay it. Read-only afterwards, so sharing
# it across probe threads is safe.
_TLS_CONTEXT = ssl.create_default_context()


@dataclass(frozen=True)
class HeaderRule:
    name: str
    equals: str | None = None
    contains: str | None = None
    regex: str | None = None


@dataclass(frozen=True)
class CookieRule:
    name: str
    equals: str | None = None


@dataclass(frozen=True)
class BodyExpect:
    contains: str | None = None
    regex: str | None = None


@dataclass(frozen=True)
class ExpectSpec:
    """What counts as a live node. Empty status rules accept any status."""

    status: tuple[int | str, ...] = ()
    headers: tuple[HeaderRule, ...] = ()
    body: BodyExpect | None = None
    cookies: tuple[CookieRule, ...] = ()
    max_latency_ms: int | None = None


@dataclass(frozen=True)
class ProbeSpec:
    method: str
    host: str
    port: int
    use_tls: bool
    path: str
    headers: tuple[tuple[str, str], ...] = ()
    cookies: tuple[tuple[str, str], ...] = ()
    body: bytes = b""
    tls_cafile: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    verdict: str
    status: int | None
    latency_ms: int
    reason: str


@dataclass(frozen=True)
class ProbeResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@lru_cache(maxsize=64)
def _compiled_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _check_token(value: object, field: str) -> str:
    if type(value) is not str or not TOKEN_RE.fullmatch(value):
        raise ValueError(f"{field} must be an RFC 9110 token")
    return cast(str, value)


def _check_header_value(value: object, field: str) -> str:
    if type(value) is not str or value == "":
        raise ValueError(f"{field} must be a non-empty string")
    text = cast(str, value)
    if "\r" in text or "\n" in text:
        raise ValueError(f"{field} must not contain CR or LF")
    return text


def _check_method(value: object) -> str:
    if type(value) is not str or not METHOD_RE.fullmatch(value):
        raise ValueError("check.method must be an uppercase token like GET or PATCH")
    return cast(str, value)


def _check_regex(pattern: object, field: str) -> str:
    if type(pattern) is not str or pattern == "":
        raise ValueError(f"{field} must be a non-empty string")
    try:
        re.compile(cast(str, pattern))
    except re.error as exc:
        raise ValueError(f"{field} is not a valid regex: {exc}") from exc
    return cast(str, pattern)


def _mapping(raw: object, field: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{field} must be a table")
    return raw


def _header_rules(raw: object) -> tuple[HeaderRule, ...]:
    if raw is None:
        return ()
    if type(raw) is not list:
        raise ValueError("check.expect.headers must be an array")
    rules: list[HeaderRule] = []
    for index, item in enumerate(cast(list[object], raw)):
        table = _mapping(item, f"check.expect.headers[{index}]")
        equals = table.get("equals")
        contains = table.get("contains")
        regex = table.get("regex")
        modes = sum(mode is not None for mode in (equals, contains, regex))
        if modes != 1:
            raise ValueError(
                f"check.expect.headers[{index}] needs exactly one of"
                " equals/contains/regex"
            )
        rules.append(
            HeaderRule(
                name=_check_token(
                    table.get("name"), f"check.expect.headers[{index}].name"
                ),
                equals=(
                    _check_header_value(equals, f"check.expect.headers[{index}].equals")
                    if equals is not None
                    else None
                ),
                contains=(
                    _check_header_value(
                        contains, f"check.expect.headers[{index}].contains"
                    )
                    if contains is not None
                    else None
                ),
                regex=(
                    _check_regex(regex, f"check.expect.headers[{index}].regex")
                    if regex is not None
                    else None
                ),
            )
        )
    return tuple(rules)


def _cookie_rules(raw: object) -> tuple[CookieRule, ...]:
    if raw is None:
        return ()
    if type(raw) is not list:
        raise ValueError("check.expect.cookies must be an array")
    rules: list[CookieRule] = []
    for index, item in enumerate(cast(list[object], raw)):
        table = _mapping(item, f"check.expect.cookies[{index}]")
        equals = table.get("equals")
        rules.append(
            CookieRule(
                name=_check_token(
                    table.get("name"), f"check.expect.cookies[{index}].name"
                ),
                equals=(
                    _check_header_value(equals, f"check.expect.cookies[{index}].equals")
                    if equals is not None
                    else None
                ),
            )
        )
    return tuple(rules)


def _body_expect(raw: object) -> BodyExpect | None:
    if raw is None:
        return None
    table = _mapping(raw, "check.expect.body")
    contains = table.get("contains")
    regex = table.get("regex")
    if contains is None and regex is None:
        raise ValueError("check.expect.body needs contains and/or regex")
    return BodyExpect(
        contains=(
            _check_header_value(contains, "check.expect.body.contains")
            if contains is not None
            else None
        ),
        regex=(
            _check_regex(regex, "check.expect.body.regex")
            if regex is not None
            else None
        ),
    )


def _status_rules(raw: object) -> tuple[int | str, ...]:
    if raw is None:
        return ()
    if type(raw) is not list:
        raise ValueError("check.expect.status must be an array")
    rules: list[int | str] = []
    for index, item in enumerate(cast(list[object], raw)):
        if type(item) is int:
            code = cast(int, item)
            if code < MIN_STATUS or code > MAX_STATUS:
                raise ValueError(
                    f"check.expect.status[{index}] must be between"
                    f" {MIN_STATUS} and {MAX_STATUS}"
                )
            rules.append(code)
        elif type(item) is str and STATUS_CLASS_RE.fullmatch(item):
            rules.append(item)
        else:
            raise ValueError(
                f"check.expect.status[{index}] must be a status code"
                ' or a class like "2xx"'
            )
    return tuple(rules)


def _latency(raw: object) -> int | None:
    if raw is None:
        return None
    if type(raw) is not int:
        raise ValueError("check.expect.max_latency_ms must be an integer")
    limit = cast(int, raw)
    if limit < MIN_LATENCY_MS or limit > MAX_LATENCY_MS:
        raise ValueError(
            f"check.expect.max_latency_ms must be between"
            f" {MIN_LATENCY_MS} and {MAX_LATENCY_MS}"
        )
    return limit


def parse_expect_spec(raw: object) -> ExpectSpec:
    """Boundary validator for the [check.expect] table. Absent means anything
    that completes the exchange is live, which preserves the historical
    tunnel-only behavior until the operator opts into strictness."""
    if raw is None:
        return ExpectSpec()
    table = _mapping(raw, "check.expect")
    return ExpectSpec(
        status=_status_rules(table.get("status")),
        headers=_header_rules(table.get("headers")),
        body=_body_expect(table.get("body")),
        cookies=_cookie_rules(table.get("cookies")),
        max_latency_ms=_latency(table.get("max_latency_ms")),
    )


def parse_probe_spec(raw: object) -> ProbeSpec:
    """Boundary validator for the [check] table itself."""
    table = _mapping(raw, "check")
    raw_url = table.get("url")
    if type(raw_url) is not str or not raw_url.strip():
        raise ValueError("check.url must be a non-empty string")
    parsed = urlparse(raw_url.strip())
    if parsed.scheme == "https":
        port = parsed.port or 443
        use_tls = True
    elif parsed.scheme == "http":
        port = parsed.port or 80
        use_tls = False
    else:
        raise ValueError("check.url must be an HTTP(S) URL")
    if not parsed.hostname:
        raise ValueError("check.url must contain a host")
    raw_method = table.get("method", "GET")
    raw_headers = table.get("headers")
    raw_cookies = table.get("cookies")
    headers = _string_pairs(raw_headers, "check.headers")
    cookies = _string_pairs(raw_cookies, "check.cookies")
    if cookies and any(name.lower() == "cookie" for name, _ in headers):
        raise ValueError(
            "check.headers must not repeat Cookie when check.cookies is set"
        )
    raw_body = table.get("body", "")
    if type(raw_body) is not str:
        raise ValueError("check.body must be a string")
    raw_cafile = table.get("tls_cafile")
    cafile: str | None = None
    if raw_cafile is not None:
        if type(raw_cafile) is not str or not cast(str, raw_cafile).strip():
            raise ValueError("check.tls_cafile must be a non-empty string")
        cafile = cast(str, raw_cafile).strip()
        if not Path(cafile).is_file():
            raise ValueError(f"check.tls_cafile not found: {cafile}")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return ProbeSpec(
        method=_check_method(raw_method),
        host=parsed.hostname,
        port=port,
        use_tls=use_tls,
        path=path,
        headers=headers,
        cookies=cookies,
        body=cast(str, raw_body).encode("utf-8"),
        tls_cafile=cafile,
    )


def _string_pairs(raw: object, field: str) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    table = _mapping(raw, field)
    pairs: list[tuple[str, str]] = []
    for name, value in table.items():
        checked_name = _check_token(name, f"{field} name")
        checked_value = _check_header_value(value, f"{field}.{name}")
        pairs.append((checked_name, checked_value))
    return tuple(pairs)


def _status_accepted(code: int, rules: tuple[int | str, ...]) -> bool:
    if not rules:
        return True
    for rule in rules:
        if type(rule) is int and code == rule:
            return True
        if type(rule) is str and code // 100 == int(rule[0]):
            return True
    return False


def _check_status(code: int, expect: ExpectSpec) -> str | None:
    if _status_accepted(code, expect.status):
        return None
    return f"status {code} not expected"


def _check_headers(
    received: tuple[tuple[str, str], ...], expect: ExpectSpec
) -> str | None:
    grouped: dict[str, list[str]] = {}
    for name, value in received:
        grouped.setdefault(name.lower(), []).append(value)
    for rule in expect.headers:
        values = grouped.get(rule.name.lower())
        if not values:
            return f"header {rule.name} missing"
        if rule.equals is not None and values[0] != rule.equals:
            return f"header {rule.name} mismatch"
        if rule.contains is not None and rule.contains not in ", ".join(values):
            return f"header {rule.name} mismatch"
        if rule.regex is not None and not _compiled_regex(rule.regex).search(
            ", ".join(values)
        ):
            return f"header {rule.name} mismatch"
    return None


def _check_body(body: bytes, expect: ExpectSpec) -> str | None:
    rule = expect.body
    if rule is None:
        return None
    if rule.contains is not None and rule.contains.encode("utf-8") not in body:
        return "body mismatch"
    if rule.regex is not None and not _compiled_regex(rule.regex).search(
        body.decode("utf-8", errors="replace")
    ):
        return "body mismatch"
    return None


def _check_cookies(
    received: tuple[tuple[str, str], ...], expect: ExpectSpec
) -> str | None:
    if not expect.cookies:
        return None
    jar = SimpleCookie()
    for name, value in received:
        if name.lower() == "set-cookie":
            jar.load(value)
    for rule in expect.cookies:
        morsel = jar.get(rule.name)
        if morsel is None:
            return f"cookie {rule.name} missing"
        if rule.equals is not None and morsel.value != rule.equals:
            return f"cookie {rule.name} mismatch"
    return None


class _TunneledConnection(http.client.HTTPConnection):
    """HTTPConnection that speaks over an already-connected tunnel socket
    instead of opening its own. The socket already carries the probe timeout
    from connect_upstream, so connect() only adopts it."""

    def __init__(self, sock: socket.socket, host: str, port: int) -> None:
        super().__init__(host, port)
        self._tunnel = sock

    def connect(self) -> None:
        self.sock = self._tunnel


def _tls_context(cafile: str | None) -> ssl.SSLContext:
    if cafile is None:
        return _TLS_CONTEXT
    context = ssl.create_default_context(cafile=cafile)
    return context


def probe_over_socket(
    sock: socket.socket, probe: ProbeSpec, timeout_s: float
) -> ProbeResponse:
    """Run one probe exchange over a connected socket. Transport failures
    surface as OSError so callers share a single failure type; HTTP-level
    verdicts are the caller's job, not this function's."""
    stream: socket.socket = sock
    if probe.use_tls:
        stream = _tls_context(probe.tls_cafile).wrap_socket(
            sock, server_hostname=probe.host
        )
    conn = _TunneledConnection(stream, probe.host, probe.port)
    try:
        sent: dict[str, str] = dict(probe.headers)
        if probe.cookies:
            sent["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in probe.cookies
            )
        try:
            conn.request(
                probe.method, probe.path, body=probe.body or None, headers=sent
            )
            response = conn.getresponse()
            body = response.read(MAX_BODY_BYTES + 1)[:MAX_BODY_BYTES]
            headers = tuple(response.getheaders())
            status = response.status
        except http.client.HTTPException as exc:
            raise OSError(f"exchange failed: {exc}") from exc
        return ProbeResponse(status=status, headers=headers, body=body)
    finally:
        conn.close()


def probe_node(
    node: Mapping[str, object],
    probe: ProbeSpec,
    expect: ExpectSpec,
    timeout_s: float,
) -> ProbeResult:
    """Full verdict for one proxy node: open the tunnel, run the exchange,
    match the response. Anything the prober cannot speak is skipped."""
    kind = node.get("type")
    if kind not in ("http", "socks5"):
        return ProbeResult(SKIPPED, None, 0, f"unsupported probe type: {kind}")
    started = time.monotonic()
    try:
        sock = connect_upstream(
            cast(dict[str, object], node), probe.host, probe.port, timeout_s
        )
    except (OSError, ValueError) as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return ProbeResult(DEAD, None, elapsed, f"tunnel: {exc}")
    try:
        response = probe_over_socket(sock, probe, timeout_s)
    except OSError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return ProbeResult(DEAD, None, elapsed, str(exc))
    elapsed = int((time.monotonic() - started) * 1000)
    for failure in (
        _check_status(response.status, expect),
        _check_headers(response.headers, expect),
        _check_body(response.body, expect),
        _check_cookies(response.headers, expect),
    ):
        if failure is not None:
            return ProbeResult(DEAD, response.status, elapsed, failure)
    if expect.max_latency_ms is not None and elapsed > expect.max_latency_ms:
        return ProbeResult(
            DEAD, response.status, elapsed, f"latency {elapsed}ms over limit"
        )
    return ProbeResult(LIVE, response.status, elapsed, "ok")


__all__ = [
    "LIVE",
    "DEAD",
    "SKIPPED",
    "BodyExpect",
    "CookieRule",
    "ExpectSpec",
    "HeaderRule",
    "ProbeResponse",
    "ProbeResult",
    "ProbeSpec",
    "parse_expect_spec",
    "parse_probe_spec",
    "probe_node",
    "probe_over_socket",
]
