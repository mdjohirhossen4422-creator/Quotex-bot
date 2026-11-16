# Running the collector inside an Android app

Short answer: **yes, it is possible**, but Android does not run Python natively.
You ship a Python runtime *inside* the app. The collector was made embed-friendly
for this (`LiveCollector.stop()` + no hard dependency on the main thread).

## Why the collector suits this

- The only third-party dependency is `websocket-client` — **pure Python**, no
  compiled extension. That is the single biggest win for mobile packaging.
- Everything else is the standard library (`sqlite3`, `ssl`, `threading`).
- No `numpy` / `scikit-learn` / `pyarrow` in the collector path.
- `run()` now skips `signal.signal()` when off the main thread, and exposes
  `stop()` so a host app can shut it down cleanly (see
  `test_live_collector.py::test_embedding_stop_from_another_thread`).

## The hard parts (be honest about these)

1. **Packaging a Python runtime.** Two realistic routes:
   - **Chaquopy** (Gradle plugin, Android Studio): adds CPython + pip wheels to
     your APK. Easiest if you build with Android Studio/Gradle.
   - **Kivy + python-for-android** (Buildozer): bundles CPython into an APK from
     a `buildozer.spec`. Good for a self-contained app; build on Linux.
   Both produce an APK that carries its own Python — the user installs nothing.

2. **A long-running background service.** Android kills background work. To keep
   collecting you need a **foreground service** with a persistent notification,
   plus battery-optimization exemption on many OEM ROMs. This is an Android
   platform requirement, not a Python one.

3. **The feed geo-block still applies to the phone's network.** The Cloudflare
   `403` we hit on Botkeep is about the *source IP*. A phone on a normal mobile
   connection is usually fine, but if the carrier/region is blocked you need the
   same `QX_PROXY` fix.

4. **Storage.** The DB grows (~63 MB/day candles-only). Phones are fine for
   days, not months — plan to export/sync and prune.

5. **No Play Store path for a scraper app.** Play policy restricts apps that
   scrape third-party services; sideload (APK) is the realistic distribution.

## Minimal embedding shape

```python
# runs inside the app's Python runtime (Chaquopy/Kivy), on a worker thread
from live_collector import LiveCollector

collector = LiveCollector(
    db_path="/data/data/<pkg>/files/candles.db",
    pairs=["EURUSD_otc", "BTCUSD_otc"],   # keep the set small on mobile
    state_path="/data/data/<pkg>/files/collector_state.json",
    store_ticks=False,                    # candles only (~10x smaller)
    proxy="",                             # set only if the carrier is blocked
)

# start on a background thread; keep the process alive with a foreground service
threading.Thread(target=collector.run, daemon=True).start()

# on service stop / app exit:
collector.stop()
```

The collector already handles reconnect/backoff, dedup, and WAL persistence, so
the app layer only has to (a) keep the process alive and (b) show a status line
from `collector._write_state()`.

## What NOT to expect

- This does not make the signals more accurate. Prior honest tests put every
  engine at ~50% (a coin flip). The app would collect data, not predict markets.
- A phone is a worse always-on host than a small VPS: sleep, Doze, and OEM
  killers fight you constantly. If the goal is continuous collection, a US VPS
  (no geo-block, no Doze) is simpler and more reliable than an Android app.
