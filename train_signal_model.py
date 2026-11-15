#!/usr/bin/env python3
"""Train the AI Signal Engine's models — one per (source, timeframe).

Honesty rules baked in (the brief is explicit):

  * real and OTC are **never** pooled: a model is trained and scored on a single
    source, and its metrics are reported per source,
  * the score is **out-of-sample** via an expanding-window time split with a
    purge/embargo gap, never a random split,
  * a **coin-flip control** is reported next to the model, and the payout
    breakeven is shown, so a "55%" is never mistaken for an edge,
  * labels are the NEXT candle's close direction; flat next candles are dropped,
    never guessed,
  * the artifact records the exact feature list and the data span, so a model can
    never be applied to the wrong features or timeframe.

Usage
-----
    python train_signal_model.py --source otc --period 60
    python train_signal_model.py --source real --period 300 --require-summary
    python train_signal_model.py --source otc --period 60 --no-write   # dry run
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import time
from pathlib import Path
from typing import Optional

import numpy as np

from signal_data import SignalData
from signal_features import build_dataset, FEATURES
from validation import metrics_from_outcomes, make_folds, DEFAULT_PAYOUT

MIN_ROWS = 400            # below this, training is not even attempted
MODEL_DIR = "models"


def _pool(sd: SignalData, source: str, period: int, mtf_period: int,
          require_summary: bool, min_candles: int):
    """Pool every pair of one source into (rows, y) sorted by time."""
    rows, ys, times, assets = [], [], [], []
    for asset in sd.assets(source, period, min_candles):
        candles, summaries, mtf = sd.load_with_mtf(asset, period, mtf_period)
        X, y, idx, _ = build_dataset(candles, summaries, mtf, require_summary)
        for x, yy, i in zip(X, y, idx):
            rows.append(x)
            ys.append(yy)
            times.append(candles[i][0])
            assets.append(asset)
    if not rows:
        return np.empty((0, len(FEATURES))), np.empty(0), [], []
    order = sorted(range(len(times)), key=lambda k: (times[k], assets[k]))
    X = np.array([rows[k] for k in order], dtype=float)
    y = np.array([ys[k] for k in order], dtype=int)
    t = [times[k] for k in order]
    a = [assets[k] for k in order]
    return X, y, t, a


def _model():
    from sklearn.ensemble import GradientBoostingClassifier
    return GradientBoostingClassifier(n_estimators=200, max_depth=3,
                                      learning_rate=0.05, subsample=0.9,
                                      random_state=42)


def walk_forward_score(X: np.ndarray, y: np.ndarray, n_splits: int,
                       min_train: float, embargo: int, payout: float):
    """Expanding-window OOS outcomes, pooled. Returns (outcomes, per_fold, aucs)."""
    outcomes, aucs, folds = [], [], []
    for k, (tr0, tr_end, te_start, te_end) in enumerate(
            make_folds(len(X), n_splits, min_train, embargo)):
        if tr_end < 100 or te_end > len(X):
            continue
        Xtr, ytr = X[:tr_end], y[:tr_end]
        Xte, yte = X[te_start:te_end], y[te_start:te_end]
        if len(set(ytr.tolist())) < 2 or len(set(yte.tolist())) < 2:
            continue
        m = _model().fit(Xtr, ytr)
        prob = m.predict_proba(Xte)[:, 1]
        pred = (prob >= 0.5).astype(int)
        for p, yy in zip(pred, yte):
            outcomes.append(bool(p == yy))
        try:
            from sklearn.metrics import roc_auc_score
            aucs.append(float(roc_auc_score(yte, prob)))
        except ValueError:
            pass
        folds.append({"fold": k, "n_train": int(tr_end), "n_test": int(te_end - te_start),
                      "hit": float(np.mean(pred == yte))})
    return outcomes, folds, aucs


def coin_control(n: int, payout: float, seed: int = 0):
    rng = np.random.default_rng(seed)
    outcomes = [bool(x) for x in rng.integers(0, 2, size=n)]
    return metrics_from_outcomes(outcomes, payout, name="coin-flip control")


def train(source: str, period: int, db: str, mtf_period: int,
          require_summary: bool, n_splits: int, min_train: float, embargo: int,
          payout: float, min_candles: int, write: bool) -> dict:
    sd = SignalData(db)
    X, y, times, assets = _pool(sd, source, period, mtf_period,
                                require_summary, min_candles)
    n = len(y)
    span = (min(times), max(times)) if times else (0, 0)
    pairs = sorted(set(assets))
    report = {
        "source": source, "period": period, "mtf_period": mtf_period,
        "features": FEATURES, "n_rows": n, "n_pairs": len(pairs),
        "require_summary": require_summary, "payout": payout,
        "data_span": span, "trained_at": time.time(),
    }
    if n < MIN_ROWS:
        report["status"] = "insufficient_data"
        report["need_rows"] = MIN_ROWS
        print(f"[{source} {period}s] insufficient data: {n} rows "
              f"(need {MIN_ROWS}); not training")
        _write(report, source, period, write)
        return report

    # out-of-sample walk-forward (time-ordered, purged)
    outcomes, folds, aucs = walk_forward_score(X, y, n_splits, min_train,
                                               embargo, payout)
    oos = metrics_from_outcomes(outcomes, payout, name=f"{source}-{period}s",
                                source=source)
    control = coin_control(len(outcomes), payout)
    report.update({
        "status": "trained",
        "oos": oos.as_dict(),
        "folds": folds,
        "auc_mean": float(np.mean(aucs)) if aucs else None,
        "control": control.as_dict(),
        "baseline_up_rate": float(np.mean(y)),
    })

    # final model on ALL rows (this is what live inference loads)
    final = _model().fit(X, y)
    if write:
        os.makedirs(MODEL_DIR, exist_ok=True)
        path = os.path.join(MODEL_DIR, f"{source}_{period}s.pkl")
        with open(path, "wb") as f:
            pickle.dump({"model": final, "features": FEATURES, "source": source,
                         "period": period, "mtf_period": mtf_period,
                         "trained_at": report["trained_at"],
                         "n_rows": n, "data_span": span}, f)
        report["model_path"] = path
    print(_format(report))
    _write(report, source, period, write)
    return report


def _format(r: dict) -> str:
    if r.get("status") != "trained":
        return f"[{r['source']} {r['period']}s] {r.get('status')}"
    o, c = r["oos"], r["control"]
    return (f"\n=== {r['source']} {r['period']}s | {r['n_rows']:,} rows | "
            f"{r['n_pairs']} pairs ===\n"
            f"  OOS   hit={o['hit']*100:5.1f}%  n={o['n']:>6}  "
            f"CI[{o['ci_low']*100:4.1f},{o['ci_high']*100:4.1f}]  "
            f"p={o['p_value']:.3f}  EV={o['ev']:+.4f}  "
            f"breakeven={o['breakeven']*100:.1f}%  -> {o['verdict'].upper()}\n"
            f"  control hit={c['hit']*100:5.1f}%  (coin flip)\n"
            f"  AUC={r['auc_mean'] if r['auc_mean'] is None else round(r['auc_mean'],3)}  "
            f"base_up={r['baseline_up_rate']*100:.1f}%")


def _write(report: dict, source: str, period: int, write: bool):
    if not write:
        return
    os.makedirs(MODEL_DIR, exist_ok=True)
    with open(os.path.join(MODEL_DIR, f"{source}_{period}s.json"), "w") as f:
        json.dump(report, f, indent=2, default=str)


def main():
    ap = argparse.ArgumentParser(description="Train per-(source,timeframe) signal models")
    ap.add_argument("--db", default=os.environ.get("CANDLE_DB", "data/candles.db"))
    ap.add_argument("--source", choices=["otc", "real", "synthetic"], default="otc")
    ap.add_argument("--period", type=int, default=60)
    ap.add_argument("--mtf-period", type=int, default=300,
                    help="higher timeframe for context; 0 to disable")
    ap.add_argument("--require-summary", action="store_true",
                    help="only use candles that carry tick summaries")
    ap.add_argument("--n-splits", type=int, default=5)
    ap.add_argument("--min-train", type=float, default=0.4)
    ap.add_argument("--embargo", type=int, default=2)
    ap.add_argument("--payout", type=float, default=DEFAULT_PAYOUT)
    ap.add_argument("--min-candles", type=int, default=200)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()
    train(args.source, args.period, args.db, args.mtf_period,
          args.require_summary, args.n_splits, args.min_train, args.embargo,
          args.payout, args.min_candles, write=not args.no_write)


if __name__ == "__main__":
    main()
