#!/usr/bin/env python3
"""Quotex OTC M1 Hybrid Scanner."""

import argparse
import json
import os
import time

from candle_store import CandleStore, PERIODS

from analysis import (
    Analysis,
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

ENGINE_COUNT = 4


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


def _normalize_action(action):
    action = str(action or "").upper().strip()

    aliases = {
        "UP": "CALL",
        "BUY": "CALL",
        "DOWN": "PUT",
        "SELL": "PUT",
    }

    return aliases.get(action, action)


def _engine_confidence(result):
    value = getattr(result, "confidence", 0.0)

    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0

    if value > 1:
        value /= 100.0

    return max(0.0, min(1.0, value))


def _rule_confidence(result):
    bull = int(getattr(result, "bull", 0) or 0)
    bear = int(getattr(result, "bear", 0) or 0)

    total = bull + bear

    if total <= 0:
        return 0.0

    return min(
        1.0,
        abs(bull - bear) / total,
    )


def analyze_hybrid(
    asset,
    period,
    candles,
    confidence_threshold=0.55,
    min_agreement=2,
):
    engines = {
        "rule": analyze,
        "smart": analyze_smart,
        "ensemble": analyze_ensemble,
        "pa": analyze_pa,
    }

    results = {}
    errors = {}

    for name, function in engines.items():
        try:
            results[name] = function(
                asset,
                period,
                candles,
            )

        except Exception as exc:
            errors[name] = str(exc)

    votes = {
        "CALL": [],
        "PUT": [],
    }

    details = {}
    reasons = []
    ready_count = 0

    for name in engines:
        result = results.get(name)

        if result is None:
            details[name] = {
                "action": "ERROR",
                "confidence": 0,
            }
            continue

        if not result.enough:
            details[name] = {
                "action": "WAIT",
                "confidence": 0,
            }
            continue

        ready_count += 1

        action = _normalize_action(result.action)

        if action not in ("CALL", "PUT"):
            details[name] = {
                "action": "NEUTRAL",
                "confidence": 0,
            }
            continue

        if name == "rule":
            confidence = _rule_confidence(result)
        else:
            confidence = _engine_confidence(result)

        votes[action].append({
            "engine": name,
            "confidence": confidence,
        })

        details[name] = {
            "action": action,
            "confidence": round(confidence * 100, 1),
        }

    call_votes = votes["CALL"]
    put_votes = votes["PUT"]

    if len(call_votes) > len(put_votes):
        direction = "CALL"
        agreeing = call_votes

    elif len(put_votes) > len(call_votes):
        direction = "PUT"
        agreeing = put_votes

    else:
        direction = "NEUTRAL"
        agreeing = []

    total_engines = ENGINE_COUNT

    average_confidence = (
        sum(v["confidence"] for v in agreeing)
        / len(agreeing)
        if agreeing else 0.0
    )

    # Agreement is measured against all four engines.
    agreement_ratio = len(agreeing) / total_engines

    combined = (
        average_confidence * 0.70
        + agreement_ratio * 0.30
    )

    final_action = "NEUTRAL"

    enough_engines = ready_count >= 2

    if (
        enough_engines
        and direction in ("CALL", "PUT")
        and len(agreeing) >= min_agreement
        and combined >= confidence_threshold
    ):
        final_action = direction

    for name, result in results.items():
        if result.reasons:
            reasons.extend(
                f"{name.upper()}: {reason}"
                for reason in result.reasons[:3]
            )

    for name, error in errors.items():
        reasons.append(
            f"{name.upper()} ERROR: {error}"
        )

    if not enough_engines:
        note = (
            f"WAIT: only {ready_count}/4 engines "
            "have sufficient candle history"
        )

    elif final_action == "NEUTRAL":
        note = (
            f"WAIT: confidence score {combined * 100:.1f}%, "
            f"agreement {len(agreeing)}/4, "
            f"minimum agreement {min_agreement}"
        )

    else:
        note = (
            f"HYBRID {final_action}: "
            f"confidence score {combined * 100:.1f}%, "
            f"agreement {len(agreeing)}/4"
        )

    base = next(
        (
            results[name]
            for name in engines
            if name in results and results[name].enough
        ),
        None,
    )

    return Analysis(
        asset=asset,
        period=period,
        candles=len(candles),
        enough=enough_engines,
        action=final_action,
        score=round(combined * 100),
        bull=len(call_votes),
        bear=len(put_votes),
        reasons=reasons,
        values={
            "engine_votes": details,
            "call_votes": len(call_votes),
            "put_votes": len(put_votes),
            "ready_engines": ready_count,
            "agreement": round(agreement_ratio * 100, 1),
            "confidence_score": round(combined * 100, 1),
            "threshold": confidence_threshold * 100,
        },
        note=note,
        engine="hybrid",
        confidence=combined,
        regime=getattr(base, "regime", "") if base else "",
    )


def scan_all(
    store,
    periods=None,
    engine="hybrid",
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
        "hybrid": analyze_hybrid,
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
            "ready": 0,
            "waiting": 0,
            "signal_count": 0,
        }

    now = time.time()

    signals = []
    waiting = 0
    dead = 0
    ready = 0
    scan_errors = []

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

            result = fn(
                asset,
                period,
                candles,
            )

            if not result.enough:
                waiting += 1
                continue

            ready += 1

            action = _normalize_action(result.action)

            if action not in ("CALL", "PUT"):
                action = "NEUTRAL"

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
                "note": result.note,
                "values": result.values,
                "gate": None,
                **plan,
            })

        except Exception as exc:
            message = (
                f"{asset} {period}: {repr(exc)}"
            )

            scan_errors.append(message)

            print(
                "PAIR SCAN ERROR:",
                message,
                flush=True,
            )

    signals.sort(
        key=lambda row: (
            row["action"] == "NEUTRAL",
            -float(row.get("confidence") or 0),
        )
    )

    signal_count = sum(
        1 for row in signals
        if row["action"] in ("CALL", "PUT")
    )

    return {
        "engine": engine,
        "periods": periods,
        "now": int(now),
        "scanned": len(pairs),
        "ready": ready,
        "waiting": waiting,
        "dead": dead,
        "errors": scan_errors[:20],
        "gate": False,
        "gated_out": 0,
        "signal_count": signal_count,
        "signals": signals[:top] if top else signals,
    }


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "periods",
        nargs="?",
        default="60",
    )

    parser.add_argument(
        "--db",
        default="data/candles.db",
    )

    parser.add_argument(
        "--top",
        type=int,
        default=20,
    )

    parser.add_argument(
        "--engine",
        choices=[
            "rule",
            "smart",
            "ensemble",
            "pa",
            "discipline",
            "hybrid",
        ],
        default="hybrid",
    )

    parser.add_argument("--all", action="store_true")
    parser.add_argument("--watch", action="store_true")

    args = parser.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(
            "Candle database not found: " + args.db
        )

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
                json.dumps(
                    report,
                    indent=2,
                    default=str,
                ),
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