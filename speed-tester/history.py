"""SQLite history of throughput sweeps (stdlib sqlite3 only).

Single responsibility: persist per-node probe results and answer two questions —
which nodes sit in cooldown after consecutive recent failures, and which known
node was fastest in the recent window. Every failure raises loudly; the caller
decides what to do about it.
"""

import sqlite3
import time

# Top-level imports only; sqlite3 is stdlib with negligible import cost.

SCHEMA = """
CREATE TABLE IF NOT EXISTS sweeps (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    started_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY,
    sweep_id INTEGER NOT NULL REFERENCES sweeps(id),
    node TEXT NOT NULL,
    kbps REAL NOT NULL,
    ok INTEGER NOT NULL,
    measured_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_measurements_node ON measurements(node, sweep_id);
"""


class History:
    """Probe-result store backed by one SQLite file."""

    def __init__(self, path: str) -> None:
        try:
            self._db = sqlite3.connect(path)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
        except sqlite3.Error as exc:
            raise RuntimeError(f"cannot open history db {path!r}: {exc}") from exc

    def record_sweep(self, run_id: str, results: dict[str, float]) -> None:
        now = int(time.time())
        try:
            cursor = self._db.execute(
                "INSERT INTO sweeps (run_id, started_at) VALUES (?, ?)", (run_id, now)
            )
            sweep_id = cursor.lastrowid
            self._db.executemany(
                "INSERT INTO measurements (sweep_id, node, kbps, ok, measured_at)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (sweep_id, node, speed, 1 if speed > 0 else 0, now)
                    for node, speed in results.items()
                ],
            )
            self._db.commit()
        except sqlite3.Error as exc:
            raise RuntimeError(f"cannot record sweep: {exc}") from exc

    def consecutive_failures(self, node: str, limit: int, window_h: int) -> int:
        """Trailing failures, or 0 when the latest probe is older than the window.

        The staleness check is what heals cooldown: a skipped node gets no new
        rows, its failures age out, and it becomes eligible for retry.
        """
        try:
            rows = self._db.execute(
                "SELECT ok, measured_at FROM measurements"
                " WHERE node = ? ORDER BY id DESC LIMIT ?",
                (node, limit),
            ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError(f"cannot read history: {exc}") from exc
        if not rows or rows[0][1] < int(time.time()) - window_h * 3600:
            return 0
        fails = 0
        for ok, _ in rows:
            if ok:
                break
            fails += 1
        return fails

    def best_node(self, nodes: list[str], window_h: int) -> str | None:
        """Node with the highest median throughput in the window, or None."""
        if not nodes:
            return None
        cutoff = int(time.time()) - window_h * 3600
        placeholders = ",".join("?" for _ in nodes)
        try:
            rows = self._db.execute(
                "SELECT node, kbps FROM measurements"
                f" WHERE node IN ({placeholders}) AND ok = 1 AND measured_at >= ?",
                (*nodes, cutoff),
            ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError(f"cannot read history: {exc}") from exc
        by_node: dict[str, list[float]] = {}
        for node, kbps in rows:
            by_node.setdefault(node, []).append(kbps)
        best: str | None = None
        best_median = -1.0
        for node, speeds in by_node.items():
            ordered = sorted(speeds)
            median = ordered[len(ordered) // 2]
            if median > best_median:
                best_median = median
                best = node
        return best

    def prune(self, retention_h: int) -> None:
        """Delete measurements older than the retention window (plus orphans)."""
        cutoff = int(time.time()) - retention_h * 3600
        try:
            self._db.execute("DELETE FROM measurements WHERE measured_at < ?", (cutoff,))
            self._db.execute(
                "DELETE FROM sweeps WHERE id NOT IN (SELECT DISTINCT sweep_id FROM measurements)"
            )
            self._db.commit()
        except sqlite3.Error as exc:
            raise RuntimeError(f"cannot prune history: {exc}") from exc

    def close(self) -> None:
        self._db.close()
