"""One log format, one correlation-ID mechanism, one redaction helper.

Every tppr service configures logging through ``configure_logging`` and threads
a correlation ID per unit of work (an HTTP request, a gateway connection, a
refresh cycle, a probe round, a bot update) via ``bound_correlation_id``, so any
line can be traced back to the unit that produced it.

Stdlib only. ``common/__init__.py`` is empty, so importing this module does not
pull in probe/upstream and stays importable from every service image.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import logging.handlers
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

# The one format. ``service`` names the process; ``rid`` is the correlation id
# of the unit of work the line belongs to ("-" when none is bound).
LOG_FORMAT = "%(asctime)s level=%(levelname)s service=%(name)s rid=%(rid)s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
MAX_FILE_BYTES = 10_485_760
LOG_BACKUP_COUNT = 5

# One correlation id per unit of work: set at the entry point, read by
# _CorrelationFilter on every record emitted inside `bound_correlation_id`.
_current_rid: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tppr_correlation_id", default="-"
)


class _CorrelationFilter(logging.Filter):
    """Stamp the current correlation id onto every record.

    Attached to handlers (not loggers): a handler filter runs for every record
    that reaches the handler, whichever logger emitted it, so ``%(rid)s`` is
    always present when the format runs. The record carries a dynamic `rid`
    attribute, written through ``__dict__`` (the LogRecord idiom mypy accepts).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.__dict__["rid"] = _current_rid.get()
        return True


@contextmanager
def bound_correlation_id(value: str | None = None) -> Iterator[str]:
    """Bind a correlation id for the duration of one unit of work.

    Generates a short id when none is supplied; restores the previous id on exit
    so a nested unit keeps its parent's id after the inner one ends.
    """
    rid = value if value is not None else uuid.uuid4().hex[:12]
    token = _current_rid.set(rid)
    try:
        yield rid
    finally:
        _current_rid.reset(token)


def current_correlation_id() -> str:
    """The correlation id bound right now, or "-" outside any unit of work."""
    return _current_rid.get()


def redact_account(account: str, keep: int = 8) -> str:
    """A short, non-identifying label for an account/user in a log line.

    Keeps a stable prefix so an operator can still group lines by account
    without the log carrying a full account identifier.
    """
    if not account:
        return "-"
    # Collapse control chars so a hostile identifier cannot forge log lines.
    safe = "".join(ch if ch.isprintable() else "?" for ch in account)
    return f"{safe[:keep]}..."


def redact_path(path: str) -> str:
    """Keep the resource type in a request path, mask identifier segments.

    ``/accounts/opencode-acc`` becomes ``/accounts/...`` so an access log shows
    which endpoint was hit without carrying an account or lease identifier.
    """
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return path
    return "/" + segments[0] + ("/..." if len(segments) > 1 else "")


def node_token(*parts: object) -> str:
    """Stable 8-hex token identifying a node without emitting proxy material.

    Hashes the node's identity (e.g. kind/server/port) so a log line can say
    "node a1b2c3d4 was chosen / failed" without ever printing a server, port or
    credential.
    """
    joined = "|".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:8]


def configure_logging(service_name: str, level: int = logging.INFO) -> logging.Logger:
    """Set up the root logger with the one format and the correlation filter.

    Adds a stdout handler and, when ``LOG_FILE`` is set, a rotating file handler
    (the recreate-proof copy). Both carry the correlation filter. Returns the
    named service logger to emit through.
    """
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    correlation = _CorrelationFilter()

    root = logging.getLogger()
    root.setLevel(level)
    # Idempotent: clear handlers a previous call (or basicConfig) installed.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(correlation)
    root.addHandler(stream)

    log_file = os.environ.get("LOG_FILE", "").strip()
    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=MAX_FILE_BYTES, backupCount=LOG_BACKUP_COUNT
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(correlation)
        root.addHandler(file_handler)

    return logging.getLogger(service_name)
