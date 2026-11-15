#!/usr/bin/env python3
"""Candle-level tick summaries — order flow preserved without raw ticks.

`live_collector.py --no-ticks` does not store the raw tick stream (it does not
fit a 1 GB free volume). That would throw away the only data the candle feed
discards: how many ticks a candle had, in which direction they moved, and how
fast. This module keeps those as **per-candle summaries**, so the information
survives even when the raw ticks are not written.

The summaries are derived from the ACTUAL price change between consecutive ticks
(up/down/flat), never from the broker's direction flag: the flag is kept as its
own feature (`broker_dir_ratio`) and is never read as BUY/SELL, because an
earlier phase measured it as uninformative (~49.7%).

Causality: a summary is emitted only when a bucket *rolls over* (a tick from a
new bucket arrives), i.e. once the candle is final. The still-forming candle is
never emitted, so no half-built summary can leak into training.

Memory is O(periods) per asset — only the current bucket's accumulators live in
memory — so this is safe on a 512 MB instance regardless of history length.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from typing import Optional

# The nine numbers stored per candle. Order is fixed so the writer and the
# reader agree without a schema lookup.
SUMMARY_FIELDS = (
    "tick_count", "tick_up", "tick_down", "tick_flat",
    "tick_dir_ratio", "tick_momentum", "tick_short_vol",
    "tick_range", "broker_dir_ratio",
)


class _Acc:
    __slots__ = ("bucket", "prices", "up", "down", "flat", "dir_sum",
                 "first_ts", "last_ts", "prev")

    def __init__(self, bucket: int, ts: float, price: float):
        self.bucket = bucket
        self.prices = [price]
        self.up = self.down = self.flat = 0
        self.dir_sum = 0
        self.first_ts = self.last_ts = ts
        self.prev = price


def _finalize(acc: _Acc) -> dict:
    prices = acc.prices
    n = len(prices)
    first, last = prices[0], prices[-1]
    moved = acc.up + acc.down
    # realised short-term volatility from consecutive log returns
    pcs = [math.log(prices[i] / prices[i - 1])
           for i in range(1, n) if prices[i - 1] > 0 and prices[i] > 0]
    vol = (sum(x * x for x in pcs) / len(pcs)) ** 0.5 if pcs else 0.0
    rng = (max(prices) - min(prices)) / first if first else 0.0
    return {
        "tick_count": n,
        "tick_up": acc.up,
        "tick_down": acc.down,
        "tick_flat": acc.flat,
        "tick_dir_ratio": ((acc.up - acc.down) / moved) if moved else 0.0,
        "tick_momentum": (last - first) / first if first else 0.0,
        "tick_short_vol": vol,
        "tick_range": rng,
        "broker_dir_ratio": (acc.dir_sum / n) if n else 0.0,
    }


class TickSummarizer:
    """Feed ticks; get a finalized summary when a candle closes.

    `add()` returns `(bucket, summary)` for the candle that just closed (or
    `None`). The summary is also kept in `pending`, keyed by
    `(asset, period, bucket)`, so a writer can attach it to the candle row it
    persists for that same bucket. `pending` is bounded so a stalled writer
    cannot grow memory without limit.
    """

    def __init__(self, periods, max_pending: int = 4096):
        self._periods = list(periods)
        self._acc: dict[tuple[str, int], _Acc] = {}
        self.pending: "OrderedDict[tuple[str, int, int], dict]" = OrderedDict()
        self.max_pending = max_pending

    def add(self, asset: str, period: int, ts: float, price: float,
            direction=None) -> Optional[tuple[int, dict]]:
        bucket = int(ts // period) * period
        key = (asset, period)
        acc = self._acc.get(key)
        rolled: Optional[tuple[int, dict]] = None
        if acc is None or acc.bucket != bucket:
            if acc is not None:
                rolled = (acc.bucket, _finalize(acc))
            acc = _Acc(bucket, ts, price)
            self._acc[key] = acc
        else:
            if price > acc.prev:
                acc.up += 1
            elif price < acc.prev:
                acc.down += 1
            else:
                acc.flat += 1
            acc.prices.append(price)
            acc.prev = price
            acc.last_ts = ts
        try:
            acc.dir_sum += int(direction)
        except (TypeError, ValueError):
            pass
        if rolled is not None:
            self.pending[(asset, period, rolled[0])] = rolled[1]
            while len(self.pending) > self.max_pending:
                self.pending.popitem(last=False)
        return rolled

    def pop(self, asset: str, period: int, bucket: int) -> Optional[dict]:
        """Take the summary for one candle (used when persisting its row)."""
        return self.pending.pop((asset, period, bucket), None)

    def drop_forming(self):
        """Discard in-progress accumulators (called when a candle is dropped)."""
        self._acc.clear()

    def stats(self) -> dict:
        return {"pending_summaries": len(self.pending),
                "active_buckets": len(self._acc)}


def summary_to_row(summary: Optional[dict]) -> tuple:
    """Flatten a summary to the fixed column order (None -> all-None row)."""
    if not summary:
        return (None,) * len(SUMMARY_FIELDS)
    return tuple(summary.get(k) for k in SUMMARY_FIELDS)
