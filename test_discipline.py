"""Tests for discipline.py: checklist gates, abstain-by-default, honest status.

Run:  python test_discipline.py
"""
from __future__ import annotations

import discipline
from discipline import DisciplineConfig, NO_TRADE, CALL, PUT


def _candles(closes, period=60):
    out, t, prev = [], 1_700_000_000, closes[0]
    for k, c in enumerate(closes):
        out.append([t + k * period, prev, c,
                    max(prev, c) + abs(c) * 0.0002,
                    min(prev, c) - abs(c) * 0.0002])
        prev = c
    return out


def _store(tmp):
    from candle_store import CandleStore
    s = CandleStore(tmp)
    return s


def test_blocked_when_no_data():
    st = discipline.assess(_store(":memory:"), "X", 60)
    assert st.status == "no_trade"
    assert st.action == NO_TRADE
    print("PASS test_blocked_when_no_data")


def test_flat_market_is_no_trade():
    """A frozen series must never produce a signal."""
    st = discipline.assess(_store(":memory:"), "X", 60,
                           candles=_candles([1.0] * 200))
    assert st.status == "no_trade", st.status
    print("PASS test_flat_market_is_no_trade")


def test_gates_are_all_reported():
    """The checklist must list every gate, in order."""
    st = discipline.assess(_store(":memory:"), "X", 60,
                           candles=_candles([1.0 + 0.0004 * i for i in range(200)]))
    names = [c[0] for c in st.checks]
    assert names[:3] == ["data quality", "regime defined", "structure directional"]
    assert "confluence" in names
    print(f"PASS test_gates_are_all_reported ({len(names)} gates)")


def test_short_series_is_no_trade():
    st = discipline.assess(_store(":memory:"), "X", 60,
                           candles=_candles([1.0 + 0.001 * i for i in range(10)]))
    assert st.status == "no_trade"
    print("PASS test_short_series_is_no_trade")


def test_candidate_not_signal_without_proof():
    """With proven=False, a fully-passing setup is a candidate, never a signal."""
    closes = [1.0 + 0.0006 * i for i in range(220)]
    cs = _candles(closes)
    st = discipline.assess(_store(":memory:"), "X", 60, candles=cs, proven=False)
    if st.status != "no_trade":
        assert st.status == "candidate", st.status
    st2 = discipline.assess(_store(":memory:"), "X", 60, candles=cs, proven=True)
    if st2.status != "no_trade":
        assert st2.status == "signal", st2.status
    print("PASS test_candidate_not_signal_without_proof")


def test_edge_proven_requires_evidence():
    """No journal rows -> edge not proven, and the EV is not fabricated."""
    cfg = DisciplineConfig()
    proven, info = discipline.edge_proven(60, cfg, db=":memory:")
    assert proven is False
    assert info["decisive"] == 0
    assert info["ev"] == 0.0
    print("PASS test_edge_proven_requires_evidence")


def test_recent_run_stops_at_gap():
    cs = _candles([1.0 + 0.001 * i for i in range(20)], period=60)
    # splice a 1-day gap
    shifted = [[c[0] + 86400, c[1], c[2], c[3], c[4]] for c in cs]
    joined = cs + shifted
    run = discipline._recent_run(joined, 60)
    assert len(run) == len(cs), (len(run), len(cs))
    print("PASS test_recent_run_stops_at_gap")


def test_breakeven_math():
    cfg = DisciplineConfig(payout=0.85)
    assert abs(cfg.breakeven() - 1 / 1.85) < 1e-9
    print("PASS test_breakeven_math")


if __name__ == "__main__":
    test_blocked_when_no_data()
    test_flat_market_is_no_trade()
    test_gates_are_all_reported()
    test_short_series_is_no_trade()
    test_candidate_not_signal_without_proof()
    test_edge_proven_requires_evidence()
    test_recent_run_stops_at_gap()
    test_breakeven_math()
    print("\nall discipline tests passed")
