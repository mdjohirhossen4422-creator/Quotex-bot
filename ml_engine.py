#!/usr/bin/env python3
"""Online ML engine: download history -> clean -> build features -> train -> predict.

This is the model behind the dashboard's "Get Analysis" button. For any pair and
timeframe it:

  1. downloads historical candles from `market_data` (Yahoo, no login),
  2. cleans them (gaps / flat bars / spikes) and refuses a bad series,
  3. builds the causal feature matrix in one pass,
  4. trains a small soft-voting ensemble (GBM + RandomForest + LogReg),
  5. scores it honestly with a time-ordered walk-forward split, and
  6. predicts the next candle's direction (UP / DOWN) with a confidence.

The walk-forward numbers are reported next to the prediction on purpose: this
feed is close to a random walk, so an edge near zero is the honest, expected
result and the UI says so instead of selling a coin flip as a signal.

Usage
-----
    from ml_engine import train, predict
    bundle = train("EURUSD", "1h")
    bundle["signal"]        # {"action","prob_up","confidence",...}
"""
from __future__ import annotations

import math
import threading
import time
from typing import Optional

import numpy as np

import market_data
from indicators import atr, bollinger, ema, macd, rsi, stochastic
from market_data import ALL_TF

# Feature order is fixed; a trained model relies on it.
FEATURES = [
    "ret_1", "ret_3", "ret_5", "ret_10",
    "body", "upper_wick", "lower_wick",
    "ema_fast_slow", "ema_slow_trend", "price_ema_fast", "price_ema_trend",
    "ema_slope_fast", "ema_slope_slow",
    "rsi", "rsi_slope",
    "macd_hist", "macd_hist_prev",
    "bb_pos", "bb_width",
    "atr_pct",
    "stoch_k", "stoch_kd",
    "consec_up", "consec_down",
    "hour_sin", "hour_cos",
]

_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 900  # seconds; retrain at most every 15 min per (pair, tf)
_MAX_ROWS = 10000  # most-recent rows used to fit; keeps a request a few seconds


def _trim(X, y, idx=None):
    """Keep only the most recent `_MAX_ROWS` samples (older rows add little)."""
    if len(X) <= _MAX_ROWS:
        return X, y, idx
    X, y = X[-_MAX_ROWS:], y[-_MAX_ROWS:]
    idx = idx[-_MAX_ROWS:] if idx is not None else None
    return X, y, idx


# ---------------------------------------------------------------------------
# Feature matrix (one pass, no look-ahead)
# ---------------------------------------------------------------------------

def build_features(candles: list[list]) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """Return (X, y, idx): features, next-candle labels, and source indices.

    X has one row per usable candle index, PLUS a final "live" row for the newest
    candle that has no label yet — that is the row the live signal is predicted
    from, so the call is always about the *next* (not-yet-closed) candle. Every
    row uses candles [0..i] only, so training and live prediction share exactly
    the same code path and there is no leakage.
    """
    n = len(candles)
    if n < 60:
        return np.empty((0, len(FEATURES))), np.empty(0), []
    t = [c[0] for c in candles]
    o = [c[1] for c in candles]
    cl = [c[2] for c in candles]
    h = [c[3] for c in candles]
    lo = [c[4] for c in candles]

    ema_f = ema(cl, 9)
    ema_s = ema(cl, 21)
    ema_t = ema(cl, 50)
    rsi_v = rsi(cl, 14)
    macd_line, sig_line, hist = macd(cl, 12, 26, 9)
    bb_u, bb_m, bb_l = bollinger(cl, 20, 2.0)
    atr_v = atr(h, lo, cl, 14)
    st_k, st_d = stochastic(h, lo, cl, 14, 3, 3)

    rows, labels, idx = [], [], []
    cu = cd = 0
    for i in range(50, n):
        price = cl[i]
        if not price or ema_f[i] is None or ema_s[i] is None or ema_t[i] is None:
            continue
        rng = h[i] - lo[i]
        # consecutive same-direction candles ending at i
        if cl[i] > o[i]:
            cu = cu + 1 if cu else 1
            cd = 0
        elif cl[i] < o[i]:
            cd = cd + 1 if cd else 1
            cu = 0
        else:
            cu = cd = 0
        hour = (t[i] // 3600) % 24
        bb_span = (bb_u[i] - bb_l[i]) if (bb_u[i] is not None and bb_l[i] is not None) else 0.0
        rows.append([
            _d(price - cl[i - 1], cl[i - 1]),
            _d(price - cl[i - 3], cl[i - 3]),
            _d(price - cl[i - 5], cl[i - 5]),
            _d(price - cl[i - 10], cl[i - 10]),
            _d(cl[i] - o[i], rng) if rng else 0.0,
            _d(h[i] - max(o[i], cl[i]), rng) if rng else 0.0,
            _d(min(o[i], cl[i]) - lo[i], rng) if rng else 0.0,
            _d(ema_f[i] - ema_s[i], price),
            _d(ema_s[i] - ema_t[i], price),
            _d(price - ema_f[i], price),
            _d(price - ema_t[i], price),
            _d(ema_f[i] - ema_f[i - 1], price) if ema_f[i - 1] else 0.0,
            _d(ema_s[i] - ema_s[i - 1], price) if ema_s[i - 1] else 0.0,
            (rsi_v[i] or 50.0) / 100.0,
            _d((rsi_v[i] or 50.0) - (rsi_v[i - 1] or 50.0), 100.0),
            _d(hist[i] or 0.0, price),
            _d(hist[i - 1] or 0.0, price),
            (_d(price - bb_l[i], bb_span) if bb_span else 0.5),
            _d(bb_span, price) if bb_span else 0.0,
            _d(atr_v[i] or 0.0, price),
            (st_k[i] or 50.0) / 100.0,
            _d((st_k[i] or 50.0) - (st_d[i] or 50.0), 100.0),
            min(cu, 5),
            min(cd, 5),
            math.sin(2 * math.pi * hour / 24),
            math.cos(2 * math.pi * hour / 24),
        ])
        idx.append(i)
        if i < n - 1:
            labels.append(1 if cl[i + 1] > cl[i] else 0)
    if not rows:
        return np.empty((0, len(FEATURES))), np.empty(0), []
    return np.asarray(rows, dtype=float), np.asarray(labels, dtype=int), idx


def _d(a, b):
    return a / b if b else 0.0


# ---------------------------------------------------------------------------
# Training + honest validation
# ---------------------------------------------------------------------------

def _make_models():
    from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    return [
        GradientBoostingClassifier(n_estimators=120, max_depth=3, learning_rate=0.05,
                                   subsample=0.9, random_state=42),
        RandomForestClassifier(n_estimators=200, max_depth=6, n_jobs=-1, random_state=42),
        make_pipeline(StandardScaler(),
                      LogisticRegression(max_iter=500, random_state=42)),
    ]


def _ensemble_prob(models, X) -> np.ndarray:
    probs = [m.predict_proba(X)[:, 1] for m in models]
    return np.mean(probs, axis=0)


def _fit_models(X, y):
    models = _make_models()
    for m in models:
        m.fit(X, y)
    return models


def walk_forward(X, y, n_splits=5, min_train=0.4) -> dict:
    """Expanding-window walk-forward on the soft-voting ensemble.

    Returns mean accuracy, the majority-class baseline, the edge over it, mean
    AUC, and the per-fold detail. Time order is preserved — never shuffled.
    """
    n = min(len(X), len(y))  # guard: X may carry one unlabeled live row
    X, y = X[:n], y[:n]
    start = int(n * min_train)
    if start < 60 or n - start < n_splits * 5:
        return {"folds": [], "n_folds": 0, "mean_acc": None, "mean_baseline": None,
                "mean_edge": None, "mean_auc": None}
    block = (n - start) // n_splits
    folds = []
    for k in range(n_splits):
        tr_end = start + k * block
        te_end = tr_end + block
        if te_end > n or tr_end < 60:
            break
        Xtr, ytr = X[:tr_end], y[:tr_end]
        Xte, yte = X[tr_end:te_end], y[tr_end:te_end]
        if len(set(ytr.tolist())) < 2 or len(set(yte.tolist())) < 2:
            continue
        models = _fit_models(Xtr, ytr)
        prob = _ensemble_prob(models, Xte)
        pred = (prob >= 0.5).astype(int)
        acc = float((pred == yte).mean())
        base = max(float(yte.mean()), 1 - float(yte.mean()))
        auc = _auc(yte, prob)
        folds.append({"fold": k + 1, "n_test": int(len(yte)), "acc": round(acc, 4),
                      "baseline": round(base, 4), "edge": round(acc - base, 4),
                      "auc": round(auc, 4) if auc is not None else None})
    if not folds:
        return {"folds": [], "n_folds": 0, "mean_acc": None, "mean_baseline": None,
                "mean_edge": None, "mean_auc": None}
    accs = [f["acc"] for f in folds]
    bases = [f["baseline"] for f in folds]
    aucs = [f["auc"] for f in folds if f["auc"] is not None]
    return {
        "folds": folds, "n_folds": len(folds),
        "mean_acc": round(float(np.mean(accs)), 4),
        "mean_baseline": round(float(np.mean(bases)), 4),
        "mean_edge": round(float(np.mean(accs) - np.mean(bases)), 4),
        "mean_auc": round(float(np.mean(aucs)), 4) if aucs else None,
    }


def _auc(y, prob) -> Optional[float]:
    try:
        from sklearn.metrics import roc_auc_score
        return float(roc_auc_score(y, prob))
    except Exception:
        return None


def _verdict(vw: dict) -> str:
    if not vw.get("n_folds"):
        return "NOT ENOUGH DATA"
    edge = vw["mean_edge"] or 0.0
    if edge >= 0.02 and (vw["mean_auc"] or 0) >= 0.52:
        return "EDGE DETECTED"
    if edge <= -0.01:
        return "BELOW BASELINE"
    return "NO EDGE (≈ random)"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _predict_last(models, X) -> tuple[str, float, float]:
    prob_up = float(_ensemble_prob(models, X[-1:])[0])
    action = "UP" if prob_up >= 0.5 else "DOWN"
    conf = prob_up if action == "UP" else 1 - prob_up
    return action, conf, prob_up


def train(pair: str, tf: str, *, force: bool = False) -> dict:
    """Download, clean, train and validate a model for (pair, tf).

    Returns a bundle with the fitted models, honest walk-forward metrics, the
    data-quality report and the live signal for the latest candle. Cached for
    `_CACHE_TTL` seconds; pass `force=True` to retrain.
    """
    key = f"{pair}|{tf}"
    now = time.time()
    if not force:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit and now - hit["trained_at"] < _CACHE_TTL:
            return hit

    candles, quality = market_data.fetch(pair, tf)
    if not quality.ok:
        raise market_data.DataError(
            f"{pair} {tf}: data failed the quality check "
            f"(score {quality.score:.2f}; {'; '.join(quality.notes)})")

    X, y, idx = build_features(candles)
    if len(y) < 150:
        raise market_data.DataError(
            f"{pair} {tf}: only {len(y)} training rows — not enough to fit a model")
    # The final feature row has no label yet: it is the newest candle, and the
    # signal is a prediction for the candle that follows it.
    X_live = X[-1:]
    X, y, idx = _trim(X[:-1], y, idx[:-1])

    vw = walk_forward(X, y)
    models = _fit_models(X, y)
    action, conf, prob_up = _predict_last(models, X_live)

    importances = _importances(models, X, y)

    bundle = {
        "pair": pair, "tf": tf,
        "symbol": market_data.PAIRS[pair][0],
        "samples": int(len(X)), "features": len(FEATURES),
        "up_rate": round(float(y.mean()), 4),
        "bars": int(len(candles)),
        "quality": quality.as_dict(),
        "validation": vw,
        "verdict": _verdict(vw),
        "signal": {
            "action": action, "confidence": round(conf, 4),
            "prob_up": round(prob_up, 4),
            "price": candles[-1][2],
            "candle_time": candles[-1][0],
            "as_of": time.time(),
        },
        "importances": importances,
        "recent": candles[-140:],
        "trained_at": now,
        "trained_on": time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)),
    }
    with _CACHE_LOCK:
        _CACHE[key] = bundle
    return bundle


def _importances(models, X, y, top=8) -> list[dict]:
    """Permutation importance for the gradient-boosting model (cheap, honest)."""
    try:
        gbm = models[0]
        base = float((gbm.predict(X) == y).mean())
        rng = np.random.default_rng(42)
        out = []
        for j in range(X.shape[1]):
            Xp = X.copy()
            rng.shuffle(Xp[:, j])
            drop = base - float((gbm.predict(Xp) == y).mean())
            out.append({"feature": FEATURES[j], "drop": round(drop, 4)})
        out.sort(key=lambda d: d["drop"], reverse=True)
        return out[:top]
    except Exception:
        return []


# Timeframes used when the user picks "all" — one intraday, one mid, one daily.
CONSENSUS_TFS = ["15m", "1h", "1d"]


def train_all(pair: str, tfs: Optional[list[str]] = None, *, force: bool = False) -> dict:
    """Train per-timeframe models and combine them into a consensus signal.

    Used for the "All timeframes" choice: each timeframe votes UP/DOWN and the
    majority wins; ties break toward the higher-confidence model. The individual
    per-timeframe bundles are included so the UI can show them all.
    """
    tfs = tfs or CONSENSUS_TFS
    per, errors = [], []
    for tf in tfs:
        try:
            per.append(train(pair, tf, force=force))
        except Exception as exc:
            errors.append(f"{tf}: {exc}")
    if not per:
        raise market_data.DataError(
            f"{pair}: could not train on any timeframe ({'; '.join(errors)})")
    return _consensus(pair, tfs, per, errors)


def _consensus(pair: str, tfs: list[str], per: list[dict], errors: list[str]) -> dict:
    ups = sum(1 for b in per if b["signal"]["action"] == "UP")
    downs = len(per) - ups
    if ups > downs:
        action = "UP"
    elif downs > ups:
        action = "DOWN"
    else:
        action = max(per, key=lambda b: b["signal"]["confidence"])["signal"]["action"]
    agree = max(ups, downs)
    conf = float(np.mean([b["signal"]["confidence"] for b in per
                          if b["signal"]["action"] == action])) if per else 0.0
    prob_up = float(np.mean([b["signal"]["prob_up"] if action == "UP"
                             else 1 - b["signal"]["prob_up"] for b in per]))
    best = per[0]
    return {
        "pair": pair, "tf": "all", "tfs": tfs,
        "symbol": market_data.PAIRS[pair][0],
        "signal": {
            "action": action,
            "confidence": round(conf, 4),
            "prob_up": round(prob_up, 4),
            "price": best["signal"]["price"],
            "candle_time": best["signal"]["candle_time"],
            "as_of": time.time(),
            "agree": f"{agree}/{len(per)} timeframes",
        },
        "verdict": _consensus_verdict(per),
        "per_timeframe": per,
        "errors": errors,
        "trained_at": time.time(),
        "trained_on": time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time())),
    }


def _consensus_verdict(per: list[dict]) -> str:
    edges = [b["validation"].get("mean_edge") for b in per
             if b["validation"].get("mean_edge") is not None]
    if not edges:
        return "NOT ENOUGH DATA"
    return "EDGE DETECTED" if sum(e > 0.02 for e in edges) >= 2 else "NO EDGE (≈ random)"


def predict_from(bundle: dict, candles: list[list]) -> Optional[dict]:
    """Re-run a trained bundle on fresh candles (used by tests / live re-check)."""
    X, y, idx = build_features(candles)
    if len(X) == 0:
        return None
    models = bundle.get("_models")
    if models is None:
        return bundle.get("signal")
    action, conf, prob_up = _predict_last(models, X[-1:])
    return {"action": action, "confidence": round(conf, 4), "prob_up": round(prob_up, 4)}


def cache_info() -> list[dict]:
    with _CACHE_LOCK:
        return [{"key": k, "age_s": round(time.time() - v["trained_at"], 1),
                 "verdict": v.get("verdict")} for k, v in _CACHE.items()]


# ---------------------------------------------------------------------------
# Background training jobs (so the UI can show progress instead of hanging)
# ---------------------------------------------------------------------------

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_SEQ = 0


def start_job(pair: str, tf: str, *, force: bool = False) -> str:
    """Kick off training in a background thread; returns a job id to poll."""
    global _JOB_SEQ
    with _JOBS_LOCK:
        _JOB_SEQ += 1
        jid = f"job{_JOB_SEQ}"
        _JOBS[jid] = {"id": jid, "pair": pair, "tf": tf, "state": "running",
                      "step": "starting", "progress": 0.0, "result": None,
                      "error": None, "started": time.time()}
    threading.Thread(target=_run_job, args=(jid, pair, tf, force), daemon=True).start()
    return jid


def _run_job(jid: str, pair: str, tf: str, force: bool) -> None:
    def setp(step, prog):
        with _JOBS_LOCK:
            j = _JOBS.get(jid)
            if j:
                j["step"] = step
                j["progress"] = round(prog, 3)
    try:
        if tf == ALL_TF:
            setp("downloading + training each timeframe", 0.05)
            res = _train_all_progress(pair, force=force, on_step=setp)
        else:
            setp("downloading history", 0.1)
            res = train(pair, tf, force=force)
        with _JOBS_LOCK:
            j = _JOBS.get(jid)
            if j:
                j["state"] = "done"
                j["step"] = "done"
                j["progress"] = 1.0
                j["result"] = res
    except Exception as exc:
        with _JOBS_LOCK:
            j = _JOBS.get(jid)
            if j:
                j["state"] = "error"
                j["error"] = f"{type(exc).__name__}: {exc}"


def _train_all_progress(pair: str, *, force: bool, on_step) -> dict:
    tfs = CONSENSUS_TFS
    per, errors = [], []
    for k, tf in enumerate(tfs):
        on_step(f"training {tf} ({k + 1}/{len(tfs)})", 0.1 + 0.8 * k / len(tfs))
        try:
            per.append(train(pair, tf, force=force))
        except Exception as exc:
            errors.append(f"{tf}: {exc}")
    if not per:
        raise market_data.DataError(
            f"{pair}: could not train on any timeframe ({'; '.join(errors)})")
    on_step("combining timeframes", 0.95)
    return _consensus(pair, tfs, per, errors)


def job_status(jid: str) -> Optional[dict]:
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        return dict(j) if j else None


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Train + predict for a pair/timeframe")
    ap.add_argument("pair", nargs="?", default="EURUSD")
    ap.add_argument("tf", nargs="?", default="1h")
    args = ap.parse_args()
    b = train(args.pair, args.tf, force=True)
    s = b["signal"]
    print(f"{b['pair']} {b['tf']}  ({b['bars']} bars, {b['samples']} samples, "
          f"up-rate {b['up_rate']:.3f})")
    print(f"quality: {b['quality']}")
    vw = b["validation"]
    print(f"walk-forward: acc={vw['mean_acc']} baseline={vw['mean_baseline']} "
          f"edge={vw['mean_edge']} auc={vw['mean_auc']}  -> {b['verdict']}")
    print(f"SIGNAL: {s['action']}  confidence {s['confidence']:.1%}  "
          f"(prob_up {s['prob_up']:.3f}) @ {s['price']}")
