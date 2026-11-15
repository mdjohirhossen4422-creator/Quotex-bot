#!/usr/bin/env python3
"""Read-only audit of the data engine (candles + ticks).

Phase 1 of the data-engine review. This module only *reads* the store; it never
writes, alters, or deletes anything. It answers, with numbers, the questions that
decide whether the feed can be trusted as training/signal input:

  candles:  OHLC validity, time alignment, duplicates, nulls, source/symbol
            consistency, staleness, frozen rate, per-series gaps
  ticks:    nulls, bad prices, future timestamps, direction values, duplicate
            (asset, ts) with conflicting price, timestamp monotonicity, rate
  consistency: rebuild OHLC from the stored ticks and diff against the candles

    python data_audit.py                     # human report
    python data_audit.py --json              # machine-readable
    python data_audit.py --consistency EURUSD_otc 60
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

DB_DEFAULT = "data/candles.db"


def _ro(path: str) -> sqlite3.Connection:
    """Open the store strictly read-only so an audit can never mutate it."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


# --------------------------------------------------------------------------- #
# candles
# --------------------------------------------------------------------------- #
def audit_candles(db: sqlite3.Connection, now: Optional[float] = None) -> dict:
    now = now or time.time()
    q = lambda s, *a: db.execute(s, a).fetchall()

    rows = q("SELECT COUNT(*) FROM candles")[0][0]
    if not rows:
        return {"rows": 0, "ok": False, "note": "empty candles table"}

    out: dict = {}
    out["rows"] = rows
    out["assets"] = q("SELECT COUNT(DISTINCT asset) FROM candles")[0][0]
    out["periods"] = sorted(r[0] for r in q("SELECT DISTINCT period FROM candles"))
    out["sources"] = dict(q("SELECT source, COUNT(*) FROM candles GROUP BY source"))
    out["feed_ids"] = [r[0] for r in q("SELECT DISTINCT feed_id FROM candles")]

    tmin, tmax = q("SELECT MIN(time), MAX(time) FROM candles")[0]
    out["time_min"], out["time_max"] = tmin, tmax
    out["newest_age_s"] = int(now - tmax)

    out["invalid_ohlc"] = q(
        "SELECT COUNT(*) FROM candles WHERE high < low OR high < open "
        "OR high < close OR low > open OR low > close "
        "OR open<=0 OR close<=0 OR high<=0 OR low<=0")[0][0]
    out["misaligned_time"] = q(
        "SELECT COUNT(*) FROM candles WHERE time % period != 0")[0][0]
    out["duplicate_keys"] = q(
        "SELECT COUNT(*) FROM (SELECT asset,period,time,COUNT(*) c FROM candles "
        "GROUP BY asset,period,time HAVING c>1)")[0][0]
    out["null_source"] = q("SELECT COUNT(*) FROM candles WHERE source IS NULL")[0][0]
    out["null_feed_id"] = q("SELECT COUNT(*) FROM candles WHERE feed_id IS NULL")[0][0]
    out["source_symbol_mismatch"] = (
        q("SELECT COUNT(*) FROM candles WHERE asset LIKE '%_otc' AND source!='otc'")[0][0]
        + q("SELECT COUNT(*) FROM candles WHERE asset NOT LIKE '%_otc' AND source='otc'")[0][0])

    flat = {}
    for (src,) in q("SELECT DISTINCT source FROM candles"):
        n = q("SELECT COUNT(*) FROM candles WHERE source=? AND open=close "
              "AND high=low", src)[0][0]
        tot = q("SELECT COUNT(*) FROM candles WHERE source=?", src)[0][0]
        flat[src] = round(n / tot, 4) if tot else 0.0
    out["frozen_rate_by_source"] = flat

    out["ok"] = (out["invalid_ohlc"] == 0 and out["misaligned_time"] == 0
                 and out["duplicate_keys"] == 0 and out["null_source"] == 0
                 and out["source_symbol_mismatch"] == 0)
    return out


def series_gaps(db: sqlite3.Connection, asset: str, period: int) -> dict:
    """Gap profile for one series: the trailing contiguous run and the biggest holes."""
    ts = [r[0] for r in db.execute(
        "SELECT time FROM candles WHERE asset=? AND period=? ORDER BY time",
        (asset, period)).fetchall()]
    if len(ts) < 2:
        return {"asset": asset, "period": period, "n": len(ts), "gaps": 0,
                "max_gap_s": 0, "recent_run": len(ts)}
    diffs = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    gaps = [d for d in diffs if d > period]
    run = 1
    for d in reversed(diffs):
        if d <= period:
            run += 1
        else:
            break
    return {"asset": asset, "period": period, "n": len(ts), "gaps": len(gaps),
            "max_gap_s": max(gaps) if gaps else 0, "recent_run": run}


# --------------------------------------------------------------------------- #
# ticks
# --------------------------------------------------------------------------- #
def audit_ticks(db: sqlite3.Connection, now: Optional[float] = None) -> dict:
    now = now or time.time()
    q = lambda s, *a: db.execute(s, a).fetchall()
    rows = q("SELECT COUNT(*) FROM ticks")[0][0]
    if not rows:
        return {"rows": 0, "ok": False, "note": "empty ticks table"}

    out: dict = {"rows": rows}
    out["assets"] = q("SELECT COUNT(DISTINCT asset) FROM ticks")[0][0]
    out["sources"] = dict(q("SELECT source, COUNT(*) FROM ticks GROUP BY source"))
    out["feed_ids"] = [r[0] for r in q("SELECT DISTINCT feed_id FROM ticks")]
    tmin, tmax = q("SELECT MIN(ts), MAX(ts) FROM ticks")[0]
    out["ts_min"], out["ts_max"] = tmin, tmax
    out["newest_age_s"] = int(now - tmax)

    out["null_source"] = q("SELECT COUNT(*) FROM ticks WHERE source IS NULL")[0][0]
    out["null_feed_id"] = q("SELECT COUNT(*) FROM ticks WHERE feed_id IS NULL")[0][0]
    out["bad_price"] = q("SELECT COUNT(*) FROM ticks WHERE price<=0")[0][0]
    out["bad_ts"] = q("SELECT COUNT(*) FROM ticks WHERE ts<=0")[0][0]
    out["future_ts"] = q("SELECT COUNT(*) FROM ticks WHERE ts > ?", now + 5)[0][0]
    out["dir_values"] = dict(q("SELECT dir, COUNT(*) FROM ticks GROUP BY dir"))
    out["source_symbol_mismatch"] = (
        q("SELECT COUNT(*) FROM ticks WHERE asset LIKE '%_otc' AND source!='otc'")[0][0]
        + q("SELECT COUNT(*) FROM ticks WHERE asset NOT LIKE '%_otc' AND source='otc'")[0][0])
    out["exact_dup_rows"] = q(
        "SELECT COUNT(*) FROM (SELECT asset,ts,price,dir,COUNT(*) c FROM ticks "
        "GROUP BY asset,ts,price,dir HAVING c>1)")[0][0]
    # the dangerous kind: same (asset, ts) carrying two different prices
    out["conflicting_ts"] = q(
        "SELECT COUNT(*) FROM (SELECT asset,ts,COUNT(DISTINCT price) c FROM ticks "
        "GROUP BY asset,ts HAVING c>1)")[0][0]

    out["ok"] = (out["null_source"] == 0 and out["bad_price"] == 0
                 and out["bad_ts"] == 0 and out["future_ts"] == 0
                 and out["source_symbol_mismatch"] == 0)
    return out


# --------------------------------------------------------------------------- #
# consistency: candles vs ticks rebuilt
# --------------------------------------------------------------------------- #
def rebuild_candles(ticks: list[tuple], period: int) -> dict:
    """OHLC buckets from (ts, price) rows, same rule as CandleBuilder."""
    out: dict = {}
    for ts, price in ticks:
        b = int(ts // period) * period
        c = out.get(b)
        if c is None:
            out[b] = [b, price, price, price, price]
        else:
            c[2] = price
            c[3] = max(c[3], price)
            c[4] = min(c[4], price)
    return out


def candle_tick_consistency(db: sqlite3.Connection, asset: str,
                            period: int, tol: float = 1e-9) -> dict:
    ticks = db.execute(
        "SELECT ts,price FROM ticks WHERE asset=? ORDER BY ts", (asset,)).fetchall()
    rebuilt = rebuild_candles(ticks, period)
    stored = {r[0]: list(r) for r in db.execute(
        "SELECT time,open,close,high,low FROM candles WHERE asset=? AND period=?",
        (asset, period)).fetchall()}
    common = sorted(set(rebuilt) & set(stored))
    mismatch = []
    for b in common:
        rb, sc = rebuilt[b], stored[b]
        if not all(math.isclose(rb[i], sc[i], rel_tol=0, abs_tol=tol)
                   for i in range(5)):
            mismatch.append({"time": b, "rebuilt": rb, "stored": sc})
    return {
        "asset": asset, "period": period,
        "ticks": len(ticks), "rebuilt": len(rebuilt), "stored": len(stored),
        "common": len(common), "mismatch": len(mismatch),
        "only_in_stored": len(set(stored) - set(rebuilt)),
        "only_in_rebuilt": len(set(rebuilt) - set(stored)),
        "examples": mismatch[:5],
        "ok": len(mismatch) == 0,
    }


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
@dataclass
class Audit:
    path: str
    candles: dict = field(default_factory=dict)
    ticks: dict = field(default_factory=dict)
    consistency: Optional[dict] = None

    def as_dict(self) -> dict:
        return {"path": self.path, "candles": self.candles,
                "ticks": self.ticks, "consistency": self.consistency}


def run(path: str = DB_DEFAULT, consistency: Optional[tuple] = None,
        now: Optional[float] = None) -> Audit:
    db = _ro(path)
    try:
        a = Audit(path=path)
        a.candles = audit_candles(db, now)
        a.ticks = audit_ticks(db, now)
        if consistency:
            a.consistency = candle_tick_consistency(db, consistency[0],
                                                    consistency[1])
        return a
    finally:
        db.close()


def format_report(a: Audit) -> str:
    L = [f"=== data-engine audit — {a.path} (read-only) ==="]
    c = a.candles
    L.append("\n[candles]")
    L.append(f"  rows {c.get('rows')}  assets {c.get('assets')}  "
             f"periods {c.get('periods')}")
    L.append(f"  sources {c.get('sources')}  feed_ids {c.get('feed_ids')}")
    L.append(f"  newest age {c.get('newest_age_s')}s")
    L.append(f"  invalid OHLC {c.get('invalid_ohlc')}  misaligned {c.get('misaligned_time')}"
             f"  dup keys {c.get('duplicate_keys')}  nulls "
             f"{c.get('null_source')}/{c.get('null_feed_id')}"
             f"  source-mismatch {c.get('source_symbol_mismatch')}")
    L.append(f"  frozen rate by source {c.get('frozen_rate_by_source')}")
    L.append(f"  → {'OK' if c.get('ok') else 'PROBLEMS'}")
    t = a.ticks
    L.append("\n[ticks]")
    L.append(f"  rows {t.get('rows')}  assets {t.get('assets')}  "
             f"sources {t.get('sources')}")
    L.append(f"  newest age {t.get('newest_age_s')}s  dir {t.get('dir_values')}")
    L.append(f"  bad price {t.get('bad_price')}  bad ts {t.get('bad_ts')}  "
             f"future {t.get('future_ts')}  nulls "
             f"{t.get('null_source')}/{t.get('null_feed_id')}")
    L.append(f"  exact-dup {t.get('exact_dup_rows')}  "
             f"conflicting-ts {t.get('conflicting_ts')}  "
             f"source-mismatch {t.get('source_symbol_mismatch')}")
    L.append(f"  → {'OK' if t.get('ok') else 'PROBLEMS'}")
    if a.consistency:
        s = a.consistency
        L.append(f"\n[consistency {s['asset']} {s['period']}s]")
        L.append(f"  ticks {s['ticks']}  rebuilt {s['rebuilt']}  stored {s['stored']}"
                 f"  common {s['common']}")
        L.append(f"  mismatch {s['mismatch']}  only-stored {s['only_in_stored']}"
                 f"  only-rebuilt {s['only_in_rebuilt']}")
        L.append(f"  → {'OK' if s['ok'] else 'DIFFERENCES (see examples)'}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="read-only data-engine audit")
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--consistency", nargs=2, metavar=("ASSET", "PERIOD"),
                    help="also rebuild this series from ticks and diff")
    args = ap.parse_args()
    cons = (args.consistency[0], int(args.consistency[1])) if args.consistency else None
    a = run(args.db, cons)
    print(json.dumps(a.as_dict(), indent=2) if args.json else format_report(a))


if __name__ == "__main__":
    main()
