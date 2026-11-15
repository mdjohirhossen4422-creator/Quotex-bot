#!/usr/bin/env python3
"""Botkeep entry point.

The Botkeep / Pterodactyl egg launches `python trader.py` by default. This shim
starts the candle-only collector so that default command works with no panel
change. It records candles; it does NOT trade and does NOT send Telegram.

Preferred, if the panel lets you set the start command, use:

    python live_collector.py --no-ticks

Environment (set these in the panel; defaults shown):

    CANDLE_DB=/data/candles.db                 # falls back to ./data/candles.db
    COLLECTOR_STATE=/data/collector_state.json # falls back to ./data/collector_state.json
    COLLECTOR_STORE_TICKS=0                    # candle-only (default here)

Any arguments you pass through (e.g. `python trader.py --pairs EURUSD_otc`)
are forwarded to the collector.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import live_collector


def _resolve(env_key: str, fallback: str) -> str:
    return os.environ.get(env_key) or fallback


def _ensure_parent(path: str) -> str:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


def main():
    # Defaults that work on a fresh container even if the panel sets nothing.
    # On Botkeep, mount persistent storage at /data and set CANDLE_DB to it.
    default_dir = "/data" if os.path.isdir("/data") else "data"
    os.environ.setdefault("CANDLE_DB", f"{default_dir}/candles.db")
    os.environ.setdefault("COLLECTOR_STATE", f"{default_dir}/collector_state.json")
    # Candle-only by default: raw ticks fill a 1 GB disk in under a day.
    os.environ.setdefault("COLLECTOR_STORE_TICKS", "0")

    _ensure_parent(_resolve("CANDLE_DB", "data/candles.db"))
    _ensure_parent(_resolve("COLLECTOR_STATE", "data/collector_state.json"))

    argv = sys.argv[1:]
    if os.environ["COLLECTOR_STORE_TICKS"] in ("0", "false", "False", "") \
            and "--no-ticks" not in argv:
        argv = ["--no-ticks"] + argv
    sys.argv = ["live_collector.py"] + argv
    live_collector.main()


if __name__ == "__main__":
    main()
