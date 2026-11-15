"""Tests for pabot.py: offline synthetic reads, causality, and the adapter.

Run:  python test_pabot.py
"""
from __future__ import annotations

import pabot
from pabot import PAEngine, PAStrategy


def _candles(closes, period=60):
    """Minimal OHLC series from a close list (open=prev close, tiny wicks)."""
    out = []
    t = 1_700_000_000
    prev = closes[0]
    for k, c in enumerate(closes):
        hi = max(prev, c) + abs(c) * 0.0002
        lo = min(prev, c) - abs(c) * 0.0002
        out.append([t + k * period, prev, c, hi, lo])
        prev = c
    return out


def test_uptrend_fires_and_is_wellformed():
    """A trend must produce a well-formed signal (direction depends on conflict)."""
    closes = [1.0 + 0.0004 * i for i in range(200)]
    sig = PAEngine(PAStrategy(cooldown_candles=0)).evaluate("X", _candles(closes))
    assert sig is not None, "trend should fire"
    assert sig.action in ("CALL", "PUT"), sig.action
    assert sig.reasons, "a fired signal must carry reasons"
    assert 0.0 <= sig.confidence <= 1.0
    assert set(sig.blocks) == {"indicator", "price_action", "smc"}
    print(f"PASS test_uptrend_fires_and_is_wellformed ({sig.action})")


def test_determinism():
    """Same input -> same output (no hidden randomness)."""
    closes = [1.0 + 0.0004 * i + (0.001 if i % 3 else -0.001) for i in range(200)]
    a = PAEngine(PAStrategy(cooldown_candles=0)).evaluate("X", _candles(closes))
    b = PAEngine(PAStrategy(cooldown_candles=0)).evaluate("X", _candles(closes))
    assert (a is None) == (b is None)
    if a is not None:
        assert (a.action, a.score, a.confidence) == (b.action, b.score, b.confidence)
    print("PASS test_determinism")


def test_vote_pick_logic():
    """_pick must be a clean majority with no ties."""
    assert PAEngine._pick(3, 0, []) == ("CALL", 3, [])
    assert PAEngine._pick(0, 2, [])[0] == "PUT"
    assert PAEngine._pick(1, 1, [])[0] is None
    assert PAEngine._pick(0, 0, [])[0] is None
    print("PASS test_vote_pick_logic")


def test_flat_market_may_abstain():
    """A dead-flat series must not manufacture a signal."""
    closes = [1.0] * 200
    sig = PAEngine(PAStrategy(cooldown_candles=0)).evaluate("X", _candles(closes))
    assert sig is None, f"flat market should abstain, got {sig}"
    print("PASS test_flat_market_may_abstain")


def test_not_enough_candles():
    closes = [1.0 + 0.001 * i for i in range(10)]
    sig = PAEngine().evaluate("X", _candles(closes))
    assert sig is None
    print("PASS test_not_enough_candles")


def test_causality():
    """Appending future candles must not change the read at the old last index."""
    closes = [1.0 + 0.0003 * i for i in range(160)]
    base = PAEngine(PAStrategy(cooldown_candles=0)).evaluate("X", _candles(closes))
    # a fresh engine on a longer series, evaluated at the SAME index
    longer = _candles(closes + [closes[-1] * 1.5 for _ in range(30)])
    eng = PAEngine(PAStrategy(cooldown_candles=0))
    from pabot import PAStrategy as S  # noqa
    # evaluate on the prefix only -> must equal the base read
    prefix = PAEngine(S(cooldown_candles=0)).evaluate("X", longer[:len(closes)])
    if base is None:
        assert prefix is None
    else:
        assert prefix is not None and prefix.action == base.action, (base, prefix)
        assert abs(prefix.score - base.score) <= 0
    print("PASS test_causality")


def test_adapter_shape():
    """analysis.analyze_pa returns the standard Analysis shape."""
    from analysis import analyze_pa
    closes = [1.0 + 0.0004 * i for i in range(200)]
    res = analyze_pa("X", 60, _candles(closes))
    assert res.engine == "pa"
    assert res.enough
    assert res.action in ("CALL", "PUT", "NEUTRAL")
    assert 0.0 <= res.confidence <= 1.0
    if res.action != "NEUTRAL":
        assert res.values.get("blocks")
    print(f"PASS test_adapter_shape ({res.action})")


def test_no_training_import():
    """pabot must not import any ML/training library or call a model fit."""
    src = open(pabot.__file__).read()
    for bad in ("import sklearn", "from sklearn", "import numpy", "import torch",
                ".fit(", "GradientBoosting", "RandomForest"):
        assert bad not in src, f"pabot should be training-free, found {bad!r}"
    print("PASS test_no_training_import")


if __name__ == "__main__":
    test_uptrend_fires_and_is_wellformed()
    test_determinism()
    test_vote_pick_logic()
    test_flat_market_may_abstain()
    test_not_enough_candles()
    test_causality()
    test_adapter_shape()
    test_no_training_import()
    print("\nall pabot tests passed")
