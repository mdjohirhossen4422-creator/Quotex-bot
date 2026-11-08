"""Combine EVERY signal source into one model, and judge it honestly.

This is the "use everything at once" experiment. For each candle it builds one
feature row from every block the repo has:

  * base features (26)        features.FEATURES      (returns, geometry, EMA/RSI/MACD...)
  * v2 features (20)          features_v2.EXTRA_FEATURES (structure, liquidity, regime, zones)
  * chart patterns (1)        patterns.pattern_vote (H&S, double, triangle, S/R, flag)
  * order flow (8)            tickflow.FEATURES      (tick count, up ratio, imbalance...)
  * engine votes (opt-in)     rule / smart / ensemble direction + strength

Label = did the NEXT candle close higher (the same rule the journal settles on).

The verdict is the whole point: pooled purged walk-forward over all pairs, sorted
by candle time, scored against the payout breakeven with a Wilson CI — plus a
coin-flip control. Combining everything cannot beat an edge that is not there,
and the `--zoo` mode demonstrates the specific trap: pick the best variant on a
validation split and watch it collapse on the held-out test.

    python combine.py --period 5                 # all blocks, honest verdict
    python combine.py --period 5 --zoo           # feature subsets x model families
    python combine.py --period 5 --engines       # also feed the 3 engine votes
"""
from __future__ import annotations

import argparse
import bisect
import random

import numpy as np

import features as base_features
import features_v2
import patterns as patterns_mod
import tickflow
from candle_store import CandleStore
from payout import DEFAULT_PAYOUT, breakeven as payout_breakeven
from provenance import classify_source
from validation import make_folds, metrics_from_outcomes

# feature-block sizes, used to slice the combined matrix for the zoo
N_BASE = len(base_features.FEATURES)
N_V2 = len(features_v2.EXTRA_FEATURES)
N_PATTERN = 1
N_OF = len(tickflow.FEATURES) + 1        # order-flow block + a "has ticks" flag
N_ENGINE = 6                              # 3 engines x (sign, strength)


# --------------------------------------------------------------------------- #
# Feature assembly
# --------------------------------------------------------------------------- #
def _pattern_col(o, c, h, l, i) -> float:
    v, _ = patterns_mod.pattern_vote(o, c, h, l, i)
    return {"CALL": 1.0, "PUT": -1.0}.get(v, 0.0)


def _orderflow_map(ticks, candles, period):
    """candle-start-time -> order-flow feature dict (None when too few ticks)."""
    if len(ticks) < 50:
        return {}
    ts = [t[0] for t in ticks]
    out = {}
    for cd in candles:
        t0 = cd[0]
        lo = bisect.bisect_left(ts, t0)
        hi = bisect.bisect_left(ts, t0 + period)
        f = tickflow.candle_features(ticks[lo:hi], t0, period)
        if f is not None:
            out[t0] = f
    return out


def _engine_votes(asset, candles, min_index):
    """Replay all three engines once; return idx -> [rule, smart, ens] sign+strength.

    O(n^2) in the series (each engine recomputes indicators over the prefix), so
    callers should cap `max_candles`. Engines keep their own debounce state, as
    live does.
    """
    from signal_bot import SignalEngine, Strategy
    from smart_engine import SmartEngine, SmartStrategy
    from ensemble import EnsembleEngine, EnsembleStrategy
    rule = SignalEngine(Strategy(), threshold=4)
    smart = SmartEngine(SmartStrategy())
    ens = EnsembleEngine(EnsembleStrategy())
    out: dict[int, list[float]] = {}

    def pack(sig, strength):
        if sig is None:
            return [0.0, 0.0]
        sign = 1.0 if sig.action == "CALL" else -1.0
        return [sign, strength]

    for i in range(min_index, len(candles)):
        pre = candles[:i + 1]
        rs = rule.evaluate(asset, pre)
        ss = smart.evaluate(pre)
        es = ens.evaluate(pre)
        out[i] = (pack(rs, abs(getattr(rs, "score", 0)) if rs else 0.0)
                  + pack(ss, getattr(ss, "confidence", 0.0) if ss else 0.0)
                  + pack(es, getattr(es, "score", 0) if es else 0.0))
    return out


def pair_rows(store, asset, period, *, min_index=60, max_candles=600,
              with_of=True, with_engines=False) -> list[tuple]:
    """Rows of (time, index, feature_vector, label) for one (asset, period)."""
    candles = store.load(asset, period, limit=max_candles)
    if len(candles) < min_index + 2:
        return []
    rows, labels, idxs = features_v2.build_matrix(candles, min_index=min_index)
    if not rows:
        return []
    o = [c[1] for c in candles]
    c = [c[2] for c in candles]
    h = [c[3] for c in candles]
    l = [c[4] for c in candles]

    of = {}
    if with_of:
        of = _orderflow_map(sorted(store.load_ticks(asset, limit=500000)),
                            candles, period)
    votes = _engine_votes(asset, candles, min_index) if with_engines else {}

    out = []
    for k, i in enumerate(idxs):
        feat = list(rows[k])                      # base + v2 (46)
        feat.append(_pattern_col(o, c, h, l, i))  # pattern (1)
        of_f = of.get(candles[i][0])
        if of_f is not None:
            feat += [of_f[x] for x in tickflow.FEATURES] + [1.0]
        else:
            feat += [0.0] * len(tickflow.FEATURES) + [0.0]
        if with_engines:
            feat += votes.get(i, [0.0] * N_ENGINE)
        out.append((candles[i][0], i, feat, labels[k]))
    return out


# --------------------------------------------------------------------------- #
# Model + honest pooled evaluation
# --------------------------------------------------------------------------- #
def _model(family: str, seed: int):
    if family == "logit":
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import make_pipeline
        return make_pipeline(StandardScaler(),
                             LogisticRegression(max_iter=2000, C=1.0))
    if family == "rf":
        from sklearn.ensemble import RandomForestClassifier
        return RandomForestClassifier(n_estimators=300, max_depth=6,
                                      min_samples_leaf=20, random_state=seed,
                                      n_jobs=-1)
    from sklearn.ensemble import GradientBoostingClassifier
    return GradientBoostingClassifier(n_estimators=200, max_depth=3,
                                      learning_rate=0.05, subsample=0.9,
                                      random_state=seed)


def _fold_oos(merged, cols, *, n_splits, min_train, embargo, family, seed):
    """Pooled purged walk-forward. Returns (val_metrics, test_metrics, coverage).

    `cols` selects feature columns (the zoo uses this to drop blocks). The
    second-to-last fold is reported as "validation" (what you would tune on); the
    last fold is the true held-out "test".
    """
    n = len(merged)
    folds = make_folds(n, n_splits=n_splits, min_train=min_train, embargo=embargo)
    per_fold = []
    for fi, (_start, tr_end, te_start, te_end) in enumerate(folds):
        train = merged[:tr_end]
        test = merged[te_start:te_end]
        if not test or len(train) < 100:
            continue
        Xtr = np.array([[r[2][j] for j in cols] for r in train])
        ytr = np.array([r[3] for r in train])
        if len(set(ytr.tolist())) < 2:
            continue
        model = _model(family, seed).fit(Xtr, ytr)
        Xte = np.array([[r[2][j] for j in cols] for r in test])
        p = model.predict_proba(Xte)[:, 1]
        pred = (p >= 0.5).astype(int)
        hits = int((pred == np.array([r[3] for r in test])).sum())
        per_fold.append((hits, len(test)))
    if len(per_fold) < 2:
        return None, None, 0.0
    val = metrics_from_outcomes([True] * per_fold[-2][0]
                                + [False] * (per_fold[-2][1] - per_fold[-2][0]),
                                payout=DEFAULT_PAYOUT, name="val")
    te_h, te_n = per_fold[-1]
    test = metrics_from_outcomes([True] * te_h + [False] * (te_n - te_h),
                                 payout=DEFAULT_PAYOUT, name="test")
    return val, test, (te_n / n if n else 0.0)


def evaluate(rows_by_pair, *, family="gbm", n_splits=5, min_train=0.4,
             embargo=1, seed=0, payout=DEFAULT_PAYOUT, cols=None) -> dict:
    merged = [r for rs in rows_by_pair.values() for r in rs]
    merged.sort(key=lambda r: r[0])
    if len(merged) < 300:
        return {"ok": False, "n": len(merged)}
    ncol = len(merged[0][2])
    cols = cols if cols is not None else list(range(ncol))
    val, test, cov = _fold_oos(merged, cols, n_splits=n_splits,
                               min_train=min_train, embargo=embargo,
                               family=family, seed=seed)
    if test is None:
        return {"ok": False, "n": len(merged)}
    return {"ok": True, "n": len(merged), "val": val, "test": test,
            "coverage": cov, "family": family, "n_features": len(cols),
            "payout": payout, "breakeven": payout_breakeven(payout)}


def search(rows_by_pair, *, n_candidates=60, seed=0, n_splits=3,
           payout=DEFAULT_PAYOUT) -> dict:
    """Try many candidate feature subsets; pick the best on VALIDATION.

    This is the honest demonstration of the trap. Searching enough variants is
    guaranteed to find one that looks good on a validation split by chance; the
    question is whether it survives on the untouched test split. Returns the best
    validation candidate and its test result, plus how many cleared 55% on
    validation vs on test.
    """
    merged = [r for rs in rows_by_pair.values() for r in rs]
    merged.sort(key=lambda r: r[0])
    if len(merged) < 300:
        return {"ok": False, "n": len(merged)}
    ncol = len(merged[0][2])
    rng = random.Random(seed)
    families = ["logit", "rf"]
    best = None
    val_over_55 = test_over_55 = 0
    for c in range(n_candidates):
        k = rng.randint(3, ncol)
        cols = sorted(rng.sample(range(ncol), k))
        fam = families[c % len(families)]
        val, test, _cov = _fold_oos(merged, cols, n_splits=n_splits,
                                    min_train=0.4, embargo=1, family=fam,
                                    seed=c)
        if test is None:
            continue
        if val.hit >= 0.55:
            val_over_55 += 1
        if test.hit >= 0.55:
            test_over_55 += 1
        if best is None or val.hit > best["val"].hit:
            best = {"val": val, "test": test, "family": fam, "n_features": k,
                    "cols": cols}
    if best is None:
        return {"ok": False, "n": len(merged)}
    best["ok"] = True
    best["n_candidates"] = n_candidates
    best["val_over_55"] = val_over_55
    best["test_over_55"] = test_over_55
    best["breakeven"] = payout_breakeven(payout)
    return best


# --------------------------------------------------------------------------- #
# Controls
# --------------------------------------------------------------------------- #
def coinflip_control(rows_by_pair, *, seed=0, payout=DEFAULT_PAYOUT) -> dict:
    """Same folds, random predictions: the null you must beat."""
    merged = [r for rs in rows_by_pair.values() for r in rs]
    merged.sort(key=lambda r: r[0])
    rng = random.Random(seed)
    n = len(merged)
    folds = make_folds(n, n_splits=5, min_train=0.4, embargo=1)
    hits = total = 0
    for _s, _tr, te_start, te_end in folds[:-1]:
        for r in merged[te_start:te_end]:
            hits += int((1 if rng.random() >= 0.5 else 0) == r[3])
            total += 1
    m = metrics_from_outcomes([True] * hits + [False] * (total - hits),
                              payout=payout, name="coinflip")
    return m


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def format_result(r: dict) -> str:
    if not r.get("ok"):
        return f"not enough data ({r.get('n', 0)} rows)"
    be = r["breakeven"] * 100
    v, t = r["val"], r["test"]
    lines = [f"combined model | {r['family']} | {r['n_features']} features | "
             f"{r['n']} rows | payout {r['payout']:.0%} (breakeven {be:.2f}%)",
             f"  validation fold : {v.hit * 100:5.1f}%  CI "
             f"[{v.ci_low * 100:.1f}, {v.ci_high * 100:.1f}]  n={v.n}",
             f"  held-out test   : {t.hit * 100:5.1f}%  CI "
             f"[{t.ci_low * 100:.1f}, {t.ci_high * 100:.1f}]  n={t.n}  p={t.p_value:.3f}"]
    verdict = ("BEATS breakeven" if t.ci_low > r["breakeven"]
               else "INDISTINGUISHABLE" if t.ci_high > r["breakeven"]
               else "BELOW")
    lines.append(f"  test verdict    : {verdict}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="combine every signal source, honestly")
    ap.add_argument("--db", default="data/candles.db")
    ap.add_argument("--period", type=int, default=5)
    ap.add_argument("--source", choices=["otc", "real", "all"], default="otc")
    ap.add_argument("--family", choices=["gbm", "rf", "logit"], default="gbm")
    ap.add_argument("--max-candles", type=int, default=600)
    ap.add_argument("--engines", action="store_true",
                    help="also feed rule/smart/ensemble votes (slow)")
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--payout", type=float, default=DEFAULT_PAYOUT)
    ap.add_argument("--zoo", action="store_true",
                    help="feature subsets x model families (shows the trap)")
    ap.add_argument("--search", type=int, default=0, metavar="N",
                    help="try N random feature subsets, pick best on validation")
    args = ap.parse_args()

    store = CandleStore(args.db)
    rows_by_pair = {}
    for asset, per in store.series(min_candles=62, periods=[args.period]):
        if args.source != "all" and classify_source(asset) != args.source:
            continue
        rs = pair_rows(store, asset, per, max_candles=args.max_candles,
                       with_engines=args.engines)
        if rs:
            rows_by_pair[asset] = rs
    total = sum(len(v) for v in rows_by_pair.values())
    print(f"\ncombine | {args.period}s | {len(rows_by_pair)} pairs | {total} rows "
          f"| engines={'on' if args.engines else 'off'}")

    if args.search:
        s = search(rows_by_pair, n_candidates=args.search, n_splits=3,
                   payout=args.payout)
        if not s.get("ok"):
            print(f"not enough data ({s.get('n', 0)} rows)")
            return
        print(f"\ntried {s['n_candidates']} candidate subsets (best picked on validation)")
        print(f"  candidates >=55% on validation : {s['val_over_55']}")
        print(f"  candidates >=55% on held-out   : {s['test_over_55']}")
        print(f"  best-on-validation : {s['family']} {s['n_features']} feats")
        print(f"    validation {s['val'].hit*100:.1f}%  ->  TEST {s['test'].hit*100:.1f}%"
              f"  CI[{s['test'].ci_low*100:.1f},{s['test'].ci_high*100:.1f}]"
              f"  breakeven {s['breakeven']*100:.2f}%")
        verdict = ("BEATS breakeven" if s["test"].ci_low > s["breakeven"]
                   else "INDISTINGUISHABLE" if s["test"].ci_high > s["breakeven"]
                   else "BELOW")
        print(f"    test verdict: {verdict}")
        return

    if args.zoo:
        subsets = {
            "base(26)": list(range(0, N_BASE)),
            "base+v2(46)": list(range(0, N_BASE + N_V2)),
            "base+v2+pattern(47)": list(range(0, N_BASE + N_V2 + N_PATTERN)),
            "ALL(+orderflow)": None,
        }
        print(f"\n{'variant':22}{'feat':>6}{'val%':>8}{'test%':>8}{'test CI':>18}  verdict")
        best = None
        for name, cols in subsets.items():
            for fam in ("gbm", "rf", "logit"):
                r = evaluate(rows_by_pair, family=fam, n_splits=args.splits,
                             payout=args.payout, cols=cols)
                if not r.get("ok"):
                    continue
                t = r["test"]
                vd = ("BEATS" if t.ci_low > r["breakeven"] else
                      "indist." if t.ci_high > r["breakeven"] else "below")
                print(f"{name+'/'+fam:22}{r['n_features']:>6}"
                      f"{r['val'].hit*100:>8.1f}{t.hit*100:>8.1f}"
                      f"   [{t.ci_low*100:4.1f},{t.ci_high*100:4.1f}]  {vd}")
                if best is None or r["val"].hit > best[1]["val"].hit:
                    best = (f"{name}/{fam}", r)
        if best:
            print(f"\nbest-on-validation: {best[0]}  val "
                  f"{best[1]['val'].hit*100:.1f}%  ->  TEST "
                  f"{best[1]['test'].hit*100:.1f}% "
                  f"CI[{best[1]['test'].ci_low*100:.1f},{best[1]['test'].ci_high*100:.1f}]")
        return

    r = evaluate(rows_by_pair, family=args.family, n_splits=args.splits,
                 payout=args.payout)
    print(format_result(r))
    ctrl = coinflip_control(rows_by_pair, payout=args.payout)
    print(f"  coin-flip control: {ctrl.hit * 100:.1f}%  CI "
          f"[{ctrl.ci_low * 100:.1f}, {ctrl.ci_high * 100:.1f}]  n={ctrl.n}")


if __name__ == "__main__":
    main()
