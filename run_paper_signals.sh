#!/usr/bin/env bash
# Paper signal watcher (Phase 2.7). Runs the AI signal engine in PAPER mode:
# it computes signals, journals them and prints them — it never sends a Telegram
# message and never places a trade. Live sending is impossible unless the
# go-live gate is LIVE-READY (see golive.py).
#
# Usage
#   ./run_paper_signals.sh                      # paper, 60s + 300s, every 60s
#   ./run_paper_signals.sh --periods 60         # one timeframe
#   ./run_paper_signals.sh --source real        # real-market pairs
#   ./run_paper_signals.sh --stop
#   ./run_paper_signals.sh --status             # tail the log
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
DB="${CANDLE_DB:-$ROOT/data/candles.db}"
JOURNAL="${JOURNAL_DB:-$ROOT/data/journal.db}"
LOGDIR="$ROOT/logs"; mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/paper_signals.log"
PIDFILE="$ROOT/data/paper_signals.pid"

if [ "${1:-}" = "--stop" ]; then
  if [ -f "$PIDFILE" ]; then kill -TERM "$(cat "$PIDFILE")" 2>/dev/null; rm -f "$PIDFILE"; echo "stopped paper watcher"; else echo "not running"; fi
  exit 0
fi
if [ "${1:-}" = "--status" ]; then
  [ -f "$PIDFILE" ] && echo "pid $(cat "$PIDFILE")" || echo "not running"
  tail -n 15 "$LOGFILE" 2>/dev/null
  exit 0
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "already running (pid $(cat "$PIDFILE")); use --stop first"; exit 1
fi

nohup "$PY" "$ROOT/signal_telegram.py" --db "$DB" --journal "$JOURNAL" \
  --mode paper --watch "$@" >> "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
echo "paper signal watcher started (pid $(cat "$PIDFILE")); log: $LOGFILE"
echo "it journals + prints only — no sends, no trades."
