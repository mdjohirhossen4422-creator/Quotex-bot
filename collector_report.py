#!/usr/bin/env python3
"""Daily monitoring report for the live collector (Phase 2.5).

Read-only. Opens `data/candles.db` with `mode=ro` and reports exactly what the
brief asks for:

  * total new ticks (and per asset),
  * complete candles per timeframe,
  * real vs OTC coverage,
  * data gaps (missing candle buckets within each series' own span),
  * duplicate count (exact duplicate tick keys),
  * collector uptime and reconnect count (from the heartbeat JSON).

"New" is measured against a stored baseline (`data/collector_baseline.json`),
which `--reset-baseline` rewrites to the current totals. Gaps are counted only
*within* a series' own first..last span, so a pair that simply started late or a
market that is closed is not reported as missing data.

Usage
-----
    python collector_report.py                 # human report
    python collector_report.py --json          # machine report
    python collector_report.py --reset-baseline
    python collector_report.py --periods 60,300
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from provenance import classify_source

REPORT_PERIODS = [60, 300, 900, 1800]
PERIOD_LABEL = {60: "1m", 300: "5m", 900: "15m", 1800: "30m"}


def connect_ro(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.execute("PRAGMA query_only=1")
    return db


def _totals(db: sqlite3.Connection) -> dict:
    t = db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    c = db.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    return {"ticks": t, "candles": c}


def tick_stats(db: sqlite3.Connection) -> dict:
    total = db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    by_asset = {r[0]: r[1] for r in db.execute(
        "SELECT asset, COUNT(*) FROM ticks GROUP BY asset")}
    by_source = {r[0] or "unknown": r[1] for r in db.execute(
        "SELECT source, COUNT(*) FROM ticks GROUP BY source")}
    # exact duplicate tick keys (asset, ts, price, dir)
    dup = db.execute(
        "SELECT COUNT(*) FROM (SELECT asset,ts,price,dir,COUNT(*) c FROM ticks "
        "GROUP BY asset,ts,price,dir HAVING c>1)").fetchone()[0]
    span = db.execute("SELECT MIN(ts), MAX(ts) FROM ticks").fetchone()
    return {"total": total, "by_asset": by_asset, "by_source": by_source,
            "duplicate_groups": dup, "ts_min": span[0], "ts_max": span[1]}


def candle_stats(db: sqlite3.Connection, periods: list[int]) -> dict:
    """Per (period, source): candle count and gap count within each series span.

    Gaps are split by size: a *small* gap (<= SMALL_GAP buckets) is a feed hiccup
    while the collector was running; a *large* gap means the collector was off.
    Reporting both keeps "we never ran" from looking like "the feed dropped".
    """
    SMALL_GAP = 20
    out = {}
    for p in periods:
        rows = db.execute(
            "SELECT asset, source, time FROM candles WHERE period=? "
            "ORDER BY asset, time", (p,)).fetchall()
        by_source = defaultdict(int)
        by_asset = defaultdict(int)
        small = large = 0
        span_total = 0
        prev_asset = None
        prev_t = None
        for asset, source, t in rows:
            src = source or classify_source(asset)
            by_source[src] += 1
            by_asset[asset] += 1
            if asset == prev_asset:
                miss = (t - prev_t) // p - 1
                if miss > 0:
                    span_total += miss + 1
                    if miss <= SMALL_GAP:
                        small += miss
                    else:
                        large += miss
                else:
                    span_total += 1
            else:
                span_total += 1
            prev_asset, prev_t = asset, t
        out[str(p)] = {"label": PERIOD_LABEL.get(p, f"{p}s"),
                       "total": sum(by_source.values()),
                       "by_source": dict(by_source),
                       "by_asset": dict(by_asset),
                       "gaps": small + large, "gaps_small": small,
                       "gaps_large": large, "expected_in_span": span_total}
    return out


def uptime_stats(state_path: str) -> dict:
    p = Path(state_path)
    if not p.exists():
        return {"status": "no heartbeat", "uptime_s": 0, "reconnects": 0,
                "connected": False, "pairs": 0, "last_tick": None}
    s = json.loads(p.read_text())
    return {"status": s.get("status"), "uptime_s": s.get("uptime_s", 0),
            "reconnects": s.get("reconnects", 0),
            "connected": s.get("connected", False),
            "pairs": s.get("pairs", 0),
            "ticks_new": s.get("ticks_new", 0),
            "candles_new": s.get("candles_new", 0),
            "ticks_duplicate": s.get("ticks_duplicate", 0),
            "candles_dropped_partial": s.get("candles_dropped_partial", 0),
            "last_tick": s.get("last_tick"),
            "updated": s.get("updated")}


def _load_baseline(path: str) -> dict:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def _save_baseline(path: str, totals: dict):
    Path(path).write_text(json.dumps({"totals": totals,
                                      "saved": datetime.now(timezone.utc).isoformat()},
                                     indent=2))


def _fmt_dur(sec: float) -> str:
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def build_report(db_path: str, state_path: str, baseline_path: str,
                 periods: list[int]) -> dict:
    db = connect_ro(db_path)
    try:
        totals = _totals(db)
        tstats = tick_stats(db)
        cstats = candle_stats(db, periods)
    finally:
        db.close()
    up = uptime_stats(state_path)
    base = _load_baseline(baseline_path).get("totals")
    new_ticks = (totals["ticks"] - base["ticks"]) if base else None
    new_candles = (totals["candles"] - base["candles"]) if base else None
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "db": db_path,
        "totals": totals,
        "new_since_baseline": ({"ticks": new_ticks, "candles": new_candles}
                               if base else None),
        "baseline": base,
        "ticks": tstats,
        "candles": cstats,
        "uptime": up,
    }


def print_report(rep: dict):
    t, up = rep["ticks"], rep["uptime"]
    print("=" * 68)
    print(f"  Live collector report — {rep['generated']}")
    print("=" * 68)
    nb = rep["new_since_baseline"]
    print(f"\nTICKS total {t['total']:,}"
          + (f"   (new since baseline: {nb['ticks']:+,})" if nb else ""))
    print(f"  by source: {t['by_source']}")
    print(f"  pairs with ticks: {len(t['by_asset'])}")
    print(f"  exact duplicate tick groups: {t['duplicate_groups']}")
    if t["ts_min"]:
        print(f"  span: {datetime.fromtimestamp(t['ts_min'], timezone.utc)} .. "
              f"{datetime.fromtimestamp(t['ts_max'], timezone.utc)}")

    print(f"\nCANDLES total {rep['totals']['candles']:,}"
          + (f"   (new since baseline: {nb['candles']:+,})" if nb else ""))
    print(f"  {'tf':<5}{'total':>10}{'otc':>10}{'real':>10}{'gap_sm':>9}"
          f"{'gap_lg':>9}{'span_expected':>15}")
    for p in sorted(rep["candles"], key=lambda x: int(x)):
        c = rep["candles"][p]
        bs = c["by_source"]
        print(f"  {c['label']:<5}{c['total']:>10,}{bs.get('otc', 0):>10,}"
              f"{bs.get('real', 0):>10,}{c['gaps_small']:>9,}"
              f"{c['gaps_large']:>9,}{c['expected_in_span']:>15,}")

    print(f"\nCOLLECTOR uptime {_fmt_dur(up['uptime_s'])}   status={up['status']} "
          f"connected={up['connected']}  reconnects={up['reconnects']}  "
          f"pairs={up['pairs']}")
    print(f"  ticks_new={up.get('ticks_new')} candles_new={up.get('candles_new')} "
          f"dup={up.get('ticks_duplicate')} "
          f"partial_dropped={up.get('candles_dropped_partial')}")
    lt = up.get("last_tick")
    if lt:
        print(f"  last tick: {lt['asset']} @ {lt['price']} "
              f"({datetime.fromtimestamp(lt['ts'], timezone.utc)} UTC)")
    print()


def main():
    ap = argparse.ArgumentParser(description="Live collector monitoring report")
    ap.add_argument("--db", default="data/candles.db")
    ap.add_argument("--state", default="data/collector_state.json")
    ap.add_argument("--baseline", default="data/collector_baseline.json")
    ap.add_argument("--periods", default=",".join(str(p) for p in REPORT_PERIODS))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--reset-baseline", action="store_true")
    args = ap.parse_args()

    periods = [int(p) for p in args.periods.split(",") if p]
    if args.reset_baseline:
        db = connect_ro(args.db)
        try:
            totals = _totals(db)
        finally:
            db.close()
        _save_baseline(args.baseline, totals)
        print(f"baseline reset: {totals}")
        return

    rep = build_report(args.db, args.state, args.baseline, periods)
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print_report(rep)


if __name__ == "__main__":
    main()
