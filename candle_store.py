usr/bin/env python3
"""Quotex MarketQX multi-pair live candle store."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import defaultdict
from typing import Optional, Callable

from market_qx_candles import CandleBuilder, MarketQX
from provenance import FEED_ID, classify_source


TIMEFRAMES = [
    (60, "1m"),
]

PERIODS = [60]


def asset_catalog(
    instruments: dict,
    assets: Optional[list[str]] = None
) -> list[dict]:
    """Group available instruments for the dashboard."""

    groups = defaultdict(list)

    if instruments:
        for symbol, info in instruments.items():
            info = info or {}
            groups[info.get("type", "other")].append(symbol)
    else:
        for symbol in assets or []:
            groups["other"].append(symbol)

    return [
        {"type": kind, "symbols": sorted(symbols)}
        for kind, symbols in sorted(groups.items())
        if symbols
    ]


class CandleStore:
    """SQLite-backed candle and tick storage."""

    def __init__(self, path="data/candles.db"):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            path, check_same_thread=False
        )

        self._db.execute("PRAGMA journal_mode=WAL")

        self._db.execute("""
            CREATE TABLE IF NOT EXISTS candles(
                asset TEXT,
                period INTEGER,
                time INTEGER,
                open REAL,
                close REAL,
                high REAL,
                low REAL,
                source TEXT,
                feed_id TEXT,
                PRIMARY KEY(asset,period,time)
            )
        """)

        self._db.execute("""
            CREATE TABLE IF NOT EXISTS ticks(
                asset TEXT,
                ts REAL,
                price REAL,
                dir INTEGER,
                source TEXT,
                feed_id TEXT
            )
        """)

        self._db.execute("""
            CREATE INDEX IF NOT EXISTS ticks_asset_ts
            ON ticks(asset,ts)
        """)

        self._db.commit()

        self._total = self._db.execute(
            "SELECT COUNT(*) FROM candles"
        ).fetchone()[0]

    def save(
        self,
        asset,
        period,
        candles,
        source=None,
        feed_id=FEED_ID
    ):
        src = source or classify_source(asset)

        rows = [
            (
                asset, period,
                c[0], c[1], c[2], c[3], c[4],
                src, feed_id
            )
            for c in candles
        ]

        if not rows:
            return

        with self._lock:
            cur = self._db.executemany("""
                INSERT OR IGNORE INTO candles
                (asset,period,time,open,close,high,low,source,feed_id)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, rows)

            self._db.commit()

            if cur.rowcount and cur.rowcount > 0:
                self._total += cur.rowcount

    def save_ticks(self, rows):
        values = [
            (
                r[0], r[1], r[2], r[3],
                r[4] if len(r) > 4 else classify_source(r[0]),
                r[5] if len(r) > 5 else FEED_ID
            )
            for r in rows
        ]

        if not values:
            return

        with self._lock:
            self._db.executemany("""
                INSERT INTO ticks
                (asset,ts,price,dir,source,feed_id)
                VALUES(?,?,?,?,?,?)
            """, values)

            self._db.commit()

    @property
    def total(self):
        return self._total

    def load(self, asset, period, limit=5000):
        with self._lock:
            rows = self._db.execute("""
                SELECT time,open,close,high,low
                FROM candles
                WHERE asset=? AND period=?
                ORDER BY time DESC LIMIT ?
            """, (asset, period, limit)).fetchall()

        return [list(r) for r in reversed(rows)]

    def count(self, asset, period):
        with self._lock:
            return self._db.execute("""
                SELECT COUNT(*) FROM candles
                WHERE asset=? AND period=?
            """, (asset, period)).fetchone()[0]

    def assets(self):
        with self._lock:
            rows = self._db.execute("""
                SELECT DISTINCT asset FROM candles
                ORDER BY asset
            """).fetchall()

        return [r[0] for r in rows]

    def tick_assets(self):
        with self._lock:
            rows = self._db.execute("""
                SELECT DISTINCT asset FROM ticks
                ORDER BY asset
            """).fetchall()

        return [r[0] for r in rows]

    def tick_count(self, asset=None):
        with self._lock:
            if asset:
                return self._db.execute(
                    "SELECT COUNT(*) FROM ticks WHERE asset=?",
                    (asset,)
                ).fetchone()[0]

            return self._db.execute(
                "SELECT COUNT(*) FROM ticks"
            ).fetchone()[0]

    def load_ticks(self, asset, limit=100000):
        with self._lock:
            rows = self._db.execute("""
                SELECT ts,price,dir
                FROM ticks
                WHERE asset=?
                ORDER BY ts DESC LIMIT ?
            """, (asset, limit)).fetchall()

        return [tuple(r) for r in reversed(rows)]

    def series(self, min_candles=1, periods=None):
        sql = """
            SELECT asset,period,COUNT(*) c
            FROM candles
            GROUP BY asset,period
            HAVING c>=?
        """

        params = [min_candles]

        if periods:
            periods = list(periods)
            sql += " AND period IN (%s)" % (
                ",".join("?" for _ in periods)
            )
            params.extend(periods)

        with self._lock:
            rows = self._db.execute(
                sql + " ORDER BY asset,period",
                params
            ).fetchall()

        return [(r[0], r[1]) for r in rows]

    def close(self):
        with self._lock:
            self._db.close()


class MultiFeed:
    """One MarketQX connection for multiple assets and timeframes."""

    def __init__(
        self,
        assets,
        periods,
        store,
        on_closed: Optional[Callable] = None,
        on_tick: Optional[Callable] = None,
        wait_instruments=75.0,
        retries=3
    ):
        self.store = store
        self.periods = list(periods)
        self.on_closed = on_closed
        self.on_tick = on_tick

        self.lock = threading.Lock()
        self.instruments = {}
        self.last_price = {}
        self.connected = False
        self._closed = False

        self._tick_buf = []
        self._tick_lock = threading.Lock()
        self.ticks_saved = 0

        self.qx = None

        for attempt in range(retries):
            qx = MarketQX().connect(
                wait_instruments=wait_instruments
            )

            if qx.instruments:
                self.qx = qx
                break

            qx.close()

            if attempt < retries - 1:
                time.sleep(3)

        if self.qx is None:
            self.qx = MarketQX().connect(
                wait_instruments=wait_instruments
            )

        self.instruments = dict(
            self.qx.instruments or {}
        )

        discovered = sorted(self.instruments.keys())

        if assets:
            self.assets = list(assets)
        else:
            self.assets = discovered

        if not self.assets:
            self.assets = store.assets()

        self._asset_set = set(self.assets)

        self.history = defaultdict(list)
        self._builders = {}

        for asset in self.assets:
            for period in self.periods:
                self.history[(asset, period)] = store.load(
                    asset, period, limit=5000
                )

                self._builders[(asset, period)] = CandleBuilder(
                    period,
                    on_closed=self._callback(asset, period)
                )

        self._flusher = threading.Thread(
            target=self._flush_loop,
            daemon=True
        )
        self._flusher.start()

        self.qx.on_tick(self._on_tick)

        for asset in self.assets:
            self.qx.follow(asset, self.periods[0])

        self.connected = True

        print(
            "MarketQX connected:",
            len(self.assets),
            "assets",
            flush=True
        )

    def _callback(self, asset, period):
        def handler(candle):
            with self.lock:
                history = self.history[(asset, period)]
                history.append(candle)

                if len(history) > 20000:
                    del history[:5000]

            self.store.save(asset, period, [candle])

            if self.on_closed:
                self.on_closed(asset, period, candle)

        return handler

    def _on_tick(self, asset, ts, price, direction=None):
        if asset not in self._asset_set:
            return

        self.last_price[asset] = price

        try:
            direction = int(direction)
        except (TypeError, ValueError):
            direction = 0

        with self._tick_lock:
            self._tick_buf.append((
                asset,
                float(ts),
                float(price),
                direction
            ))

        for period in self.periods:
            builder = self._builders.get((asset, period))
            if builder:
                builder.add(ts, price)

        if self.on_tick:
            self.on_tick(asset, ts, price)

    def _flush_loop(self):
        while not self._closed:
            time.sleep(5)

            with self._tick_lock:
                if not self._tick_buf:
                    continue

                batch = self._tick_buf
                self._tick_buf = []

            try:
                self.store.save_ticks(batch)
                self.ticks_saved += len(batch)
            except Exception as exc:
                print("TICK SAVE ERROR:", exc, flush=True)

    def candles(self, asset, period):
        with self.lock:
            return list(self.history.get((asset, period), []))

    def forming(self, asset, period):
        builder = self._builders.get((asset, period))
        return builder.forming if builder else None

    def close(self):
        self._closed = True
        self.connected = False

        with self._tick_lock:
            batch = self._tick_buf
            self._tick_buf = []

        if batch:
            self.store.save_ticks(batch)

        if self.qx:
            self.qx.close()