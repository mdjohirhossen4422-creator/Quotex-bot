# Botkeep.cloud deployment — AI Signal Pro collector + AI system

Deploy the **collector** to Botkeep (24/7, free plan) so real + OTC data keeps
accumulating, and run the AI Signal System (training, backtest, paper signals)
wherever you develop. Nothing here trades, and nothing here fabricates data.

See `../../BOTKEEP_AUDIT.md` for the full compatibility audit and measurements.

## Free-plan limits (verified 2026-11-15)

1 slot · 512 MB RAM · 0.5 vCore · **1 GB storage** · 24/7, no renewal timer.

## Start command (Botkeep → Python profile)

```
python live_collector.py --no-ticks
```

or use the bundled supervisor (crash-restart loop + the env below):

```
bash deploy/botkeep/start.sh
```

`--no-ticks` is required on the free plan: raw ticks grow ~1.3 GB/day and would
fill a 1 GB disk in under a day. Candle-only mode grows ~63 MB/day (~16 days) and
**still preserves order-flow** as per-candle tick summaries (`tick_summary.py`),
so the AI features survive.

## Environment variables (Botkeep → Environment)

| var | value | why |
| --- | --- | --- |
| `CANDLE_DB` | `/data/candles.db` | the one durable asset; point at the persistent path |
| `COLLECTOR_STATE` | `/data/collector_state.json` | heartbeat survives restart |
| `COLLECTOR_STORE_TICKS` | `0` | same as `--no-ticks` (belt and braces) |
| `COLLECTOR_LOG_EVERY` | `60` | stdout log cadence |
| `COLLECTOR_PAIRS` | *(unset = all)* | optional: `EURUSD_otc,BTCUSD_otc` to shrink scope |

No secret is needed to collect. If you later run the Telegram watcher on the same
box, add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as Environment secrets —
never in the repo.

## Files to include (ZIP or GitHub)

Collector bundle (minimal):
`live_collector.py`, `tick_summary.py`, `market_qx_candles.py`, `provenance.py`,
`candle_store.py`, `requirements-collector.txt`, `botkeep_preflight.py`,
`storage_monitor.py`.

Make the workload's `requirements.txt` contain exactly the line from
`requirements-collector.txt` (only `websocket-client`). The ML stack
(numpy/scikit-learn/pyarrow) is **not** needed on the collector box.

## Before starting — preflight

```
python botkeep_preflight.py --db "$CANDLE_DB" --storage-mb 1024 --ram-mb 512
```

## Keep storage safe (priority 11)

Run the storage guard on a schedule (or from the panel's task runner):

```
python storage_monitor.py --db "$CANDLE_DB" --budget-mb 1024     # WARN 80%, ALERT 90%
python storage_monitor.py --db "$CANDLE_DB" --budget-mb 1024 --watch --interval 300 \
    --auto-backup --auto-prune-days 7 --yes                      # warn before it fills
python storage_monitor.py --backup                               # compressed candle archive
python storage_monitor.py --prune-ticks 7 --dry-run              # preview
python storage_monitor.py --prune-ticks 7 --yes                  # reclaim (only if ticks were stored)
```

Exit codes: `0` OK, `10` WARN (≥80%), `20` ALERT (≥90%), so a monitor can react.
`--watch` loops, backs up candles on WARN/ALERT, and (with `--yes
--auto-prune-days N`) prunes raw ticks older than N days on ALERT. Candles are
never pruned automatically.

## Verify persistence locally before you deploy (requirement 2)

```
python verify_restart.py --minutes 1.3 --pairs EURUSD_otc
```

Runs the real collector twice against an isolated temp DB and asserts run-1
candles survive the restart, no duplicates appear, and the file persists. Needs
>60 s per run (a 1 m candle must close).

## Two checks that must be done ON the instance (not provable locally)

1. **Filesystem persistence across redeploy** — start, note the DB size, force a
   restart, confirm `$CANDLE_DB` is unchanged and the heartbeat resumes.
2. **24 h uptime** — leave it running, then read `collector_state.json` for
   `uptime_s`, `reconnects`, `ticks_duplicate`, `rss_kb`, `storage_bytes`.
   Success = connected, `reconnects` low, **0 duplicates**, RSS ≪ 512 MB,
   storage ≪ 1 GB.

Both require your go-ahead to deploy; nothing is deployed by the repo itself.
