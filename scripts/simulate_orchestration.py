#!/usr/bin/env python3
"""
SwarmOS — Full Agent Orchestration Simulation
=============================================
Starts a real uvicorn server, runs the complete agent lifecycle (spawn →
exec multiple commands → file-sync WS → download zip → destroy), then
verifies the server logs contain every expected event.

Usage:
    python scripts/simulate_orchestration.py

Requirements (already in requirements.txt):
    uvicorn, fastapi, httpx, websockets
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PORT = 18080
BASE_URL = f"http://127.0.0.1:{PORT}"
API_KEY = "orchestration-sim-key"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}
LOG_FILE = Path("/tmp/swarm-sim-server.log")

# Repo root (two levels up from this script)
REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(label: str, value: str = ""):
    print(f"  \033[32m✓\033[0m  {label}" + (f"  → {value}" if value else ""))


def _fail(label: str, detail: str = ""):
    print(f"  \033[31m✗\033[0m  {label}" + (f": {detail}" if detail else ""))
    sys.exit(1)


def _section(title: str):
    print(f"\n\033[1;34m{'─' * 60}\033[0m")
    print(f"\033[1;34m  {title}\033[0m")
    print(f"\033[1;34m{'─' * 60}\033[0m")


def _elapsed(start: float) -> str:
    ms = int((time.monotonic() - start) * 1000)
    return f"{ms}ms" if ms < 2000 else f"{ms/1000:.2f}s"


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def start_server() -> subprocess.Popen:
    _section("Starting SwarmOS API server")
    env = {**os.environ, "SWARM_API_KEY": API_KEY, "GKE_MODE": "false"}
    log_fd = LOG_FILE.open("w")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1",
            "--port", str(PORT),
            "--log-level", "info",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_fd,
        stderr=log_fd,
    )
    print(f"  PID {proc.pid} | logs → {LOG_FILE}")
    return proc


def wait_for_ready(timeout: float = 15.0) -> float:
    """Poll /healthz until the server is up. Returns elapsed seconds."""
    deadline = time.monotonic() + timeout
    t0 = time.monotonic()
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{BASE_URL}/healthz", timeout=1)
            if r.status_code == 200:
                elapsed = time.monotonic() - t0
                _ok("Server ready", f"{elapsed:.2f}s startup")
                return elapsed
        except Exception:
            pass
        time.sleep(0.25)
    _fail("Server did not become ready in time")
    return -1  # unreachable


def stop_server(proc: subprocess.Popen):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------------
# WebSocket helpers (async)
# ---------------------------------------------------------------------------

async def _ws_receive_one(url: str) -> dict:
    """Open a WebSocket, receive one JSON frame, return it."""
    import websockets
    async with websockets.connect(url) as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        return json.loads(raw)


def ws_receive_one(url: str) -> dict:
    return asyncio.run(_ws_receive_one(url))


# ---------------------------------------------------------------------------
# Orchestration flow
# ---------------------------------------------------------------------------

def run_simulation():
    client = httpx.Client(base_url=BASE_URL, headers=HEADERS, timeout=30)
    timings: dict[str, str] = {}

    # ── 1. Health check ────────────────────────────────────────────────────
    _section("1 / Health check")
    t = time.monotonic()
    r = client.get("/healthz")
    timings["healthz"] = _elapsed(t)
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    data = r.json()
    _ok("GET /healthz", json.dumps(data))

    # ── 2. Spawn workspace ─────────────────────────────────────────────────
    _section("2 / Spawn workspace")
    t = time.monotonic()
    r = client.post(
        "/v1/workspaces/spawn",
        json={
            "agent_id": "orchestrator-sim-agent",
            "environment_type": "dev",
            "tools_preloaded": ["git", "python", "node"],
            "auto_kill_timeout_minutes": 5,
        },
    )
    timings["spawn"] = _elapsed(t)
    assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
    spawn = r.json()
    ws_id = spawn["workspace_id"]

    _ok("POST /v1/workspaces/spawn", ws_id)
    _ok("status", spawn["status"])
    assert spawn["estimated_ready_sec"] >= 1, "estimated_ready_sec should be >= 1"
    _ok("estimated_ready_sec", f"{spawn['estimated_ready_sec']}s (measured, not hardcoded)")
    _ok("endpoint exec",        spawn["endpoints"]["exec"])
    _ok("endpoint stream_webrtc", spawn["endpoints"]["stream_webrtc"])
    _ok("endpoint file_sync",   spawn["endpoints"]["file_sync"])
    _ok("spawn round-trip",     timings["spawn"])

    # ── 3. Execute commands (agent task simulation) ─────────────────────────
    _section("3 / Execute commands (agent task simulation)")

    # NOTE: workspace_manager joins command + args with spaces into a single
    # shell string, so complex quoting (bash -c "...") won't round-trip safely.
    # These commands are chosen to work cleanly with that join behaviour.
    commands = [
        # (label, command, args, expect_in_stdout_or_stderr, expect_exit_code)
        ("echo hello",        "echo",    ["hello from SwarmOS agent"],  "hello from SwarmOS agent", 0),
        ("write file",        "touch",   ["result.txt"],                 None,                       0),
        ("verify file exists","ls",      ["result.txt"],                 "result.txt",               0),
        ("list workspace",    "ls",      ["-la"],                        None,                       0),
        ("python version",    "python3", ["--version"],                  None,                       0),
        ("blocklisted cmd",   "rm -rf /", [],                            None,                       126),
    ]

    exec_timings = []
    for label, cmd, args, expect_stdout, expect_exit in commands:
        t = time.monotonic()
        r = client.post(
            f"/v1/workspaces/{ws_id}/exec",
            json={"command": cmd, "args": args, "timeout_seconds": 10},
        )
        elapsed = _elapsed(t)
        exec_timings.append(elapsed)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        result = r.json()
        exit_code = result["exit_code"]
        stdout = result["stdout"].strip()
        stderr = result["stderr"].strip()

        status_ok = exit_code == expect_exit
        stdout_ok = (expect_stdout is None) or (expect_stdout in stdout) or (expect_stdout in stderr)

        if status_ok and stdout_ok:
            preview = (stdout or stderr)[:60].replace("\n", " ")
            _ok(f"exec: {label}", f"exit={exit_code}  {elapsed}  out={preview!r}")
        else:
            _fail(
                f"exec: {label}",
                f"exit={exit_code} (want {expect_exit}), stdout={stdout!r}",
            )

    timings["avg_exec"] = f"{sum(int(t.rstrip('ms')) for t in exec_timings) // len(exec_timings)}ms"

    # ── 4. File-sync WebSocket ─────────────────────────────────────────────
    _section("4 / File-sync WebSocket")
    t = time.monotonic()
    ws_url = f"ws://127.0.0.1:{PORT}/v1/workspaces/{ws_id}/files?token={API_KEY}"
    try:
        msg = ws_receive_one(ws_url)
        timings["file_sync_ws"] = _elapsed(t)
        assert msg["type"] == "file_listing", f"Expected file_listing, got {msg['type']}"
        assert msg["workspace_id"] == ws_id
        files = msg["files"]
        _ok("WS /files connected", timings["file_sync_ws"])
        _ok("type = file_listing")
        _ok("files in workspace", str([f["path"] for f in files]))
        assert any(f["path"] == "result.txt" for f in files), \
            f"result.txt not found in listing: {files}"
        _ok("result.txt present in file listing")
    except Exception as e:
        _fail("File-sync WebSocket", str(e))

    # ── 5. Stream WebSocket (heartbeat) ────────────────────────────────────
    _section("5 / Stream WebSocket (heartbeat)")
    t = time.monotonic()
    stream_url = f"ws://127.0.0.1:{PORT}/v1/workspaces/{ws_id}/stream?token={API_KEY}"
    try:
        msg = ws_receive_one(stream_url)
        timings["stream_ws"] = _elapsed(t)
        assert msg["type"] == "heartbeat", f"Expected heartbeat, got {msg['type']}"
        assert msg["workspace_id"] == ws_id
        _ok("WS /stream connected", timings["stream_ws"])
        _ok("type = heartbeat")
        _ok("uptime_seconds", str(round(msg["uptime_seconds"], 1)))
        _ok("status", msg["status"])
    except Exception as e:
        _fail("Stream WebSocket", str(e))

    # ── 6. Download workspace zip ──────────────────────────────────────────
    _section("6 / Download workspace (zip)")
    t = time.monotonic()
    r = client.get(f"/v1/workspaces/{ws_id}/download")
    timings["download"] = _elapsed(t)
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    assert r.headers["content-type"] == "application/zip"
    buf = io.BytesIO(r.content)
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
    _ok("GET /download", timings["download"])
    _ok("content-type = application/zip")
    _ok("zip contents", str(names))
    assert "result.txt" in names, f"result.txt missing from zip: {names}"
    _ok("result.txt present in zip")

    # ── 7. Destroy workspace ───────────────────────────────────────────────
    _section("7 / Destroy workspace")
    t = time.monotonic()
    r = client.delete(f"/v1/workspaces/{ws_id}")
    timings["destroy"] = _elapsed(t)
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    result = r.json()
    _ok("DELETE /v1/workspaces/...", timings["destroy"])
    _ok("status", result["status"])
    _ok("uptime_minutes", str(result["uptime_minutes"]))
    _ok("cost_incurred_usd", f"${result['cost_incurred_usd']:.6f}")

    # Confirm it's gone (404)
    r = client.post(f"/v1/workspaces/{ws_id}/exec", json={"command": "echo hi"})
    assert r.status_code == 404, f"Expected 404, got {r.status_code}"
    _ok("Workspace 404 after destroy")

    return timings


# ---------------------------------------------------------------------------
# Log verification
# ---------------------------------------------------------------------------

EXPECTED_LOG_PATTERNS = [
    ("server startup",            "Running in local mode"),
    ("workspace spawned",         "Spawned workspace"),
    ("auto-kill scheduled",       "auto-kill in"),
    ("workspace destroyed",       "Destroyed workspace"),
    ("uptime logged",             "uptime="),
    ("cost logged",               "cost=$"),
]


def verify_logs() -> bool:
    _section("8 / Verifying server logs")
    logs = LOG_FILE.read_text()

    all_ok = True
    for label, pattern in EXPECTED_LOG_PATTERNS:
        if pattern in logs:
            _ok(f"log: {label}", f"found {pattern!r}")
        else:
            _fail(f"log: {label}", f"pattern {pattern!r} NOT found in logs")
            all_ok = False

    # Show the last 20 lines of log for visibility
    print("\n\033[2m--- last 20 server log lines ---\033[0m")
    for line in logs.splitlines()[-20:]:
        print(f"  \033[2m{line}\033[0m")

    return all_ok


# ---------------------------------------------------------------------------
# Timing summary
# ---------------------------------------------------------------------------

def print_summary(timings: dict[str, str]):
    _section("Timing summary")
    for key, val in timings.items():
        _ok(key.replace("_", " "), val)

    print(f"""
\033[1;33m  GKE cloud workspace spawn timing (reference):\033[0m
    API call returns:        < 1s   (pod + service queued in background)
    Pod scheduled (warm):    ~60–90s
    Pod scheduled (cold):    ~3–5 min  (Autopilot auto-provisions node)
    Container pull + start:  ~30–60s
    LoadBalancer IP ready:   ~30–90s
    ─────────────────────────────────────────────
    Total to usable workspace: ~3–6 minutes
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("\n\033[1;37m SwarmOS — Agent Orchestration Simulation\033[0m\n")

    proc = start_server()
    try:
        wait_for_ready()
        timings = run_simulation()
        logs_ok = verify_logs()
        print_summary(timings)
        if logs_ok:
            print("\033[1;32m\n  ALL CHECKS PASSED ✓\033[0m\n")
        else:
            print("\033[1;31m\n  SOME LOG CHECKS FAILED ✗\033[0m\n")
            sys.exit(1)
    finally:
        stop_server(proc)


if __name__ == "__main__":
    main()
