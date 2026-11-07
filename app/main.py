"""SwarmOS GCP Backend - FastAPI Application.

Exposes the workspace lifecycle API that the orchestrator calls to
spawn, exec in, stream, sync files, and destroy agent workspaces.
Each workspace gets a virtual desktop viewable via noVNC in the browser.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import config
from app.workspace_manager import WorkspaceManager

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("swarm.api")

# ---------------------------------------------------------------------------
# App & shared state
# ---------------------------------------------------------------------------
if config.GKE_MODE:
    from app.gke_manager import GKEWorkspaceManager
    manager: WorkspaceManager = GKEWorkspaceManager()  # type: ignore[assignment]
    logger.info("Running in GKE mode — workspaces are real GKE pods")
else:
    manager = WorkspaceManager()
    logger.info("Running in local mode — workspaces are subprocesses")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.GKE_MODE:
        asyncio.create_task(manager.start_warm_pool())
    yield


app = FastAPI(
    title="SwarmOS GCP Backend",
    version="0.1.0",
    description="Multi-agent workspace management API",
    lifespan=lifespan,
)
security = HTTPBearer()
_FRONTEND_PATH = Path(__file__).parent / "static" / "index.html"

# Serve noVNC static files if available (container has them at /usr/share/novnc)
_NOVNC_DIR = "/usr/share/novnc"
if os.path.isdir(_NOVNC_DIR):
    app.mount("/novnc", StaticFiles(directory=_NOVNC_DIR, html=True), name="novnc")
    logger.info("Serving noVNC static files from %s", _NOVNC_DIR)


def _public_endpoint_urls(request: Request, workspace_id: str) -> dict[str, str]:
    """Build public URLs from the actual incoming request host/scheme.

    Cloud Run's real service URL is the hash-based ``*.a.run.app`` hostname,
    not the guessed ``service-project.region.run.app`` pattern. Using the
    request headers keeps the response endpoints accurate in Cloud Run, local
    dev, and tests.
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get(
        "x-forwarded-host",
        request.headers.get("host", request.url.netloc),
    )
    base_url = f"{proto}://{host}"
    ws_proto = "wss" if proto == "https" else "ws"
    ws_base_url = f"{ws_proto}://{host}"
    return {
        "exec": f"{base_url}/v1/workspaces/{workspace_id}/exec",
        "view": f"{base_url}/v1/workspaces/{workspace_id}/view",
        "stream": f"{ws_base_url}/v1/workspaces/{workspace_id}/stream",
        "stream_webrtc": f"{ws_base_url}/v1/workspaces/{workspace_id}/stream",  # compat alias
        "file_sync": f"{ws_base_url}/v1/workspaces/{workspace_id}/files",
    }


@app.get("/", include_in_schema=False)
async def home():
    if _FRONTEND_PATH.is_file():
        return FileResponse(_FRONTEND_PATH)
    return HTMLResponse("<h1>MoonClaw</h1><p>Frontend not found.</p>", status_code=500)


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------
def _verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    if credentials.credentials != config.SWARM_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return credentials.credentials


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class SpawnRequest(BaseModel):
    agent_id: str
    environment_type: str = "dev"
    tools_preloaded: list[str] = Field(default_factory=list)
    auto_kill_timeout_minutes: Optional[int] = None


class SpawnResponse(BaseModel):
    workspace_id: str
    status: str
    estimated_ready_sec: int
    endpoints: dict


class ExecRequest(BaseModel):
    command: str
    args: list[str] = Field(default_factory=list)
    working_directory: str = "/home/agent/workspace"
    timeout_seconds: Optional[int] = None


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    execution_time_ms: int


class DestroyResponse(BaseModel):
    workspace_id: str
    status: str
    uptime_minutes: float
    cost_incurred_usd: float


# ---------------------------------------------------------------------------
# REST Endpoints
# ---------------------------------------------------------------------------

@app.post(
    "/v1/workspaces/spawn",
    response_model=SpawnResponse,
    status_code=status.HTTP_201_CREATED,
)
async def spawn_workspace(
    body: SpawnRequest,
    request: Request,
    _token: str = Depends(_verify_token),
):
    t0 = time.monotonic()
    ws = await manager.spawn_workspace(
        agent_id=body.agent_id,
        environment_type=body.environment_type,
        tools_preloaded=body.tools_preloaded,
        auto_kill_timeout_minutes=body.auto_kill_timeout_minutes,
    )
    spawn_elapsed_sec = time.monotonic() - t0

    # Warm pool claim  → status "running",  ready now   (0s)
    # GKE cold spawn   → status "booting",  ~3-6 min
    # Local mode       → status "running",  measured spawn time
    warm = config.GKE_MODE and ws.status == "running"
    if config.GKE_MODE:
        estimated_ready_sec = 0 if warm else config.SPAWN_ESTIMATED_READY_SEC
    else:
        estimated_ready_sec = max(1, round(spawn_elapsed_sec))

    return SpawnResponse(
        workspace_id=ws.workspace_id,
        status=ws.status,
        estimated_ready_sec=estimated_ready_sec,
        endpoints=_public_endpoint_urls(request, ws.workspace_id),
    )


@app.post(
    "/v1/workspaces/{workspace_id}/exec",
    response_model=ExecResponse,
)
async def exec_command(
    workspace_id: str,
    body: ExecRequest,
    _token: str = Depends(_verify_token),
):
    ws = manager.get_workspace(workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    result = await manager.execute_command(
        workspace_id=workspace_id,
        command=body.command,
        args=body.args,
        working_directory=body.working_directory,
        timeout_seconds=body.timeout_seconds,
    )
    return ExecResponse(
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        execution_time_ms=result.execution_time_ms,
    )


@app.get("/v1/workspaces/{workspace_id}/download")
async def download_workspace(
    workspace_id: str,
    path: str = Query(default="/home/agent/workspace"),
    _token: str = Depends(_verify_token),
):
    try:
        zip_bytes = manager.download_workspace_bytes(workspace_id, path)
    except KeyError:
        raise HTTPException(status_code=404, detail="Workspace not found")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Workspace directory not found")

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename={workspace_id}.zip"
        },
    )


@app.delete(
    "/v1/workspaces/{workspace_id}",
    response_model=DestroyResponse,
)
async def destroy_workspace(
    workspace_id: str, _token: str = Depends(_verify_token)
):
    try:
        result = await manager.destroy_workspace(workspace_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Workspace not found")

    return DestroyResponse(**result)


# ---------------------------------------------------------------------------
# Demo: one-click spawn + demo + viewer redirect
# ---------------------------------------------------------------------------
@app.get("/v1/demo", response_class=HTMLResponse)
async def demo(
    request: Request,
    token: str = Query(...),
):
    """One-click demo: spawns a workspace, starts the agent demo, and
    redirects to the live viewer so you see it all happen."""
    if token != config.SWARM_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid token")

    ws = await manager.spawn_workspace(
        agent_id="demo-agent",
        auto_kill_timeout_minutes=10,
    )

    # Launch the demo script in the background
    await manager.execute_command(
        workspace_id=ws.workspace_id,
        command="bash /opt/swarm-os/scripts/demo_agent.sh &",
        timeout_seconds=5,
    )

    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get(
        "x-forwarded-host",
        request.headers.get("host", request.url.netloc),
    )
    view_url = f"{proto}://{host}/v1/workspaces/{ws.workspace_id}/view?token={token}"

    # Redirect to the viewer
    return HTMLResponse(
        f'<html><head><meta http-equiv="refresh" content="0;url={view_url}"></head>'
        f'<body>Redirecting to <a href="{view_url}">live viewer</a>...</body></html>'
    )


# ---------------------------------------------------------------------------
# Browser viewer: opens noVNC to watch the agent's screen live
# ---------------------------------------------------------------------------
@app.get("/v1/workspaces/{workspace_id}/view", response_class=HTMLResponse)
async def view_workspace(
    workspace_id: str,
    token: str = Query(...),
    request: Request = None,
):
    """Render an in-browser noVNC viewer for the workspace's virtual desktop.

    Uses the noVNC static files served from the container's /usr/share/novnc/
    via ES module import. Falls back to a terminal activity stream if noVNC
    can't connect.
    """
    if token != config.SWARM_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid token")

    ws = manager.get_workspace(workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get(
        "x-forwarded-host",
        request.headers.get("host", request.url.netloc),
    )
    ws_proto = "wss" if proto == "https" else "ws"
    stream_url = f"{ws_proto}://{host}/v1/workspaces/{workspace_id}/stream?token={token}"

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>SwarmOS — {workspace_id}</title>
<style>
  * {{ margin:0; padding:0; }}
  body {{ background:#0d1117; overflow:hidden; }}
  #top-bar {{ background:#161b22; color:#c9d1d9; padding:6px 16px; font:13px/1.4 monospace;
              display:flex; align-items:center; gap:16px; border-bottom:1px solid #30363d; }}
  .dot {{ width:8px; height:8px; border-radius:50%; background:#3fb950; }}
  .dot.off {{ background:#f85149; }}
  .dot.connecting {{ background:#d29922; }}
  #screen {{ width:100vw; height:calc(100vh - 34px); }}
  #activity {{ position:fixed; bottom:0; left:0; right:0; max-height:40vh; overflow-y:auto;
               background:rgba(13,17,23,0.95); border-top:1px solid #30363d; padding:8px 12px;
               font:12px/1.5 monospace; color:#8b949e; z-index:10; }}
  .act-line {{ white-space:pre-wrap; word-break:break-all; }}
  .act-cmd {{ color:#d2a8ff; font-weight:bold; }}
  .act-out {{ color:#c9d1d9; }}
  .act-err {{ color:#f85149; }}
  .act-sys {{ color:#484f58; }}
</style>
</head><body>
<div id="top-bar">
  <span id="dot" class="dot connecting"></span>
  <span>{workspace_id}</span>
  <span id="status">Connecting...</span>
  <button onclick="toggleLog()" style="margin-left:auto;background:#21262d;color:#c9d1d9;border:1px solid #30363d;
    padding:2px 10px;border-radius:4px;cursor:pointer;font:12px monospace;">Toggle Log</button>
</div>
<div id="screen"></div>
<div id="activity" style="display:none;"></div>

<script type="module">
// Load noVNC RFB from the container's static files
import RFB from '/novnc/core/rfb.js';

const streamUrl = "{stream_url}";
const screenEl = document.getElementById('screen');
const dotEl = document.getElementById('dot');
const statusEl = document.getElementById('status');
const actEl = document.getElementById('activity');
let rfb = null;

function setStatus(state, text) {{
  statusEl.textContent = text;
  dotEl.className = 'dot' + (state === 'live' ? '' : state === 'error' ? ' off' : ' connecting');
}}

function addLog(text, cls) {{
  const d = document.createElement('div');
  d.className = 'act-line ' + (cls || 'act-sys');
  d.textContent = text;
  actEl.appendChild(d);
  if (actEl.children.length > 500) actEl.removeChild(actEl.firstChild);
  actEl.scrollTop = actEl.scrollHeight;
}}

window.toggleLog = function() {{ actEl.style.display = actEl.style.display === 'none' ? 'block' : 'none'; }};

function connectVNC() {{
  setStatus('connecting', 'Connecting to desktop...');
  addLog('[vnc] Connecting to ' + streamUrl);
  try {{
    rfb = new RFB(screenEl, streamUrl, {{
      wsProtocols: ['binary'],
    }});
    rfb.scaleViewport = true;
    rfb.resizeSession = false;
    rfb.addEventListener('connect', () => {{
      setStatus('live', 'Live');
      addLog('[vnc] Connected — screen is live');
    }});
    rfb.addEventListener('disconnect', (e) => {{
      const reason = e.detail.clean ? 'clean close' : 'connection lost';
      setStatus('error', 'Disconnected — ' + reason);
      addLog('[vnc] Disconnected: ' + reason);
      setTimeout(connectVNC, 3000);
    }});
  }} catch(err) {{
    setStatus('error', 'noVNC error — see log');
    addLog('[vnc] Error: ' + err.message, 'act-err');
    setTimeout(connectVNC, 5000);
  }}
}}

connectVNC();
</script>
</body></html>""")


# ---------------------------------------------------------------------------
# WebSocket: VNC screen stream (proxies to local websockify)
# ---------------------------------------------------------------------------
@app.websocket("/v1/workspaces/{workspace_id}/stream")
async def stream_workspace(
    websocket: WebSocket,
    workspace_id: str,
    token: str = Query(...),
):
    """Live screen stream via WebSocket.

    Bidirectional proxy between the browser's noVNC client and the
    workspace's local websockify. Carries binary RFB/VNC frames.
    """
    if token != config.SWARM_API_KEY:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    ws = manager.get_workspace(workspace_id)
    if ws is None:
        await websocket.close(code=4004, reason="Workspace not found")
        return

    # GKE mode: redirect to pod's direct noVNC URL
    if config.GKE_MODE:
        await websocket.accept()
        try:
            deadline = time.time() + 180
            while not getattr(ws, "stream_url", "") and time.time() < deadline:
                await websocket.send_json({
                    "type": "provisioning",
                    "workspace_id": workspace_id,
                    "message": "Waiting for agent pod to get a public IP...",
                    "timestamp": time.time(),
                })
                await asyncio.sleep(5)

            stream_url = getattr(ws, "stream_url", "")
            if stream_url:
                await websocket.send_json({
                    "type": "redirect",
                    "workspace_id": workspace_id,
                    "stream_url": stream_url,
                    "timestamp": time.time(),
                })
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": "Pod did not get a public IP in time",
                    "timestamp": time.time(),
                })
        except WebSocketDisconnect:
            pass
        return

    # Local mode: if the client explicitly requests the noVNC binary
    # subprotocol, proxy raw VNC traffic. Otherwise fall back to the JSON
    # activity stream so generic websocket clients still work.
    requested_protocols = websocket.scope.get("subprotocols", [])
    wants_vnc = "binary" in requested_protocols

    if ws.vnc_ws_port and wants_vnc:
        import websockets

        # Accept with the binary subprotocol that noVNC requests
        await websocket.accept(subprotocol="binary")

        upstream_url = f"ws://localhost:{ws.vnc_ws_port}"
        try:
            async with websockets.connect(
                upstream_url,
                subprotocols=["binary"],
                max_size=2**22,
                open_timeout=10,
            ) as upstream:
                logger.info(
                    "VNC proxy connected for %s → localhost:%d",
                    workspace_id, ws.vnc_ws_port,
                )

                async def _client_to_vnc():
                    """Forward frames from browser → websockify."""
                    try:
                        while True:
                            msg = await websocket.receive()
                            if msg.get("bytes"):
                                await upstream.send(msg["bytes"])
                            elif msg.get("text"):
                                await upstream.send(msg["text"])
                            elif msg.get("type") == "websocket.disconnect":
                                break
                    except (WebSocketDisconnect, Exception):
                        pass

                async def _vnc_to_client():
                    """Forward frames from websockify → browser."""
                    try:
                        async for frame in upstream:
                            if isinstance(frame, bytes):
                                await websocket.send_bytes(frame)
                            else:
                                await websocket.send_text(frame)
                    except (WebSocketDisconnect, Exception):
                        pass

                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(_client_to_vnc()),
                        asyncio.create_task(_vnc_to_client()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()

            logger.info("VNC proxy closed for %s", workspace_id)
            return
        except Exception as exc:
            logger.warning(
                "VNC proxy failed for %s: %s — falling back to activity stream",
                workspace_id, exc,
            )
            # Fall through to activity stream

    # Fallback: terminal activity stream (no desktop available)
    # Accept without subprotocol for JSON activity events
    try:
        await websocket.accept()
    except RuntimeError:
        pass  # already accepted above in error path

    q = manager.subscribe(workspace_id)
    try:
        while True:
            sent_any = False
            while not q.empty():
                try:
                    event = q.get_nowait()
                    event["workspace_id"] = workspace_id
                    await websocket.send_json(event)
                    sent_any = True
                except asyncio.QueueEmpty:
                    break
            if not sent_any:
                uptime = round(time.time() - ws.created_at, 1)
                await websocket.send_json({
                    "type": "heartbeat",
                    "workspace_id": workspace_id,
                    "status": ws.status,
                    "uptime_seconds": uptime,
                    "timestamp": time.time(),
                })
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        logger.info("Stream client disconnected from %s", workspace_id)
    except Exception:
        logger.exception("Stream error for %s", workspace_id)
    finally:
        manager.unsubscribe(workspace_id, q)


# ---------------------------------------------------------------------------
# WebSocket: File sync
# ---------------------------------------------------------------------------
@app.websocket("/v1/workspaces/{workspace_id}/files")
async def file_sync(
    websocket: WebSocket,
    workspace_id: str,
    token: str = Query(...),
):
    """File-listing sync via WebSocket.

    Sends a snapshot of the workspace file tree on connect and periodically.
    """
    if token != config.SWARM_API_KEY:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    ws = manager.get_workspace(workspace_id)
    if ws is None:
        await websocket.close(code=4004, reason="Workspace not found")
        return

    await websocket.accept()

    try:
        while True:
            files = manager.list_workspace_files(workspace_id)
            await websocket.send_json({
                "type": "file_listing",
                "workspace_id": workspace_id,
                "files": files,
                "timestamp": time.time(),
            })
            await asyncio.sleep(config.STREAM_HEARTBEAT)
    except WebSocketDisconnect:
        logger.info("File-sync client disconnected from %s", workspace_id)
    except Exception:
        logger.exception("File-sync error for %s", workspace_id)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"status": "ok", "workspaces_active": len(manager.list_workspaces())}
