#!/usr/bin/env bash
# Start the on-demand Telegram signal bot (BUY / SELL / WAIT on request).
#
# One-time setup:
#   1. @BotFather -> /newbot -> copy the token
#   2. message your bot once, then:  ./run_alerts.sh --whoami --token <TOKEN>
#   3. put the token + chat id in telegram.json:
#        {"bot_token": "123456:ABC...", "chat_id": "987654321"}
#      (or export TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)
#
# The collector must be running so candles exist for the pair you ask about.
#
# Usage:
#   ./run_ask.sh                              # rule engine, default tf 1m
#   ./run_ask.sh --engine smart               # regime-aware engine
#   ./run_ask.sh --period 60                  # default timeframe (seconds)
#   ./run_ask.sh --dry-run                    # print replies, send nothing
#
# Then in Telegram:  /signal EURUSD_otc 60   ->  BUY / SELL / WAIT
set -u
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "creating venv..."
  python -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

pkill -f "signal_ask.py" 2>/dev/null
sleep 1
mkdir -p out data

nohup .venv/bin/python -u signal_ask.py "$@" > /tmp/ask.log 2>&1 &
echo "on-demand bot started (pid $!) — log: /tmp/ask.log"
echo "tail it with:  tail -f /tmp/ask.log"
