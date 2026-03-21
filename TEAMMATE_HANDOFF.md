# SwarmOS — Cloud Backend Handoff
**For:** Local Lead (Orchestration & UI)
**From:** Cloud Lead (GCP Sandboxing & Security)
**Date:** 21 March 2026

---

## What Was Built

The entire GCP backend is live. This is **Layer 3 (The Muscle)** from our architecture doc — the API that your local Orchestrator talks to when it needs to spawn, command, and kill agent workspaces.

### What's Running
- **FastAPI backend** deployed on Google Cloud Run (europe-west2 / London)
- **Docker container** pre-loaded with: Python 3.11, Node.js, npm, git, curl, Playwright, Xvfb (virtual screen), noVNC, x11vnc, websockify, fluxbox
- **Auto-kill safety** hardcoded inside every container — it shuts itself down after 30 minutes regardless of what the agent is doing
- **Command blocklist** — dangerous commands (`rm -rf /`, `shutdown`, `dd`, fork bombs, etc.) are rejected before execution
- **Artifact Registry** storing the container image in GCP

---

## Your Credentials

| Key | Value |
|-----|-------|
| **Base URL** | `https://swarm-os-api-980802873588.europe-west2.run.app/v1` |
| **API Key** | `G0U6xo_Jivhwh3zwMPaPA81OYvmaDOWZ2_lqJ-0BN1s` |
| **Auth Header** | `Authorization: Bearer G0U6xo_Jivhwh3zwMPaPA81OYvmaDOWZ2_lqJ-0BN1s` |

Keep the API key secret. Put it in your local `.env` file, never hardcode it.

---

## The 5 Endpoints — How To Use Them

### 1. Spawn a Workspace
Call this when your Orchestrator needs a new agent environment.

```bash
curl -X POST https://swarm-os-api-980802873588.europe-west2.run.app/v1/workspaces/spawn \
  -H "Authorization: Bearer G0U6xo_Jivhwh3zwMPaPA81OYvmaDOWZ2_lqJ-0BN1s" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "coder_alpha_01",
    "environment_type": "ubuntu_python_node",
    "tools_preloaded": ["playwright", "git"],
    "auto_kill_timeout_minutes": 30
  }'
```

**Response (201):**
```json
{
  "workspace_id": "ws_893853283",
  "status": "booting",
  "estimated_ready_sec": 8,
  "endpoints": {
    "exec": "https://swarm-os-api-.../v1/workspaces/ws_893853283/exec",
    "stream_webrtc": "wss://swarm-os-api-.../v1/workspaces/ws_893853283/stream",
    "file_sync": "wss://swarm-os-api-.../v1/workspaces/ws_893853283/files"
  }
}
```

Save the `workspace_id` — you'll need it for every other call.

---

### 2. Execute a Command (The ReAct Loop)
This is how your Coder/Tester agents run code. Send a command, get back stdout/stderr.

```bash
curl -X POST https://swarm-os-api-980802873588.europe-west2.run.app/v1/workspaces/ws_893853283/exec \
  -H "Authorization: Bearer G0U6xo_Jivhwh3zwMPaPA81OYvmaDOWZ2_lqJ-0BN1s" \
  -H "Content-Type: application/json" \
  -d '{
    "command": "python3",
    "args": ["server.py"],
    "working_directory": "/home/agent/workspace"
  }'
```

**Response (200):**
```json
{
  "exit_code": 0,
  "stdout": "Server running on port 8000...",
  "stderr": "",
  "execution_time_ms": 312
}
```

Your Tester agent reads `exit_code` and `stderr` to decide if the Coder needs to fix something.

---

### 3. Download Files
When agents finish building something, pull the files back to your machine.

```bash
curl -O -J \
  -H "Authorization: Bearer G0U6xo_Jivhwh3zwMPaPA81OYvmaDOWZ2_lqJ-0BN1s" \
  "https://swarm-os-api-980802873588.europe-west2.run.app/v1/workspaces/ws_893853283/download?path=/home/agent/workspace"
```

Returns a `.zip` file of whatever path you specify.

---

### 4. Stream the Agent's Screen (WebSocket)
Connect your HTML UI to this WebSocket to see what the agent is doing in real time.

```
wss://swarm-os-api-980802873588.europe-west2.run.app/v1/workspaces/ws_893853283/stream?token=G0U6xo_Jivhwh3zwMPaPA81OYvmaDOWZ2_lqJ-0BN1s
```

In your frontend JavaScript:
```js
const ws = new WebSocket(
  `wss://swarm-os-api-980802873588.europe-west2.run.app/v1/workspaces/${workspaceId}/stream?token=${API_KEY}`
);
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  // data.type === "heartbeat" | "frame"
  console.log(data);
};
```

> **Note:** Live video streaming (KasmVNC) is the next step — currently sends heartbeat status frames. The WebSocket path and auth are final.

---

### 5. Destroy a Workspace
Always call this when a task is done. Stops billing immediately.

```bash
curl -X DELETE \
  -H "Authorization: Bearer G0U6xo_Jivhwh3zwMPaPA81OYvmaDOWZ2_lqJ-0BN1s" \
  "https://swarm-os-api-980802873588.europe-west2.run.app/v1/workspaces/ws_893853283"
```

**Response (200):**
```json
{
  "workspace_id": "ws_893853283",
  "status": "destroyed",
  "uptime_minutes": 14,
  "cost_incurred_usd": 0.02
}
```

---

## Python Integration (Copy-Paste Ready)

Drop this into your local Orchestrator:

```python
import httpx

BASE_URL = "https://swarm-os-api-980802873588.europe-west2.run.app/v1"
API_KEY  = "G0U6xo_Jivhwh3zwMPaPA81OYvmaDOWZ2_lqJ-0BN1s"
HEADERS  = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

def spawn(agent_id: str, env_type: str = "ubuntu_python_node") -> dict:
    r = httpx.post(f"{BASE_URL}/workspaces/spawn", headers=HEADERS, json={
        "agent_id": agent_id,
        "environment_type": env_type,
        "auto_kill_timeout_minutes": 30
    })
    r.raise_for_status()
    return r.json()  # contains workspace_id and endpoints

def execute(workspace_id: str, command: str, args: list[str]) -> dict:
    r = httpx.post(f"{BASE_URL}/workspaces/{workspace_id}/exec", headers=HEADERS, json={
        "command": command,
        "args": args,
        "working_directory": "/home/agent/workspace"
    })
    r.raise_for_status()
    return r.json()  # contains exit_code, stdout, stderr

def destroy(workspace_id: str) -> dict:
    r = httpx.delete(f"{BASE_URL}/workspaces/{workspace_id}", headers=HEADERS)
    r.raise_for_status()
    return r.json()

# --- Example ReAct loop ---
ws = spawn("coder_01")
workspace_id = ws["workspace_id"]

result = execute(workspace_id, "python3", ["-c", "print('hello from GCP')"])
print(result["stdout"])   # hello from GCP
print(result["exit_code"]) # 0

destroy(workspace_id)
```

---

## Safety Guarantees Built In

| Threat | Protection |
|--------|-----------|
| Agent runs infinite loop | Container auto-kills after 30 min, no exceptions |
| Agent runs destructive command | Blocklist rejects it with 400 before execution |
| Rogue agent deletes GCP resources | Worker service account has no GCP project permissions |
| Billing runaway | Cloud Run scales to 0 when idle; $50/month budget alert configured |
| Agent downloads huge files | Network egress policy limits in GKE network config |

---

## What's Left (Sprint 3 Items)

| Item | Status |
|------|--------|
| All 5 HTTP endpoints | ✅ Live |
| Auth (Bearer token) | ✅ Live |
| Auto-kill timer | ✅ In container |
| Command blocklist | ✅ Live |
| File download (zip) | ✅ Live |
| Stream WebSocket path | ✅ Live (heartbeat only) |
| **Live KasmVNC video stream** | ⏳ Wire into container — path is ready |
| **GKE worker cluster** | ⏳ Run `infra/gke/cluster-setup.sh` to spin up |
| **Civic proxy registration** | ⏳ Register the Base URL above in Civic and swap your agent URLs |

For Sprint 3, once you have the Civic token, your agents should point to the Civic gateway URL, not directly to this backend URL.
