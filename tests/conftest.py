"""Pytest fixtures for SwarmOS API tests."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# Set a deterministic API key for tests BEFORE importing the app
os.environ["SWARM_API_KEY"] = "test-secret-key-for-ci"

from app.config import SWARM_API_KEY  # noqa: E402
from app.main import app, manager  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SWARM_API_KEY}"}


@pytest.fixture()
def workspace(client: TestClient, auth_headers: dict[str, str]):
    """Spawn a workspace, yield it, then destroy on teardown."""
    resp = client.post(
        "/v1/workspaces/spawn",
        json={"agent_id": "test-agent-001"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    ws_id = data["workspace_id"]

    yield data

    # Teardown: destroy (ignore errors if already destroyed by test)
    client.delete(
        f"/v1/workspaces/{ws_id}",
        headers=auth_headers,
    )
