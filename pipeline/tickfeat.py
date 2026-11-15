"""Tick-based order-flow features (Task 5).

Up/down/flat counts use the ACTUAL price change between consecutive ticks, not
the broker direction flag. The broker flag is kept as its own feature
(`broker_dir_ratio`) and is never interpreted as BUY/SELL — its meaning is
unverified, and an earlier phase found it uninformative.

No volume is invented: the public feed carries no genuine trade size, so there
is no volume feature here.
"""
from __future__ import annotations

import math
from typing import Optional


def _bucket(ts: float, period: int) -> int:
    return int(ts // period) * period


def tick_features(ticks: list[tuple[float, float, int]], period: int
                  ) -> dict[int, dict]:
    """Per-bucket tick features, keyed by candle bucket time.

    `ticks` must be chronological (ts, price, dir). One pass, O(n).
    """
    out: dict[int, dict] = {}
    cur_b: Optional[int] = None
    # per-bucket accumulators
    prices: list[float] = []
    dirs: list[int] = []
    first_ts = last_ts = 0.0
    up = down = flat = 0

    def flush(b: int):
        n = len(prices)
        if n == 0:
            return
        span = last_ts - first_ts
        interval = span / (n - 1) if n > 1 else float(period)
        first, last = prices[0], prices[-1]
        ret = (last - first) / first if first else 0.0
        # realised short-term vol from tick-to-tick pct changes
        pcs = [math.log(prices[i] / prices[i - 1])
               for i in range(1, n) if prices[i - 1] > 0]
        vol = (sum(x * x for x in pcs) / len(pcs)) ** 0.5 if pcs else 0.0
        # acceleration: second-half drift minus first-half drift
        half = n // 2
        accel = 0.0
        if half >= 1 and n - half >= 1:
            f0, f1 = prices[0], prices[half]
            s0, s1 = prices[half], prices[-1]
            d1 = (f1 - f0) / f0 if f0 else 0.0
            d2 = (s1 - s0) / s0 if s0 else 0.0
            accel = d2 - d1
        rng = (max(prices) - min(prices)) / first if first else 0.0
        out[b] = {
            "tick_count": n,
            "tick_velocity": n / period,
            "avg_tick_interval": interval,
            "tick_up_count": up,
            "tick_down_count": down,
            "tick_flat_count": flat,
            "tick_dir_ratio": ((up - down) / (up + down)) if (up + down) else 0.0,
            "tick_momentum": ret,
            "tick_short_vol": vol,
            "tick_accel": accel,
            "tick_range": rng,
            "broker_dir_ratio": (sum(dirs) / n) if n else 0.0,
        }

    prev_price: Optional[float] = None
    for ts, price, d in ticks:
        b = _bucket(ts, period)
        if cur_b is None or b != cur_b:
            if cur_b is not None:
                flush(cur_b)
            cur_b = b
            prices, dirs = [], []
            up = down = flat = 0
            first_ts = ts
            prev_price = None
        if prev_price is not None:
            if price > prev_price:
                up += 1
            elif price < prev_price:
                down += 1
            else:
                flat += 1
        prices.append(price)
        dirs.append(int(d))
        prev_price = price
        last_ts = ts
    if cur_b is not None:
        flush(cur_b)
    return out


def tick_activity(counts: list[int], window: int = 50) -> list[float]:
    """Causal activity ratio: this candle's tick_count vs the running median.

    Makes raw tick counts comparable across instruments (which tick at very
    different rates). Uses only the current and past candles.
    """
    import statistics
    out: list[float] = []
    for i in range(len(counts)):
        lo = max(0, i - window)
        seg = [c for c in counts[lo:i + 1] if c > 0]
        med = statistics.median(seg) if seg else 0.0
        out.append(counts[i] / med if med else 0.0)
    return out
