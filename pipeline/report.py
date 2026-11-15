"""Print the Phase 2 final report from the generated artifacts (Task 14).

    python -m pipeline.report
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import OUT_DIR, VERSION


def _load():
    root = Path(OUT_DIR)
    rep = json.loads((root / "phase2_report.json").read_text())
    metas = {}
    for m in root.glob("*/*/metadata.json"):
        metas[(m.parent.parent.name, m.parent.name)] = json.loads(m.read_text())
    return rep, metas


def main():
    rep, metas = _load()
    fp = rep["source_fingerprint"]
    L = []
    L.append(f"PHASE 2 — HISTORICAL DATA & FEATURE ENGINEERING ({VERSION})")
    L.append("=" * 64)
    L.append("1. raw ticks inspected        : "
             f"{fp['ticks']:,}")
    L.append("2. candles inspected (stored) : "
             f"{fp['candles']:,}  (+ per-tick rebuild for every dataset)")
    rows = sum(d["rows"] for d in rep["datasets"])
    L.append(f"3. usable samples (all sets)  : {rows:,}")
    dropped = sum(d["dropped_unusable"] for d in rep["datasets"])
    L.append(f"4. rejected samples           : {dropped:,} "
             "(feature warmup / label undefined)")
    avail = {d["features_available"] for d in rep["datasets"] if d["written"]}
    L.append(f"5. features generated         : {max(avail) if avail else 0} "
             "(per usable dataset)")
    L.append("6. feature groups             : trend, momentum, volatility, "
             "geometry, tick, structure")
    L.append("7. labels generated           : 5 "
             "(label + next_ret + next_range + next_dir + label_ts)")
    L.append("8. label distribution (usable datasets):")
    for d in rep["datasets"]:
        if d["written"]:
            L.append(f"     {d['market']} {d['timeframe']}: {d['label_distribution']}")
    L.append("9. samples per timeframe:")
    for tf in ("1m", "5m", "15m", "30m"):
        n = sum(d["rows"] for d in rep["datasets"] if d["timeframe"] == tf)
        L.append(f"     {tf}: {n:,}")
    L.append("10. samples per market type:")
    for mk in ("real", "otc"):
        n = sum(d["rows"] for d in rep["datasets"] if d["market"] == mk)
        L.append(f"     {mk}: {n:,}")
    L.append("11. historical coverage (per usable dataset, candles/asset):")
    for d in rep["datasets"]:
        if d["written"]:
            L.append(f"     {d['market']} {d['timeframe']}: "
                     f"{d['coverage_min']}..{d['coverage_max']}")
    L.append("12. missing data statistics:")
    for d in rep["datasets"]:
        c = d["cleaning"]
        L.append(f"     {d['market']} {d['timeframe']}: gaps={c['gaps']} "
                 f"session_gaps={c['session_gaps']} partial={c['partial']} "
                 f"spikes_flagged={c['flagged_spike']} "
                 f"dropped(dup/bad/misaligned)={c['dropped_duplicate']}/"
                 f"{c['dropped_bad_ohlc']}/{c['dropped_misaligned']}")
    L.append("13. data leakage test results:")
    for d in rep["datasets"]:
        if d["written"]:
            ok = {k: v["ok"] for k, v in d["leakage"].items()}
            L.append(f"     {d['market']} {d['timeframe']}: {ok}  "
                     f"split_disjoint={d['split_disjoint']['ok']}")
    L.append("14. train/validation/test sample counts:")
    for d in rep["datasets"]:
        if d["written"]:
            L.append(f"     {d['market']} {d['timeframe']}: {d['split_counts']}")
    L.append(f"15. processing time           : {rep['total_seconds']} s")
    L.append(f"16. memory usage (peak RSS)   : {rep['peak_memory_mb']} MB")
    L.append("17. tests                     : see `python test_pipeline.py` "
             "(26 tests) and the full suite")
    L.append("18. files created:")
    for (mk, tf), m in sorted(metas.items()):
        L.append(f"     processed_data/{mk}/{tf}/data.parquet  "
                 f"(samples={m['sample_count']}, v={m['dataset_version']})")
    L.append("19. remaining problems / honesty notes:")
    for d in rep["datasets"]:
        if not d["written"]:
            L.append(f"     {d['market']} {d['timeframe']}: NOT WRITTEN — "
                     f"coverage {d['coverage_min']}..{d['coverage_max']} candles, "
                     f"{d['rows']} usable rows "
                     f"(insufficient history for this timeframe)")
    L.append("     - Contiguous history is ~2.2 h/instrument; EMA200 (needs 200 "
             "bars) is excluded where unsupported.")
    L.append("     - Real (non-OTC) pairs were frozen (market closed) -> no "
             "labels; only OTC has usable 1m data.")
    print("\n".join(L))


if __name__ == "__main__":
    main()
