#!/usr/bin/env python3
"""Tests for the discipline engine wired into the dashboard/scan/ask surfaces.

`analyze_discipline` is the real-trade gate: it must abstain (NEUTRAL) unless
every checklist gate passes, and even then it must not present an unproven setup
as a signal. These tests exercise the adapter shape, the abstain-by-default
behaviour, and that the CLI engine choices accept it.

Run:  python test_analyze_discipline.py
"""
from __future__ import annotations

from analysis import analyze_discipline, Analysis


def _candles(closes, period=60):
    out, t, prev = [], 1_700_000_000, closes[0]
    for k, c in enumerate(closes):
        out.append([t + k * period, prev, c,
                    max(prev, c) + abs(c) * 0.0002,
                    min(prev, c) - abs(c) * 0.0002])
        prev = c
    return out


def test_shape_matches_other_engines():
    cs = _candles([1.0 + 0.0004 * i for i in range(200)])
    res = analyze_discipline("X", 60, cs)
    assert isinstance(res, Analysis)
    assert res.engine == "discipline"
    assert res.action in ("CALL", "PUT", "NEUTRAL")
    assert isinstance(res.reasons, list) and res.reasons
    assert "price" in res.values
    print("PASS test_shape_matches_other_engines")


def test_flat_market_abstains():
    """A frozen series must be NEUTRAL — never a tradeable signal."""
    cs = _candles([1.0] * 200)
    res = analyze_discipline("X", 60, cs)
    assert res.action == "NEUTRAL", res.action
    assert "NO TRADE" in res.note or "blocked" in res.note.lower()
    print("PASS test_flat_market_abstains")


def test_short_series_is_not_enough():
    res = analyze_discipline("X", 60, _candles([1.0 + 0.001 * i for i in range(5)]))
    assert res.enough is False
    assert res.action == "NEUTRAL"
    print("PASS test_short_series_is_not_enough")


def test_candidate_never_labelled_a_proven_signal():
    """With no journal history the edge is unproven, so a passing setup must be
    a CANDIDATE, not a SIGNAL."""
    cs = _candles([1.0 + 0.0004 * i for i in range(200)])
    res = analyze_discipline("X", 60, cs)
    if res.action != "NEUTRAL":
        assert "CANDIDATE" in res.note or "not yet proven" in res.note, res.note
    print("PASS test_candidate_never_labelled_a_proven_signal")


def test_checklist_is_reported_in_reasons():
    cs = _candles([1.0 + 0.0004 * i for i in range(200)])
    res = analyze_discipline("X", 60, cs)
    joined = " ".join(res.reasons)
    assert "data quality" in joined and "confluence" in joined, joined
    print("PASS test_checklist_is_reported_in_reasons")


if __name__ == "__main__":
    test_shape_matches_other_engines()
    test_flat_market_abstains()
    test_short_series_is_not_enough()
    test_candidate_never_labelled_a_proven_signal()
    test_checklist_is_reported_in_reasons()
    print("\nall discipline-adapter tests passed")
