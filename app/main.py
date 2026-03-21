"""SwarmOS GCP Backend - FastAPI Application.

Exposes the workspace lifecycle API that the orchestrator calls to
spawn, exec in, stream, sync files, and destroy agent workspaces.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from contextlib import asynccontextmanager
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
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
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
        "stream_webrtc": f"{ws_base_url}/v1/workspaces/{workspace_id}/stream",
        "file_sync": f"{ws_base_url}/v1/workspaces/{workspace_id}/files",
    }


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
# WebSocket: Visual stream
# ---------------------------------------------------------------------------
@app.websocket("/v1/workspaces/{workspace_id}/stream")
async def stream_workspace(
    websocket: WebSocket,
    workspace_id: str,
    token: str = Query(...),
):
    """Visual stream via WebSocket.

    In GKE mode: waits for the pod's noVNC LoadBalancer IP to be assigned,
    then sends a 'redirect' message so the frontend can connect directly to
    ws://POD_IP:6080 (noVNC/websockify). This avoids routing 30fps video
    through Cloud Run and gives the lowest possible latency.

    In local mode: sends heartbeat status frames (for development).
    """
    if token != config.SWARM_API_KEY:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    ws = manager.get_workspace(workspace_id)
    if ws is None:
        await websocket.close(code=4004, reason="Workspace not found")
        return

    await websocket.accept()

    # GKE mode: wait for the pod's external IP then redirect the client
    if config.GKE_MODE:
        try:
            # Poll until stream_url is resolved (LoadBalancer IP assigned)
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
                    "message": "Connect your UI directly to stream_url for live video",
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
        except Exception:
            logger.exception("GKE stream error for %s", workspace_id)
        return

    # Local mode: heartbeat loop
    try:
        while True:
            uptime = round(time.time() - ws.created_at, 1)
            await websocket.send_json({
                "type": "heartbeat",
                "workspace_id": workspace_id,
                "status": ws.status,
                "uptime_seconds": uptime,
                "timestamp": time.time(),
            })
            await asyncio.sleep(config.STREAM_HEARTBEAT)
    except WebSocketDisconnect:
        logger.info("Stream client disconnected from %s", workspace_id)
    except Exception:
        logger.exception("Stream error for %s", workspace_id)


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
