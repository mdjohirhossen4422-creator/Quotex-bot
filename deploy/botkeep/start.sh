#!/usr/bin/env bash
# Botkeep start script — candle-only collector with a crash-restart loop.
#
# Deploy this as the workload's start command (or `bash deploy/botkeep/start.sh`).
# It writes ONLY candles (no raw ticks) so a 1 GB free-plan disk lasts ~16 days,
# and it keeps order-flow as per-candle tick summaries. Nothing trades.
#
# Required env (set in the Botkeep panel):
#   CANDLE_DB=/data/candles.db          COLLECTOR_STATE=/data/collector_state.json
# Optional:
#   COLLECTOR_PAIRS=EURUSD_otc,BTCUSD_otc   COLLECTOR_LOG_EVERY=60
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

DB="${CANDLE_DB:-/data/candles.db}"
STATE="${COLLECTOR_STATE:-/data/collector_state.json}"
LOG_EVERY="${COLLECTOR_LOG_EVERY:-60}"

export CANDLE_DB="$DB" COLLECTOR_STATE="$STATE" COLLECTOR_STORE_TICKS=0

echo "[start] collector db=$DB state=$STATE no-ticks (order-flow kept as summaries)"

backoff=5
while true; do
  echo "[start] launching collector $(date -u +%FT%TZ)"
  "$PY" "$ROOT/live_collector.py" --db "$DB" --no-ticks --log-every "$LOG_EVERY"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "[start] collector exited cleanly; supervisor done"
    break
  fi
  echo "[start] collector crashed rc=$rc; restarting in ${backoff}s"
  sleep "$backoff"
  backoff=$(( backoff < 60 ? backoff * 2 : 60 ))
done
