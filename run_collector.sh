#!/usr/bin/env bash
# Supervised launcher for the continuous live collector (Phase 2.5).
#
# Runs live_collector.py under a restart loop so a crash never ends collection:
# the loop relaunches it (with a short backoff) until you stop it. A pidfile
# prevents two collectors writing the same DB, and stdout/stderr go to a
# timestamped log. `setsid` puts the supervisor + child in one process group so
# --stop tears down both.
#
# Nothing here touches the dashboard or the WebSocket client; it only starts the
# collector, which appends to data/candles.db.
#
# Usage
#   ./run_collector.sh                       # start (background) — all pairs
#   ./run_collector.sh --foreground          # run in the foreground
#   ./run_collector.sh --pairs EURUSD_otc,BTCUSD_otc
#   ./run_collector.sh --stop                # stop a running collector
#   ./run_collector.sh --status              # print the heartbeat
#   ./run_collector.sh --report              # print the monitoring report
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
DB="${CANDLE_DB:-$ROOT/data/candles.db}"
PIDFILE="$ROOT/data/collector.pid"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/collector.log"

FOREGROUND=0
PASSTHRU=()
while [ $# -gt 0 ]; do
  case "$1" in
    --foreground) FOREGROUND=1; shift ;;
    --stop)
      if [ -f "$PIDFILE" ]; then
        pid=$(cat "$PIDFILE")
        # negative pid = whole process group (supervisor + collector child)
        kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
        echo "stopped collector group $pid"
        rm -f "$PIDFILE"
      else
        echo "no pidfile at $PIDFILE"
      fi
      exit 0 ;;
    --status) exec "$PY" "$ROOT/live_collector.py" --db "$DB" --status ;;
    --report) exec "$PY" "$ROOT/collector_report.py" --db "$DB" ;;
    *) PASSTHRU+=("$1"); shift ;;
  esac
done

# one collector per DB: refuse to start a second
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "collector already running (pid $(cat "$PIDFILE")); use --stop first"
  exit 1
fi

run_loop() {
  local backoff=5
  while true; do
    echo "[$(date -u +%FT%TZ)] starting collector (db=$DB) $*" >> "$LOGFILE"
    "$PY" "$ROOT/live_collector.py" --db "$DB" "$@" >> "$LOGFILE" 2>&1
    rc=$?
    # rc=0 means a clean stop (SIGTERM / --minutes reached) -> do not restart.
    if [ "$rc" -eq 0 ]; then
      echo "[$(date -u +%FT%TZ)] collector exited cleanly; supervisor done" >> "$LOGFILE"
      break
    fi
    echo "[$(date -u +%FT%TZ)] collector crashed rc=$rc; restarting in ${backoff}s" >> "$LOGFILE"
    sleep "$backoff"
    backoff=$(( backoff < 60 ? backoff * 2 : 60 ))
  done
}

if [ "$FOREGROUND" = "1" ]; then
  echo $$ > "$PIDFILE"
  trap 'rm -f "$PIDFILE"' EXIT
  run_loop "${PASSTHRU[@]}"
else
  export ROOT PY DB LOGFILE
  setsid bash -c "$(declare -f run_loop); run_loop ${PASSTHRU[*]@Q}" \
    >> "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  echo "collector supervisor started (pid $(cat "$PIDFILE")); log: $LOGFILE"
  echo "stop with: $0 --stop"
fi
