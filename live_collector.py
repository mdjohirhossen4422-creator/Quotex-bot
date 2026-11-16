#!/usr/bin/env python3
"""Continuous live tick collector for the Quotex / market-qx feed (Phase 2.5).

Runs forever, subscribes to every pair, and appends raw ticks + closed candles to
`data/candles.db` **additively**. It never deletes, overwrites or modifies rows
that are already there:

  * ticks  -> plain INSERT, guarded by an in-memory dedup set seeded from the
              tail of the table at startup, so a reconnect replay cannot store a
              tick twice;
  * candles -> INSERT OR IGNORE on the (asset, period, time) primary key, and the
              candle that was still forming across a reconnect is dropped (never
              written half-built), so no fabricated candle is ever stored.

No existing module is touched: the feed comes from `market_qx_candles.MarketQX`
(unchanged), the schema from `candle_store.CandleStore` (unchanged). Only
`provenance.classify_source` is reused, so real and OTC stay separated.

Reliability:
  * exponential backoff reconnect, never crash-exits on a feed error,
  * a per-run JSON heartbeat (`data/collector_state.json`) with status, last
    tick, pair count, db growth, uptime and reconnect count,
  * a daily JSON report and a log line every `--log-every` seconds,
  * SIGINT/SIGTERM -> flush the tick buffer, drop the forming candle, exit clean.

Usage
-----
    python live_collector.py                      # all pairs, forever
    python live_collector.py --pairs EURUSD_otc,BTCUSD_otc
    python live_collector.py --minutes 30         # smoke test, then stop
    python live_collector.py --status             # print the last heartbeat, exit
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from market_qx_candles import CandleBuilder, MarketQX
from provenance import FEED_ID, classify_source
from tick_summary import TickSummarizer, SUMMARY_FIELDS, summary_to_row

# Prediction timeframes the collector maintains, in seconds. 30m is included as
# the brief requires; on this feed the higher timeframes simply take longer to
# fill, and nothing is fabricated to make them appear sooner.
CANDLE_PERIODS = [60, 300, 900, 1800]

# A reconnect with no new ticks before this many seconds means the candle that
# was forming is stale — drop it instead of writing a partial one.
STALE_CANDLE_S = 180.0


class LiveCollector:
    def __init__(self, db_path: str, pairs: list[str], state_path: str,
                 log_every: float = 60.0, status_every: float = 30.0,
                 max_backoff: float = 60.0, verbose: bool = False,
                 store_ticks: bool = True, proxy: str = ""):
        self.db_path = db_path
        self.state_path = state_path
        self._pairs = list(pairs)
        self.log_every = log_every
        self.status_every = status_every
        self.max_backoff = max_backoff
        self.verbose = verbose
        self.proxy = proxy or os.environ.get("QX_PROXY", "")
        # When False, ticks are still received and turned into candles, but the
        # raw `ticks` table is not written. This keeps the DB ~10x smaller, which
        # is what makes continuous collection fit a small (1 GiB) volume. Tick
        # order-flow is still preserved as per-candle summaries (tick_summary.py).
        self.store_ticks = store_ticks
        self.summarizer = TickSummarizer(CANDLE_PERIODS)

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._buf: deque = deque()
        self._flush_now = threading.Event()

        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

        self.baseline_ticks = self._count_ticks()
        self.baseline_candles = self._count_candles()
        self.baseline_pairs = self._count_pairs()

        # Dedup: in-memory set of the last `dedup_window` keys per asset, seeded
        # from the tail of the ticks table. A reconnect replays at most a short
        # recent span, so this window is what stops the duplicate.
        self.dedup_window = 500
        # Hard cap on the in-memory tick buffer so a stuck writer cannot OOM a
        # small (512 MB) instance; ~200k rows * ~100 B is tens of MB.
        self.max_buffer = 200_000
        self._seen: dict[str, set] = defaultdict(set)
        self._seed_dedup()

        self.stats = {
            "reconnects": 0,
            "ticks_received": 0,
            "ticks_saved": 0,
            "ticks_duplicate": 0,
            "candles_saved": 0,
            "candles_dropped_partial": 0,
            "per_asset": defaultdict(int),
            "per_period": defaultdict(int),
            "started": time.time(),
        }
        self._last_tick: dict = {}
        self._flusher: Optional[threading.Thread] = None

    # ---- schema (additive only) ----------------------------------------- #
    def _ensure_schema(self):
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS ticks(
                   asset TEXT, ts REAL, price REAL, dir INTEGER)""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ticks_asset_ts ON ticks(asset, ts)")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS candles(
                   asset TEXT, period INTEGER, time INTEGER,
                   open REAL, close REAL, high REAL, low REAL,
                   PRIMARY KEY(asset, period, time))""")
        # Additive migration for older DBs: these columns already exist in the
        # current file, so this is a no-op there and only helps a fresh DB.
        tcols = {r[1] for r in self.conn.execute("PRAGMA table_info(ticks)")}
        if "source" not in tcols:
            self.conn.execute("ALTER TABLE ticks ADD COLUMN source TEXT")
        if "feed_id" not in tcols:
            self.conn.execute("ALTER TABLE ticks ADD COLUMN feed_id TEXT")
        ccols = {r[1] for r in self.conn.execute("PRAGMA table_info(candles)")}
        if "source" not in ccols:
            self.conn.execute("ALTER TABLE candles ADD COLUMN source TEXT")
        if "feed_id" not in ccols:
            self.conn.execute("ALTER TABLE candles ADD COLUMN feed_id TEXT")
        # Tick-summary columns: order-flow preserved even when raw ticks are not
        # stored. Additive, so an existing DB is extended in place.
        for col in SUMMARY_FIELDS:
            if col not in ccols:
                self.conn.execute(f"ALTER TABLE candles ADD COLUMN {col} REAL")
        self.conn.commit()

    def _count_ticks(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]

    def _count_candles(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]

    def _count_pairs(self) -> int:
        t = self.conn.execute("SELECT COUNT(DISTINCT asset) FROM ticks").fetchone()[0]
        c = self.conn.execute("SELECT COUNT(DISTINCT asset) FROM candles").fetchone()[0]
        return max(t, c)

    def _seed_dedup(self):
        """Seed the dedup set from each asset's most recent ticks (read-only)."""
        rows = self.conn.execute(
            "SELECT asset, ts, price, dir FROM ticks "
            "ORDER BY rowid DESC LIMIT ?", (self.dedup_window * 100,)).fetchall()
        for asset, ts, price, d in rows:
            if len(self._seen[asset]) < self.dedup_window:
                self._seen[asset].add((ts, price, d))

    # ---- ingest ---------------------------------------------------------- #
    def _on_tick(self, asset: str, ts: float, price: float, direction=None):
        if asset not in self._assets:
            return
        try:
            d = int(direction)
        except (TypeError, ValueError):
            d = 0
        key = (float(ts), float(price), d)
        seen = self._seen[asset]
        if key in seen:
            self.stats["ticks_duplicate"] += 1
            return
        seen.add(key)
        if len(seen) > self.dedup_window * 2:
            # bound memory: drop the oldest half (set is unordered, so rebuild
            # from the most recent ticks of this asset).
            self._seen[asset] = set(list(seen)[-self.dedup_window:])
        if self.store_ticks:
            with self._lock:
                self._buf.append((asset, key[0], key[1], d,
                                  classify_source(asset), FEED_ID))
                self.stats["ticks_received"] += 1
                self.stats["per_asset"][asset] += 1
                # Bound RAM on a small instance: if the writer is stuck (e.g. full
                # disk) the buffer must not grow without limit. Drop the oldest and
                # count it, so the loss is visible rather than an OOM kill.
                if len(self._buf) > self.max_buffer:
                    drop = len(self._buf) - self.max_buffer
                    for _ in range(drop):
                        self._buf.popleft()
                    self.stats["ticks_buffer_dropped"] = \
                        self.stats.get("ticks_buffer_dropped", 0) + drop
        # Tick summaries first: a tick that opens a new bucket finalizes the
        # previous candle's summary into `pending`, and the builder below fires
        # `on_closed` for that same bucket and pops it — so the order matters.
        for p in CANDLE_PERIODS:
            self.summarizer.add(asset, p, key[0], key[1], d)
        # Feed the candle builders (1m/5m/15m/30m) from the same tick.
        builders = getattr(self, "_builders", None)
        if builders:
            for p in CANDLE_PERIODS:
                b = builders.get((asset, p))
                if b is not None:
                    b.add(key[0], key[1])
        self._last_tick = {"asset": asset, "ts": key[0], "price": key[1],
                           "at": time.time()}

    def _flush(self, wait: bool = False):
        while True:
            with self._lock:
                if not self._buf:
                    return
                batch = list(self._buf)
                self._buf.clear()
            try:
                with self._lock:
                    self.conn.executemany(
                        "INSERT INTO ticks(asset,ts,price,dir,source,feed_id) "
                        "VALUES(?,?,?,?,?,?)", batch)
                    self.conn.commit()
                self.stats["ticks_saved"] += len(batch)
            except Exception as e:                       # never lose the loop
                # The insert is one transaction, so on failure nothing was
                # committed — put the batch back so the next cycle retries it
                # instead of silently dropping ticks (e.g. a transient full disk).
                with self._lock:
                    self._buf.extendleft(reversed(batch))
                self.stats["flush_errors"] = self.stats.get("flush_errors", 0) + 1
                print(f"  [warn] tick flush failed ({len(batch)} buffered): {e!r}",
                      flush=True)
                return
            if not wait:
                return

    def _flush_loop(self):
        while not self._stop.is_set():
            self._flush_now.wait(5.0)
            self._flush_now.clear()
            self._flush()
            self._write_state()

    # ---- candle persistence --------------------------------------------- #
    def _candle_cb(self, asset: str, period: int):
        def handler(candle: list):
            summary = self.summarizer.pop(asset, period, candle[0])
            srow = summary_to_row(summary)
            cols = ("asset,period,time,open,close,high,low,source,feed_id,"
                    + ",".join(SUMMARY_FIELDS))
            ph = ",".join(["?"] * (9 + len(SUMMARY_FIELDS)))
            with self._lock:
                cur = self.conn.execute(
                    f"INSERT OR IGNORE INTO candles({cols}) VALUES({ph})",
                    (asset, period, candle[0], candle[1], candle[2], candle[3],
                     candle[4], classify_source(asset), FEED_ID) + srow)
                self.conn.commit()
                if cur.rowcount and cur.rowcount > 0:
                    self.stats["candles_saved"] += 1
                    self.stats["per_period"][period] += 1
                    if summary is not None:
                        self.stats["candles_with_summary"] = \
                            self.stats.get("candles_with_summary", 0) + 1
        return handler

    # ---- heartbeat / report --------------------------------------------- #
    def _rss_kb(self) -> int:
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1])
        except OSError:
            pass
        return 0

    def _snapshot(self) -> dict:
        now = time.time()
        ticks_now = self._count_ticks()
        candles_now = self._count_candles()
        pairs_now = self._count_pairs()
        db_bytes = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        wal_bytes = 0
        for suffix in ("-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                wal_bytes += os.path.getsize(p)
        return {
            "status": "running" if not self._stop.is_set() else "stopped",
            "updated": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "uptime_s": round(now - self.stats["started"], 1),
            "rss_kb": self._rss_kb(),
            "connected": getattr(self, "connected", False),
            "session": self.stats.get("session", 0),
            "reconnects": self.stats["reconnects"],
            "pairs": pairs_now,
            "assets_subscribed": len(getattr(self, "_assets", [])),
            "ticks_total": ticks_now,
            "ticks_new": ticks_now - self.baseline_ticks,
            "ticks_saved": self.stats["ticks_saved"],
            "ticks_duplicate": self.stats["ticks_duplicate"],
            "ticks_buffer_dropped": self.stats.get("ticks_buffer_dropped", 0),
            "flush_errors": self.stats.get("flush_errors", 0),
            "ticks_pending": len(self._buf),
            "candles_total": candles_now,
            "candles_new": candles_now - self.baseline_candles,
            "candles_saved": self.stats["candles_saved"],
            "candles_with_summary": self.stats.get("candles_with_summary", 0),
            "candles_dropped_partial": self.stats["candles_dropped_partial"],
            "candles_per_period": {str(k): v for k, v in
                                   sorted(self.stats["per_period"].items())},
            "last_tick": self._last_tick or None,
            "db_bytes": db_bytes,
            "wal_bytes": wal_bytes,
            "storage_bytes": db_bytes + wal_bytes,
        }

    def _write_state(self):
        snap = self._snapshot()
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snap, f, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as e:
            print(f"  [warn] state write failed: {e!r}", flush=True)
        return snap

    def _log(self, snap: dict):
        lt = snap["last_tick"]
        lt_s = (f"{lt['asset']} @ {lt['price']} "
                f"({time.strftime('%H:%M:%S', time.localtime(lt['ts']))})"
                if lt else "none")
        print(f"[{time.strftime('%H:%M:%S')}] conn={snap['connected']} "
              f"pairs={snap['pairs']} ticks_new={snap['ticks_new']:,} "
              f"candles_new={snap['candles_new']:,} dup={snap['ticks_duplicate']} "
              f"reconnects={snap['reconnects']} rss={snap['rss_kb']//1024}MB "
              f"db={snap['storage_bytes']//1048576}MB last={lt_s}", flush=True)

    def _maybe_daily_report(self, last_report_day: str) -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today == last_report_day:
            return last_report_day
        snap = self._snapshot()
        d = Path(self.state_path).parent / "collector_reports"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{today}.json").write_text(json.dumps(snap, indent=2))
        print(f"  daily report -> {d / (today + '.json')}", flush=True)
        return today

    # ---- connection loop ------------------------------------------------- #
    def _connect(self, assets: list[str]) -> bool:
        # Retry instrument discovery; fall back to stored pairs if empty.
        qx = None
        for attempt in range(3):
            qx = MarketQX(proxy=self.proxy).connect(wait_instruments=40)
            if qx.instruments:
                break
            qx.close()
            time.sleep(3)
        if qx is None:
            return False
        instruments = dict(qx.instruments)
        discovered = sorted(instruments.keys())
        self._assets = list(assets) or discovered or self._stored_assets()
        self._asset_set = set(self._assets)
        if not self._assets:
            print("  [warn] no assets to subscribe — feed discovery empty and db empty",
                  flush=True)
            qx.close()
            return False
        self.qx = qx
        # Fresh builders: the candle that was forming across the gap is dropped,
        # never written half-built.
        self._reset_builders()
        qx.on_tick(self._on_tick)
        for a in self._assets:
            qx.follow(a, CANDLE_PERIODS[0])
        self.connected = True
        return True

    def _stored_assets(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT asset FROM candles ORDER BY asset").fetchall()]

    def run(self, minutes: float = 0.0):
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())

        self._flusher = threading.Thread(target=self._flush_loop, daemon=True)
        self._flusher.start()

        deadline = time.time() + minutes * 60 if minutes else 0
        last_report_day = ""
        last_log = 0.0
        backoff = 5.0

        while not self._stop.is_set():
            if deadline and time.time() > deadline:
                break
            self.stats["session"] = self.stats.get("session", 0) + 1
            session_ok = False
            try:
                if not self._connect(self._pairs):
                    self._sleep(backoff)
                    backoff = min(backoff * 2, self.max_backoff)
                    continue
                session_ok = True
                backoff = 5.0
                print(f"[session {self.stats['session']}] collecting "
                      f"{len(self._assets)} pairs (db {self.db_path})", flush=True)
                last_seen = time.time()
                while not self._stop.is_set():
                    if deadline and time.time() > deadline:
                        self._stop.set()
                        break
                    time.sleep(1)
                    if self._last_tick and self._last_tick["at"] > last_seen:
                        last_seen = self._last_tick["at"]
                    if not self.qx.connected:
                        raise ConnectionError("feed dropped")
                    if time.time() - last_seen > 60 and self.verbose:
                        print("  [warn] no ticks in 60s", flush=True)
                    now = time.time()
                    if now - last_log >= self.log_every:
                        last_log = now
                        self._log(self._write_state())
                        last_report_day = self._maybe_daily_report(last_report_day)
            except Exception as e:
                print(f"  feed error: {e!r} — reconnecting", flush=True)
            finally:
                if session_ok and self.stats["session"] > 1:
                    self.stats["reconnects"] += 1
                self._teardown_session()
            if not self._stop.is_set():
                self._sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)

        # graceful shutdown
        self._stop.set()
        self._flush_now.set()
        time.sleep(0.2)
        self._flush(wait=True)
        self._drop_forming()
        snap = self._write_state()
        snap["status"] = "stopped"
        self._log(snap)
        print(f"stopped. ticks_new={snap['ticks_new']:,} "
              f"candles_new={snap['candles_new']:,} dup={snap['ticks_duplicate']}",
              flush=True)

    def _reset_builders(self):
        """After a (re)connect, rebuild candle builders so the candle that was
        still forming across the gap is dropped, never written half-built."""
        if getattr(self, "_builders", None):
            dropped = sum(1 for b in self._builders.values() if b.forming is not None)
            self.stats["candles_dropped_partial"] += dropped
        self.summarizer.drop_forming()
        self._builders = {(a, p): CandleBuilder(p, on_closed=self._candle_cb(a, p))
                          for a in self._assets for p in CANDLE_PERIODS}

    def _drop_forming(self):
        if getattr(self, "_builders", None):
            dropped = sum(1 for b in self._builders.values() if b.forming is not None)
            self.stats["candles_dropped_partial"] += dropped
            self._builders = {}
        # The in-progress tick-summary accumulators are for the same dropped
        # candles, so discard them too — no half-built summary is ever written.
        self.summarizer.drop_forming()

    def _teardown_session(self):
        try:
            self._flush(wait=True)
        except Exception:
            pass
        try:
            self.qx.close()
        except Exception:
            pass
        self.connected = False

    def _sleep(self, s: float):
        self._stop.wait(s)


def print_status(state_path: str):
    if not os.path.exists(state_path):
        print("no heartbeat yet")
        return
    snap = json.loads(Path(state_path).read_text())
    for k in ("status", "updated", "uptime_s", "rss_kb", "connected", "pairs",
              "ticks_new", "candles_new", "ticks_duplicate", "reconnects",
              "storage_bytes", "last_tick"):
        print(f"  {k}: {snap.get(k)}")


def main():
    ap = argparse.ArgumentParser(description="Continuous live tick collector")
    ap.add_argument("--db", default=os.environ.get("CANDLE_DB", "data/candles.db"))
    ap.add_argument("--pairs", default=os.environ.get("COLLECTOR_PAIRS", ""),
                    help="comma list; default all pairs")
    ap.add_argument("--minutes", type=float, default=float(os.environ.get("COLLECTOR_MINUTES", 0) or 0),
                    help="0 = run forever")
    ap.add_argument("--log-every", type=float, default=float(os.environ.get("COLLECTOR_LOG_EVERY", 60) or 60))
    ap.add_argument("--state", default=os.environ.get("COLLECTOR_STATE", "data/collector_state.json"))
    ap.add_argument("--no-ticks", dest="store_ticks", action="store_false",
                    default=os.environ.get("COLLECTOR_STORE_TICKS", "1") not in ("0", "false", "False", ""),
                    help="store candles only, not raw ticks (~10x smaller DB)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--proxy", default=os.environ.get("QX_PROXY", ""),
                    help="route the feed through a proxy, e.g. "
                         "socks5://user:pass@host:1080 or http://host:3128 "
                         "(needed when the host IP is geo/Cloudflare-blocked)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.status:
        print_status(args.state)
        return

    pairs = [a for a in args.pairs.split(",") if a]
    coll = LiveCollector(args.db, pairs, args.state,
                         log_every=args.log_every, verbose=args.verbose,
                         store_ticks=args.store_ticks, proxy=args.proxy)
    coll.run(minutes=args.minutes)


if __name__ == "__main__":
    main()
