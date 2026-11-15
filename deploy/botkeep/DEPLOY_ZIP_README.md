# Quotex AI Signal Pro — Botkeep collector (deployment package)

This archive deploys the **data collector only** to Botkeep.cloud. It records
live candles (and per-candle order-flow summaries) 24/7. It does **not** trade,
does **not** send Telegram, and contains **no secrets and no database**.

> Automated deployment is not available: Botkeep exposes no public deploy API —
> deployment is done from the authenticated panel (ZIP wizard or GitHub App).
> See "Is there an API?" at the bottom. **Nothing is uploaded for you.**

## What is in this ZIP

Everything sits at the archive root so Botkeep finds `requirements.txt` directly.

| file | purpose |
| --- | --- |
| `live_collector.py` | the collector (start command target) |
| `market_qx_candles.py` | WebSocket client + `CandleBuilder` |
| `candle_store.py` | SQLite candle/state store |
| `provenance.py` | otc / real / synthetic classification |
| `tick_summary.py` | per-candle order-flow features (kept even with `--no-ticks`) |
| `botkeep_preflight.py` | on-instance preflight check |
| `storage_monitor.py` | disk guard (WARN 80% / ALERT 90%) |
| `requirements.txt` | `websocket-client>=1.6,<2.0` (the only dependency) |
| `requirements-collector.txt` | same pin, kept for reference |
| `start.sh` | optional crash-restart supervisor |
| `BOTKEEP_README.md` | fuller deploy notes |

Deliberately **excluded**: `.env`, `telegram.json`, `payout.json`, `.git`,
`__pycache__`, `.venv`, and every `*.db` (including `data/candles.db`).

## Step 1 — create the workload

1. Botkeep panel → **Create** → choose the **Python** profile.
2. Source: **ZIP** → upload this archive.
3. Runtime: pick the offered Python 3.11+ version.
4. Start command (exact):

   ```
   python live_collector.py --no-ticks
   ```

   or, to get a crash-restart loop, use the bundled supervisor:

   ```
   bash start.sh
   ```

   `--no-ticks` is required on the free plan: raw ticks grow ~1.3 GB/day and
   would fill the 1 GB disk in under a day. Candle-only mode grows ~63 MB/day
   (~16 days) and still keeps order-flow as per-candle summaries.

## Step 2 — environment variables (panel → Environment)

| var | value | why |
| --- | --- | --- |
| `CANDLE_DB` | `/data/candles.db` | the one durable asset; put it on persistent storage |
| `COLLECTOR_STATE` | `/data/collector_state.json` | heartbeat survives restart |
| `COLLECTOR_STORE_TICKS` | `0` | same as `--no-ticks` (belt and braces) |
| `COLLECTOR_LOG_EVERY` | `60` | stdout log cadence |
| `COLLECTOR_PAIRS` | *(unset = all)* | optional: `EURUSD_otc,BTCUSD_otc` to shrink scope |

No secret is needed to collect. **Do not** add a Telegram token here — this
package is collector-only, and the AI Telegram path stays in paper mode.

## Step 3 — preflight on the instance

Open the Console and run:

```
python botkeep_preflight.py --db "$CANDLE_DB" --storage-mb 1024 --ram-mb 512
```

Expect all checks PASS (python version, `websocket-client`, feed reachable,
storage writable, RAM budget). Fix anything that fails before starting.

## Step 4 — start and confirm

1. Press **Start**. Watch the Console for the first heartbeat line:

   ```
   [..] conn=True pairs=91 ticks_new=.. candles_new=.. dup=0 reconnects=0 rss=..MB
   ```

   `conn=True` may take ~90 s (the feed handshake is slow). `dup=0` must hold.
2. Keep **one** collector running per host. market-qx serves **one live session
   per source IP**; a second collector on the same IP silently gets `assets=0`.

## Step 5 — keep storage safe

Run the guard periodically (panel task runner or by hand):

```
python storage_monitor.py --db "$CANDLE_DB" --budget-mb 1024     # WARN 80%, ALERT 90%
python storage_monitor.py --db "$CANDLE_DB" --budget-mb 1024 --watch --interval 300 \
    --auto-backup --auto-prune-days 7 --yes
```

Exit codes: `0` OK, `10` WARN (≥80%), `20` ALERT (≥90%). Candles are never
pruned automatically; only raw ticks (if you ever store them) are.

## Step 6 — verify persistence across a restart

Botkeep redeploy restarts the server. To confirm `$CANDLE_DB` survives:

1. Note the DB size (`ls -l "$CANDLE_DB"`).
2. Force a restart from the panel.
3. Confirm the file is unchanged and the heartbeat in `$COLLECTOR_STATE` resumes.

Locally, `verify_restart.py` proves the same property (run it with the production
collector stopped, since the feed is single-session per IP):

```
python verify_restart.py --minutes 2.5 --pairs EURUSD_otc
```

## Honest scope

This package collects data. It makes no accuracy claim. Prior honest tests on
this feed (backtests, walk-forward, the paper journal) all sit at ~50% — a coin
flip — so the collector's job is to accumulate enough clean history for a future
test to be believable, nothing more.

## Is there an API for automated deployment?

**No public deploy API exists.** Findings (checked 2026-11-08):

- Botkeep's own docs describe deployment only through the authenticated panel —
  the **ZIP wizard** and the **GitHub App** (`botkeep.cloud/docs/zip`,
  `/docs/github`). Both are interactive.
- The panel talks to a private `/api/v1/...` (auth, panel, staff routes). It is
  session/CSRF-guarded and is not a documented, stable integration surface; it
  has no public app/deploy endpoint and no API tokens are issued.
- `api.botkeep.cloud` returns `403` for anonymous requests; there is no
  `/api/docs`, `/openapi.json`, or `/developers` page.

So deployment stays **manual**. This package is built to make that manual step a
single upload + one start command.
