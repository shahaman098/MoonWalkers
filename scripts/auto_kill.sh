#!/usr/bin/env bash
# =============================================================================
# SwarmOS Auto-Kill Safety Timer
#
# THE most critical safety feature.  Ensures no workspace runs forever and
# racks up charges.  Even if the API crashes, this script will still fire.
#
# Usage: auto_kill.sh [timeout_seconds]
# Default: 1800 (30 minutes)
# =============================================================================
set -euo pipefail

TIMEOUT=${1:-1800}

if [ "$TIMEOUT" -lt 60 ]; then
    echo "[auto-kill] WARNING: timeout ${TIMEOUT}s is very short, minimum is 60s"
    TIMEOUT=60
fi

WARN_5=$(( TIMEOUT - 300 ))
WARN_1=$(( TIMEOUT - 60 ))

echo "[auto-kill] Armed. Container will be force-killed in ${TIMEOUT}s ($(( TIMEOUT / 60 ))m)."

# Wait until 5 minutes before kill
if [ "$WARN_5" -gt 0 ]; then
    sleep "$WARN_5"
    echo "[auto-kill] WARNING: 5 minutes remaining before forced shutdown."
fi

# Wait until 1 minute before kill
SLEEP_TO_1=$(( WARN_1 - (WARN_5 > 0 ? WARN_5 : 0) ))
if [ "$SLEEP_TO_1" -gt 0 ]; then
    sleep "$SLEEP_TO_1"
fi
echo "[auto-kill] WARNING: 1 minute remaining before forced shutdown."

# Final wait
REMAINING=$(( TIMEOUT - (WARN_1 > 0 ? WARN_1 : 0) ))
if [ "$REMAINING" -gt 0 ]; then
    sleep "$REMAINING"
fi

echo "[auto-kill] CRITICAL: Time limit reached (${TIMEOUT}s). Force-killing container NOW."

# Kill all processes in the container
kill -TERM 1 2>/dev/null || true
sleep 5
kill -KILL 1 2>/dev/null || true

# If we're still alive (shouldn't be), exit hard
exit 137
