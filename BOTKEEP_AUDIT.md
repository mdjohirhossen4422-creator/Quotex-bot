# Botkeep.cloud Free Plan — Collector Compatibility Audit (Phase 2.6)

Scope: can the **live collector** (`live_collector.py`) run on Botkeep.cloud's
**Free** plan (1 workload slot, **512 MB RAM**, **0.5 vCore / 50 % CPU**,
**1 GB storage**, 24/7, no renewal timer, beta, no uptime SLA)?

This is an **audit + preparation** only. **Nothing is deployed.** No secret is
committed. The Render config is untouched. The production collector keeps
running as-is.

Target from Botkeep (verified on botkeep.cloud, 2026-11-15):

| resource | Free plan |
| --- | --- |
| slots | 1 |
| RAM | 512 MB total |
| CPU | 0.5 vCore (50 %) |
| storage | 1 GB total |
| runtime | 24/7, no manual renewal, no idle sleep |
| Python | panel offers a Python profile (docs example uses `python main.py`) |

---

## 1. Result table

| # | Check | Verdict | Evidence |
| --- | --- | --- | --- |
| 1 | Python version compatible | **PASS** | needs ≥ 3.9 (type hints `list[str]`, `dict[str, set]`); dev box runs 3.13.15; Botkeep offers a Python profile |
| 2 | Requirements compatible | **PASS** | collector imports only stdlib + `websocket-client`; no numpy/sklearn/pyarrow. `requirements-collector.txt` pins it |
| 3 | Long-running worker fit | **PASS** | single non-interactive process, logs to stdout, handles SIGINT/SIGTERM, runs forever with `--minutes 0` |
| 4 | `run_collector.sh` supervisor / PID / restart | **PASS** | pidfile guard, `setsid` process group, restart-on-crash proven by SIGKILL test |
| 5 | SQLite size + storage budget | **FAIL (ticks) / PASS (candles-only)** | DB is **201 MB** now; ticks grow **~1.3 GB/day** → over the 1 GB limit in <1 day. Candles-only grows **~63 MB/day** → fits |
| 6 | RAM usage ≤ 512 MB | **PASS** | measured **47–50 MB RSS**, flat over 60 s; ~9× headroom. Heartbeat now reports `rss_kb` |
| 7 | Persistent filesystem for db/logs/state | **NEEDS TEST** | Botkeep has a writable workload FS; *persistence across redeploy* must be confirmed on the instance (it is not documented as a separate mount) |
| 8 | Restart keeps DB + state intact | **PASS (local) / NEEDS TEST (Botkeep)** | stop→counts unchanged, restart→growth resumes, 0 dup. Re-verify on the instance |
| 9 | Outbound WebSocket + 92 subscriptions | **PASS** | `ws2.market-qx.info:443` reachable; 92 instruments discovered; 91 pairs streaming live |
| 10 | Crash auto-restart | **PASS** | `kill -9` → supervisor relaunched in 5 s (rc=137 → new PID); 0 duplicates after |
| 11 | Telegram/API secrets via env vars | **PASS** | collector needs **no** secrets; `alerts.py` reads `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` from env; no `.env` in git |
| 12 | 24-hour test uptime | **NEEDS TEST** | cannot be produced without deploying; see §6 |

Legend: **PASS** = verified here; **FAIL** = will not fit as-is; **NEEDS TEST** =
cannot be confirmed without the Botkeep instance.

---

## 2. The one hard blocker: storage

Measured on the live feed (2026-11-15):

* tick row ≈ **86 bytes** on disk, candle row ≈ **103 bytes** (incl. index);
* ≈ **15 M ticks/day** (173 ticks/s across 91 pairs) → **~1.3 GB/day** of ticks;
* current DB **201 MB** (1.9 M ticks + 300 k candles).

On a **1 GB** volume that is **under one day** before the disk is full. Ticks are
the entire problem: they are ~85 % of the bytes.

**Remedy (implemented): `--no-ticks` mode.** The collector still receives every
tick and still builds 1m/5m/15m/30m candles, but does not write the raw `ticks`
table:

| mode | storage/day | 1 GB lasts |
| --- | --- | --- |
| default (ticks + candles) | ~1.3 GB | **< 1 day — FAIL** |
| `--no-ticks` (candles only) | ~63 MB | **~16 days — PASS** |

Candles-only keeps every timeframe the pipeline trains on; the tick-level
order-flow study (Phase 2.5 §9) would not be possible on the free tier, which is
an honest trade-off, not a hidden one.

Also added: a **bounded in-memory buffer** (`max_buffer = 200 000`) and
**retry-on-flush-failure**, so a transient full disk warns and retries instead of
either silently dropping the batch or OOM-killing the box. Drops are counted in
`ticks_buffer_dropped`.

---

## 3. What was verified locally (evidence)

* **RAM** — `/proc/<pid>/status` VmRSS sampled every 10 s for 60 s: flat
  **48 144 kB (47 MB)**. Heartbeat `rss_kb` now records it.
* **CPU** — `ps %cpu` steady at **3.3 %** of one core (≈ 0.033 vCore of the
  0.5 vCore budget).
* **Dependencies** — fresh venv with *only* `websocket-client`: all four modules
  (`live_collector`, `collector_report`, `market_qx_candles`, `provenance`,
  `candle_store`) import cleanly. Confirmed the collector does **not** need the
  heavy ML stack.
* **Feed** — TCP `ws2.market-qx.info:443` reachable; 92 instruments; live ticks
  growing the DB at ~170/s.
* **Restart safety** — stop: counts identical, 0 duplicate groups; restart:
  growth resumes from the same totals, still 0 duplicates.
* **Crash recovery** — `kill -9` on the collector child; supervisor logged
  `exited rc=137; restarting in 5s` and relaunched; 0 duplicates after.
* **Minimal file set** — the collector bundle needs only `live_collector.py`,
  `market_qx_candles.py`, `provenance.py`, `candle_store.py`, plus
  `requirements-collector.txt`.
* **Tests** — `test_live_collector.py` **9/9** (added a `--no-ticks` test); full
  suite **33/33** green.

---

## 4. Botkeep deployment settings (for when you approve)

**Start command** (Botkeep Python profile — one explicit, non-interactive entry
point):

```
python live_collector.py --no-ticks
```

Rationale: `--no-ticks` is what makes continuous collection fit 1 GB. To keep
raw ticks instead, the volume must be larger (paid plan) — do not run the
default mode on Free, it fills the disk in under a day.

**Environment variables** (Botkeep → Environment):

| var | value | why |
| --- | --- | --- |
| `CANDLE_DB` | `/data/candles.db` (or the workload's persistent path) | the one durable asset; must point at the persistent volume |
| `COLLECTOR_STATE` | `/data/collector_state.json` | heartbeat survives restart |
| `COLLECTOR_STORE_TICKS` | `0` | same as `--no-ticks`; belt-and-braces |
| `COLLECTOR_PAIRS` | *(unset = all pairs)* | optional; e.g. `EURUSD_otc,BTCUSD_otc` to shrink scope |
| `COLLECTOR_LOG_EVERY` | `60` | stdout log cadence |

No secret is required to run the collector. If the Telegram watcher is deployed
instead, add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as Environment secrets
(never in the repo).

**Files to include** (ZIP or GitHub): `live_collector.py`,
`market_qx_candles.py`, `provenance.py`, `candle_store.py`,
`requirements-collector.txt`, and (optional) `collector_report.py`,
`botkeep_preflight.py`. Make `requirements.txt` contain exactly the line from
`requirements-collector.txt`.

**Before starting**, run the preflight on the instance:

```
python botkeep_preflight.py --db "$CANDLE_DB" --storage-mb 1024 --ram-mb 512
```

It checks Python, deps, feed reachability, storage headroom, RAM and writable
paths, and prints PASS/FAIL.

---

## 5. Restrictions — compliance

* **No old row deleted/overwritten** — writes are append-only (`INSERT`, and
  `INSERT OR IGNORE` on the candle PK); schema only extended. Tested.
* **Render config untouched** — `render.yaml`, `Dockerfile`, `fly.toml`,
  `docker-compose.yml` are unmodified.
* **Production collector not stopped/moved** — still running on this box.
* **No secret pushed to GitHub** — none added; `.env`/`telegram.json` are
  git-ignored and none is staged.
* **Permission before deploying** — not deployed; this document is the request.
* **One writer per DB** — the supervisor's pidfile refuses a second collector;
  do **not** run the Botkeep collector and the local one against the same file.

---

## 6. The two checks that require the instance (NEEDS TEST)

1. **Persistent filesystem across redeploy/restart.** Botkeep gives each
   workload a writable filesystem, but it is not documented as a separate
   persistent volume. Test on the instance: start, note the DB size, force a
   restart, confirm `data/candles.db` is unchanged and the heartbeat resumes.
   If it does not persist, keep the authoritative DB here and treat Botkeep as a
   collector that uploads, not the store of record.
2. **24-hour uptime.** Start with `--no-ticks`, leave it 24 h, then read
   `collector_state.json` / `logs/collector.log` for `uptime_s`, `reconnects`,
   `ticks_duplicate`, `rss_kb`, `storage_bytes`. Success = stays connected,
   `reconnects` low, **0 duplicates**, RSS well under 512 MB, storage well under
   1 GB. This cannot be faked locally.

Both require your go-ahead to deploy. I have not started anything on Botkeep.
