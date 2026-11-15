#!/usr/bin/env python3
"""End-to-end tests for the AI Signal System (Phase 3).

Covers the real code paths (no mocks):

  * `signal_features` is causal — a future change cannot alter a past row,
  * `train_signal_model` learns a real pattern (momentum) and shows ~no edge on
    a random walk (negative control),
  * real and OTC never mix: a model trained on one source is not applied to the
    other,
  * `ai_signal_engine` loads the trained model and emits CALL/PUT, abstains when
    confidence is low, and falls back to the heuristic with no model,
  * the heuristic is not fooled by a random walk into a strong signal.

Run:  python test_ai_signal.py
"""
from __future__ import annotations

import os
import random
import sqlite3
import tempfile

from provenance import FEED_ID, classify_source
from tick_summary import SUMMARY_FIELDS

PERIOD = 60


def _schema(con: sqlite3.Connection):
    cols = ["asset TEXT", "period INTEGER", "time INTEGER", "open REAL",
            "close REAL", "high REAL", "low REAL", "source TEXT",
            "feed_id TEXT"] + [f"{c} REAL" for c in SUMMARY_FIELDS]
    con.execute(f"CREATE TABLE IF NOT EXISTS candles({', '.join(cols)}, "
                f"PRIMARY KEY(asset, period, time))")
    con.execute("CREATE TABLE IF NOT EXISTS ticks("
                "asset TEXT, ts REAL, price REAL, dir INTEGER)")


def _make(n: int, mom: float, seed: int, start: float = 1.10,
          vol: float = 0.0004):
    rng = random.Random(seed)
    t = 1_700_000_000
    price = start
    drift = 0.0
    out = []
    for i in range(n):
        o = price
        drift = mom * drift + rng.gauss(0, vol)
        price += drift
        c = price
        hi = max(o, c) + abs(rng.gauss(0, vol / 2))
        lo = min(o, c) - abs(rng.gauss(0, vol / 2))
        out.append([t + i * PERIOD, o, c, hi, lo])
    return out


def _summary(candle):
    up = 40 if candle[2] > candle[1] else 10
    dn = 10 if candle[2] > candle[1] else 40
    first = candle[1] or 1.0
    return (up + dn + 5, up, dn, 5, (up - dn) / (up + dn),
            (candle[2] - candle[1]) / first, abs(candle[2] - candle[1]) / first,
            (candle[3] - candle[4]) / first, 0.5)


def _seed(db: str, asset: str, candles: list[list]):
    con = sqlite3.connect(db)
    _schema(con)
    cols = ("asset,period,time,open,close,high,low,source,feed_id,"
            + ",".join(SUMMARY_FIELDS))
    ph = ",".join(["?"] * (9 + len(SUMMARY_FIELDS)))
    for c in candles:
        con.execute(f"INSERT OR IGNORE INTO candles({cols}) VALUES({ph})",
                    (asset, PERIOD, c[0], c[1], c[2], c[3], c[4],
                     classify_source(asset), FEED_ID) + _summary(c))
    con.commit()
    con.close()


def _tmpdb() -> str:
    d = tempfile.mkdtemp()
    return os.path.join(d, "t.db")


# --------------------------------------------------------------------------- #
def test_features_are_causal():
    from signal_features import compute_row
    candles = _make(200, 0.5, seed=1)
    i = 120
    row_a = compute_row(candles, i)
    mutated = [list(c) for c in candles]
    for k in range(i + 1, len(mutated)):        # change only the FUTURE
        mutated[k][2] = mutated[k][2] * 1.5
    row_b = compute_row(mutated, i)
    assert row_a == row_b, "a future change altered a past feature row"
    print("PASS test_features_are_causal")


def test_momentum_is_learned_random_is_not():
    import train_signal_model as T
    db = _tmpdb()
    _seed(db, "EURUSD_otc", _make(3000, 0.7, seed=2))   # momentum -> learnable
    _seed(db, "EURUSD", _make(3000, 0.0, seed=3))       # random walk -> not
    otc = T.train("otc", PERIOD, db, 0, False, 5, 0.4, 2, 0.85, 200, write=False)
    real = T.train("real", PERIOD, db, 0, False, 5, 0.4, 2, 0.85, 200, write=False)
    assert otc["status"] == "trained" and real["status"] == "trained"
    # a real pattern must be learnable out-of-sample
    assert otc["oos"]["hit"] > 0.56, f"momentum not learned: {otc['oos']['hit']}"
    # a random walk must NOT show an edge (negative control)
    assert real["oos"]["hit"] < 0.56, f"random walk faked an edge: {real['oos']['hit']}"
    print(f"PASS test_momentum_is_learned_random_is_not "
          f"(momentum {otc['oos']['hit']*100:.1f}% / random "
          f"{real['oos']['hit']*100:.1f}%)")


def test_sources_never_mix():
    import train_signal_model as T
    db = _tmpdb()
    _seed(db, "EURUSD_otc", _make(1200, 0.7, seed=4))
    _seed(db, "EURUSD", _make(1200, 0.7, seed=5))
    otc = T.train("otc", PERIOD, db, 0, False, 4, 0.4, 2, 0.85, 200, write=False)
    real = T.train("real", PERIOD, db, 0, False, 4, 0.4, 2, 0.85, 200, write=False)
    assert otc["n_pairs"] == 1 and real["n_pairs"] == 1, "sources were pooled"
    assert otc["source"] == "otc" and real["source"] == "real"
    print("PASS test_sources_never_mix")


def test_engine_uses_model_and_abstains_on_low_confidence():
    import train_signal_model as T
    from ai_signal_engine import AISignalEngine, load_model
    db = _tmpdb()
    _seed(db, "EURUSD_otc", _make(2500, 0.7, seed=6))
    T.train("otc", PERIOD, db, 0, False, 5, 0.4, 2, 0.85, 200, write=True)
    # the artifact landed where the engine looks for it
    assert load_model("otc", PERIOD) is not None, "model artifact not found"
    eng = AISignalEngine(db, min_confidence=0.50)      # permissive -> fires
    res = eng.evaluate("EURUSD_otc", PERIOD)
    assert res.enough and res.action in ("CALL", "PUT"), res.action
    assert "model_prob_up" in res.values
    eng2 = AISignalEngine(db, min_confidence=0.99)     # impossible -> abstain
    res2 = eng2.evaluate("EURUSD_otc", PERIOD)
    assert res2.action == "NEUTRAL", "must abstain when confidence is too low"
    # clean the artifacts so the repo stays tidy
    for f in (f"models/otc_{PERIOD}s.pkl", f"models/otc_{PERIOD}s.json"):
        if os.path.exists(f):
            os.remove(f)
    print("PASS test_engine_uses_model_and_abstains_on_low_confidence")


def test_engine_falls_back_to_heuristic_without_model():
    from ai_signal_engine import AISignalEngine
    db = _tmpdb()
    _seed(db, "EURUSD_otc", _make(400, 0.7, seed=7))
    eng = AISignalEngine(db, model_dir="/nonexistent", min_confidence=0.55)
    res = eng.evaluate("EURUSD_otc", PERIOD)
    assert res.enough
    assert res.action in ("CALL", "PUT", "NEUTRAL")
    assert any("no trained model" in r for r in res.reasons), "no fallback note"
    print("PASS test_engine_falls_back_to_heuristic_without_model")


def test_negative_control_random_walk_heuristic_is_not_strong():
    from ai_signal_engine import AISignalEngine
    from signal_data import SignalData
    db = _tmpdb()
    _seed(db, "EURUSD_otc", _make(600, 0.0, seed=8))     # pure random walk
    eng = AISignalEngine(db, model_dir="/nonexistent", min_confidence=0.55)
    sd = SignalData(db)
    candles = sd.load("EURUSD_otc", PERIOD)
    sd.close()
    strong = 0
    for cut in range(300, 600, 25):
        r = eng.evaluate("EURUSD_otc", PERIOD, candles=candles[:cut],
                         summaries=[None] * cut)
        if r.confidence >= 0.70:
            strong += 1
    # a random walk must not look strongly predictable
    assert strong <= 2, f"random walk looked strong {strong} times"
    print("PASS test_negative_control_random_walk_heuristic_is_not_strong")


if __name__ == "__main__":
    test_features_are_causal()
    test_momentum_is_learned_random_is_not()
    test_sources_never_mix()
    test_engine_uses_model_and_abstains_on_low_confidence()
    test_engine_falls_back_to_heuristic_without_model()
    test_negative_control_random_walk_heuristic_is_not_strong()
    print("\nall AI-signal tests passed")
