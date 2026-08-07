#!/bin/bash
# Verified restart of the LTP Monitor app.
#
# Two lessons are baked in here, both learned the hard way:
#
# 1. VERIFY THE PROCESS, NEVER A PROXY. On 2026-08-06 /api/version was
#    checked after a restart and answered CORRECTLY — from the OLD
#    process, which was still alive. Two orchestrators ran for three
#    minutes, each exiting the same positions, and produced 2 phantom
#    journal records.
#
# 2. DO NOT MATCH YOUR OWN COMMAND LINE. On 2026-08-08 this script used
#    `ps | grep -E '[Pp]ython app\.py|caffeinate .*app\.py'`, which
#    matched the zsh wrapper RUNNING THE SCRIPT — the wrapper's command
#    line embeds the pattern text. The script killed its own parent and
#    exited 144 mid-way. The match below is anchored: the command must
#    END with app.py and contain python, which a shell wrapper or a
#    grep never does.
set -u
cd "/Users/user/Documents/Stock Tools/LTP Monitor online/ltp-monitor"
PY=/Users/user/venv/bin/python

pids() {
  ps -eo pid,command | awk 'NR>1 {
    cmd = $0; sub(/^[ ]*[0-9]+[ ]+/, "", cmd)
    if (cmd ~ /app\.py$/ && tolower(cmd) ~ /python/) print $1
  }'
}

echo "  before: $(pids | tr '\n' ' ')"
for p in $(pids); do kill -TERM "$p" 2>/dev/null; done
for _ in $(seq 1 20); do [ -z "$(pids)" ] && break; sleep 1; done
if [ -n "$(pids)" ]; then
  echo "  SIGTERM insufficient, escalating: $(pids | tr '\n' ' ')"
  for p in $(pids); do kill -9 "$p" 2>/dev/null; done
  sleep 2
fi
[ -n "$(pids)" ] && { echo "  ABORT: old process survived: $(pids | tr '\n' ' ')"; exit 1; }
echo "  all old processes gone"

nohup "$PY" app.py > /tmp/ltp-app.out 2>&1 &
for _ in $(seq 1 30); do
  curl -sf http://127.0.0.1:8000/api/version >/dev/null 2>&1 && break
  sleep 1
done

n=$(pids | wc -l | tr -d ' ')
echo "  app.py processes now: $n  ($(pids | tr '\n' ' '))"
[ "$n" -eq 1 ] || { echo "  ABORT: expected exactly 1, got $n"; exit 1; }
echo "  version endpoint: $(curl -s http://127.0.0.1:8000/api/version)"
echo "  OK — one process, serving."
