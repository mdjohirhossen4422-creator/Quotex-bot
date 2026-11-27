#!/usr/bin/env python3
"""Quotex OTC M1 scanner using CandleStore."""

import argparse
import json
import os
import time

from candle_store import CandleStore, PERIODS
from analysis import (
    analyze,
    analyze_smart,
    analyze_ensemble,
    analyze_pa,
    analyze_discipline,
)

from payout import DEFAULT_PAYOUT


PERIOD_LABEL = {
    60: "1m",
}


def parse_periods(spec):
    if not spec or spec.strip().lower() == "all":
        return list(PERIODS)

    result = []

    for item in spec.split(","):
        item = item.strip()
        if item:
            result.append(int(item))

    return result or list(PERIODS)


def entry_plan(period, last_time, now, expiry_candles=1):
    next_time = last_time + period
    boundary = (int(now // period) + 1) * period

    entry = max(next_time, boundary)
    expiry = entry + period * max(1, expiry_candles)

    return {
        "entry_at": entry,
        "expiry_at": expiry,
        "entry_in": max(0, int(entry - now)),
        "expires_in": max(0, int(expiry - now)),
        "expiry_candles": max(1, expiry_candles),
    }


def is_dead(candles, window=20):
    if len(candles) < window:
        return False

    return all(
        candle[1] == candle[2]
        for candle in candles[-window:]
    )


def scan_all(
    store,
    periods=None,
    engine="rule",
    top=200,
    include_neutral=True,
    min_candles=0,
    expiry_candles=1,
    skip_dead=False,
    gate=False,
    gate_threshold=0.55,
    payout=DEFAULT_PAYOUT,
):
    periods = list(periods or PERIODS)

    engines = {
        "rule": analyze,
        "smart": analyze_smart,
        "ensemble": analyze_ensemble,
        "pa": analyze_pa,
        "discipline": analyze_discipline,
    }

    fn = engines.get(engine, analyze)

    try:
        pairs = store.series(
            min_candles=min_candles,
            periods=periods,
        )
    except Exception as exc:
        return {
            "error": "Cannot read candle series: " + str(exc),
            "signals": [],
            "scanned": 0,
            "signal_count": 0,
        }

    now = time.time()

    signals = []
    waiting = 0
    dead = 0
    ready = 0

    for asset, period in pairs:
        try:
            candles = store.load(
                asset,
                period,
                limit=5000,
            )

            if skip_dead and is_dead(candles):
                dead += 1
                continue

            if not candles:
                waiting += 1
                continue

            result = fn(asset, period, candles)

            if not result.enough:
                waiting += 1
                continue

            ready += 1

            action = str(result.action).upper()

            if action == "NEUTRAL" and not include_neutral:
                continue

            last_time = int(candles[-1][0])

            plan = entry_plan(
                period,
                last_time,
                now,
                expiry_candles,
            )

            signals.append({
                "asset": asset,
                "symbol": asset,
                "period": period,
                "label": PERIOD_LABEL.get(
                    period,
                    str(period) + "s",
                ),
                "action": action,
                "signal": action,
                "score": result.score,
                "bull": result.bull,
                "bear": result.bear,
                "regime": result.regime,
                "confidence": result.confidence,
                "candles": result.candles,
                "reasons": result.reasons,
                "reason": "; ".join(
                    str(x) for x in result.reasons
                ),
                "gate": None,
                **plan,
            })

        except Exception as exc:
            print(
                "PAIR SCAN ERROR:",
                asset,
                period,
                repr(exc),
                flush=True,
            )

    signals.sort(
        key=lambda row: (
            row["action"] == "NEUTRAL",
            -abs(row["score"]),
        )
    )

    return {
        "engine": engine,
        "periods": periods,
        "now": int(now),
        "scanned": len(pairs),
        "ready": ready,
        "waiting": waiting,
        "dead": dead,
        "gate": False,
        "gated_out": 0,
        "signal_count": sum(
            1 for row in signals
            if row["action"] != "NEUTRAL"
        ),
        "signals": signals[:top] if top else signals,
    }


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "periods",
        nargs="?",
        default="60",
    )

    parser.add_argument("--db", default="data/candles.db")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument(
        "--engine",
        choices=[
            "rule",
            "smart",
            "ensemble",
            "pa",
            "discipline",
        ],
        default="rule",
    )
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--watch", action="store_true")

    args = parser.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit("Candle database not found: " + args.db)

    periods = parse_periods(args.periods)
    store = CandleStore(args.db)

    try:
        while True:
            report = scan_all(
                store,
                periods,
                engine=args.engine,
                top=args.top,
                include_neutral=args.all,
            )

            print(
                json.dumps(report, indent=2, default=str),
                flush=True,
            )

            if not args.watch:
                break

            time.sleep(15)

    except KeyboardInterrupt:
        print("Scanner stopped")

    finally:
        store.close()


if __name__ == "__main__":
    main()