#!/usr/bin/env python3
"""Discipline + market-understanding bot.

The premise (yours): a bot's real edge over a human is not a magic indicator, it
is **discipline** — it never gets tired, never revenge-trades, and it can hold a
multi-pair map of market state in its head that no person can. So this bot's job
is (a) to *understand* the market state and (b) to **refuse to trade** whenever
it does not. It abstains by default; a signal is the exception, not the rule.

Every read passes through an ordered checklist. If any gate fails, the verdict
is NO TRADE and the failing gate is named:

  1. data quality   — provenance.quality (gaps / frozen candles / spikes)
  2. regime defined — regime.classify (not a transitional, ambiguous market)
  3. structure      — structure_state (a directional trend, not a range)
  4. confluence     — pabot (>= N families: indicator / price action / SMC agree)
  5. agreement      — the read agrees with the structure trend
  6. confidence+EV  — payout-adjusted expected value clears breakeven

Honest note (AGENTS.md): fewer signals is *not* the same as better odds — the
repo measured that being pickier only fires less, it does not raise the per-signal
hit-rate. This bot is the *discipline* half of the problem, which is real and
measurable; it tracks itself in journal.db so its forward accuracy is measured,
never assumed. Alerts only — it never places a trade.

    python discipline.py EURUSD_otc 60          # one-shot, with the full checklist
    python discipline.py --scan 60              # every pair, only passing setups
    python discipline.py --scan 60 --watch      # re-scan every 30s (discipline in action)
"""
from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from typing import Optional

import provenance
import regime as regime_mod
import structure as structure_mod
from candle_store import CandleStore
from pabot import PAEngine, PAStrategy

CALL, PUT, NO_TRADE = "CALL", "PUT", "NO TRADE"


@dataclass
class DisciplineConfig:
    min_quality: float = 0.70        # provenance quality floor
    require_regime_defined: bool = True
    require_structure: bool = True   # structure must be up/down, not range
    min_blocks: int = 2              # pabot confluence requirement
    min_strength: float = 0.60       # signal strength floor (NOT a probability)
    payout: float = 0.85
    min_settled: int = 30            # settled signals before an edge is "proven"

    def breakeven(self) -> float:
        return 1.0 / (1.0 + self.payout)


@dataclass
class Setup:
    asset: str
    period: int
    action: str = NO_TRADE
    status: str = "no_trade"         # signal | candidate | no_trade
    proven: bool = False             # measured edge confirmed in the journal
    confidence: float = 0.0          # signal strength (heuristic, not calibrated)
    price: float = 0.0
    time: int = 0
    regime: str = ""
    structure: str = ""
    quality: float = 0.0
    blocks: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)     # [(name, ok, detail)]
    blocked_by: list = field(default_factory=list)
    reasons: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _check(name: str, ok: bool, detail: str, out: list):
    out.append((name, ok, detail))
    return ok


def _recent_run(cs: list, period: int, max_gap_mult: int = 3) -> list:
    """The trailing contiguous stretch, stopping at the first large gap.

    `provenance.quality` scores lifetime *coverage*, so a feed that was down for
    a day would look '98% gaps' even though the recent session is perfectly
    spaced. We only ever act on the current contiguous session, so measure
    quality on that run (the last candles that are <= `max_gap_mult * period`
    apart), not on the whole history.
    """
    end = len(cs) - 1
    start = end
    while start > 0 and (cs[start][0] - cs[start - 1][0]) <= max_gap_mult * period:
        start -= 1
    return cs[start:end + 1]


def edge_proven(period: int, cfg: DisciplineConfig,
                db: str = "data/journal.db") -> tuple[bool, dict]:
    """Is there a *measured* edge for this timeframe? (never assumed.)

    Reads the live journal (same signals the bot would fire), filters to this
    period, and requires: enough settled decisive signals, a Wilson CI lower
    bound above the payout breakeven, and a positive payout-adjusted EV. Until
    that holds, every passing setup is a 'candidate', not a 'signal'.
    """
    from journal import Journal, summary
    j = Journal(db)
    s = summary(j, engine="discipline", payout=cfg.payout)
    rows = j.rows(engine="discipline")
    d = [r for r in rows if r[4] in ("win", "loss", "flat") and r[1] == period]
    wins = sum(1 for r in d if r[4] == "win")
    losses = sum(1 for r in d if r[4] == "loss")
    decisive = wins + losses
    from journal import wilson
    lo, hi = wilson(wins, decisive) if decisive else (0.0, 0.0)
    hit = (wins / decisive) if decisive else 0.0
    be = cfg.breakeven()
    ev = (hit * cfg.payout - (1 - hit)) if decisive else 0.0
    proven = (decisive >= cfg.min_settled and lo > be and ev > 0)
    return proven, {"period": period, "decisive": decisive, "wins": wins,
                    "losses": losses, "hit": round(hit, 4),
                    "ci": [round(lo, 4), round(hi, 4)],
                    "breakeven": round(be, 4), "ev": round(ev, 4),
                    "proven": proven, "min_settled": cfg.min_settled}


def assess(store: CandleStore, asset: str, period: int,
           cfg: Optional[DisciplineConfig] = None,
           candles: Optional[list] = None,
           engine: Optional[PAEngine] = None,
           proven: bool = False) -> Setup:
    """Run the full market-understanding + discipline checklist for one series."""
    cfg = cfg or DisciplineConfig()
    cs = candles if candles is not None else store.load(asset, period, limit=600)
    st = Setup(asset=asset, period=period)
    if not cs:
        st.blocked_by.append("no candles")
        return st

    i = len(cs) - 1
    o = [c[1] for c in cs]
    c = [c[2] for c in cs]
    h = [c[3] for c in cs]
    l = [c[4] for c in cs]
    times = [c[0] for c in cs]
    st.time = times[i]
    st.price = c[i]

    # 1. data quality ------------------------------------------------------- #
    run = _recent_run(cs, period)
    q = provenance.quality(run, period)
    st.quality = q["score"]
    if not _check("data quality", q["tradable"] and q["score"] >= cfg.min_quality,
                  f"score {q['score']:.2f} over {len(run)} recent candles, "
                  f"gaps {q['gap_ratio']:.0%}, flat {q['flat_ratio']:.0%}",
                  st.checks):
        st.blocked_by.append("data quality")

    # 2. regime defined ----------------------------------------------------- #
    rg = regime_mod.classify(o, c, h, l, times, i)
    st.regime = rg.trend
    if cfg.require_regime_defined and not _check(
            "regime defined", rg.defined,
            f"{rg.trend}, ADX {rg.adx:.0f}, ER {rg.efficiency:.2f}, vol {rg.volatility}",
            st.checks):
        st.blocked_by.append("regime undefined")

    # 3. structure ---------------------------------------------------------- #
    sx = structure_mod.structure_state(o, c, h, l, i)
    st.structure = sx.trend
    if cfg.require_structure and not _check(
            "structure directional", sx.trend in (structure_mod.UP, structure_mod.DOWN),
            f"structure {sx.trend}"
            + (f", last BOS {sx.last_bos.direction}" if sx.last_bos else ""),
            st.checks):
        st.blocked_by.append("structure is range")

    # 4. confluence (pabot) ------------------------------------------------- #
    eng = engine or PAEngine(PAStrategy(min_blocks=cfg.min_blocks))
    sig = eng.evaluate(asset, cs)
    if sig is not None:
        st.blocks = sig.blocks
    if not _check("confluence", sig is not None,
                  (f"{sig.action}: " + ", ".join(
                      f"{k}={v or '-'}" for k, v in sig.blocks.items()))
                  if sig else f"fewer than {cfg.min_blocks} families agree",
                  st.checks):
        st.blocked_by.append("no confluence")
        return st  # nothing to agree/disagree with
    st.action = sig.action
    st.confidence = sig.confidence
    st.reasons = list(sig.reasons)

    # 5. agreement with structure + regime ---------------------------------- #
    want = structure_mod.UP if sig.action == CALL else structure_mod.DOWN
    agree_struct = sx.trend == want
    rg_dir = (rg.trend.endswith("up") if sig.action == CALL
              else rg.trend.endswith("down"))
    if not _check("agrees with structure", agree_struct,
                  f"read {sig.action} vs structure {sx.trend}", st.checks):
        st.blocked_by.append("conflicts with structure")
    if not _check("agrees with regime", rg_dir or rg.trend == regime_mod.SIDEWAYS,
                  f"read {sig.action} vs regime {rg.trend}", st.checks):
        st.blocked_by.append("conflicts with regime")

    # 6. strength (heuristic floor, NOT a probability) ---------------------- #
    if not _check("signal strength", st.confidence >= cfg.min_strength,
                  f"strength {st.confidence:.2f} (floor {cfg.min_strength:.2f}) "
                  f"— a display score, not a calibrated probability",
                  st.checks):
        st.blocked_by.append("strength below floor")

    st.proven = proven
    if st.blocked_by:
        st.status = "no_trade"
        st.action = NO_TRADE
    elif proven:
        st.status = "signal"          # every gate passed AND the edge is measured
    else:
        st.status = "candidate"       # every gate passed but the edge is NOT yet proven
    return st


def log_candidates(store: CandleStore, period: int, setups: list[Setup],
                   journal_db: str = "data/journal.db",
                   expiry_candles: int = 1) -> int:
    """Record every passing setup in the journal so it can be forward-settled.

    This is what turns a 'candidate' into a measured 'signal' (or proves it was
    noise). Uses the same entry/expiry plan the scanner uses, so the record is
    honest: enter at the next candle boundary, settle on the expiry close.
    """
    from journal import Journal, settle_open
    from scan import entry_plan
    j = Journal(journal_db)
    now = int(time.time())
    added = 0
    for st in setups:
        if st.status == "no_trade":
            continue
        plan = entry_plan(period, st.time, now, expiry_candles)
        row = {"asset": st.asset, "period": st.period, "action": st.action,
               "price": st.price, "score": round(st.confidence, 4),
               "confidence": st.confidence, "regime": st.regime, **plan}
        if j.add(row, "discipline", now):
            added += 1
    settle_open(j, store, now)
    return added


def format_setup(st: Setup) -> str:
    if st.status == "signal":
        icon = "🟢 CALL" if st.action == CALL else "🔴 PUT"
        head = (f"{icon}  {st.asset} {st.period}s @ {st.price:.5f}  "
                f"conf {st.confidence:.2f}  [{st.regime}/{st.structure}]  PROVEN")
    elif st.status == "candidate":
        icon = "◐ CALL" if st.action == CALL else "◑ PUT"
        head = (f"{icon}  {st.asset} {st.period}s @ {st.price:.5f}  "
                f"conf {st.confidence:.2f}  [{st.regime}/{st.structure}]  "
                f"CANDIDATE (edge not yet proven)")
    else:
        head = (f"⛔ NO TRADE  {st.asset} {st.period}s  "
                f"(blocked: {', '.join(st.blocked_by)})")
    lines = [head]
    for name, ok, detail in st.checks:
        lines.append(f"    {'✓' if ok else '✗'} {name:22} {detail}")
    for r in st.reasons:
        lines.append(f"    · {r}")
    return "\n".join(lines)


def scan(store: CandleStore, period: int, cfg: DisciplineConfig,
         min_candles: int = 60, proven: bool = False) -> list[Setup]:
    out = []
    for asset, per in store.series(min_candles=min_candles, periods=[period]):
        st = assess(store, asset, per, cfg, proven=proven)
        out.append(st)
    out.sort(key=lambda s: (s.status == "no_trade", -s.confidence))
    return out


def main():
    ap = argparse.ArgumentParser(description="discipline + market-understanding bot")
    ap.add_argument("asset", nargs="?", default="EURUSD_otc")
    ap.add_argument("period", nargs="?", type=int, default=60)
    ap.add_argument("--db", default="data/candles.db")
    ap.add_argument("--journal-db", default="data/journal.db")
    ap.add_argument("--scan", action="store_true", help="scan every pair")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--min-strength", type=float, default=0.60)
    ap.add_argument("--min-quality", type=float, default=0.70)
    ap.add_argument("--payout", type=float, default=0.85)
    ap.add_argument("--all", action="store_true", help="show NO TRADE rows too")
    ap.add_argument("--log", action="store_true",
                    help="record passing setups in the journal for forward tracking")
    args = ap.parse_args()

    cfg = DisciplineConfig(min_strength=args.min_strength,
                           min_quality=args.min_quality, payout=args.payout)
    store = CandleStore(args.db)
    while True:
        proven, edge = edge_proven(args.period, cfg, args.journal_db)
        note = (f"edge {'PROVEN' if proven else 'NOT proven'} "
                f"({edge['decisive']} settled, hit {edge['hit']:.1%}, "
                f"CI {edge['ci'][0]:.1%}–{edge['ci'][1]:.1%} vs breakeven "
                f"{edge['breakeven']:.2%}; need {cfg.min_settled} settled)")
        if args.scan:
            setups = scan(store, args.period, cfg, proven=proven)
            passing = [s for s in setups if s.status != "no_trade"]
            if args.log and passing:
                n = log_candidates(store, args.period, passing, args.journal_db)
                print(f"    logged {n} new candidate(s) to the journal")
            print(f"\n=== discipline scan {args.period}s | {len(passing)} of "
                  f"{len(setups)} pairs pass the checklist ===")
            print(f"    {note}")
            shown = setups if args.all else passing
            for st in shown[:40]:
                if args.all or st.status != "no_trade":
                    print(format_setup(st))
                else:
                    print(f"⛔ NO TRADE  {st.asset}  ({', '.join(st.blocked_by)})")
            if not passing:
                print("  no pair clears every gate right now — that is the bot "
                      "working, not failing")
        else:
            st = assess(store, args.asset, args.period, cfg, proven=proven)
            print(format_setup(st))
            print(f"    {note}")
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
