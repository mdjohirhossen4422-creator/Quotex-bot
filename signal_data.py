#!/usr/bin/env python3
"""Read-only data access for the AI Signal Engine.

Loads candles **with their tick summaries** and higher-timeframe context from the
shared store, and keeps real and OTC strictly separated — their datasets are
never pooled, trained together or scored together (the brief is explicit on
this).

The source DB is opened with `mode=ro`, so this module physically cannot modify
the collector's data.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from provenance import classify_source
from signal_features import MTFContext
from tick_summary import SUMMARY_FIELDS

# candle row layout used everywhere in this package:
# [time, open, close, high, low, <summary...>]
CANDLE_COLS = "time,open,close,high,low," + ",".join(SUMMARY_FIELDS)


def _summary_from_row(row: tuple, offset: int = 5) -> Optional[dict]:
    if row[offset] is None:
        return None
    return {name: row[offset + k] for k, name in enumerate(SUMMARY_FIELDS)}


class SignalData:
    """Read-only view over the candle store, split by source."""

    def __init__(self, db_path: str = "data/candles.db"):
        self.db_path = db_path
        self._db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                                   check_same_thread=False)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(candles)")}
        self._has_summaries = all(c in cols for c in SUMMARY_FIELDS)

    def close(self):
        self._db.close()

    # ---- series discovery ------------------------------------------------- #
    def assets(self, source: str, period: int, min_candles: int = 1) -> list[str]:
        rows = self._db.execute(
            "SELECT asset, COUNT(*) c FROM candles WHERE period=? "
            "GROUP BY asset HAVING c >= ? ORDER BY asset",
            (period, min_candles)).fetchall()
        return [a for a, _ in rows if classify_source(a) == source]

    def count(self, asset: str, period: int) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM candles WHERE asset=? AND period=?",
            (asset, period)).fetchone()[0]

    def load(self, asset: str, period: int, limit: int = 0) -> list[list]:
        """Candles (with summary columns) ascending by time."""
        sel = f"time,open,close,high,low"
        if self._has_summaries:
            sel = CANDLE_COLS
        sql = (f"SELECT {sel} FROM candles WHERE asset=? AND period=? "
               f"ORDER BY time DESC")
        params: tuple = (asset, period)
        if limit:
            sql += " LIMIT ?"
            params = (asset, period, limit)
        rows = self._db.execute(sql, params).fetchall()
        return [list(r) for r in reversed(rows)]

    def summaries(self, asset: str, period: int, limit: int = 0
                  ) -> list[Optional[dict]]:
        """One summary dict (or None) per candle, aligned with `load`."""
        rows = self.load(asset, period, limit)
        if not self._has_summaries:
            return [None] * len(rows)
        return [_summary_from_row(tuple(r)) for r in rows]

    def load_with_mtf(self, asset: str, period: int, mtf_period: int,
                      limit: int = 0):
        """(candles, summaries, MTFContext) for one series.

        The MTF context is built from the higher-timeframe candles of the SAME
        asset. If the higher series does not exist yet, the context is empty and
        its features are zero — nothing is fabricated.
        """
        candles = self.load(asset, period, limit)
        if self._has_summaries:
            summaries = [_summary_from_row(tuple(r)) for r in candles]
        else:
            summaries = [None] * len(candles)
        higher = self.load(asset, mtf_period, limit) if mtf_period else []
        mtf = MTFContext(higher, mtf_period) if mtf_period and higher else None
        return candles, summaries, mtf

    def usable_summary_fraction(self, asset: str, period: int) -> float:
        rows = self.load(asset, period)
        if not rows:
            return 0.0
        have = sum(1 for r in rows if r[5] is not None)
        return have / len(rows)

    def describe(self) -> str:
        out = [f"db={self.db_path} summaries={'yes' if self._has_summaries else 'no'}"]
        for src in ("otc", "real", "synthetic"):
            for p in (60, 300, 900, 1800):
                a = self.assets(src, p)
                if a:
                    out.append(f"  {src:9} {p:>5}s : {len(a)} pairs")
        return "\n".join(out)
