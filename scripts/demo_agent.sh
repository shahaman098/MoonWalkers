#!/usr/bin/env bash
# =============================================================================
# SwarmOS Agent Demo — visual showcase of what an agent looks like working.
#
# Opens a browser, writes code in a terminal, runs commands — all visible
# on the virtual desktop via noVNC.
#
# Usage: Called via the /exec endpoint with DISPLAY already set.
#   e.g. {"command": "bash /opt/swarm-os/scripts/demo_agent.sh &"}
# =============================================================================

DISPLAY="${DISPLAY:-:10}"
export DISPLAY

type_text() {
    # Simulate typing with realistic delays
    local text="$1"
    local delay="${2:-0.04}"
    for ((i=0; i<${#text}; i++)); do
        char="${text:$i:1}"
        if [ "$char" = " " ]; then
            xdotool key space
        elif [ "$char" = "/" ]; then
            xdotool key slash
        elif [ "$char" = "." ]; then
            xdotool key period
        elif [ "$char" = ":" ]; then
            xdotool key colon
        elif [ "$char" = "-" ]; then
            xdotool key minus
        elif [ "$char" = "=" ]; then
            xdotool key equal
        elif [ "$char" = "'" ]; then
            xdotool key apostrophe
        elif [ "$char" = '"' ]; then
            xdotool key shift+apostrophe
        elif [ "$char" = "(" ]; then
            xdotool key shift+9
        elif [ "$char" = ")" ]; then
            xdotool key shift+0
        elif [ "$char" = "{" ]; then
            xdotool key shift+bracketleft
        elif [ "$char" = "}" ]; then
            xdotool key shift+bracketright
        elif [ "$char" = "[" ]; then
            xdotool key bracketleft
        elif [ "$char" = "]" ]; then
            xdotool key bracketright
        elif [ "$char" = "_" ]; then
            xdotool key shift+minus
        elif [ "$char" = ">" ]; then
            xdotool key shift+period
        elif [ "$char" = "<" ]; then
            xdotool key shift+comma
        elif [ "$char" = "!" ]; then
            xdotool key shift+1
        elif [ "$char" = "@" ]; then
            xdotool key shift+2
        elif [ "$char" = "#" ]; then
            xdotool key shift+3
        elif [ "$char" = "$" ]; then
            xdotool key shift+4
        elif [ "$char" = "," ]; then
            xdotool key comma
        elif [ "$char" = ";" ]; then
            xdotool key semicolon
        elif [ "$char" = "*" ]; then
            xdotool key shift+8
        elif [ "$char" = "+" ]; then
            xdotool key shift+equal
        elif [ "$char" = "\\" ]; then
            xdotool key backslash
        elif [ "$char" = "|" ]; then
            xdotool key shift+backslash
        else
            xdotool type --delay 0 "$char"
        fi
        sleep "$delay"
    done
}

press_enter() {
    xdotool key Return
    sleep "${1:-0.3}"
}

echo "[demo] Starting SwarmOS Agent Demo on $DISPLAY"

# ---------------------------------------------------------------------------
# Phase 1: Open terminal and show agent "thinking"
# ---------------------------------------------------------------------------
echo "[demo] Phase 1: Agent terminal"

xterm -geometry 90x28+20+30 -title "SwarmOS Agent" \
      -bg "#0d1117" -fg "#c9d1d9" -fa "Monospace" -fs 11 &
XTERM_PID=$!
sleep 1.5

# Focus the terminal
xdotool search --name "SwarmOS Agent" windowactivate --sync 2>/dev/null
sleep 0.5

type_text "echo '[ SwarmOS Agent v0.1 - Autonomous Task Execution ]'" 0.02
press_enter 0.5
type_text "echo 'Task: Build a REST API health dashboard'" 0.03
press_enter 1.0

type_text "echo '> Analyzing requirements...'" 0.03
press_enter 0.8
type_text "echo '> Planning implementation: FastAPI + htmx + Chart.js'" 0.02
press_enter 1.0

# ---------------------------------------------------------------------------
# Phase 2: Agent "writes code"
# ---------------------------------------------------------------------------
echo "[demo] Phase 2: Writing code"

type_text "cat > dashboard.py << 'PYEOF'" 0.02
press_enter 0.3

type_text 'from fastapi import FastAPI' 0.03
press_enter 0.2
type_text 'from fastapi.responses import HTMLResponse' 0.03
press_enter 0.2
type_text 'import psutil, time' 0.03
press_enter 0.4
type_text '' 0.03
press_enter 0.2
type_text 'app = FastAPI(title="Health Dashboard")' 0.03
press_enter 0.4
type_text '' 0.03
press_enter 0.2
type_text '@app.get("/health")' 0.03
press_enter 0.2
type_text 'def health():' 0.04
press_enter 0.2
type_text '    return {' 0.03
press_enter 0.2
type_text '        "cpu": psutil.cpu_percent(),' 0.03
press_enter 0.2
type_text '        "memory": psutil.virtual_memory().percent,' 0.03
press_enter 0.2
type_text '        "uptime": time.time(),' 0.03
press_enter 0.2
type_text '        "status": "healthy"' 0.03
press_enter 0.2
type_text '    }' 0.03
press_enter 0.3

type_text 'PYEOF' 0.03
press_enter 0.8

type_text "echo '> Code written: dashboard.py'" 0.02
press_enter 0.5
type_text "wc -l dashboard.py" 0.03
press_enter 0.8

# ---------------------------------------------------------------------------
# Phase 3: Agent installs deps and tests
# ---------------------------------------------------------------------------
echo "[demo] Phase 3: Testing"

type_text "echo '> Installing dependencies...'" 0.02
press_enter 0.3
type_text "pip install fastapi psutil uvicorn --quiet 2>&1 | tail -1" 0.02
press_enter 3.0

type_text "echo '> Running syntax check...'" 0.02
press_enter 0.3
type_text "python3 -c 'import ast; ast.parse(open(\"dashboard.py\").read()); print(\"Syntax OK\")'" 0.02
press_enter 1.0

type_text "echo '> Starting server for smoke test...'" 0.02
press_enter 0.5
type_text "timeout 3 uvicorn dashboard:app --port 9090 &" 0.02
press_enter 2.0
type_text "curl -s http://localhost:9090/health | python3 -m json.tool" 0.02
press_enter 2.0

# ---------------------------------------------------------------------------
# Phase 4: Open browser to show the result
# ---------------------------------------------------------------------------
echo "[demo] Phase 4: Browser"

type_text "echo '> Opening browser to verify...'" 0.02
press_enter 0.5

chromium --no-sandbox --disable-gpu --disable-software-rasterizer \
    --window-size=800,500 --window-position=200,100 \
    "http://localhost:9090/docs" 2>/dev/null &
CHROME_PID=$!
sleep 4

# ---------------------------------------------------------------------------
# Phase 5: Agent summary
# ---------------------------------------------------------------------------
echo "[demo] Phase 5: Summary"

xdotool search --name "SwarmOS Agent" windowactivate --sync 2>/dev/null
sleep 0.5

type_text "" 0.02
press_enter 0.3
type_text "echo ''" 0.02
press_enter 0.2
type_text "echo '========================================'" 0.02
press_enter 0.2
type_text "echo ' Task Complete'" 0.02
press_enter 0.2
type_text "echo '========================================'" 0.02
press_enter 0.2
type_text "echo ' Created: dashboard.py (15 lines)'" 0.02
press_enter 0.2
type_text "echo ' Tested:  syntax OK, endpoint OK'" 0.02
press_enter 0.2
type_text "echo ' Browser: API docs verified'" 0.02
press_enter 0.2
type_text "echo '========================================'" 0.02
press_enter 1.0

echo "[demo] Demo complete — display stays live"

# Keep everything alive so the viewer can see it
wait
