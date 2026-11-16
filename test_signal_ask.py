#!/usr/bin/env python3
"""Tests for the on-demand Telegram bot (signal_ask.py).

No network: `handle_text` / `answer` are the testable core. The reply shape is
checked for BUY/SELL/WAIT, the "not enough candles" path, unknown pairs, and
that a stale feed is flagged.

  python test_signal_ask.py
"""
from __future__ import annotations

import os
import tempfile
import time

from analysis import Analysis
from candle_store import CandleStore
import signal_ask as sa


def _store_with(pair: str = "EURUSD_otc", period: int = 60, n: int = 120,
                uptrend: bool = True) -> CandleStore:
    d = tempfile.mkdtemp()
    s = CandleStore(os.path.join(d, "c.db"))
    base = 1.1000
    t0 = int(time.time()) - n * period
    rows = []
    for i in range(n):
        step = 0.0002 if uptrend else -0.0002
        o = base + i * step
        c = o + step * 0.75
        rows.append([t0 + i * period, round(o, 5), round(c, 5),
                     round(max(o, c) + 0.00005, 5), round(min(o, c) - 0.00005, 5)])
    s.save(pair, period, rows)
    return s


def test_verdict_mapping_buy_sell_wait():
    for action, word in (("CALL", "BUY"), ("PUT", "SELL"), ("NEUTRAL", "WAIT")):
        a = Analysis(asset="X", period=60, candles=100, enough=True,
                     action=action, score=5, bull=5, bear=1,
                     reasons=["r1"], values={"price": 1.2})
        head = sa.format_reply("X", 60, a, {}, None).splitlines()[0]
        assert word in head, f"{action} should map to {word}, got {head!r}"
    print("PASS test_verdict_mapping_buy_sell_wait")


def test_not_enough_candles_says_wait():
    d = tempfile.mkdtemp()
    s = CandleStore(os.path.join(d, "c.db"))
    s.save("EURUSD_otc", 60, [[int(time.time()), 1.1, 1.1, 1.1, 1.1]])
    text = sa.answer(s, "EURUSD_otc", 60)
    assert "WAIT" in text, text
    assert "not enough candles" in text.lower() or "building history" in text.lower()
    print("PASS test_not_enough_candles_says_wait")


def test_unknown_pair_is_reported_not_a_crash():
    s = _store_with()
    text = sa.answer(s, "NOPE_xyz", 60)
    assert "unknown pair" in text.lower()
    print("PASS test_unknown_pair_is_reported_not_a_crash")


def test_resolve_asset_tolerates_case_and_suffix():
    s = _store_with(pair="EURUSD_otc")
    assert sa.resolve_asset(s, "EURUSD_otc") == "EURUSD_otc"
    assert sa.resolve_asset(s, "eurusd_otc") == "EURUSD_otc"
    assert sa.resolve_asset(s, "EURUSD") == "EURUSD_otc"     # suffix added
    assert sa.resolve_asset(s, "nope") is None
    print("PASS test_resolve_asset_tolerates_case_and_suffix")


def test_parse_signal_args():
    assert sa.parse_signal_args("EURUSD_otc 60") == ("EURUSD_otc", 60)
    assert sa.parse_signal_args("EURUSD_otc") == ("EURUSD_otc", None)
    assert sa.parse_signal_args("EURUSD_otc 1m") == ("EURUSD_otc", 1)
    assert sa.parse_signal_args("") == ("", None)
    print("PASS test_parse_signal_args")


def test_handle_text_dispatch():
    s = _store_with()
    assert sa.handle_text("hello", s, engine="rule", period=60,
                          threshold=4, expiry=1) is None          # not a command
    assert "On-demand" in sa.handle_text("/help", s, engine="rule",
                                         period=60, threshold=4, expiry=1)
    assert "pairs" in sa.handle_text("/pairs", s, engine="rule",
                                     period=60, threshold=4, expiry=1).lower()
    r = sa.handle_text("/signal EURUSD_otc 60", s, engine="rule",
                       period=60, threshold=4, expiry=1)
    assert "EURUSD_otc" in r and "1m" in r
    # @botname suffix must still resolve
    r2 = sa.handle_text("/signal@my_bot EURUSD_otc", s, engine="rule",
                        period=60, threshold=4, expiry=1)
    assert "EURUSD_otc" in r2
    print("PASS test_handle_text_dispatch")


def test_stale_feed_is_flagged():
    """Candles older than 3 periods must warn that the collector may be down."""
    d = tempfile.mkdtemp()
    s = CandleStore(os.path.join(d, "c.db"))
    n, period = 120, 60
    t0 = int(time.time()) - n * period - 10 * period    # ends 10 candles in the past
    rows = [[t0 + i * period, 1.1 + i * 1e-4, 1.1 + i * 1e-4 + 1e-4,
             1.1 + i * 1e-4 + 2e-4, 1.1 + i * 1e-4 - 1e-4] for i in range(n)]
    s.save("EURUSD_otc", period, rows)
    text = sa.answer(s, "EURUSD_otc", period)
    assert "old" in text.lower() and "collector" in text.lower(), text
    print("PASS test_stale_feed_is_flagged")


def test_engine_choice_runs_without_error():
    s = _store_with()
    for eng in ("rule", "smart", "pa", "ensemble"):
        text = sa.answer(s, "EURUSD_otc", 60, engine=eng)
        assert any(w in text for w in ("BUY", "SELL", "WAIT")), (eng, text)
    print("PASS test_engine_choice_runs_without_error")


if __name__ == "__main__":
    test_verdict_mapping_buy_sell_wait()
    test_not_enough_candles_says_wait()
    test_unknown_pair_is_reported_not_a_crash()
    test_resolve_asset_tolerates_case_and_suffix()
    test_parse_signal_args()
    test_handle_text_dispatch()
    test_stale_feed_is_flagged()
    test_engine_choice_runs_without_error()
    print("\nall on-demand bot tests passed")
