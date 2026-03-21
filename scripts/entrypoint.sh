#!/usr/bin/env bash
# =============================================================================
# SwarmOS Agent Runtime Entrypoint
#
# Starts the virtual desktop stack and the API server.
# =============================================================================
set -euo pipefail

echo "[entrypoint] Starting SwarmOS agent runtime..."

# ---------------------------------------------------------------------------
# 1. Virtual framebuffer (headless X server)
# ---------------------------------------------------------------------------
echo "[entrypoint] Starting Xvfb on display :1..."
Xvfb :1 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &
XVFB_PID=$!
sleep 1

export DISPLAY=:1

# ---------------------------------------------------------------------------
# 2. Window manager
# ---------------------------------------------------------------------------
echo "[entrypoint] Starting fluxbox..."
fluxbox &
FLUXBOX_PID=$!
sleep 0.5

# ---------------------------------------------------------------------------
# 3. VNC server (captures display :1)
# ---------------------------------------------------------------------------
echo "[entrypoint] Starting x11vnc on port 5900..."
x11vnc -display :1 -forever -nopw -shared -rfbport 5900 -quiet &
X11VNC_PID=$!

# ---------------------------------------------------------------------------
# 4. WebSocket proxy (noVNC - VNC over WebSocket on port 6080)
# ---------------------------------------------------------------------------
echo "[entrypoint] Starting websockify on port 6080 -> VNC 5900..."
websockify --web /usr/share/novnc 6080 localhost:5900 &
WEBSOCKIFY_PID=$!

# ---------------------------------------------------------------------------
# 5. Auto-kill safety timer
# ---------------------------------------------------------------------------
AUTO_KILL_TIMEOUT=${AUTO_KILL_TIMEOUT:-1800}
echo "[entrypoint] Starting auto-kill timer (${AUTO_KILL_TIMEOUT}s)..."
/opt/swarm-os/scripts/auto_kill.sh "$AUTO_KILL_TIMEOUT" &
AUTOKILL_PID=$!

# ---------------------------------------------------------------------------
# 6. API server (uvicorn)
# ---------------------------------------------------------------------------
echo "[entrypoint] Starting uvicorn API on port 8080..."
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8080 \
    --workers 1 \
    --log-level info \
    --app-dir /opt/swarm-os
