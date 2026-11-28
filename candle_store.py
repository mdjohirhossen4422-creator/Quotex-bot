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

    def classify_source(symbol):
        return "live"


PERIODS = [60]
TIMEFRAMES = [(60, "1m")]

DB_PATH = os.environ.get("CANDLE_DB", "data/candles.db")


def is_otc(symbol, info=None):
    info = info or {}

    return (
        str(symbol).lower().endswith("_otc")
        or "otc" in str(info.get("type", "")).lower()
        or "otc" in str(info.get("name", "")).lower()
    )


def otc_assets(instruments):
    return sorted(
        symbol
        for symbol, info in instruments.items()
        if is_otc(symbol, info)
    )


def asset_catalog(instruments, assets=None):
    selected = (
        list(assets)
        if assets is not None
        else otc_assets(instruments)
    )

    groups = {}

    for symbol in selected:
        category = str(
            instruments.get(symbol, {}).get("type", "OTC")
        )
        groups.setdefault(category, []).append(symbol)

    return [
        {
            "type": category,
            "symbols": sorted(symbols),
        }
        for category, symbols in sorted(groups.items())
    ]


class CandleStore:

    def __init__(self, path=DB_PATH):
        self.path = str(path)

        Path(self.path).parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.lock = threading.RLock()

        self.db = sqlite3.connect(
            self.path,
            check_same_thread=False,
            timeout=30,
        )

        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
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

        print(
            "CANDLE DATABASE READY:",
            self.path,
            flush=True,
        )

    def save_candle(self, asset, period, candle):
        ts, open_, close, high, low = candle

        asset = str(asset)
        source = str(classify_source(asset))

        with self.lock:
            self.db.execute("""
                INSERT OR REPLACE INTO candles (
                    asset, period, ts,
                    open, close, high, low,
                    source, feed_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                asset,
                int(period),
                int(ts),
                float(open_),
                float(close),
                float(high),
                float(low),
                source,
                str(FEED_ID),
            ))

            self.db.commit()

    def add_tick(self, asset, ts, price):
        with self.lock:
            self.db.execute("""
                INSERT INTO ticks(asset, ts, price)
                VALUES (?, ?, ?)
            """, (
                str(asset),
                float(ts),
                float(price),
            ))

            self.db.commit()

    def get_candles(self, asset, period=60, limit=200):
        with self.lock:
            rows = self.db.execute("""
                SELECT ts, open, close, high, low
                FROM candles
                WHERE asset=? AND period=?
                ORDER BY ts DESC
                LIMIT ?
            """, (
                str(asset),
                int(period),
                max(1, int(limit)),
            )).fetchall()

        return [list(row) for row in reversed(rows)]

    def get(self, asset, period=60, limit=200):
        return self.get_candles(asset, period, limit)

    def latest(self, asset, period=60):
        rows = self.get_candles(asset, period, 1)
        return rows[-1] if rows else None

    def series(self, min_candles=0, periods=None):
        try:
            with self.lock:
                if periods:
                    periods = [int(p) for p in periods]
                    placeholders = ",".join(
                        "?" for _ in periods
                    )

                    query = f"""
                        SELECT asset, period
                        FROM candles
                        WHERE period IN ({placeholders})
                        GROUP BY asset, period
                        HAVING COUNT(*) >= ?
                        ORDER BY asset, period
                    """

                    params = periods + [int(min_candles)]

                else:
                    query = """
                        SELECT asset, period
                        FROM candles
                        GROUP BY asset, period
                        HAVING COUNT(*) >= ?
                        ORDER BY asset, period
                    """

                    params = [int(min_candles)]

                rows = self.db.execute(
                    query,
                    params,
                ).fetchall()

            return [
                (row[0], int(row[1]))
                for row in rows
            ]

        except Exception as exc:
            print(
                "SERIES QUERY ERROR:",
                repr(exc),
                flush=True,
            )
            return []

    def load(self, asset, period=60, limit=5000):
        return self.get_candles(asset, period, limit)

    def get_assets(self):
        with self.lock:
            rows = self.db.execute("""
                SELECT DISTINCT asset
                FROM candles
                ORDER BY asset
            """).fetchall()

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

    @property
    def tick_total(self):
        with self.lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM ticks"
            ).fetchone()[0]

    def count(self):
        return self.total

    def close(self):
        with self.lock:
            try:
                self.db.commit()
                self.db.close()
            except sqlite3.Error:
                pass


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
        self.last_tick = {}
        self.tick_count = 0
        self.error_count = 0
        self.followed_count = 0

        if not self.periods:
            raise ValueError("At least one timeframe is required")

        self.qx = None
        self.instruments = {}
        self.assets = []
        self.builders = {}

        try:
            self.qx = MarketQX(
                verbose=verbose,
            ).connect(
                wait_instruments=wait_instruments,
            )

            self.instruments = self.qx.instruments or {}

            if assets:
                self.assets = [
                    asset for asset in assets
                    if asset in self.instruments
                ]
            else:
                self.assets = otc_assets(self.instruments)

            if not self.assets:
                raise RuntimeError(
                    "No OTC pairs found in MarketQX instrument list"
                )

            for asset in self.assets:
                self.builders[asset] = {}

                for period in self.periods:
                    builder = CandleBuilder(
                        period,
                        on_closed=(
                            lambda candle, a=asset, p=period:
                            self._save_closed(a, p, candle)
                        ),
                    )

                    self.builders[asset][period] = builder

            self.qx.on_tick(self._on_tick)

            followed = 0

            for asset in self.assets:
                try:
                    self.qx.follow(
                        asset,
                        self.periods[0],
                    )
                    followed += 1

                except Exception as exc:
                    print(
                        "FOLLOW ERROR:",
                        asset,
                        repr(exc),
                        flush=True,
                    )

            self.followed_count = followed

            if followed == 0:
                raise RuntimeError(
                    "Could not follow any OTC pair"
                )

            print(
                "MarketQX connected:",
                followed,
                "OTC pairs",
                flush=True,
            )

        except Exception:
            if self.qx is not None:
                try:
                    self.qx.close()
                except Exception:
                    pass
            raise

    def _save_closed(self, asset, period, candle):
        try:
            self.store.save_candle(
                asset,
                period,
                candle,
            )

            print(
                "CANDLE SAVED:",
                asset,
                period,
                candle[0],
                flush=True,
            )

        except Exception as exc:
            self.error_count += 1

            print(
                "CANDLE SAVE ERROR:",
                repr(exc),
                flush=True,
            )

    def _on_tick(self, asset, ts, price, direction=0):
        if self.closed or asset not in self.builders:
            return

        try:
            ts = float(ts)
            price = float(price)

            if ts > 100000000000:
                ts /= 1000.0

            if price <= 0:
                return

            previous = self.last_tick.get(asset)

            if previous and ts < previous[0]:
                return

            self.last_tick[asset] = (ts, price)
            self.tick_count += 1

            self.store.add_tick(asset, ts, price)

            for period, builder in self.builders[asset].items():
                builder.add(ts, price)

        except Exception as exc:
            self.error_count += 1

            print(
                "TICK ERROR:",
                repr(exc),
                flush=True,
            )

    def get_candles(self, asset, period=60, limit=200):
        saved = self.store.get_candles(
            asset,
            period,
            limit,
        )

        builder = self.builders.get(
            asset, {}
        ).get(period)

        if builder and builder.forming:
            current = list(builder.forming)

            if not saved or saved[-1][0] != current[0]:
                saved.append(current)
            else:
                saved[-1] = current

        return saved[-limit:]

    def status(self):
        return {
            "connected": (
                not self.closed and self.qx is not None
            ),
            "assets": len(self.assets),
            "followed": self.followed_count,
            "ticks": self.tick_count,
            "errors": self.error_count,
            "last_tick": max(
                (
                    value[0]
                    for value in self.last_tick.values()
                ),
                default=None,
            ),
            "database_candles": self.store.total,
            "database_ticks": self.store.tick_total,
        }

    def close(self):
        if self.closed:
            return

        self.closed = True

        if self.qx is not None:
            try:
                self.qx.close()
            except Exception as exc:
                print(
                    "FEED CLOSE ERROR:",
                    repr(exc),
                    flush=True,
                )