#!/usr/bin/env bash
# Start (or restart) the prototype bot in the background: 127.0.0.1:${CALL_PORT:-9150}, log in bot.log.
cd "$(dirname "$0")" || exit 1
[ -f bot.pid ] && kill "$(cat bot.pid)" 2>/dev/null && sleep 1
setsid nohup .venv/bin/python bot.py > bot.log 2>&1 &
echo $! > bot.pid
for _ in $(seq 1 30); do curl -s -o /dev/null "http://127.0.0.1:${CALL_PORT:-9150}/" && { echo "bot up, pid $(cat bot.pid)"; exit 0; }; sleep 1; done
echo "bot did not come up; see bot.log"; exit 1
