"""SQLite layer for Weekend — votes (preference signal) + stars (family signal).

Schema notes:
- `votes`: persistent across weeks for taste learning (8-week rolling window in
  the cron preferences query). Each row is a single up/down by a single voter
  on a single item. Latest row wins (no de-dup at write time; aggregate at read).
- `stars`: scoped to a weekend. When a new week drops on Wednesday, old stars
  become stale by date filter — we don't delete them, just don't surface them.
  Lazy cleanup runs at startup to keep the table from growing forever.

VALID_VOTERS is enforced server-side so the URL can't claim arbitrary names.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path("/app/var/weekend.db")

VALID_VOTERS = {"John", "Tonia", "Logan", "Hailey", "Faith"}
VALID_DIRECTIONS = {"up", "down"}


def init_db() -> None:
    """Create tables if missing. Idempotent. Called once at startup."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('up','down')),
                voter_tag TEXT NOT NULL,
                weekend_start TEXT NOT NULL,
                ts TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS votes_item ON votes(item_id);
            CREATE INDEX IF NOT EXISTS votes_weekend ON votes(weekend_start);

            CREATE TABLE IF NOT EXISTS stars (
                item_id TEXT NOT NULL,
                voter_tag TEXT NOT NULL,
                weekend_start TEXT NOT NULL,
                starred_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (item_id, voter_tag, weekend_start)
            );
            CREATE INDEX IF NOT EXISTS stars_weekend ON stars(weekend_start);
            """
        )


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Per-call SQLite connection. SQLite handles concurrent reads fine and
    our write volume is trivial (a household)."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ----- writes -----

def record_vote(item_id: str, direction: str, voter_tag: str, weekend_start: str) -> None:
    """Upsert latest vote per (voter, item, weekend). Old votes from same voter
    on same item get removed first so /api/state always reflects current choice."""
    if voter_tag not in VALID_VOTERS:
        raise ValueError(f"Unknown voter: {voter_tag}")
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"Invalid direction: {direction}")
    with _conn() as c:
        c.execute(
            "DELETE FROM votes WHERE item_id=? AND voter_tag=? AND weekend_start=?",
            (item_id, voter_tag, weekend_start),
        )
        c.execute(
            "INSERT INTO votes (item_id, direction, voter_tag, weekend_start) VALUES (?,?,?,?)",
            (item_id, direction, voter_tag, weekend_start),
        )


def clear_vote(item_id: str, voter_tag: str, weekend_start: str) -> None:
    if voter_tag not in VALID_VOTERS:
        raise ValueError(f"Unknown voter: {voter_tag}")
    with _conn() as c:
        c.execute(
            "DELETE FROM votes WHERE item_id=? AND voter_tag=? AND weekend_start=?",
            (item_id, voter_tag, weekend_start),
        )


def set_star(item_id: str, voter_tag: str, weekend_start: str) -> None:
    if voter_tag not in VALID_VOTERS:
        raise ValueError(f"Unknown voter: {voter_tag}")
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO stars (item_id, voter_tag, weekend_start) VALUES (?,?,?)",
            (item_id, voter_tag, weekend_start),
        )


def clear_star(item_id: str, voter_tag: str, weekend_start: str) -> None:
    if voter_tag not in VALID_VOTERS:
        raise ValueError(f"Unknown voter: {voter_tag}")
    with _conn() as c:
        c.execute(
            "DELETE FROM stars WHERE item_id=? AND voter_tag=? AND weekend_start=?",
            (item_id, voter_tag, weekend_start),
        )


# ----- reads -----

def get_state(weekend_start: str) -> dict:
    """Return per-item current state for this weekend:
       {item_id: {"up": int, "down": int, "stars": ["John","Tonia"], "votes_by": {"John":"up"}}}
    """
    out: dict = {}
    with _conn() as c:
        for row in c.execute(
            "SELECT item_id, direction, voter_tag FROM votes WHERE weekend_start=?",
            (weekend_start,),
        ):
            entry = out.setdefault(row["item_id"], {"up": 0, "down": 0, "stars": [], "votes_by": {}})
            entry[row["direction"]] += 1
            entry["votes_by"][row["voter_tag"]] = row["direction"]
        for row in c.execute(
            "SELECT item_id, voter_tag FROM stars WHERE weekend_start=? ORDER BY starred_at",
            (weekend_start,),
        ):
            entry = out.setdefault(row["item_id"], {"up": 0, "down": 0, "stars": [], "votes_by": {}})
            if row["voter_tag"] not in entry["stars"]:
                entry["stars"].append(row["voter_tag"])
    return out


def get_preferences(weeks: int = 8) -> dict:
    """Rolling-window summary for the Wednesday cron to ingest.
    Aggregates up/down counts per item across the last N weeks.
    """
    with _conn() as c:
        rows = c.execute(
            """
            SELECT item_id, direction, COUNT(*) AS n
            FROM votes
            WHERE date(weekend_start) >= date('now', ?)
            GROUP BY item_id, direction
            """,
            (f"-{weeks * 7} days",),
        ).fetchall()
    summary: dict = {}
    for r in rows:
        summary.setdefault(r["item_id"], {"up": 0, "down": 0})[r["direction"]] = r["n"]
    return summary


def purge_old_stars(keep_weekend_start: str) -> int:
    """Lazy cleanup — delete any stars not from the current weekend. Returns rows deleted."""
    with _conn() as c:
        cur = c.execute("DELETE FROM stars WHERE weekend_start != ?", (keep_weekend_start,))
        return cur.rowcount
