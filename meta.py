#!/usr/bin/env python3
"""Meta-labeling gate: keep the signals the engine gets right, drop the rest.

The engines fire on a fixed rule and treat every signal as equal. Meta-labeling
(López de Prado) adds a second model whose only job is to predict, *given a
signal has fired*, whether that particular signal will win. Signals the meta
model scores below a floor are dropped. The primary engine still decides the
direction; the meta model decides whether to act at all.

Why this is the honest way to "increase accuracy":

  * It does not invent a direction. It only filters the engine's existing
    CALL/PUT signals, so a bad filter can never make the direction worse.
  * It is judged the only way that matters — purged walk-forward, on signals the
    model has never seen, against the payout breakeven, with a Wilson CI.
  * It reports coverage alongside precision. A filter that keeps 3% of signals
    and calls it "accurate" is exposed by the coverage column.
  * It carries a **negative control** (random-walk signals: the gate must not
    help) and a **positive control** (signals with an injected, learnable edge:
    the gate must help). Without both, "no improvement" would be meaningless.

If the gate cannot beat the ungated engine out-of-sample, the tool says so and
the gate is not worth using. That is the expected result on this feed.

Signals are alerts only — this never places a trade.

Usage
-----
    python meta.py --period 60 --engine rule --source otc
    python meta.py --period 5 --engine ensemble --threshold 0.55
    python meta.py --control            # negative + positive controls, no db
"""
from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from typing import Optional

import numpy as np

import features as base_features
from candle_store import CandleStore
from payout import DEFAULT_PAYOUT, breakeven as payout_breakeven
from provenance import classify_source
from validation import make_folds, metrics_from_outcomes

PERIOD_LABEL = {5: "5s", 15: "15s", 30: "30s", 60: "1m",
                120: "2m", 300: "5m", 600: "10m", 900: "15m"}

# Meta features = the engine's own read + the base causal feature vector.
# Kept to things computable identically offline and live, so the model sees the
# same inputs in both (no train/live skew).
META_EXTRA = ["engine_score", "action_sign"]
META_FEATURES = META_EXTRA + base_features.FEATURES

# The engine replay recomputes indicators over the whole prefix at each step, so
# it is O(n^2) in the series length. Capping the history keeps a scan pass fast
# as days of candles accumulate; the newest signals are what the gate acts on.
GATE_MAX_CANDLES = 800


# --------------------------------------------------------------------------- #
# Signal collection
# --------------------------------------------------------------------------- #

@dataclass
class SignalRow:
    idx: int                    # candle index the signal was read from
    label: int                  # 1 if the next candle closed up, 0 if down
    action: str                 # CALL | PUT
    score: int                  # engine's raw strength
    feats: list[float]          # meta feature vector (causal)
    t: float = 0.0              # candle close time (for time-based folds)


def collect_signals(engine_name: str, asset: str, candles: list[list],
                    min_index: int = 60, use_v2: bool = True) -> list[SignalRow]:
    """Replay the engine over a candle series; return one row per signal.

    A fresh engine is built here so debounce/cooldown state is that engine's own
    and never shared across series. When `use_v2` is set, each signal also gets
    the structure/liquidity/regime/zone features from `features_v2` (causal, one
    matrix build for the whole series) so the meta model can use them too.
    """
    if len(candles) < min_index + 2:
        return []
    # one engine instance for the whole replay so debounce behaves as live
    if engine_name == "rule":
        from signal_bot import SignalEngine, Strategy
        eng = SignalEngine(Strategy(), threshold=4)
        evaluate = lambda i: eng.evaluate(asset, candles[:i + 1])
    elif engine_name == "ensemble":
        from ensemble import EnsembleEngine, EnsembleStrategy
        eng = EnsembleEngine(EnsembleStrategy())
        evaluate = lambda i: eng.evaluate(candles[:i + 1])
    elif engine_name == "smart":
        from smart_engine import SmartEngine, SmartStrategy
        eng = SmartEngine(SmartStrategy())
        evaluate = lambda i: eng.evaluate(candles[:i + 1])
    else:
        raise ValueError(f"unknown engine {engine_name!r}")

    v2_by_idx: dict[int, list[float]] = {}
    if use_v2:
        import features_v2
        rows_v2, _labels, idxs = features_v2.build_matrix(candles, min_index=min_index)
        v2_by_idx = {idx: row for idx, row in zip(idxs, rows_v2)}

    rows: list[SignalRow] = []
    for i in range(min_index, len(candles) - 1):
        sig = evaluate(i)
        if sig is None:
            continue
        nxt = candles[i + 1][2]
        cur = candles[i][2]
        if nxt == cur:
            continue                     # flat: a tie, not a win/loss
        label = 1 if nxt > cur else 0
        base = base_features.compute_features(candles, i)
        if base is None:
            continue
        # engines report strength differently: rule/ensemble -> score (int),
        # smart -> confidence (0..1). Normalise both to a comparable magnitude.
        raw = getattr(sig, "score", None)
        if raw is None:
            raw = float(getattr(sig, "confidence", 0.0)) * 10.0
        row_feats = [
            float(abs(raw)),
            1.0 if sig.action == "CALL" else -1.0,
        ] + base_features.features_to_vector(base)
        if use_v2:
            row_feats += v2_by_idx.get(i, [0.0] * len(features_v2.EXTRA_FEATURES))
        rows.append(SignalRow(idx=i, label=label, action=sig.action,
                              score=abs(raw), feats=row_feats,
                              t=float(candles[i][0])))
    return rows


# --------------------------------------------------------------------------- #
# Meta model + gate evaluation
# --------------------------------------------------------------------------- #

def _fit_meta(Xtr: np.ndarray, ytr: np.ndarray, seed: int = 0):
    """Small gradient-boosted classifier. Kept deliberately shallow: with a few
    hundred signals, a deep model would memorise the train fold."""
    from sklearn.ensemble import GradientBoostingClassifier
    m = GradientBoostingClassifier(n_estimators=100, max_depth=2,
                                   learning_rate=0.05, subsample=0.9,
                                   random_state=seed)
    m.fit(Xtr, ytr)
    return m


def _outcome(r: SignalRow) -> bool:
    """Did this signal win? CALL wins on an up candle, PUT on a down candle."""
    up = r.label == 1
    return up if r.action == "CALL" else not up


def evaluate_gate(tables: dict[str, list[SignalRow]], *, payout: float,
                  n_splits: int = 5, min_train: float = 0.4, embargo: int = 1,
                  threshold: float = 0.55, min_train_signals: int = 60,
                  seed: int = 0, mode: str = "pooled") -> dict:
    """Purged walk-forward: does gating beat keeping every signal?

    Two modes:

    * `mode="pooled"` (default): all pairs' signals are merged and sorted by
      candle time; folds are cut by *time*, so a fold trains on signals that
      happened before the ones it is tested on. Pooling is what makes the
      sample usable here — a single pair has far too few signals. The meta model
      is therefore global (one model for all pairs).
    * `mode="per_pair"`: each series gets its own folds and its own model, so the
      gate is judged within a pair. Needs many signals per pair to be meaningful.

    In each fold the model is fit on the train signals and applied to the test
    signals; a test signal is kept when P(win) >= `threshold`. We pool kept test
    signals and report precision, coverage and a CI, alongside the ungated
    baseline on the very same test signals. Folds with fewer than
    `min_train_signals` training signals keep everything (no data to learn from).
    """
    kept_outcomes: list[bool] = []
    all_outcomes: list[bool] = []
    folds_used = 0

    if mode == "pooled":
        merged: list[SignalRow] = [r for rows in tables.values() for r in rows]
        merged.sort(key=lambda r: r.t)
        n = len(merged)
        fold_defs = make_folds(n, n_splits=n_splits, min_train=min_train,
                               embargo=embargo) if n else []
        for _, tr_end, te_start, te_end in fold_defs:
            train = merged[:tr_end]
            test = merged[te_start:te_end]
            if not test:
                continue
            test_outcomes = [_outcome(r) for r in test]
            all_outcomes.extend(test_outcomes)
            if len(train) < min_train_signals:
                kept_outcomes.extend(test_outcomes)
                continue
            ytr = np.array([1 if _outcome(r) else 0 for r in train])
            if len(set(ytr.tolist())) < 2:
                kept_outcomes.extend(test_outcomes)
                continue
            model = _fit_meta(np.array([r.feats for r in train]), ytr, seed=seed)
            p = model.predict_proba(np.array([r.feats for r in test]))[:, 1]
            for prob, out in zip(p, test_outcomes):
                if prob >= threshold:
                    kept_outcomes.append(out)
            folds_used += 1
    else:
        for asset, rows in tables.items():
            n = len(rows)
            if n < min_train_signals + 5:
                continue
            fold_defs = make_folds(n, n_splits=n_splits, min_train=min_train,
                                   embargo=embargo)
            for _, tr_end, te_start, te_end in fold_defs:
                train = rows[:tr_end]
                test = rows[te_start:te_end]
                if not test:
                    continue
                test_outcomes = [_outcome(r) for r in test]
                all_outcomes.extend(test_outcomes)
                if len(train) < min_train_signals:
                    kept_outcomes.extend(test_outcomes)
                    continue
                ytr = np.array([1 if _outcome(r) else 0 for r in train])
                if len(set(ytr.tolist())) < 2:
                    kept_outcomes.extend(test_outcomes)
                    continue
                model = _fit_meta(np.array([r.feats for r in train]), ytr, seed=seed)
                p = model.predict_proba(np.array([r.feats for r in test]))[:, 1]
                for prob, out in zip(p, test_outcomes):
                    if prob >= threshold:
                        kept_outcomes.append(out)
                folds_used += 1

    base = metrics_from_outcomes(all_outcomes, payout=payout, name="all signals")
    gate = metrics_from_outcomes(kept_outcomes, payout=payout, name="gated")
    coverage = (gate.n / base.n) if base.n else 0.0
    return {
        "baseline": base, "gated": gate, "coverage": coverage,
        "threshold": threshold, "folds": folds_used, "mode": mode,
    }


def format_gate(r: dict, payout: float) -> str:
    b, g = r["baseline"], r["gated"]
    be = payout_breakeven(payout)
    lines = [f"\n=== meta-labeling gate | payout {payout:.0%} "
             f"(breakeven {be * 100:.2f}%) | threshold {r['threshold']:.2f} "
             f"| {r.get('mode', 'pooled')} ==="]
    lines.append(f"  {'':16}{'signals':>9}{'hit%':>7}   {'95% CI':>16}  "
                 f"{'EV':>9}  verdict")
    for name, m in (("engine (all)", b), ("gated (kept)", g)):
        if not m.n:
            lines.append(f"  {name:16}{'0':>9}      -   {'-':>16}  {'-':>9}  -")
            continue
        lines.append(
            f"  {name:16}{m.n:>9}{m.hit * 100:>6.1f}%   "
            f"[{m.ci_low * 100:>5.1f},{m.ci_high * 100:>5.1f}]  "
            f"{m.ev:>+8.4f}  {m.verdict}")
    lines.append(f"  coverage kept   : {r['coverage'] * 100:.1f}% "
                 f"({g.n} of {b.n} test signals)")
    if b.n and g.n:
        delta = g.hit - b.hit
        lines.append(f"  precision gain  : {delta * 100:+.2f} pp  "
                     f"(gated {g.hit * 100:.1f}% vs engine {b.hit * 100:.1f}%)")
        if g.ci_low > be and b.ci_low <= be:
            lines.append("  RESULT          : gated clears breakeven, engine does not")
        elif g.hit > b.hit and g.ci_low > be:
            lines.append("  RESULT          : gated improves AND clears breakeven")
        elif g.hit > b.hit:
            lines.append("  RESULT          : gated looks better but CI still "
                         "straddles breakeven — not a usable edge")
        else:
            lines.append("  RESULT          : gating did NOT improve precision — "
                         "keep every signal")
    lines.append("  NOTE: a real filter must beat the engine on unseen signals; "
                 "coverage tells you what it cost.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Live gate — fit on history, score the newest signal
# --------------------------------------------------------------------------- #

@dataclass
class GateResult:
    keep: bool
    p_win: float
    n_train: int
    reason: str = ""


def live_gate(tables: dict[str, list[SignalRow]], asset: str, period: int, *,
              threshold: float = 0.55, min_train_signals: int = 60,
              seed: int = 0) -> Optional[GateResult]:
    """Score the newest signal of (asset, period) with the pooled meta model.

    Thin wrapper over `live_gate_batch` so the single-pair and batch paths can
    never disagree. Trains on every *earlier* signal across all pairs (never any
    pair's newest), so the decision is out-of-sample by construction. Returns
    None when there is not enough history or no fresh signal. The direction is
    the engine's; the gate only decides `keep`.
    """
    return live_gate_batch(tables, threshold=threshold,
                           min_train_signals=min_train_signals,
                           seed=seed).get(asset)


def live_gate_batch(tables: dict[str, list[SignalRow]], *,
                    threshold: float = 0.55, min_train_signals: int = 60,
                    seed: int = 0) -> dict[str, GateResult]:
    """Gate the newest signal of every pair with one pooled model.

    Fits a single model on all-but-the-newest signals of every pair, then scores
    each pair's newest signal. One fit per scan pass instead of one per signal —
    same out-of-sample guarantee, far cheaper. Pairs with too little history are
    absent from the result (the caller keeps those signals unfiltered).
    """
    train: list[SignalRow] = []
    newest: dict[str, SignalRow] = {}
    for asset, rows in tables.items():
        if len(rows) < 2:
            continue
        newest[asset] = rows[-1]
        train.extend(rows[:-1])
    if not newest or len(train) < min_train_signals:
        return {}
    ytr = np.array([1 if _outcome(r) else 0 for r in train])
    if len(set(ytr.tolist())) < 2:
        return {}
    model = _fit_meta(np.array([r.feats for r in train]), ytr, seed=seed)
    assets = list(newest.keys())
    p = model.predict_proba(np.array([newest[a].feats for a in assets]))[:, 1]
    return {a: GateResult(keep=float(pi) >= threshold, p_win=float(pi),
                          n_train=len(train),
                          reason=f"meta P(win)={pi:.3f} vs {threshold:.2f}")
            for a, pi in zip(assets, p)}


# --------------------------------------------------------------------------- #
# Controls — prove the gate machinery can and cannot find an edge
# --------------------------------------------------------------------------- #

def _synthetic_tables(kind: str, n_series: int = 4, n_signals: int = 600,
                      seed: int = 0) -> dict[str, list[SignalRow]]:
    """Build signal tables with a known answer.

    kind='random'  : labels are a fair coin, independent of the features.
    kind='edge'    : label is a noisy function of feature 0 (a learnable edge),
                     so a correct gate must raise precision out-of-sample.
    """
    rng = random.Random(seed)
    tables: dict[str, list[SignalRow]] = {}
    for s in range(n_series):
        rows: list[SignalRow] = []
        for k in range(n_signals):
            feats = [rng.gauss(0, 1) for _ in range(len(META_FEATURES))]
            action = "CALL" if rng.random() < 0.5 else "PUT"
            feats[1] = 1.0 if action == "CALL" else -1.0   # action_sign
            if kind == "edge":
                # P(win) is a direct, monotone function of feature 0, so the
                # gate must keep the high-feature-0 signals to raise precision.
                p_win = 1.0 / (1.0 + np.exp(-2.5 * feats[0]))
                won = rng.random() < p_win
                label = (1 if action == "CALL" else 0) if won else (0 if action == "CALL" else 1)
            else:
                label = 1 if rng.random() < 0.5 else 0
            rows.append(SignalRow(idx=k, label=label, action=action,
                                  score=4, feats=feats, t=float(s * n_signals + k)))
        tables[f"SYN{s}"] = rows
    return tables


def run_controls(payout: float = DEFAULT_PAYOUT, threshold: float = 0.55) -> int:
    print("=== meta gate controls ===")
    fails = 0

    rnd = evaluate_gate(_synthetic_tables("random", seed=1), payout=payout,
                        threshold=threshold)
    print(f"  [negative] random labels : engine {rnd['baseline'].hit * 100:.1f}%  "
          f"gated {rnd['gated'].hit * 100:.1f}%  "
          f"(gain {(rnd['gated'].hit - rnd['baseline'].hit) * 100:+.1f} pp, "
          f"coverage {rnd['coverage'] * 100:.0f}%)")
    if rnd["gated"].hit - rnd["baseline"].hit > 0.04:
        print("     FAIL: the gate invented an edge on random labels")
        fails += 1

    edge = evaluate_gate(_synthetic_tables("edge", seed=2), payout=payout,
                         threshold=threshold)
    print(f"  [positive] learnable edge: engine {edge['baseline'].hit * 100:.1f}%  "
          f"gated {edge['gated'].hit * 100:.1f}%  "
          f"(gain {(edge['gated'].hit - edge['baseline'].hit) * 100:+.1f} pp, "
          f"coverage {edge['coverage'] * 100:.0f}%)")
    if edge["gated"].hit <= edge["baseline"].hit:
        print("     FAIL: the gate did not exploit a learnable edge")
        fails += 1

    print("  both controls behave correctly" if not fails else f"  {fails} control(s) FAILED")
    return fails


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _load_tables(store: CandleStore, engine: str, period: int, source: str,
                 limit: int = 0) -> dict[str, list[SignalRow]]:
    tables: dict[str, list[SignalRow]] = {}
    for asset, per in store.series(min_candles=62, periods=[period]):
        if classify_source(asset) != source:
            continue
        candles = store.load(asset, per, limit=GATE_MAX_CANDLES)
        rows = collect_signals(engine, asset, candles)
        if rows:
            tables[asset] = rows
    if limit:
        tables = dict(list(tables.items())[:limit])
    return tables


def main():
    ap = argparse.ArgumentParser(description="Meta-labeling signal gate")
    ap.add_argument("--db", default="data/candles.db")
    ap.add_argument("--period", type=int, default=60)
    ap.add_argument("--engine", choices=["rule", "smart", "ensemble"], default="rule")
    ap.add_argument("--source", choices=["otc", "real", "synthetic"], default="otc")
    ap.add_argument("--payout", type=float, default=DEFAULT_PAYOUT)
    ap.add_argument("--threshold", type=float, default=0.55,
                    help="keep a signal when P(win) >= this")
    ap.add_argument("--mode", choices=["pooled", "per_pair"], default="pooled",
                    help="pooled (default, time folds across pairs) or per_pair")
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--embargo", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--control", action="store_true",
                    help="run the synthetic controls instead of the live db")
    args = ap.parse_args()

    if args.control:
        return run_controls(args.payout, args.threshold)

    store = CandleStore(args.db)
    tables = _load_tables(store, args.engine, args.period, args.source, args.limit)
    total = sum(len(v) for v in tables.values())
    if not tables:
        print(f"no {args.source} signals for period {args.period} — collect more data")
        return
    print(f"\nmeta-labeling | {PERIOD_LABEL.get(args.period, args.period)} | "
          f"engine {args.engine} | {len(tables)} pairs | {total} signals")
    r = evaluate_gate(tables, payout=args.payout, n_splits=args.splits,
                      embargo=args.embargo, threshold=args.threshold,
                      mode=args.mode)
    print(format_gate(r, args.payout))


if __name__ == "__main__":
    main()
