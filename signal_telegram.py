#!/usr/bin/env python3
"""Telegram delivery for AI signals — paper first, live only when earned.

The brief is explicit (priorities 9–10): run **paper signals** before enabling
live Telegram signals, and enable production signals only after the model passes
predefined performance and reliability checks. This module enforces that:

  * default mode is **paper** — signals are computed, journaled and printed, but
    **no Telegram message is sent**,
  * `--mode live` sends messages, but only if `golive.gate()` reports
    **LIVE-READY**; otherwise it refuses and stays in paper mode,
  * `--mode auto` picks live only when the gate is LIVE-READY, else paper,
  * it **never places a trade** — alerts only.

Transport is reused from `alerts.py` (stdlib urllib, no new dependency). Signals
are deduplicated per (asset, period) with a cooldown so a signal re-fires only
when it flips or the cooldown elapses.

Usage
-----
    python signal_telegram.py --dry-run            # print, no creds, no journal write
    python signal_telegram.py                       # paper mode (default)
    python signal_telegram.py --mode auto           # live only if gate passes
    python signal_telegram.py --mode live --test    # verify Telegram creds
    python signal_telegram.py --periods 60,300 --source otc --min-conf 0.55
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from ai_signal_engine import AISignalEngine, entry_plan, DEFAULT_MTF_PERIOD
from alerts import api, load_config, send
from golive import gate
from journal import Journal, settle_open
from provenance import classify_source
from signal_data import SignalData

DEFAULT_PERIODS = [60, 300]
STATE_PATH = "data/ai_alert_state.json"
DEFAULT_COOLDOWN = 300.0


def gate_stage(db: str, journal_db: str, payout: float) -> str:
    """Current go-live stage. LIVE-READY is the only stage that unlocks live."""
    try:
        j = Journal(journal_db)
        res = gate(j, payout=payout)
        return res.get("stage", "DEMO")
    except Exception as e:                                    # never crash a run
        return f"ERROR({e!r})"


def load_state(path: str = STATE_PATH) -> dict:
    if os.path.exists(path):
        try:
            return json.loads(Path(path).read_text())
        except (ValueError, OSError):
            return {}
    return {}


def save_state(state: dict, path: str = STATE_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)


def should_send(prev: dict, key: str, action: str, cooldown: float,
                now: float) -> bool:
    last = prev.get(key)
    if not last:
        return True
    if last.get("action") != action:
        return True
    return (now - last.get("at", 0)) >= cooldown


def format_signal(res, plan: dict) -> str:
    arrow = {"CALL": "🟢 CALL (up)", "PUT": "🔴 PUT (down)"}.get(res.action, res.action)
    src = classify_source(res.asset)
    return (f"🤖 AI SIGNAL — {arrow}\n"
            f"{res.asset} · {res.period}s · source={src}\n"
            f"confidence: {res.confidence:.2f}\n"
            f"entry: {time.strftime('%H:%M:%S', time.localtime(plan['entry_at']))} "
            f"(in {plan['entry_in']}s)\n"
            f"expiry: {time.strftime('%H:%M:%S', time.localtime(plan['expiry_at']))} "
            f"({plan['expiry_candles']} candle)\n"
            f"note: paper signal — not a trade recommendation")


def scan(engine: AISignalEngine, sd: SignalData, periods: list[int],
         source: str, min_conf: float, min_candles: int) -> list[dict]:
    out = []
    for period in periods:
        for asset in sd.assets(source, period, min_candles):
            res = engine.evaluate(asset, period)
            if res.action not in ("CALL", "PUT"):
                continue
            if res.confidence < min_conf:
                continue
            plan = entry_plan(period, time.time())
            out.append({"asset": asset, "period": period, "action": res.action,
                        "confidence": res.confidence, "res": res, "plan": plan})
    return out


def run_once(args, *, send_messages: bool) -> dict:
    sd = SignalData(args.db)
    engine = AISignalEngine(args.db, args.model_dir, args.min_confidence,
                            args.mtf_period)
    signals = scan(engine, sd, args.periods, args.source, args.min_confidence,
                   args.min_candles)
    sd.close()

    now = time.time()
    state = load_state(args.state)
    sent = paper = skipped = 0
    journal = None if args.dry_run else Journal(args.journal)
    token = chat = ""

    for s in signals:
        key = f"{s['asset']}|{s['period']}"
        if not should_send(state, key, s["action"], args.cooldown, now):
            skipped += 1
            continue
        if journal is not None:
            journal.add({"asset": s["asset"], "period": s["period"],
                         "action": s["action"], "entry_at": int(s["plan"]["entry_at"]),
                         "price": 0.0, "expiry_at": int(s["plan"]["expiry_at"]),
                         "expiry_candles": s["plan"]["expiry_candles"],
                         "confidence": s["confidence"], "regime": ""},
                        engine="ai", now=int(now))
        text = format_signal(s["res"], s["plan"])
        if send_messages and not args.dry_run:
            if not token:
                token, chat = load_config(args)
            if token and chat:
                try:
                    send(token, chat, text)
                    sent += 1
                except Exception as e:                        # pragma: no cover
                    print(f"  [warn] telegram send failed: {e!r}")
            else:
                print("  [warn] live mode but no telegram credentials; "
                      "signal not sent")
        else:
            paper += 1
            print("PAPER " + text.replace("\n", " | "))
        state[key] = {"action": s["action"], "at": now}
    if not args.dry_run:
        save_state(state, args.state)
    return {"signals": len(signals), "sent": sent, "paper": paper,
            "skipped": skipped}


def main():
    ap = argparse.ArgumentParser(description="AI signal Telegram delivery (paper-first)")
    ap.add_argument("--db", default=os.environ.get("CANDLE_DB", "data/candles.db"))
    ap.add_argument("--journal", default=os.environ.get("JOURNAL_DB", "data/journal.db"))
    ap.add_argument("--model-dir", default="models")
    ap.add_argument("--source", choices=["otc", "real", "synthetic"], default="otc")
    ap.add_argument("--periods", default=",".join(str(p) for p in DEFAULT_PERIODS))
    ap.add_argument("--min-confidence", type=float, default=0.55)
    ap.add_argument("--min-candles", type=int, default=120)
    ap.add_argument("--mtf-period", type=int, default=DEFAULT_MTF_PERIOD)
    ap.add_argument("--mode", choices=["paper", "live", "auto"], default="paper")
    ap.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN)
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--payout", type=float, default=0.85)
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true", help="send a test Telegram message")
    ap.add_argument("--gate", action="store_true", help="print the go-live stage")
    ap.add_argument("--token", default="")
    ap.add_argument("--chat", default="")
    args = ap.parse_args()

    if args.gate:
        print("go-live stage:", gate_stage(args.db, args.journal, args.payout))
        return

    if args.test:
        token, chat = load_config(args)
        if not token or not chat:
            print("no credentials (set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
            return
        send(token, chat, "AI signal bot: test message (paper system, no trades).")
        print("test message sent")
        return

    args.periods = [int(p) for p in args.periods.split(",") if p]
    live = False
    if args.mode in ("live", "auto"):
        stage = gate_stage(args.db, args.journal, args.payout)
        live = stage == "LIVE-READY"
        print(f"go-live stage={stage}; live sending {'ENABLED' if live else 'OFF (paper)'}")
        if args.mode == "live" and not live:
            print("refusing --mode live: the gate is not LIVE-READY. Staying paper.")

    if args.dry_run:
        print("DRY RUN — no journal writes, no Telegram sends")

    while True:
        summary = run_once(args, send_messages=live)
        # settle any AI signals whose expiry candle has now closed
        if not args.dry_run:
            try:
                sd = SignalData(args.db)
                from candle_store import CandleStore
                settle_open(Journal(args.journal), CandleStore(args.db), int(time.time()))
                sd.close()
            except Exception as e:
                print(f"  [warn] settle failed: {e!r}")
        print(f"scan: {summary['signals']} signals, {summary['paper']} paper, "
              f"{summary['sent']} sent, {summary['skipped']} suppressed")
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
