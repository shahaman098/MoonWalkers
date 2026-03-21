# SwarmOS GCP Backend

Multi-agent workspace management backend for the SwarmOS hackathon project. Spawns isolated containerized workspaces for AI agents, with command execution, visual streaming, and file sync.

## Architecture

```
Orchestrator/UI (Local Lead)
      |
      v
  Cloud Run (FastAPI API)
      |
      v
  GKE Autopilot (Agent Pods)
      |-- Xvfb + fluxbox + x11vnc (visual desktop)
      |-- noVNC via websockify (browser-accessible stream)
      |-- uvicorn API (command execution)
      '-- Auto-kill timer (safety net)
```

**Components:**
- `app/` - FastAPI backend (config, main API, workspace manager)
- `scripts/` - Container entrypoint and auto-kill safety timer
- `infra/` - GCP infrastructure configs (Cloud Run, GKE, billing)
- `tests/` - Full pytest test suite
- `deploy.sh` - One-click deploy script

## Run Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Run the API server
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

# The API key is auto-generated on startup. Check the logs or set it:
export SWARM_API_KEY="your-token-here"
```

## Run Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

## Deploy to GCP

### Quick Deploy (Cloud Run only)

```bash
bash deploy.sh
```

### Full Deploy (Cloud Run + GKE)

```bash
# 1. Set up GKE cluster, service accounts, IAM
bash infra/gke/cluster-setup.sh

# 2. Build and deploy
bash deploy.sh
```

## API Contract

All endpoints require `Authorization: Bearer <SWARM_API_KEY>` header.
WebSocket endpoints take `?token=<SWARM_API_KEY>` query parameter.

### POST /v1/workspaces/spawn

Spawn a new agent workspace.

```bash
curl -X POST https://YOUR_URL/v1/workspaces/spawn \
  -H "Authorization: Bearer $SWARM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent-42",
    "environment_type": "dev",
    "tools_preloaded": ["git", "node"],
    "auto_kill_timeout_minutes": 30
  }'
```

Response (201):
```json
{
  "workspace_id": "ws_123456789",
  "status": "booting",
  "estimated_ready_sec": 8,
  "endpoints": {
    "exec": "https://YOUR_URL/v1/workspaces/ws_123456789/exec",
    "stream_webrtc": "wss://YOUR_URL/v1/workspaces/ws_123456789/stream",
    "file_sync": "wss://YOUR_URL/v1/workspaces/ws_123456789/files"
  }
}
```

### POST /v1/workspaces/{workspace_id}/exec

Execute a command in the workspace.

```bash
curl -X POST https://YOUR_URL/v1/workspaces/ws_123456789/exec \
  -H "Authorization: Bearer $SWARM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "command": "echo",
    "args": ["hello world"],
    "working_directory": "/home/agent/workspace",
    "timeout_seconds": 30
  }'
```

Response (200):
```json
{
  "exit_code": 0,
  "stdout": "hello world\n",
  "stderr": "",
  "execution_time_ms": 12
}
```

### GET /v1/workspaces/{workspace_id}/download

Download workspace files as a zip archive.

```bash
curl -O -J https://YOUR_URL/v1/workspaces/ws_123456789/download \
  -H "Authorization: Bearer $SWARM_API_KEY"
```

### DELETE /v1/workspaces/{workspace_id}

Destroy a workspace and get cost summary.

```bash
curl -X DELETE https://YOUR_URL/v1/workspaces/ws_123456789 \
  -H "Authorization: Bearer $SWARM_API_KEY"
```

Response (200):
```json
{
  "workspace_id": "ws_123456789",
  "status": "destroyed",
  "uptime_minutes": 12.5,
  "cost_incurred_usd": 0.017857
}
```

### WebSocket /v1/workspaces/{workspace_id}/stream

Visual stream (heartbeat for now, KasmVNC integration TBD).

```bash
websocat "wss://YOUR_URL/v1/workspaces/ws_123456789/stream?token=$SWARM_API_KEY"
```

### WebSocket /v1/workspaces/{workspace_id}/files

File listing sync.

```bash
websocat "wss://YOUR_URL/v1/workspaces/ws_123456789/files?token=$SWARM_API_KEY"
```

### GET /healthz

Health check (no auth required).

```bash
curl https://YOUR_URL/healthz
```

## Auto-Kill Safety Mechanism

Every workspace has a mandatory kill timer (default: 30 minutes). This is the most critical safety feature to prevent runaway costs.

**Three layers of protection:**

1. **Application-level** (`workspace_manager.py`): asyncio task that destroys the workspace after the timeout, with warnings at 5 min and 1 min before kill.

2. **Container-level** (`scripts/auto_kill.sh`): Independent bash script running in the container that sends SIGTERM/SIGKILL to PID 1. Works even if the Python process crashes.

3. **Kubernetes-level** (`agent-pod.yaml`): `activeDeadlineSeconds: 1800` on the pod spec. Works even if the container's auto-kill script fails.

## What's Implemented vs. What Needs Wiring

| Feature | Status | Notes |
|---------|--------|-------|
| Workspace spawn/destroy lifecycle | Done | Works locally via subprocess |
| Command execution with blocklist | Done | Subprocess-based, needs kubectl exec for GKE |
| File download (zip) | Done | |
| Bearer token auth | Done | |
| Auto-kill timer | Done | 3-layer safety |
| Visual stream WebSocket | Partial | Sends heartbeats; needs KasmVNC frame relay |
| File sync WebSocket | Done | Sends file listings periodically |
| GKE pod spawning | Config only | Pod YAML ready, needs wiring in workspace_manager |
| Network policy | Config only | Apply after cluster creation |
| Billing budget | Config only | Needs billing account ID |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GCP_PROJECT_ID` | `gdm-hacks2603-lon-2812` | GCP project |
| `GCP_REGION` | `europe-west2` | GCP region |
| `SWARM_API_KEY` | (auto-generated) | API authentication token |
| `WORKSPACE_ROOT` | `/tmp/swarm-os-gcp/workspaces` | Local workspace directory |
| `AUTO_KILL_DEFAULT` | `30` | Default workspace timeout (minutes) |
| `EXEC_TIMEOUT` | `300` | Command execution timeout (seconds) |
| `COST_PER_HOUR` | `0.085714` | Estimated cost per workspace-hour |
| `STREAM_HEARTBEAT` | `5` | WebSocket heartbeat interval (seconds) |
| `BASE_URL` | (derived) | API base URL |
| `STREAM_URL` | (derived) | WebSocket stream URL |
| `SYNC_URL` | (derived) | WebSocket file sync URL |
