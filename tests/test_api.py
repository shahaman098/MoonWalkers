"""Full test suite for the SwarmOS API."""

from __future__ import annotations

import zipfile
import io

from fastapi.testclient import TestClient

from app.config import SWARM_API_KEY


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------

def test_spawn_workspace(client: TestClient, auth_headers: dict):
    resp = client.post(
        "/v1/workspaces/spawn",
        json={"agent_id": "agent-42"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["workspace_id"].startswith("ws_")
    assert data["status"] in ("booting", "running")  # running in local mode, booting in GKE cold spawn

    # Cleanup
    client.delete(
        f"/v1/workspaces/{data['workspace_id']}",
        headers=auth_headers,
    )


def test_spawn_returns_correct_shape(client: TestClient, auth_headers: dict):
    resp = client.post(
        "/v1/workspaces/spawn",
        json={
            "agent_id": "agent-shape-test",
            "environment_type": "sandbox",
            "tools_preloaded": ["git", "node"],
            "auto_kill_timeout_minutes": 15,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()

    assert "workspace_id" in data
    assert "status" in data
    assert "estimated_ready_sec" in data
    assert "endpoints" in data

    endpoints = data["endpoints"]
    assert "exec" in endpoints
    assert "stream_webrtc" in endpoints
    assert "file_sync" in endpoints

    ws_id = data["workspace_id"]
    assert endpoints["exec"] == f"http://testserver/v1/workspaces/{ws_id}/exec"
    assert endpoints["stream_webrtc"] == f"ws://testserver/v1/workspaces/{ws_id}/stream"
    assert endpoints["file_sync"] == f"ws://testserver/v1/workspaces/{ws_id}/files"

    # Cleanup
    client.delete(f"/v1/workspaces/{ws_id}", headers=auth_headers)


# ---------------------------------------------------------------------------
# Exec
# ---------------------------------------------------------------------------

def test_exec_success(client: TestClient, auth_headers: dict, workspace: dict):
    ws_id = workspace["workspace_id"]
    resp = client.post(
        f"/v1/workspaces/{ws_id}/exec",
        json={"command": "echo", "args": ["hello world"]},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["exit_code"] == 0
    assert "hello world" in data["stdout"]
    assert data["execution_time_ms"] >= 0


def test_exec_failure(client: TestClient, auth_headers: dict, workspace: dict):
    ws_id = workspace["workspace_id"]
    resp = client.post(
        f"/v1/workspaces/{ws_id}/exec",
        json={"command": "ls", "args": ["/nonexistent_dir_xyz"]},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["exit_code"] != 0
    assert data["stderr"] != ""


def test_exec_blocked_command(
    client: TestClient, auth_headers: dict, workspace: dict
):
    ws_id = workspace["workspace_id"]
    resp = client.post(
        f"/v1/workspaces/{ws_id}/exec",
        json={"command": "rm -rf /"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["exit_code"] == 126
    assert "Blocked" in data["stderr"]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def test_download_zip(client: TestClient, auth_headers: dict, workspace: dict):
    ws_id = workspace["workspace_id"]

    # Create a file in the workspace first
    client.post(
        f"/v1/workspaces/{ws_id}/exec",
        json={"command": "echo 'test content' > testfile.txt"},
        headers=auth_headers,
    )

    resp = client.get(
        f"/v1/workspaces/{ws_id}/download",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    # Verify it's a valid zip
    buf = io.BytesIO(resp.content)
    with zipfile.ZipFile(buf, "r") as zf:
        names = zf.namelist()
        assert "testfile.txt" in names


# ---------------------------------------------------------------------------
# Destroy
# ---------------------------------------------------------------------------

def test_destroy_workspace(client: TestClient, auth_headers: dict):
    # Spawn one explicitly for this test
    spawn_resp = client.post(
        "/v1/workspaces/spawn",
        json={"agent_id": "agent-destroy-test"},
        headers=auth_headers,
    )
    ws_id = spawn_resp.json()["workspace_id"]

    resp = client.delete(
        f"/v1/workspaces/{ws_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["workspace_id"] == ws_id
    assert data["status"] == "destroyed"
    assert "uptime_minutes" in data
    assert "cost_incurred_usd" in data


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_auth_required(client: TestClient):
    resp = client.post(
        "/v1/workspaces/spawn",
        json={"agent_id": "no-auth-agent"},
    )
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# 404
# ---------------------------------------------------------------------------

def test_workspace_not_found(client: TestClient, auth_headers: dict):
    resp = client.post(
        "/v1/workspaces/ws_000000000/exec",
        json={"command": "echo hi"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

def test_stream_websocket_connect(
    client: TestClient, auth_headers: dict, workspace: dict
):
    ws_id = workspace["workspace_id"]
    with client.websocket_connect(
        f"/v1/workspaces/{ws_id}/stream?token={SWARM_API_KEY}"
    ) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "heartbeat"
        assert msg["workspace_id"] == ws_id
        assert "uptime_seconds" in msg


def test_file_sync_websocket_connect(
    client: TestClient, auth_headers: dict, workspace: dict
):
    ws_id = workspace["workspace_id"]
    with client.websocket_connect(
        f"/v1/workspaces/{ws_id}/files?token={SWARM_API_KEY}"
    ) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "file_listing"
        assert msg["workspace_id"] == ws_id
        assert "files" in msg


# ---------------------------------------------------------------------------
# Working directory
# ---------------------------------------------------------------------------

def test_exec_honors_working_directory(
    client: TestClient, auth_headers: dict, workspace: dict
):
    ws_id = workspace["workspace_id"]

    # Create a subdirectory
    client.post(
        f"/v1/workspaces/{ws_id}/exec",
        json={"command": "mkdir -p subdir"},
        headers=auth_headers,
    )

    # Write a file from that working directory using pwd to confirm cwd
    resp = client.post(
        f"/v1/workspaces/{ws_id}/exec",
        json={
            "command": "pwd",
            "working_directory": "/home/agent/workspace/subdir",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["exit_code"] == 0
    # The cwd should be the subdir (local path ends in subdir)
    assert "subdir" in data["stdout"]

    # Write a file in subdir by specifying the full shell command
    resp2 = client.post(
        f"/v1/workspaces/{ws_id}/exec",
        json={
            "command": "touch greeting.txt",
            "working_directory": "/home/agent/workspace/subdir",
        },
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    assert resp2.json()["exit_code"] == 0

    # Verify the file exists in the subdir
    resp3 = client.post(
        f"/v1/workspaces/{ws_id}/exec",
        json={
            "command": "ls subdir/greeting.txt",
        },
        headers=auth_headers,
    )
    assert resp3.status_code == 200
    assert resp3.json()["exit_code"] == 0


# ---------------------------------------------------------------------------
# Download path variations
# ---------------------------------------------------------------------------

def test_download_default_path(
    client: TestClient, auth_headers: dict, workspace: dict
):
    ws_id = workspace["workspace_id"]

    # Create a file
    client.post(
        f"/v1/workspaces/{ws_id}/exec",
        json={"command": "sh", "args": ["-c", "echo data > file.txt"]},
        headers=auth_headers,
    )

    resp = client.get(
        f"/v1/workspaces/{ws_id}/download",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    buf = io.BytesIO(resp.content)
    with zipfile.ZipFile(buf, "r") as zf:
        names = zf.namelist()
    assert "file.txt" in names


def test_download_subpath(
    client: TestClient, auth_headers: dict, workspace: dict
):
    ws_id = workspace["workspace_id"]

    # Create files at root and in a subdir
    client.post(
        f"/v1/workspaces/{ws_id}/exec",
        json={
            "command": (
                "mkdir -p subdir && "
                "echo rootdata > rootfile.txt && "
                "echo subdata > subdir/nested.txt"
            )
        },
        headers=auth_headers,
    )

    # Download only the subdir contents.
    resp = client.get(
        f"/v1/workspaces/{ws_id}/download?path=/home/agent/workspace/subdir",
        headers=auth_headers,
    )
    assert resp.status_code == 200

    buf = io.BytesIO(resp.content)
    with zipfile.ZipFile(buf, "r") as zf:
        names = zf.namelist()
    assert "nested.txt" in names
    assert "rootfile.txt" not in names


# ---------------------------------------------------------------------------
# Workspace ID format
# ---------------------------------------------------------------------------

def test_workspace_id_uses_underscore(client: TestClient, auth_headers: dict):
    resp = client.post(
        "/v1/workspaces/spawn",
        json={"agent_id": "id-format-test"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    workspace_id = resp.json()["workspace_id"]
    assert workspace_id.startswith("ws_"), (
        f"Expected workspace_id to start with 'ws_', got: {workspace_id!r}"
    )

    # Cleanup
    client.delete(f"/v1/workspaces/{workspace_id}", headers=auth_headers)


def test_exec_missing_working_directory_fails(
    client: TestClient, auth_headers: dict, workspace: dict
):
    ws_id = workspace["workspace_id"]
    resp = client.post(
        f"/v1/workspaces/{ws_id}/exec",
        json={
            "command": "pwd",
            "working_directory": "/home/agent/workspace/does/not/exist",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["exit_code"] != 0
    assert "Working directory not found" in data["stderr"]
