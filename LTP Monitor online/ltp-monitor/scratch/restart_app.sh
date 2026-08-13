#!/bin/bash
# Verified restart of the LTP Monitor app.
#
# Four lessons are baked in here, all learned the hard way:
#
# 1. VERIFY THE PROCESS, NEVER A PROXY. On 2026-08-06 /api/version was
#    checked after a restart and answered CORRECTLY — from the OLD
#    process, which was still alive. Two orchestrators ran for three
#    minutes, each exiting the same positions, and produced 2 phantom
#    journal records. Hence: count processes, and compare the served
#    version against the VERSION file rather than merely printing it.
#
# 2. DO NOT MATCH YOUR OWN COMMAND LINE. On 2026-08-08 this script used
#    `ps | grep -E '[Pp]ython app\.py|caffeinate .*app\.py'`, which
#    matched the zsh wrapper RUNNING THE SCRIPT — the wrapper's command
#    line embeds the pattern text. The script killed its own parent and
#    exited 144 mid-way. The match below is anchored: the command must
#    END with app.py and contain python, which a shell wrapper or a
#    grep never does.
#
# 3. RELAUNCH UNDER caffeinate, AND PROVE THE ASSERTION IS HELD. On
#    2026-08-13 macOS suspended the host from 11:52 to 18:25 — 3.6
#    hours of live market. The machine never rebooted and the process
#    never died; every timer simply froze, and on wake it ran the
#    missed 15:45 backtest and EOD square-off at 18:26. The app does
#    NOT self-spawn caffeinate, so a plain `nohup python app.py`
#    relaunch silently leaves the host free to sleep again. Starting
#    caffeinate is not enough either — check pmset, because a wrapper
#    that died on exec looks identical to one that is working.
#
# 4. USE THE VENV INTERPRETER, AND TEST IT BEFORE KILLING ANYTHING.
#    On 2026-08-13 a restart picked /opt/homebrew/bin/python3 because
#    that is the path `ps` displays for the running app — but that is
#    the venv's RESOLVED BASE interpreter, not the venv itself, and
#    homebrew's python has no fastapi. The old process was already
#    dead when the new one failed to import, so the app stayed down.
#    The preflight below imports fastapi with $PY *first*; a restart
#    must never begin by destroying the only working process.
set -u
cd "/Users/user/Documents/Stock Tools/LTP Monitor online/ltp-monitor"
PY=/Users/user/venv/bin/python

# --- preflight: lesson 4. Nothing is killed until this passes. -------
"$PY" -c 'import fastapi, uvicorn' 2>/dev/null || {
  echo "  ABORT: $PY cannot import fastapi/uvicorn — refusing to stop"
  echo "         the running app for an interpreter that cannot serve."
  exit 1
}
command -v caffeinate >/dev/null || { echo "  ABORT: caffeinate not found"; exit 1; }
WANT=$(cat VERSION 2>/dev/null | tr -d ' \n')
echo "  preflight OK — $PY can serve; target version $WANT"

pids() {
  ps -eo pid,command | awk 'NR>1 {
    cmd = $0; sub(/^[ ]*[0-9]+[ ]+/, "", cmd)
    if (cmd ~ /app\.py$/ && tolower(cmd) ~ /python/) print $1
  }'
}

echo "  before: $(pids | tr '\n' ' ')"
for p in $(pids); do kill -TERM "$p" 2>/dev/null; done
# 20s: must exceed app.py's timeout_graceful_shutdown, which
# test_graceful_shutdown.py pins BELOW this window so the clean path
# always wins the race against the SIGKILL below.
for _ in $(seq 1 20); do [ -z "$(pids)" ] && break; sleep 1; done
if [ -n "$(pids)" ]; then
  echo "  SIGTERM insufficient, escalating: $(pids | tr '\n' ' ')"
  for p in $(pids); do kill -9 "$p" 2>/dev/null; done
  sleep 2
fi
[ -n "$(pids)" ] && { echo "  ABORT: old process survived: $(pids | tr '\n' ' ')"; exit 1; }
echo "  all old processes gone"

nohup caffeinate -is "$PY" app.py > /tmp/ltp-app.out 2>&1 &
for _ in $(seq 1 40); do
  curl -sf http://127.0.0.1:8000/api/version >/dev/null 2>&1 && break
  sleep 1
done

# --- verification: lesson 1, applied to every claim ------------------
npy() { ps -eo pid,command | awk 'NR>1 { cmd=$0; sub(/^[ ]*[0-9]+[ ]+/,"",cmd)
        if (cmd ~ /app\.py$/ && tolower(cmd) ~ /python/ && cmd !~ /caffeinate/) print $1 }'; }
ncaf() { ps -eo pid,command | awk 'NR>1 { cmd=$0; sub(/^[ ]*[0-9]+[ ]+/,"",cmd)
        if (cmd ~ /app\.py$/ && cmd ~ /caffeinate/) print $1 }'; }

NPY=$(npy | wc -l | tr -d ' ')
NCAF=$(ncaf | wc -l | tr -d ' ')
echo "  python app.py: $(npy | tr '\n' ' ') ($NPY)   caffeinate: $(ncaf | tr '\n' ' ') ($NCAF)"
[ "$NPY" -eq 1 ] || { echo "  ABORT: expected exactly 1 python app.py, got $NPY"; exit 1; }
[ "$NCAF" -eq 1 ] || { echo "  ABORT: expected exactly 1 caffeinate wrapper, got $NCAF"; exit 1; }

# Lesson 3: a live caffeinate pid proves nothing. Ask the power manager.
CAFPID=$(ncaf)
if pmset -g assertions 2>/dev/null | grep -q "pid ${CAFPID}(caffeinate)"; then
  echo "  sleep assertion held by pid $CAFPID — host will not idle-sleep"
else
  echo "  ABORT: caffeinate $CAFPID holds NO pmset assertion — the host"
  echo "         can still sleep mid-session (see 2026-08-13)."
  exit 1
fi

GOT=$(curl -s http://127.0.0.1:8000/api/version | sed 's/.*"version":"\([^"]*\)".*/\1/')
echo "  version endpoint: $GOT (VERSION file: $WANT)"
[ -n "$WANT" ] && [ "$GOT" != "$WANT" ] && {
  echo "  ABORT: served version does not match the VERSION file — a stale"
  echo "         process may still be answering (see 2026-08-06)."; exit 1; }
echo "  OK -- one process, under a verified caffeinate, serving $GOT."
