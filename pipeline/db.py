"""Read-only access to the source store, with chunked tick streaming.

The source database is opened with `mode=ro` and every query is a SELECT, so the
pipeline physically cannot modify it. Ticks are streamed in chunks so a 1.7M-row
table never has to sit in RAM at once.
"""
from __future__ import annotations

import sqlite3
from typing import Iterator, Optional


def connect(path: str) -> sqlite3.Connection:
    """Open the source database strictly read-only."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.execute("PRAGMA query_only=1")
    return db


def list_assets(db: sqlite3.Connection, market: Optional[str] = None) -> list[str]:
    rows = db.execute("SELECT DISTINCT asset FROM candles ORDER BY asset").fetchall()
    assets = [r[0] for r in rows]
    if market == "otc":
        return [a for a in assets if a.endswith("_otc")]
    if market == "real":
        return [a for a in assets if not a.endswith("_otc")]
    return assets


def assets_with_ticks(db: sqlite3.Connection, market: Optional[str] = None) -> list[str]:
    rows = db.execute("SELECT DISTINCT asset FROM ticks ORDER BY asset").fetchall()
    assets = [r[0] for r in rows]
    if market == "otc":
        return [a for a in assets if a.endswith("_otc")]
    if market == "real":
        return [a for a in assets if not a.endswith("_otc")]
    return assets


def iter_ticks(db: sqlite3.Connection, asset: str, chunk: int = 200_000
               ) -> Iterator[list[tuple[float, float, int]]]:
    """Yield (ts, price, dir) for one asset in chronological chunks.

    Uses a keyset cursor (`ts > last_ts`) so it never re-scans, and orders by
    (ts, rowid) to keep the true arrival order for ties within a millisecond.
    """
    last_ts = -1.0
    last_row = -1
    while True:
        rows = db.execute(
            "SELECT rowid, ts, price, dir FROM ticks "
            "WHERE asset=? AND (ts>? OR (ts=? AND rowid>?)) "
            "ORDER BY ts, rowid LIMIT ?",
            (asset, last_ts, last_ts, last_row, chunk)).fetchall()
        if not rows:
            break
        out = [(r[1], r[2], r[3]) for r in rows]
        last_ts, last_row = rows[-1][1], rows[-1][0]
        yield out
        if len(rows) < chunk:
            break


def load_candles(db: sqlite3.Connection, asset: str, period: int,
                 limit: Optional[int] = None) -> list[list]:
    """Stored candles as [time, open, close, high, low] (chronological)."""
    sql = ("SELECT time,open,close,high,low FROM candles "
           "WHERE asset=? AND period=? ORDER BY time")
    if limit:
        rows = db.execute(sql + " DESC LIMIT ?", (asset, period, limit)).fetchall()
        return [list(r) for r in reversed(rows)]
    return [list(r) for r in db.execute(sql, (asset, period)).fetchall()]


def tick_span(db: sqlite3.Connection, asset: str) -> tuple[float, float, int]:
    r = db.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM ticks WHERE asset=?",
                   (asset,)).fetchone()
    return (r[0] or 0.0, r[1] or 0.0, r[2] or 0)


def source_fingerprint(db: sqlite3.Connection) -> dict:
    """A cheap identity for the source data, for the metadata sidecar."""
    c = db.execute("SELECT COUNT(*), MIN(time), MAX(time) FROM candles").fetchone()
    t = db.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM ticks").fetchone()
    return {"candles": c[0], "candles_min": c[1], "candles_max": c[2],
            "ticks": t[0], "ticks_min": t[1], "ticks_max": t[2]}
