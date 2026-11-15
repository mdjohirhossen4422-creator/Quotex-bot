"""Phase 2 runner — build every processed dataset and the final report.

Read-only on the source DB, write-only to processed_data/. Each asset's ticks are
streamed once per market and reused across timeframes; per (market, timeframe)
the rows of all assets are pooled, sorted chronologically, split, leak-checked,
and persisted.

    python -m pipeline.run                    # everything
    python -m pipeline.run --markets otc
    python -m pipeline.run --timeframes 1m,5m
    python -m pipeline.run --limit-assets 3   # quick smoke run
"""
from __future__ import annotations

import argparse
import json
import math
import resource
import time
from collections import defaultdict
from pathlib import Path

from . import candles as cb
from . import cleaning, dataset, db, leakage, splits, store
from .config import (FEATURE_ORDER, LABEL_COLUMNS, META_COLUMNS, TIMEFRAMES,
                     MARKETS, HARD_MIN_ROWS, OUT_DIR, VERSION)

_LABEL_NAME = {0: "DOWN", 1: "SIDEWAYS", 2: "UP"}


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def _pool(frames: list[dict]) -> dict:
    """Concatenate per-asset frames into one chronologically sorted frame."""
    tagged = []
    for fi, m in enumerate(frames):
        for i in range(m["n"]):
            tagged.append((m["meta"]["time"][i], fi, i))
    tagged.sort(key=lambda x: (x[0], x[1], x[2]))
    pooled = {"meta": {k: [] for k in META_COLUMNS},
              "features": {k: [] for k in FEATURE_ORDER},
              "labels": {k: [] for k in LABEL_COLUMNS}}
    for _, fi, i in tagged:
        m = frames[fi]
        for k in META_COLUMNS:
            pooled["meta"][k].append(m["meta"][k][i])
        for k in FEATURE_ORDER:
            pooled["features"][k].append(m["features"][k][i])
        for k in LABEL_COLUMNS:
            pooled["labels"][k].append(m["labels"][k][i])
    return pooled


def _drop_unusable(pooled: dict, feat_keys: list[str]) -> tuple[dict, int]:
    """Keep rows where every available feature and the label are finite."""
    n = len(pooled["meta"]["time"])
    keep = []
    for i in range(n):
        if pooled["labels"]["label"][i] is None:
            continue
        if not all(_finite(pooled["features"][k][i]) for k in feat_keys):
            continue
        keep.append(i)
    if len(keep) == n:
        return pooled, 0
    out = {"meta": {k: [v[i] for i in keep] for k, v in pooled["meta"].items()},
           "features": {k: [v[i] for i in keep] for k, v in pooled["features"].items()},
           "labels": {k: [v[i] for i in keep] for k, v in pooled["labels"].items()}}
    return out, n - len(keep)


def _available_features(pooled: dict) -> tuple[list[str], list[str]]:
    """Split FEATURE_ORDER into those the series can support and those it cannot.

    An indicator like EMA200 needs 200 bars; if a series never reaches that, its
    column is all-None. Such a column is *excluded* (and recorded in metadata)
    rather than fabricated — the honest handling of insufficient history.
    """
    avail, unavail = [], []
    for k in FEATURE_ORDER:
        if any(_finite(v) for v in pooled["features"][k]):
            avail.append(k)
        else:
            unavail.append(k)
    return avail, unavail


def _columnar(pooled: dict, feat_keys: list[str]) -> dict:
    cols: dict[str, list] = {}
    for k in META_COLUMNS:
        cols[k] = pooled["meta"][k]
    for k in feat_keys:
        cols[k] = pooled["features"][k]
    for k in LABEL_COLUMNS:
        cols[k] = pooled["labels"][k]
    return cols


def _label_dist(labels: list) -> dict:
    d = defaultdict(int)
    for v in labels:
        d[_LABEL_NAME.get(v, str(v))] += 1
    return dict(d)


def _higher_series(base_candles: list[dict], base_period: int,
                   higher_tfs: list[str]) -> dict:
    """Aggregate base candles up to each higher timeframe -> {tf: (times, codes)}."""
    out: dict = {}
    for ht in higher_tfs:
        hp = TIMEFRAMES[ht]
        factor = hp // base_period
        agg = cb.aggregate_candles(base_candles, factor) if factor > 1 else base_candles
        times = [c["time"] for c in agg]
        codes = dataset.trend_code([c["close"] for c in agg])
        out[ht] = (times, codes)
    return out


def build_market(conn, market: str, tfs: list[str],
                 limit_assets: int | None) -> dict:
    """Build every requested timeframe for one market in a single asset pass."""
    assets = db.assets_with_ticks(conn, market)
    if limit_assets:
        assets = assets[:limit_assets]

    frames: dict[str, list[dict]] = {tf: [] for tf in tfs}
    clean_reports: dict[str, list] = {tf: [] for tf in tfs}
    coverage: dict[str, list] = {tf: [] for tf in tfs}

    for a in assets:
        ticks: list[tuple[float, float, int]] = []
        for chunk in db.iter_ticks(conn, a):
            ticks.extend(chunk)
        if not ticks:
            continue
        base_candles = cb.build_from_ticks(ticks, 60)
        for tf in tfs:
            period = TIMEFRAMES[tf]
            raw = base_candles if period == 60 else cb.build_from_ticks(ticks, period)
            clean, rep = cleaning.clean_candles(a, period, raw)
            clean_reports[tf].append(rep)
            coverage[tf].append(len(clean))
            higher_tfs = [t for t in TIMEFRAMES if TIMEFRAMES[t] > period]
            higher = _higher_series(base_candles, 60, higher_tfs)
            m = dataset.build_matrix(a, market, tf, period, clean,
                                     ticks=ticks, higher=higher)
            frames[tf].append(m)
        del ticks, base_candles

    results = {}
    for tf in tfs:
        pooled_raw = _pool(frames[tf])
        avail, unavail = _available_features(pooled_raw)
        pooled, dropped = _drop_unusable(pooled_raw, avail)
        cols = _columnar(pooled, avail)
        times = cols["time"]

        # time-based split + per-row split label (an extra meta column)
        split = splits.time_split(times, TIMEFRAMES[tf])
        split_counts = splits.counts(split)
        split_col = ["embargo"] * len(times)
        for name in ("train", "val", "test"):
            for i in split[name]:
                split_col[i] = name
        cols["split"] = split_col

        # leakage checks
        leak = leakage.run_all(avail, times, cols["asset"])
        ok_corr, corr = leakage.check_feature_label_correlation(
            {k: cols[k] for k in avail}, cols["label"])
        leak["feature_label_correlation"] = {"ok": ok_corr, **corr}
        ok_split, split_detail = leakage.check_split_disjoint(
            [times[i] for i in split["train"]],
            [times[i] for i in split["val"]],
            [times[i] for i in split["test"]])

        cov = coverage[tf]
        summary = {
            "market": market, "timeframe": tf, "period": TIMEFRAMES[tf],
            "assets": len(cov), "rows": len(times), "dropped_unusable": dropped,
            "features_available": len(avail),
            "features_unavailable": unavail,
            "label_distribution": _label_dist(cols["label"]),
            "split_counts": split_counts,
            "leakage": leak,
            "split_disjoint": {"ok": ok_split, **split_detail},
            "cleaning": cleaning.merge_reports(clean_reports[tf]),
            "coverage_min": min(cov) if cov else 0,
            "coverage_max": max(cov) if cov else 0,
            "written": False,
        }
        if len(times) >= HARD_MIN_ROWS and all(v["ok"] for v in leak.values()) \
                and ok_split:
            store.write_dataset(market, tf, cols, {
                "sample_count": len(times),
                "feature_list": avail,
                "features_unavailable": unavail,
                "label_distribution": summary["label_distribution"],
                "split_counts": split_counts,
                "assets": sorted({a for a in cols["asset"]}),
                "time_min": times[0] if times else None,
                "time_max": times[-1] if times else None,
            })
            summary["written"] = True
        results[tf] = summary
    return results


def main():
    ap = argparse.ArgumentParser(description="Phase 2 pipeline")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent.parent
                                        / "data" / "candles.db"))
    ap.add_argument("--markets", default=",".join(MARKETS))
    ap.add_argument("--timeframes", default=",".join(TIMEFRAMES))
    ap.add_argument("--limit-assets", type=int, default=None)
    args = ap.parse_args()

    t_start = time.time()
    conn = db.connect(args.db)
    source_fp = db.source_fingerprint(conn)
    report = {"version": VERSION, "source_fingerprint": source_fp,
              "started": time.strftime("%Y-%m-%d %H:%M:%S"), "datasets": []}

    for market in args.markets.split(","):
        tfs = [t for t in args.timeframes.split(",") if t in TIMEFRAMES]
        t0 = time.time()
        res = build_market(conn, market, tfs, args.limit_assets)
        for tf in tfs:
            s = res[tf]
            s["seconds"] = round(time.time() - t0, 2)
            report["datasets"].append(s)
            print(f"[{market} {tf}] rows={s['rows']} assets={s['assets']} "
                  f"written={s['written']}", flush=True)
    conn.close()

    report["total_seconds"] = round(time.time() - t_start, 2)
    report["peak_memory_mb"] = round(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    out = Path(OUT_DIR) / "phase2_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nreport -> {out}")
    print(f"total {report['total_seconds']}s, peak RSS {report['peak_memory_mb']} MB")


if __name__ == "__main__":
    main()
