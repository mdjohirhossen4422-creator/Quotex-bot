#!/usr/bin/env python3
"""Daily performance report for the paper signal journal.

Reads `data/journal.db` and reports, per UTC day, the honest numbers: sample
size, hit-rate with a Wilson 95% CI, and the payout-adjusted expected value —
but ONLY where a real payout is known. The feed never transmits payout, so by
default every figure is labelled "assumed 0.85"; supply `payout.json` (see
`payout.py`) or `--payout` to make it a measured input.

Nothing here is a claim of accuracy. A day with a handful of signals and a CI
that straddles the breakeven is reported as exactly that.

Usage
  python signal_report.py                      # last 30 days, table
  python signal_report.py --days 7
  python signal_report.py --json               # machine-readable
  python signal_report.py --write reports/     # also dump one JSON per day
  python signal_report.py --payout 0.80        # override the payout
"""
import argparse
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from journal import Journal, wilson
from payout import PayoutConfig, DEFAULT_PAYOUT, breakeven, expected_value


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def daily_rows(j: Journal, asset: str = "", engine: str = "",
               payout: float = DEFAULT_PAYOUT) -> list[dict]:
    """Per-day settled performance, oldest day first.

    A signal is bucketed by the UTC day of its ENTRY (entry_at) — the day the
    trade would actually have been placed. Only settled rows (win/loss/flat)
    count; still-open signals are ignored, not guessed.
    """
    be = breakeven(payout)
    buckets: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "losses": 0, "flats": 0,
                 "models": defaultdict(int)})
    for r in j.rows(asset, engine):
        # rows: asset,period,engine,action,result,entry_at,expiry_at,
        #       entry_price,settle_price,score,confidence,model_version
        result = r[4]
        if result not in ("win", "loss", "flat"):
            continue
        day = _day(r[5])
        b = buckets[day]
        b["n"] += 1
        if result == "win":
            b["wins"] += 1
        elif result == "loss":
            b["losses"] += 1
        else:
            b["flats"] += 1
        b["models"][r[11] or "unknown"] += 1

    out = []
    for day in sorted(buckets):
        b = buckets[day]
        decisive = b["wins"] + b["losses"]
        hit = (b["wins"] / decisive) if decisive else None
        lo = hi = None
        ev = None
        if decisive:
            lo, hi = wilson(b["wins"], decisive)
            ev = expected_value(hit, payout)
        out.append({
            "day": day,
            "settled": b["n"],
            "wins": b["wins"], "losses": b["losses"], "flats": b["flats"],
            "decisive": decisive,
            "hit": None if hit is None else round(hit, 4),
            "ci_lo": None if lo is None else round(lo, 4),
            "ci_hi": None if hi is None else round(hi, 4),
            "payout": payout, "breakeven": round(be, 4),
            "ev": None if ev is None else round(ev, 4),
            "verdict": _verdict(decisive, lo, hi, be),
            "models": dict(b["models"]),
        })
    return out


def _verdict(decisive: int, lo, hi, be: float) -> str:
    if not decisive:
        return "no_decisive"
    if decisive < 30:
        return "insufficient"          # a CI on <30 trades is not usable
    if lo > be:
        return "above"
    if hi < be:
        return "below"
    return "indistinguishable"


def overall(rows: list[dict]) -> dict:
    n = sum(r["settled"] for r in rows)
    w = sum(r["wins"] for r in rows)
    l = sum(r["losses"] for r in rows)
    f = sum(r["flats"] for r in rows)
    d = w + l
    payout = rows[0]["payout"] if rows else DEFAULT_PAYOUT
    be = breakeven(payout)
    hit = (w / d) if d else None
    lo = hi = None
    if d:
        lo, hi = wilson(w, d)
    return {
        "days": len(rows), "settled": n, "wins": w, "losses": l, "flats": f,
        "decisive": d, "hit": None if hit is None else round(hit, 4),
        "ci_lo": None if lo is None else round(lo, 4),
        "ci_hi": None if hi is None else round(hi, 4),
        "payout": payout, "breakeven": round(be, 4),
        "ev": None if hit is None else round(expected_value(hit, payout), 4),
        "verdict": _verdict(d, lo, hi, be),
    }


def _fmt_pct(x):
    return "  n/a" if x is None else f"{x * 100:5.1f}"


def print_report(rows: list[dict], asset: str = "", engine: str = ""):
    print("\n=== daily performance report"
          f"{' — ' + asset if asset else ''}"
          f"{' — ' + engine if engine else ''} ===")
    if not rows:
        print("  no settled signals yet — nothing to report.\n")
        return
    o = overall(rows)
    src = ("[from payout.json]" if not _assumed(o) else "[assumed — no payout.json]")
    print(f"  payout {o['payout'] * 100:.0f}% (breakeven {o['breakeven'] * 100:.1f}%)  {src}")
    print(f"  {'day':<12}{'settled':>8}{'win':>5}{'loss':>5}{'flat':>5}"
          f"{'hit%':>7}{'95% CI':>16}{'EV':>9}  verdict")
    for r in rows:
        ci = (f"[{_fmt_pct(r['ci_lo'])},{_fmt_pct(r['ci_hi'])}]"
              if r["ci_lo"] is not None else "     n/a")
        ev = "  n/a" if r["ev"] is None else f"{r['ev']:+.4f}"
        print(f"  {r['day']:<12}{r['settled']:>8}{r['wins']:>5}{r['losses']:>5}"
              f"{r['flats']:>5}{_fmt_pct(r['hit']):>7}{ci:>16}{ev:>9}"
              f"  {r['verdict']}")
    total_ci = (f"[{_fmt_pct(o['ci_lo'])},{_fmt_pct(o['ci_hi'])}]"
                if o["ci_lo"] is not None else "     n/a")
    total_ev = "  n/a" if o["ev"] is None else f"{o['ev']:+.4f}"
    print(f"  {'TOTAL':<12}{o['settled']:>8}{o['wins']:>5}{o['losses']:>5}"
          f"{o['flats']:>5}{_fmt_pct(o['hit']):>7}{total_ci:>16}{total_ev:>9}"
          f"  {o['verdict']}")
    if o["verdict"] == "insufficient":
        print("  note: <30 decisive trades — the CI is not yet meaningful.")
    print("  'above' requires the CI LOWER bound to clear the payout breakeven.\n")


def _assumed(o: dict) -> bool:
    # The report is honest about where the payout came from: a caller that
    # loaded payout.json sets 'payout_source' to 'config'. Default => assumed.
    return o.get("payout_source", "assumed") != "config"


def write_reports(rows: list[dict], outdir: str):
    os.makedirs(outdir, exist_ok=True)
    for r in rows:
        path = os.path.join(outdir, f"signal_report_{r['day']}.json")
        with open(path, "w") as f:
            json.dump(r, f, indent=2)
    # a rolling summary too
    with open(os.path.join(outdir, "signal_report_summary.json"), "w") as f:
        json.dump({"generated_at": int(time.time()), "overall": overall(rows),
                   "days": rows}, f, indent=2)


def _resolve_payout(args) -> tuple[float, str]:
    if args.payout is not None:
        return float(args.payout), "cli"
    cfg = PayoutConfig.load(args.payout_config)
    if os.path.exists(args.payout_config):
        return cfg.default, "config"
    return DEFAULT_PAYOUT, "assumed"


def main():
    ap = argparse.ArgumentParser(description="Daily paper-signal performance report")
    ap.add_argument("--journal", default=os.environ.get("JOURNAL_DB", "data/journal.db"))
    ap.add_argument("--days", type=int, default=30, help="keep only the last N days")
    ap.add_argument("--asset", default="")
    ap.add_argument("--engine", default="")
    ap.add_argument("--payout", type=float, default=None,
                    help="override the payout (else payout.json / default 0.85)")
    ap.add_argument("--payout-config", default="payout.json")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", default="", help="also dump per-day JSON to this dir")
    args = ap.parse_args()

    payout, source = _resolve_payout(args)
    j = Journal(args.journal)
    rows = daily_rows(j, args.asset, args.engine, payout)
    if args.days and len(rows) > args.days:
        rows = rows[-args.days:]
    o = overall(rows)
    o["payout_source"] = source
    for r in rows:
        r["payout_source"] = source

    if args.write:
        write_reports(rows, args.write)
        print(f"wrote {len(rows)} daily report(s) + summary to {args.write}")
    if args.json:
        print(json.dumps({"overall": o, "days": rows}, indent=2))
    else:
        print_report(rows, args.asset, args.engine)


if __name__ == "__main__":
    main()
