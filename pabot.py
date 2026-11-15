#!/usr/bin/env python3
"""Price-action + SMC + indicator bot — rule based, NO training, instant.

Every other model in this repo needs training or a walk-forward fit. This one
does not: it reads the last closed candle and scores three independent families
against each other, then fires only when they agree.

  * indicators   — EMA stack + fresh EMA cross, MACD cross, RSI, Stochastic, Bollinger
  * price action — support/resistance rejection vs break, chart patterns (H&S,
                   double top/bottom, triangle, flag)
  * SMC          — market structure, liquidity sweep, order block, fair value gap,
                   premium/discount zone (via `smc.smc_vote`)

A signal needs at least `min_blocks` of the three families to point the same way;
the ADX regime (trend vs range) only colours the read, it does not veto. The
verdict is explainable: every reason is tagged with the family that produced it.

Honest note (read AGENTS.md): no rule set in this repo has beaten the payout
breakeven on the live feed. This bot is a *presentation* of three classic
readings in one place, not a proven edge. Use it as an alert, never as a trade.

    python pabot.py EURUSD_otc 60            # one-shot read on stored candles
    python pabot.py EURUSD_otc 60 --watch    # re-read every 5s (live)
    python scan.py --engine pa               # scan every pair with this bot
"""
from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from typing import Optional

import indicators as ta
import patterns
import smc
from candle_store import CandleStore


@dataclass
class PAStrategy:
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 50
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    bb_period: int = 20
    bb_mult: float = 2.0
    stoch_period: int = 14
    stoch_k: int = 3
    stoch_d: int = 3
    adx_period: int = 14
    adx_trend: float = 25.0        # >= -> trending regime
    adx_range: float = 20.0        # <  -> ranging regime
    min_blocks: int = 2            # how many of the 3 families must agree
    cooldown_candles: int = 3

    def min_candles(self) -> int:
        return max(self.ema_trend, self.macd_slow + self.macd_signal,
                   self.bb_period, self.stoch_period + self.stoch_k + self.stoch_d,
                   2 * self.adx_period) + 2


@dataclass
class PASignal:
    time: int
    asset: str
    action: str                     # "CALL" | "PUT"
    price: float
    score: int
    confidence: float               # 0..1
    regime: str                     # trend | range | transition
    blocks: dict = field(default_factory=dict)   # family -> direction
    reasons: list[str] = field(default_factory=list)


def _last(series, i):
    if 0 <= i < len(series):
        return series[i]
    return None


class PAEngine:
    """Indicator + price action + SMC confluence, no training required."""

    def __init__(self, strategy: Optional[PAStrategy] = None):
        self.s = strategy or PAStrategy()
        self._last_action: Optional[str] = None
        self._last_index: int = -10 ** 9

    # ---- family 1: indicators -------------------------------------------- #
    def _indicator_block(self, o, c, h, l, i, rsi, macd_line, sig_line,
                         st_k, st_d, bb_u, bb_l, ema_f, ema_s, ema_t):
        bull = bear = 0
        reasons: list[str] = []
        ef, es, et = _last(ema_f, i), _last(ema_s, i), _last(ema_t, i)
        if ef is not None and es is not None and et is not None:
            if ef > es > et:
                bull += 2; reasons.append("EMA stack bullish (9>21>50)")
            elif ef < es < et:
                bear += 2; reasons.append("EMA stack bearish (9<21<50)")
            if c[i] > et:
                bull += 1; reasons.append("price above EMA50")
            elif c[i] < et:
                bear += 1; reasons.append("price below EMA50")
        if ta.crossed_above(ema_f, ema_s, i):
            bull += 2; reasons.append("fresh EMA9/21 cross up")
        elif ta.crossed_below(ema_f, ema_s, i):
            bear += 2; reasons.append("fresh EMA9/21 cross down")
        if ta.crossed_above(macd_line, sig_line, i):
            bull += 2; reasons.append("MACD cross up")
        elif ta.crossed_below(macd_line, sig_line, i):
            bear += 2; reasons.append("MACD cross down")
        r = _last(rsi, i)
        if r is not None:
            if r <= self.s.rsi_oversold:
                bull += 1; reasons.append(f"RSI oversold ({r:.0f})")
            elif r >= self.s.rsi_overbought:
                bear += 1; reasons.append(f"RSI overbought ({r:.0f})")
        k, d = _last(st_k, i), _last(st_d, i)
        if k is not None and d is not None:
            if k < 20 and k > d:
                bull += 1; reasons.append("Stochastic turn up from oversold")
            elif k > 80 and k < d:
                bear += 1; reasons.append("Stochastic turn down from overbought")
        u, lo = _last(bb_u, i), _last(bb_l, i)
        if u is not None and lo is not None:
            if c[i] <= lo:
                bull += 1; reasons.append("close at lower Bollinger band")
            elif c[i] >= u:
                bear += 1; reasons.append("close at upper Bollinger band")
        return self._pick(bull, bear, reasons)

    # ---- family 2: price action ------------------------------------------ #
    def _price_action_block(self, o, c, h, l, i):
        bull = bear = 0
        reasons: list[str] = []
        sr = patterns.sr_price_action(h, l, c, o, i)
        if sr == "bull":
            bull += 2; reasons.append("S/R rejection (bounce)")
        elif sr == "bear":
            bear += 2; reasons.append("S/R rejection (reject)")
        vote, pat_reasons = patterns.pattern_vote(o, c, h, l, i)
        if vote == "CALL":
            bull += 1; reasons.extend(pat_reasons or ["chart pattern (bull)"])
        elif vote == "PUT":
            bear += 1; reasons.extend(pat_reasons or ["chart pattern (bear)"])
        return self._pick(bull, bear, reasons)

    # ---- family 3: SMC ---------------------------------------------------- #
    def _smc_block(self, o, c, h, l, i):
        action, reasons = smc.smc_vote(o, c, h, l, i)
        if action is None:
            return None, 0, []
        # strength = number of SMC reasons that support the chosen side
        return action, max(1, len(reasons)), reasons

    @staticmethod
    def _pick(bull: int, bear: int, reasons: list[str]):
        if bull == bear:
            return None, 0, reasons
        return ("CALL" if bull > bear else "PUT"), abs(bull - bear), reasons

    def evaluate(self, asset: str, candles: list[list]) -> Optional[PASignal]:
        s = self.s
        if len(candles) < s.min_candles():
            return None
        i = len(candles) - 1
        if (self._last_action is not None
                and i - self._last_index < s.cooldown_candles):
            return None

        o = [c[1] for c in candles]
        c = [c[2] for c in candles]
        h = [c[3] for c in candles]
        l = [c[4] for c in candles]

        ema_f = ta.ema(c, s.ema_fast)
        ema_s = ta.ema(c, s.ema_slow)
        ema_t = ta.ema(c, s.ema_trend)
        rsi = ta.rsi(c, s.rsi_period)
        macd_line, sig_line, _hist = ta.macd(c, s.macd_fast, s.macd_slow,
                                             s.macd_signal)
        st_k, st_d = ta.stochastic(h, l, c, s.stoch_period, s.stoch_k, s.stoch_d)
        bb_u, _bb_m, bb_l = ta.bollinger(c, s.bb_period, s.bb_mult)
        adx, _pdi, _mdi = ta.adx(h, l, c, s.adx_period)

        a = _last(adx, i)
        regime = ("trend" if a is not None and a >= s.adx_trend else
                  "range" if a is not None and a < s.adx_range else "transition")

        ind = self._indicator_block(o, c, h, l, i, rsi, macd_line, sig_line,
                                    st_k, st_d, bb_u, bb_l, ema_f, ema_s, ema_t)
        pa = self._price_action_block(o, c, h, l, i)
        sx = self._smc_block(o, c, h, l, i)

        blocks = {"indicator": ind, "price_action": pa, "smc": sx}
        bull = sum(1 for a2, _w, _r in blocks.values() if a2 == "CALL")
        bear = sum(1 for a2, _w, _r in blocks.values() if a2 == "PUT")
        if max(bull, bear) < s.min_blocks or bull == bear:
            return None
        action = "CALL" if bull > bear else "PUT"

        agreeing = [(w, r) for a2, w, r in blocks.values() if a2 == action]
        total_w = sum(w for _a, w, _r in blocks.values()) or 1
        agree_w = sum(w for w, _r in agreeing)
        confidence = round(0.5 * (max(bull, bear) / len(blocks))
                           + 0.5 * (agree_w / total_w), 3)
        score = agree_w

        reasons: list[str] = []
        for name, (a2, _w, rs) in blocks.items():
            if a2 == action:
                reasons.append(f"{name}: " + "; ".join(rs))

        self._last_action = action
        self._last_index = i
        return PASignal(time=candles[i][0], asset=asset, action=action,
                        price=c[i], score=score, confidence=confidence,
                        regime=regime,
                        blocks={k: v[0] for k, v in blocks.items()},
                        reasons=reasons)


def format_signal(sig: PASignal) -> str:
    icon = "🟢 CALL" if sig.action == "CALL" else "🔴 PUT"
    lines = [f"{icon} {sig.asset} @ {sig.price:.5f}  "
             f"score {sig.score}  conf {sig.confidence:.2f}  [{sig.regime}]",
             f"  blocks: " + ", ".join(f"{k}={v or '—'}" for k, v in sig.blocks.items())]
    for r in sig.reasons:
        lines.append(f"  - {r}")
    return "\n".join(lines)


def read(store: CandleStore, asset: str, period: int, limit: int = 500):
    candles = store.load(asset, period, limit=limit)
    eng = PAEngine()
    sig = eng.evaluate(asset, candles)
    return sig, len(candles), eng.s.min_candles()


def main():
    ap = argparse.ArgumentParser(description="indicator + price action + SMC bot")
    ap.add_argument("asset", nargs="?", default="EURUSD_otc")
    ap.add_argument("period", nargs="?", type=int, default=60)
    ap.add_argument("--db", default="data/candles.db")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--min-blocks", type=int, default=2)
    args = ap.parse_args()

    store = CandleStore(args.db)
    strat = PAStrategy(min_blocks=args.min_blocks)
    while True:
        eng = PAEngine(strat)
        candles = store.load(args.asset, args.period, limit=500)
        if len(candles) < strat.min_candles():
            print(f"need {strat.min_candles()} candles, have {len(candles)} — "
                  f"let it stream for {(strat.min_candles()-len(candles))*args.period}s")
        else:
            sig = eng.evaluate(args.asset, candles)
            if sig is None:
                print(f"no setup on {args.asset} {args.period}s "
                      f"(need >={args.min_blocks} families to agree)")
            else:
                print(format_signal(sig))
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
