#!/usr/bin/env python3
"""Quotex OTC M1 Hybrid Scanner with 5th AI-style Sensor."""

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

ENGINE_COUNT = 5


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

    return min(1.0, abs(bull - bear) / total)


# --------------------------------------------------
# 5TH ENGINE: AI-STYLE CANDLE SENSOR
# --------------------------------------------------

def _ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2.0 / (period + 1)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1.0 - multiplier)
        )

    return result


def _rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def analyze_ai_sensor(asset, period, candles):
    """
    Local AI-style sensor.
    Uses EMA trend, momentum, RSI and candle strength.
    Requires at least 25 closed candles.
    """

    if len(candles) < 25:
        return Analysis(
            asset=asset,
            period=period,
            candles=len(candles),
            enough=False,
            action="NEUTRAL",
            note="AI SENSOR: waiting for 25 candles",
            engine="ai_sensor",
        )

    try:
        opens = [float(c[1]) for c in candles]
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]
        closes = [float(c[4]) for c in candles]

        if any(
            not all(map(
                lambda x: x == x and abs(x) != float("inf"),
                values
            ))
            for values in (opens, highs, lows, closes)
        ):
            raise ValueError("Invalid candle values")

        score = 0.0
        reasons = []

        # 1. EMA trend sensor
        ema9 = _ema(closes[-40:], 9)
        ema21 = _ema(closes[-40:], 21)

        if ema9 is not None and ema21 is not None:
            if ema9 > ema21:
                score += 1.5
                reasons.append("EMA trend bullish")
            elif ema9 < ema21:
                score -= 1.5
                reasons.append("EMA trend bearish")

        # 2. Price momentum sensor
        momentum = closes[-1] - closes[-6]

        if momentum > 0:
            score += 1.0
            reasons.append("Price momentum UP")
        elif momentum < 0:
            score -= 1.0
            reasons.append("Price momentum DOWN")

        # 3. RSI sensor
        rsi = _rsi(closes, 14)

        if rsi is not None:
            if 52 <= rsi <= 70:
                score += 1.0
                reasons.append("RSI bullish momentum")
            elif 30 <= rsi <= 48:
                score -= 1.0
                reasons.append("RSI bearish momentum")
            elif rsi > 75:
                score -= 0.5
                reasons.append("RSI overbought warning")
            elif rsi < 25:
                score += 0.5
                reasons.append("RSI oversold warning")

        # 4. Latest candle strength
        candle_open = opens[-1]
        candle_close = closes[-1]
        candle_high = highs[-1]
        candle_low = lows[-1]

        candle_range = candle_high - candle_low
        body = candle_close - candle_open

        if candle_range > 0:
            body_strength = abs(body) / candle_range

            if body > 0 and body_strength >= 0.55:
                score += 1.0
                reasons.append("Strong bullish candle")

            elif body < 0 and body_strength >= 0.55:
                score -= 1.0
                reasons.append("Strong bearish candle")

        # 5. Recent high/low structure
        recent_high = max(highs[-6:-1])
        recent_low = min(lows[-6:-1])

        if closes[-1] > recent_high:
            score += 0.5
            reasons.append("Recent high breakout")

        elif closes[-1] < recent_low:
            score -= 0.5
            reasons.append("Recent low breakdown")

        # Final sensor decision
        if score >= 2.0:
            action = "CALL"
        elif score <= -2.0:
            action = "PUT"
        else:
            action = "NEUTRAL"

        confidence = min(1.0, abs(score) / 5.0)

        return Analysis(
            asset=asset,
            period=period,
            candles=len(candles),
            enough=True,
            action=action,
            score=round(score * 20),
            bull=1 if score > 0 else 0,
            bear=1 if score < 0 else 0,
            reasons=reasons,
            values={
                "sensor_score": round(score, 2),
                "ema9": ema9,
                "ema21": ema21,
                "rsi": round(rsi, 2) if rsi is not None else None,
                "momentum": momentum,
                "candle_strength": (
                    round(abs(body) / candle_range, 3)
                    if candle_range > 0 else 0
                ),
            },
            note=f"AI SENSOR score: {score:.2f}/5",
            engine="ai_sensor",
            confidence=confidence,
        )

    except Exception as exc:
        return Analysis(
            asset=asset,
            period=period,
            candles=len(candles),
            enough=False,
            action="NEUTRAL",
            note=f"AI SENSOR ERROR: {exc}",
            engine="ai_sensor",
        )


# --------------------------------------------------
# 5 ENGINE HYBRID ANALYSIS
# --------------------------------------------------

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
        "ai_sensor": analyze_ai_sensor,
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
            "direction": "UP" if action == "CALL" else "DOWN",
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

    average_confidence = (
        sum(v["confidence"] for v in agreeing)
        / len(agreeing)
        if agreeing else 0.0
    )

    agreement_ratio = len(agreeing) / ENGINE_COUNT

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
            f"WAIT: only {ready_count}/{ENGINE_COUNT} engines "
            "have sufficient candle history"
        )

    elif final_action == "NEUTRAL":
        note = (
            f"WAIT: score {combined * 100:.1f}%, "
            f"agreement {len(agreeing)}/{ENGINE_COUNT}"
        )

    else:
        note = (
            f"HYBRID {final_action}: "
            f"score {combined * 100:.1f}%, "
            f"agreement {len(agreeing)}/{ENGINE_COUNT}"
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
            "total_engines": ENGINE_COUNT,
            "agreement": round(agreement_ratio * 100, 1),
            "confidence_score": round(combined * 100, 1),
            "threshold": confidence_threshold * 100,
        },
        note=note,
        engine="hybrid",
        confidence=combined,
        regime=getattr(base, "regime", "") if base else "",
    )


# --------------------------------------------------
# SCAN ALL PAIRS
# --------------------------------------------------

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
        "ai_sensor": analyze_ai_sensor,
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

            direction = {
                "CALL": "UP",
                "PUT": "DOWN",
                "NEUTRAL": "NEUTRAL",
            }[action]

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

                # Existing compatibility
                "action": action,

                # UP/DOWN display fields
                "direction": direction,
                "signal": direction,
                "display_signal": direction,

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
            message = f"{asset} {period}: {repr(exc)}"
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


# --------------------------------------------------
# COMMAND LINE
# --------------------------------------------------

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
            "ai_sensor",
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