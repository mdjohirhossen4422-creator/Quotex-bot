"""Quotex OTC live M1 candle store."""

import os
import sqlite3
import threading
from pathlib import Path

from market_qx_candles import MarketQX, CandleBuilder

try:
    from provenance import FEED_ID, classify_source
except ImportError:
    FEED_ID = "market-qx-live"

    def classify_source(*args, **kwargs):
        return "live"


TIMEFRAMES = [(60, "1m")]
PERIODS = [60]

DB_PATH = os.environ.get("CANDLE_DB", "data/candles.db")


def is_otc(symbol, info=None):
    info = info or {}
    return (
        str(symbol).lower().endswith("_otc")
        or "otc" in str(info.get("type", "")).lower()
    )


def otc_assets(instruments):
    return [
        symbol
        for symbol, info in instruments.items()
        if is_otc(symbol, info)
    ]


def asset_catalog(instruments, assets=None):
    selected = list(assets) if assets is not None else otc_assets(instruments)
    groups = {}

    for symbol in selected:
        category = str(instruments.get(symbol, {}).get("type", "OTC"))
        groups.setdefault(category, []).append(symbol)

    return [
        {"type": name, "symbols": sorted(symbols)}
        for name, symbols in sorted(groups.items())
    ]


class CandleStore:
    def __init__(self, path=DB_PATH):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

        self.db = sqlite3.connect(
            self.path,
            check_same_thread=False,
            timeout=30,
        )

        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")

        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS candles (
            asset TEXT NOT NULL,
            period INTEGER NOT NULL,
            ts INTEGER NOT NULL,
            open REAL NOT NULL,
            close REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            source TEXT DEFAULT 'live',
            feed_id TEXT DEFAULT '',
            PRIMARY KEY(asset, period, ts)
        );

        CREATE TABLE IF NOT EXISTS ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL,
            ts REAL NOT NULL,
            price REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_candles
        ON candles(asset, period, ts);

        CREATE INDEX IF NOT EXISTS idx_ticks
        ON ticks(asset, ts);
        """)

        columns = {
            row[1]
            for row in self.db.execute(
                "PRAGMA table_info(candles)"
            ).fetchall()
        }

        if "source" not in columns:
            self.db.execute(
                "ALTER TABLE candles ADD COLUMN source TEXT DEFAULT 'live'"
            )

        if "feed_id" not in columns:
            self.db.execute(
                "ALTER TABLE candles ADD COLUMN feed_id TEXT DEFAULT ''"
            )

        self.db.commit()

    def save_candle(self, asset, period, candle):
        ts, open_, close, high, low = candle

        with self.lock:
            self.db.execute("""
                INSERT OR REPLACE INTO candles
                (asset, period, ts, open, close, high, low, source, feed_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                asset,
                int(period),
                int(ts),
                float(open_),
                float(close),
                float(high),
                float(low),
                str(classify_source()),
                str(FEED_ID),
            ))
            self.db.commit()

    def add_tick(self, asset, ts, price):
        with self.lock:
            self.db.execute("""
                INSERT INTO ticks(asset, ts, price)
                VALUES (?, ?, ?)
            """, (asset, float(ts), float(price)))
            self.db.commit()

    def get_candles(self, asset, period=60, limit=200):
        with self.lock:
            rows = self.db.execute("""
                SELECT ts, open, close, high, low
                FROM candles
                WHERE asset=? AND period=?
                ORDER BY ts DESC
                LIMIT ?
            """, (asset, int(period), int(limit))).fetchall()

        return [list(row) for row in reversed(rows)]

    def get(self, asset, period=60, limit=200):
        return self.get_candles(asset, period, limit)

    def latest(self, asset, period=60):
        rows = self.get_candles(asset, period, 1)
        return rows[-1] if rows else None

    def get_assets(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT DISTINCT asset FROM candles ORDER BY asset"
            ).fetchall()
        return [row[0] for row in rows]

    @property
    def assets(self):
        return self.get_assets()

    @property
    def total(self):
        with self.lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM candles"
            ).fetchone()[0]

    def count(self):
        return self.total

    def close(self):
        with self.lock:
            self.db.commit()
            self.db.close()


class MultiFeed:
    def __init__(
        self,
        assets=None,
        periods=None,
        store=None,
        wait_instruments=75,
        verbose=False,
        **kwargs,
    ):
        self.store = store or CandleStore()
        self.periods = list(periods or PERIODS)
        self.closed = False

        self.qx = MarketQX(verbose=verbose).connect(
            wait_instruments=wait_instruments
        )

        self.instruments = self.qx.instruments

        if assets:
            self.assets = [
                a for a in assets
                if a in self.instruments
            ]
        else:
            self.assets = otc_assets(self.instruments)

        if not self.assets:
            self.qx.close()
            raise RuntimeError(
                "No OTC pairs found in MarketQX instrument list."
            )

        self.builders = {}

        for asset in self.assets:
            self.builders[asset] = {}

            for period in self.periods:
                builder = CandleBuilder(
                    period,
                    on_closed=lambda candle, a=asset, p=period:
                        self._save_closed(a, p, candle),
                )
                self.builders[asset][period] = builder

        self.qx.on_tick(self._on_tick)

        for asset in self.assets:
            self.qx.follow(asset, self.periods[0])

        print(
            "MarketQX connected:",
            len(self.assets),
            "OTC pairs",
            flush=True,
        )

    def _save_closed(self, asset, period, candle):
        try:
            self.store.save_candle(asset, period, candle)
        except Exception as exc:
            print("Candle save error:", repr(exc), flush=True)

    def _on_tick(self, asset, ts, price, direction=0):
        if self.closed or asset not in self.builders:
            return

        try:
            self.store.add_tick(asset, ts, price)

            for period, builder in self.builders[asset].items():
                builder.add(ts, price)

        except Exception as exc:
            print("Tick processing error:", repr(exc), flush=True)

    def get_candles(self, asset, period=60, limit=200):
        saved = self.store.get_candles(asset, period, limit)
        builder = self.builders.get(asset, {}).get(period)

        if builder and builder.forming:
            current = list(builder.forming)

            if not saved or saved[-1][0] != current[0]:
                saved.append(current)
            else:
                saved[-1] = current

        return saved[-limit:]

    def close(self):
        if self.closed:
            return

        self.closed = True

        try:
            self.qx.close()
        except Exception:
            pass