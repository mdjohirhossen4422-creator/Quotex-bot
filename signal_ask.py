#!/usr/bin/env python3
"""On-demand Telegram signals — you ask, the bot answers BUY / SELL / WAIT.

`alerts.py` *pushes* a message when a signal appears. This module is the
opposite: it stays quiet until *you* ask. Send the bot:

    /signal EURUSD_otc 60     -> a BUY / SELL / WAIT verdict for that pair+tf
    /signal EURUSD_otc        -> uses the default timeframe (--period)
    /pairs                    -> pairs that currently have enough candles
    /help                     -> this help

The engine's action is translated into trade language:

    CALL    -> BUY
    PUT     -> SELL
    NEUTRAL -> WAIT

It reads candles the collector has already stored, so the collector must be
running (or have run) for the pair+timeframe you ask about. Nothing is traded.

Honest limit: every engine in this project measures ~50% (a coin flip) on this
feed — see AGENTS.md. This shows the indicator read; it is not a proven edge.
Treat every reply as an alert, never as a trade instruction.

Setup
-----
    export TELEGRAM_BOT_TOKEN=123456:ABC...
    export TELEGRAM_CHAT_ID=987654321        # only this chat gets replies
    # (or telegram.json: {"bot_token": "...", "chat_id": "..."})

Run
---
    python signal_ask.py                 # long-poll forever
    python signal_ask.py --engine smart
    python signal_ask.py --dry-run       # print replies, send nothing
    python signal_ask.py --once          # process pending updates, then exit
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from alerts import api, load_config, send
from analysis import analyze, analyze_smart, analyze_ensemble, analyze_pa
from candle_store import CandleStore
from scan import PERIOD_LABEL, entry_plan, parse_periods

STATE_PATH = "data/ask_state.json"
DEFAULT_PERIOD = 60

HELP = (
    "On-demand signals. Commands:\n"
    "  /signal <pair> <tf>   e.g. /signal EURUSD_otc 60\n"
    "  /signal <pair>        uses the default timeframe\n"
    "  /pairs                list pairs with enough candles\n"
    "  /help                 this message\n\n"
    "Reply is one of: BUY (CALL), SELL (PUT), WAIT (no clear setup).\n"
    "Alerts only — never a trade instruction."
)

# action -> (icon+word, trade verb)
_VERDICT = {
    "CALL": ("\U0001F7E2 BUY", "CALL"),
    "PUT": ("\U0001F534 SELL", "PUT"),
    "NEUTRAL": ("\u26AA WAIT", "NEUTRAL"),
}


def resolve_asset(store: CandleStore, name: str) -> str | None:
    """Map a user-typed pair to a stored asset, tolerating case and a missing
    `_otc` suffix. Returns None if nothing matches."""
    if not name:
        return None
    want = name.strip()
    assets = store.assets()
    by_lower = {a.lower(): a for a in assets}
    for cand in (want, want.upper(), want.lower(),
                 want + "_otc", want.upper() + "_otc"):
        if cand in assets:
            return cand
        if cand.lower() in by_lower:
            return by_lower[cand.lower()]
    return None


def parse_signal_args(arg: str) -> tuple[str, int | None]:
    """Split the text after `/signal` into (pair, period|None)."""
    toks = (arg or "").split()
    if not toks:
        return "", None
    pair = toks[0]
    period = None
    if len(toks) > 1:
        try:
            period = int(toks[1].rstrip("sm"))
        except ValueError:
            period = None
    return pair, period


def _engine_fn(engine: str):
    return {"smart": analyze_smart, "ensemble": analyze_ensemble,
            "pa": analyze_pa}.get(engine, analyze)


def format_reply(asset: str, period: int, res, plan: dict,
                 age: float | None, *, engine: str = "rule") -> str:
    """Build the Telegram reply text for one analysis result."""
    label = PERIOD_LABEL.get(period, f"{period}s")
    head, _ = _VERDICT.get(res.action, ("\u26AA WAIT", res.action))
    lines = [f"{head}  {asset}  {label}"]

    if not res.enough:
        lines.append(res.note or "not enough candles yet")
        lines.append("")
        lines.append("WAIT — the collector is still building history for this "
                     "pair/timeframe.")
        return "\n".join(lines)

    if engine in ("smart", "pa", "ensemble") or res.regime:
        lines.append(f"confidence {res.confidence:.2f}"
                     f"  ({res.regime or 'range'})")
    else:
        lines.append(f"score {res.score}  (bull {res.bull} / bear {res.bear})")

    if res.action == "NEUTRAL":
        lines.append(res.note or "no clear setup")

    if plan:
        lines.append(f"entry {_clock(plan['entry_at'])}"
                     f" ({_countdown(plan['entry_in'])})  ->  "
                     f"expiry {_clock(plan['expiry_at'])}"
                     f" ({_countdown(plan['expires_in'])})")

    price = (res.values or {}).get("price")
    if price is not None:
        lines.append(f"price {price}")

    if age is not None and age > period * 3:
        lines.append(f"\u26A0 data is {_countdown(age)} old — the collector may "
                     f"be down; this read is stale")

    if res.reasons:
        lines.append("")
        lines += [f"- {r}" for r in res.reasons]

    lines += ["", "alert only — not a trade instruction."]
    return "\n".join(lines)


def _clock(ts) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _countdown(secs) -> str:
    secs = int(max(0, secs))
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def answer(store: CandleStore, asset_arg: str, period: int, *, engine: str = "rule",
           threshold: int = 4, expiry_candles: int = 1,
           now: float | None = None) -> str:
    """Produce the reply text for a /signal request. No network, so it is the
    testable core."""
    now = time.time() if now is None else now
    asset = resolve_asset(store, asset_arg)
    if asset is None:
        return (f"unknown pair '{asset_arg}'. Send /pairs to see what has data.")
    candles = store.load(asset, period, limit=5000)
    if engine == "rule":
        res = analyze(asset, period, candles, threshold=threshold)
    else:
        res = _engine_fn(engine)(asset, period, candles)

    plan = {}
    age = None
    if res.enough and candles:
        last_time = candles[-1][0]
        plan = entry_plan(period, last_time, now, expiry_candles)
        age = max(0.0, now - (last_time + period))
    return format_reply(asset, period, res, plan, age, engine=engine)


def list_pairs(store: CandleStore, min_candles: int = 50) -> str:
    pairs = store.series(min_candles=min_candles)
    if not pairs:
        return ("no pair has enough candles yet. Is the collector running? "
                "(python live_collector.py)")
    by_asset: dict[str, list[int]] = {}
    for asset, period in pairs:
        by_asset.setdefault(asset, []).append(period)
    lines = [f"{len(by_asset)} pairs with candles:"]
    for asset in sorted(by_asset):
        tfs = ",".join(PERIOD_LABEL.get(p, str(p)) for p in sorted(by_asset[asset]))
        lines.append(f"  {asset}  [{tfs}]")
    return "\n".join(lines)


# ------------------------------------------------------------------- transport
def load_offset(path: str = STATE_PATH) -> int:
    try:
        with open(path) as f:
            return int(json.load(f).get("offset", 0))
    except (OSError, ValueError, TypeError):
        return 0


def save_offset(offset: int, path: str = STATE_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"offset": offset}, f)


def handle_text(text: str, store: CandleStore, *, engine: str, period: int,
                threshold: int, expiry: int, now: float | None = None) -> str | None:
    """Turn a message into a reply. Returns None for messages we ignore."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    # strip a possible @botname suffix on the command
    parts = text.split(None, 1)
    cmd = parts[0].split("@", 1)[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("/help", "/start"):
        return HELP
    if cmd == "/pairs":
        return list_pairs(store)
    if cmd == "/signal":
        pair, tf = parse_signal_args(arg)
        if not pair:
            return "usage: /signal <pair> [timeframe]   e.g. /signal EURUSD_otc 60\n\n" + HELP
        return answer(store, pair, tf or period, engine=engine,
                      threshold=threshold, expiry_candles=expiry, now=now)
    return f"unknown command '{cmd}'. " + HELP


def run(args, *, send_messages: bool) -> None:
    token, chat = load_config(args)
    if not args.dry_run and not token:
        raise SystemExit("no bot token. Set TELEGRAM_BOT_TOKEN (or --token), "
                         "or create telegram.json")

    store = CandleStore(args.db)
    offset = load_offset(args.state)
    # Drop a webhook if one is set, else getUpdates returns nothing.
    if not args.dry_run:
        try:
            api(token, "deleteWebhook", {"drop_pending_updates": "false"})
        except Exception:
            pass
    print(f"on-demand bot ready | engine {args.engine} | default tf "
          f"{PERIOD_LABEL.get(args.period, args.period)} | "
          f"{'DRY-RUN' if args.dry_run else 'live'}")

    while True:
        try:
            res = api(token, "getUpdates",
                      {"timeout": 25, "offset": offset,
                       "allowed_updates": '["message"]'})
        except Exception as exc:
            print(f"poll error: {exc}")
            time.sleep(3)
            continue

        updates = res.get("result", []) if isinstance(res, dict) else []
        if not updates:
            if args.once:
                return
            continue

        for upd in updates:
            offset = max(offset, int(upd.get("update_id", 0)) + 1)
            msg = upd.get("message") or {}
            text = msg.get("text")
            src = (msg.get("chat") or {}).get("id")
            if text is None or src is None:
                continue
            if chat and str(src) != str(chat):
                continue                      # only the owner gets replies
            reply = handle_text(text, store, engine=args.engine,
                                period=args.period, threshold=args.threshold,
                                expiry=args.expiry)
            if reply is None:
                continue
            if send_messages and not args.dry_run:
                try:
                    send(token, str(src), reply)
                except Exception as exc:
                    print(f"send error: {exc}")
            else:
                print(f"[reply -> {src}]\n{reply}\n{'-' * 40}")

        save_offset(offset, args.state)
        if args.once:
            return


def main():
    ap = argparse.ArgumentParser(description="On-demand Telegram BUY/SELL/WAIT bot")
    ap.add_argument("--token", help="bot token (else $TELEGRAM_BOT_TOKEN)")
    ap.add_argument("--chat", help="only reply to this chat id (else any)")
    ap.add_argument("--db", default="data/candles.db")
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--engine", choices=["rule", "smart", "pa", "ensemble"],
                    default="rule")
    ap.add_argument("--period", type=int, default=DEFAULT_PERIOD,
                    help="default timeframe in seconds when none is given")
    ap.add_argument("--threshold", type=int, default=4,
                    help="rule engine: net vote needed to fire")
    ap.add_argument("--expiry", type=int, default=1,
                    help="expiry in candles after entry, shown in the reply")
    ap.add_argument("--dry-run", action="store_true",
                    help="print replies, send nothing (still polls Telegram)")
    ap.add_argument("--once", action="store_true",
                    help="process pending updates, then exit")
    ap.add_argument("--text", metavar="CMD",
                    help="answer one command offline (no Telegram), then exit "
                         "e.g. --text '/signal EURUSD_otc 60'")
    args = ap.parse_args()
    if args.text:
        store = CandleStore(args.db)
        reply = handle_text(args.text, store, engine=args.engine,
                            period=args.period, threshold=args.threshold,
                            expiry=args.expiry)
        print(reply if reply is not None else "(not a command)")
        return
    try:
        run(args, send_messages=True)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
