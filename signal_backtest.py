#!/usr/bin/env python3
"""Honest backtest for the AI Signal Engine, per (source, timeframe).

This does NOT re-run the engine candle-by-candle (that would be slow and would
tempt one to tune on it). It reproduces the exact model path the live engine
uses — same features, same label, same 0.5 threshold — but with an
**expanding-window, purged, time-ordered** walk-forward, and reports:

  * hit-rate with a 95% Wilson CI,
  * a binomial p-value,
  * payout-adjusted EV and profit factor at the configured payout,
  * the payout breakeven hit-rate,
  * a coin-flip control of the same size,
  * a verdict (above / below / indistinguishable).

Real and OTC are scored separately and never pooled. A model is only scored if
it was trained on the SAME source and timeframe — the split is by time, so the
test rows were never seen during training.

Usage
-----
    python signal_backtest.py --source otc --period 60
    python signal_backtest.py --source real --period 300 --payout 0.80
    python signal_backtest.py --source otc --all-tf
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np

from signal_data import SignalData
from validation import metrics_from_outcomes, make_folds, DEFAULT_PAYOUT
from train_signal_model import _pool, _model, coin_control

PERIODS = [60, 300, 900, 1800]


def backtest(source: str, period: int, db: str, mtf_period: int,
             require_summary: bool, n_splits: int, min_train: float,
             embargo: int, payout: float, min_candles: int) -> dict:
    sd = SignalData(db)
    X, y, times, assets = _pool(sd, source, period, mtf_period,
                                require_summary, min_candles)
    out = {"source": source, "period": period, "n_rows": int(len(y)),
           "n_pairs": len(set(assets)), "payout": payout}
    if len(y) < 200:
        out["status"] = "insufficient_data"
        return out

    outcomes = []
    for k, (tr0, tr_end, te_start, te_end) in enumerate(
            make_folds(len(X), n_splits, min_train, embargo)):
        if tr_end < 100 or te_end > len(X):
            continue
        Xtr, ytr = X[:tr_end], y[:tr_end]
        Xte, yte = X[te_start:te_end], y[te_start:te_end]
        if len(set(ytr.tolist())) < 2 or len(set(yte.tolist())) < 2:
            continue
        m = _model().fit(Xtr, ytr)
        pred = (m.predict_proba(Xte)[:, 1] >= 0.5).astype(int)
        outcomes.extend(bool(p == yy) for p, yy in zip(pred, yte))

    metrics = metrics_from_outcomes(outcomes, payout,
                                    name=f"{source}-{period}s", source=source)
    control = coin_control(len(outcomes), payout)
    out.update({"status": "ok", "oos": metrics.as_dict(),
                "control": control.as_dict()})
    return out


def _fmt(r: dict) -> str:
    if r.get("status") != "ok":
        return (f"[{r['source']} {r['period']}s] {r.get('status')} "
                f"(rows={r['n_rows']})")
    o, c = r["oos"], r["control"]
    return (f"[{r['source']:9} {r['period']:>5}s] rows={r['n_rows']:>7,} "
            f"pairs={r['n_pairs']:>2}  hit={o['hit']*100:5.1f}% "
            f"CI[{o['ci_low']*100:4.1f},{o['ci_high']*100:4.1f}] "
            f"p={o['p_value']:.3f} EV={o['ev']:+.4f} "
            f"PF={o['profit_factor']:.2f} BE={o['breakeven']*100:.1f}% "
            f"ctrl={c['hit']*100:4.1f}% -> {o['verdict'].upper()}")


def main():
    ap = argparse.ArgumentParser(description="Honest backtest of the AI signal models")
    ap.add_argument("--db", default=os.environ.get("CANDLE_DB", "data/candles.db"))
    ap.add_argument("--source", choices=["otc", "real", "synthetic"], default="otc")
    ap.add_argument("--period", type=int, default=60)
    ap.add_argument("--all-tf", action="store_true")
    ap.add_argument("--mtf-period", type=int, default=300)
    ap.add_argument("--require-summary", action="store_true")
    ap.add_argument("--n-splits", type=int, default=5)
    ap.add_argument("--min-train", type=float, default=0.4)
    ap.add_argument("--embargo", type=int, default=2)
    ap.add_argument("--payout", type=float, default=DEFAULT_PAYOUT)
    ap.add_argument("--min-candles", type=int, default=200)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    periods = PERIODS if args.all_tf else [args.period]
    results = []
    for p in periods:
        r = backtest(args.source, p, args.db, args.mtf_period,
                     args.require_summary, args.n_splits, args.min_train,
                     args.embargo, args.payout, args.min_candles)
        results.append(r)
        if not args.json:
            print(_fmt(r))
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print("\nVerdict 'above' means the CI lower bound cleared the payout "
              "breakeven — the only honest bar. Anything else is not an edge.")


if __name__ == "__main__":
    main()
