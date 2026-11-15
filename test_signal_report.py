#!/usr/bin/env python3
"""Tests for the daily performance report (signal_report.py).

The controls: an empty journal must report nothing (not zeros pretending to be
a result), a tiny sample must be flagged 'insufficient', and the payout source
must be labelled honestly (assumed vs from config).
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from journal import Journal
from signal_report import daily_rows, overall, _verdict, _resolve_payout


def _j():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return Journal(path), path


def _add(j, asset, action, result, entry_at, engine="ai", mv="heuristic"):
    ok = j.add({"asset": asset, "period": 60, "action": action,
                "entry_at": entry_at, "price": 1.1,
                "expiry_at": entry_at + 60, "expiry_candles": 1,
                "confidence": 0.6, "regime": "", "model_version": mv},
               engine=engine, now=entry_at)
    assert ok
    sid = j._db.execute("SELECT id FROM signals WHERE entry_at=?",
                        (entry_at,)).fetchone()[0]
    j.settle(sid, result, 1.11, entry_at + 60)


def test_empty_journal_reports_nothing():
    j, path = _j()
    rows = daily_rows(j)
    assert rows == [], rows
    o = overall(rows)
    assert o["settled"] == 0 and o["hit"] is None, o
    print("PASS test_empty_journal_reports_nothing")


def test_only_settled_rows_count_and_bucket_by_entry_day():
    j, path = _j()
    day1 = 1_700_000_000
    day2 = day1 + 86400
    _add(j, "EURUSD_otc", "CALL", "win", day1)
    _add(j, "EURUSD_otc", "CALL", "loss", day1 + 60)
    _add(j, "EURUSD_otc", "CALL", "win", day2)
    # an OPEN signal must be ignored, never guessed
    j.add({"asset": "EURUSD_otc", "period": 60, "action": "PUT",
           "entry_at": day2 + 60, "price": 1.1, "expiry_at": day2 + 120,
           "expiry_candles": 1, "confidence": 0.6},
          engine="ai", now=day2)
    rows = daily_rows(j)
    assert len(rows) == 2, rows
    assert rows[0]["settled"] == 2 and rows[0]["wins"] == 1, rows[0]
    assert rows[1]["settled"] == 1 and rows[1]["wins"] == 1, rows[1]
    assert rows[0]["day"] < rows[1]["day"]
    print("PASS test_only_settled_rows_count_and_bucket_by_entry_day")


def test_small_sample_is_insufficient_not_a_verdict():
    assert _verdict(0, None, None, 0.54) == "no_decisive"
    assert _verdict(10, 0.4, 0.6, 0.54) == "insufficient"
    # 30+ with a CI that clears breakeven -> above; straddling -> indistinguishable
    assert _verdict(50, 0.60, 0.80, 0.54) == "above"
    assert _verdict(50, 0.40, 0.60, 0.54) == "indistinguishable"
    assert _verdict(50, 0.10, 0.30, 0.54) == "below"
    print("PASS test_small_sample_is_insufficient_not_a_verdict")


def test_ev_and_ci_present_only_for_decisive():
    j, path = _j()
    base = 1_700_000_000
    for i in range(10):
        _add(j, "EURUSD_otc", "CALL", "win" if i % 2 else "loss", base + i * 60)
    rows = daily_rows(j, payout=0.85)
    assert rows[0]["decisive"] == 10
    assert rows[0]["ci_lo"] is not None and rows[0]["ev"] is not None
    # a day with only flats has no hit-rate at all
    j2, _ = _j()
    _add(j2, "EURUSD_otc", "CALL", "flat", base)
    r2 = daily_rows(j2)[0]
    assert r2["decisive"] == 0 and r2["hit"] is None and r2["ev"] is None
    print("PASS test_ev_and_ci_present_only_for_decisive")


def test_payout_source_labelled_honestly():
    class A:
        payout = None
        payout_config = "/nonexistent/payout.json"
    pay, src = _resolve_payout(A())
    assert abs(pay - 0.85) < 1e-9 and src == "assumed", (pay, src)
    # explicit CLI override wins
    A.payout = 0.90
    pay, src = _resolve_payout(A())
    assert abs(pay - 0.90) < 1e-9 and src == "cli", (pay, src)
    print("PASS test_payout_source_labelled_honestly")


if __name__ == "__main__":
    test_empty_journal_reports_nothing()
    test_only_settled_rows_count_and_bucket_by_entry_day()
    test_small_sample_is_insufficient_not_a_verdict()
    test_ev_and_ci_present_only_for_decisive()
    test_payout_source_labelled_honestly()
    print("\nall signal-report tests passed")
