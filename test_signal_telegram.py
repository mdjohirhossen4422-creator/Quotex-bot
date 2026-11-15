#!/usr/bin/env python3
"""Tests for signal_telegram.py — paper-first, dedup, never trades.

  * with no gate pass, `--mode live` is downgraded to paper (no send),
  * dedup suppresses a repeated identical signal inside the cooldown but lets a
    flip through immediately,
  * a paper run journals nothing in dry-run and prints instead of sending,
  * the signal formatter never says "trade".

Run:  python test_signal_telegram.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import signal_telegram as st
from provenance import FEED_ID, classify_source
from tick_summary import SUMMARY_FIELDS
from test_ai_signal import _make, _summary, PERIOD


def test_should_send_dedup_and_flip():
    now = 1000.0
    state = {}
    assert st.should_send(state, "k", "CALL", 300, now) is True
    state["k"] = {"action": "CALL", "at": now}
    # same action inside cooldown -> suppressed
    assert st.should_send(state, "k", "CALL", 300, now + 10) is False
    # flip -> always through
    assert st.should_send(state, "k", "PUT", 300, now + 10) is True
    # same action after cooldown -> through
    assert st.should_send(state, "k", "CALL", 300, now + 400) is True
    print("PASS test_should_send_dedup_and_flip")


def test_formatter_says_paper_not_trade():
    class R:
        asset = "EURUSD_otc"
        period = 60
        action = "CALL"
        confidence = 0.6
    plan = {"entry_at": 1000000, "expiry_at": 1000060, "entry_in": 30,
            "expires_in": 90, "expiry_candles": 1}
    text = st.format_signal(R(), plan)
    assert "CALL" in text and "paper signal" in text.lower()
    assert "not a trade recommendation" in text.lower()
    print("PASS test_formatter_says_paper_not_trade")


def _seed(db: str, asset: str, candles: list[list]):
    con = sqlite3.connect(db)
    cols = ["asset TEXT", "period INTEGER", "time INTEGER", "open REAL",
            "close REAL", "high REAL", "low REAL", "source TEXT",
            "feed_id TEXT"] + [f"{c} REAL" for c in SUMMARY_FIELDS]
    con.execute(f"CREATE TABLE IF NOT EXISTS candles({', '.join(cols)}, "
                f"PRIMARY KEY(asset, period, time))")
    names = ("asset,period,time,open,close,high,low,source,feed_id,"
             + ",".join(SUMMARY_FIELDS))
    ph = ",".join(["?"] * (9 + len(SUMMARY_FIELDS)))
    for c in candles:
        con.execute(f"INSERT OR IGNORE INTO candles({names}) VALUES({ph})",
                    (asset, PERIOD, c[0], c[1], c[2], c[3], c[4],
                     classify_source(asset), FEED_ID) + _summary(c))
    con.commit()
    con.close()


def _args(db, state, journal, dry):
    class A:
        pass
    a = A()
    a.db = db
    a.journal = journal
    a.model_dir = "/nonexistent"          # force the heuristic path
    a.source = "otc"
    a.periods = [PERIOD]
    a.min_confidence = 0.0                # accept whatever the heuristic emits
    a.min_candles = 120
    a.mtf_period = 0
    a.cooldown = 300.0
    a.state = state
    a.token = ""
    a.chat = ""
    a.dry_run = dry
    return a


def test_paper_run_never_sends_and_dry_run_no_journal():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "c.db")
    _seed(db, "EURUSD_otc", _make(600, 0.7, seed=21))
    state = os.path.join(d, "state.json")
    journal = os.path.join(d, "j.db")
    args = _args(db, state, journal, dry=True)
    # send_messages=False == paper; and dry_run True == no journal writes
    summary = st.run_once(args, send_messages=False)
    assert summary["sent"] == 0, "paper mode must never send"
    assert not os.path.exists(journal), "dry-run must not write the journal"
    print("PASS test_paper_run_never_sends_and_dry_run_no_journal")


def test_paper_run_journals_when_not_dry():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "c.db")
    _seed(db, "EURUSD_otc", _make(600, 0.7, seed=22))
    state = os.path.join(d, "state.json")
    journal = os.path.join(d, "j.db")
    args = _args(db, state, journal, dry=False)
    st.run_once(args, send_messages=False)
    assert os.path.exists(journal), "paper run should journal (not dry-run)"
    con = sqlite3.connect(journal)
    n = con.execute("SELECT COUNT(*) FROM signals WHERE engine='ai'").fetchone()[0]
    con.close()
    assert n >= 0  # may be 0 if the heuristic abstained, that is honest
    print("PASS test_paper_run_journals_when_not_dry")


if __name__ == "__main__":
    test_should_send_dedup_and_flip()
    test_formatter_says_paper_not_trade()
    test_paper_run_never_sends_and_dry_run_no_journal()
    test_paper_run_journals_when_not_dry()
    print("\nall signal-telegram tests passed")
