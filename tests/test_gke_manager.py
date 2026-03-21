"""Unit tests for GKEWorkspaceManager using mocks.

Does NOT import real kubernetes — uses sys.modules mocking to inject a
stub kubernetes package, and object.__new__ to bypass __init__.
"""

from __future__ import annotations

import asyncio
import sys
import types
from threading import Lock
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Inject a stub 'kubernetes' module before importing app.gke_manager
# ---------------------------------------------------------------------------

def _install_kubernetes_stub():
    """Create a minimal fake kubernetes package in sys.modules."""
    kube = types.ModuleType("kubernetes")
    kube_client = types.ModuleType("kubernetes.client")
    kube_stream_mod = types.ModuleType("kubernetes.stream")
    kube_config = types.ModuleType("kubernetes.client.rest")

    # kubernetes.client.CoreV1Api stub
    kube_client.CoreV1Api = MagicMock
    kube_client.Configuration = MagicMock

    # kubernetes.stream.stream — will be overridden per-test
    kube_stream_mod.stream = MagicMock()

    # kubernetes.client.rest.ApiException
    class ApiException(Exception):
        def __init__(self, status=None, reason=None):
            self.status = status
            self.reason = reason
    kube_client_rest = types.ModuleType("kubernetes.client.rest")
    kube_client_rest.ApiException = ApiException

    kube.client = kube_client
    kube.stream = kube_stream_mod
    kube.config = types.ModuleType("kubernetes.config")
    kube.config.load_incluster_config = MagicMock(side_effect=Exception("no incluster"))
    kube.config.load_kube_config = MagicMock(side_effect=Exception("no kubeconfig"))

    sys.modules.setdefault("kubernetes", kube)
    sys.modules.setdefault("kubernetes.client", kube_client)
    sys.modules.setdefault("kubernetes.stream", kube_stream_mod)
    sys.modules.setdefault("kubernetes.config", kube.config)
    sys.modules.setdefault("kubernetes.client.rest", kube_client_rest)

    return kube_stream_mod


_kube_stream_mod = _install_kubernetes_stub()


# ---------------------------------------------------------------------------
# Fake WSClient
# ---------------------------------------------------------------------------

class FakeWSClient:
    def __init__(self, stdout="", returncode=0):
        self._stdout = stdout
        self._done = False
        self.returncode = returncode

    def is_open(self):
        return not self._done

    def update(self, timeout=1):
        self._done = True

    def peek_stdout(self):
        return bool(self._stdout)

    def read_stdout(self):
        s, self._stdout = self._stdout, ""
        return s

    def peek_stderr(self):
        return False

    def read_stderr(self):
        return ""

    def close(self):
        self._done = True


class NeverEndingWSClient(FakeWSClient):
    """A client that never closes — used to simulate timeout."""

    def is_open(self):
        return True

    def update(self, timeout=1):
        pass  # Never finishes


# ---------------------------------------------------------------------------
# Helper: build a minimal GKEWorkspaceManager without calling __init__
# ---------------------------------------------------------------------------

def _make_gke_manager():
    """Instantiate GKEWorkspaceManager without touching kubernetes."""
    from app.gke_manager import GKEWorkspaceManager, Workspace

    mgr = object.__new__(GKEWorkspaceManager)
    mgr._workspaces = {}
    mgr._lock = Lock()
    mgr._core = MagicMock()
    mgr._pool = asyncio.Queue()
    return mgr, Workspace


def _make_workspace(Workspace, ws_id="ws_123456789"):
    return Workspace(
        workspace_id=ws_id,
        agent_id="test-agent",
        environment_type="dev",
        tools_preloaded=[],
        auto_kill_timeout_minutes=30,
        directory=ws_id,
        pod_name=ws_id,
        service_name=f"svc-{ws_id}",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_gke_generate_id_uses_underscore():
    mgr, _ = _make_gke_manager()
    ws_id = mgr._generate_id()
    assert ws_id.startswith("ws_"), (
        f"Expected 'ws_' prefix but got: {ws_id!r}"
    )


def test_gke_resource_names_are_dns_safe():
    mgr, _ = _make_gke_manager()
    ws_id = "ws_123456789"
    assert mgr._resource_name(ws_id) == "ws-123456789"
    assert mgr._service_name(ws_id) == "svc-ws-123456789"

    pod = mgr._pod_manifest(ws_id, "agent-1", "dev", ["git"], 30)
    service = mgr._service_manifest(ws_id)
    assert pod["metadata"]["name"] == "ws-123456789"
    assert pod["metadata"]["labels"]["workspace_id"] == ws_id
    assert service["metadata"]["name"] == "svc-ws-123456789"
    assert service["spec"]["selector"]["workspace_id"] == ws_id


def test_gke_get_workspace_rehydrates_from_cluster_without_cache():
    mgr, _ = _make_gke_manager()
    pod = types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name="ws-123456789",
            labels={"workspace_id": "ws_123456789", "agent_id": "agent-1"},
            annotations={
                "swarm-os/agent_id": "agent-1",
                "swarm-os/environment_type": "dev",
                "swarm-os/tools_preloaded": '["git"]',
                "swarm-os/auto_kill_timeout_minutes": "15",
            },
            creation_timestamp=None,
        ),
        status=types.SimpleNamespace(phase="Running"),
    )
    service = types.SimpleNamespace(
        status=types.SimpleNamespace(
            load_balancer=types.SimpleNamespace(
                ingress=[types.SimpleNamespace(ip="34.1.2.3")]
            )
        )
    )
    mgr._core.read_namespaced_pod.return_value = pod
    mgr._core.read_namespaced_service.return_value = service

    ws = mgr.get_workspace("ws_123456789")

    assert ws is not None
    assert ws.workspace_id == "ws_123456789"
    assert ws.pod_name == "ws-123456789"
    assert ws.service_name == "svc-ws-123456789"
    assert ws.tools_preloaded == ["git"]
    assert ws.stream_url == "ws://34.1.2.3:6080"


def test_gke_exec_returns_real_exit_code():
    """A WSClient with returncode=1 should give ExecResult.exit_code == 1."""
    mgr, Workspace = _make_gke_manager()
    ws = _make_workspace(Workspace)
    mgr._workspaces[ws.workspace_id] = ws

    fake_resp = FakeWSClient(stdout="some output", returncode=1)
    _kube_stream_mod.stream = MagicMock(return_value=fake_resp)

    result = asyncio.run(mgr.execute_command(
        workspace_id=ws.workspace_id,
        command="false",
        timeout_seconds=5,
    ))

    assert result.exit_code == 1, (
        f"Expected exit_code=1 but got {result.exit_code}"
    )
    assert result.stdout == "some output"


def test_gke_exec_success_exit_code():
    """A WSClient with returncode=0 should give ExecResult.exit_code == 0."""
    mgr, Workspace = _make_gke_manager()
    ws = _make_workspace(Workspace)
    mgr._workspaces[ws.workspace_id] = ws

    fake_resp = FakeWSClient(stdout="hello", returncode=0)
    _kube_stream_mod.stream = MagicMock(return_value=fake_resp)

    result = asyncio.run(mgr.execute_command(
        workspace_id=ws.workspace_id,
        command="echo hello",
        timeout_seconds=5,
    ))

    assert result.exit_code == 0, (
        f"Expected exit_code=0 but got {result.exit_code}"
    )
    assert result.stdout == "hello"


def test_warm_pool_claim_returns_instantly():
    """spawn_workspace claims a pre-booted pod from the pool immediately."""
    mgr, Workspace = _make_gke_manager()

    # Put a ready pod into the pool
    ready_pod = _make_workspace(Workspace, "ws_pool_001")
    ready_pod.status = "ready"
    ready_pod.stream_url = "ws://1.2.3.4:6080"
    asyncio.run(mgr._pool.put(ready_pod))

    async def _spawn():
        # Patch _refill_pool so the background task doesn't try to hit GKE
        async def _noop_refill():
            pass
        mgr._refill_pool = _noop_refill

        # Also need a running loop for create_task inside spawn_workspace
        ws = await mgr.spawn_workspace(agent_id="test-agent", auto_kill_timeout_minutes=30)
        # Cancel the kill task so the test event loop can exit cleanly
        if ws._kill_task:
            ws._kill_task.cancel()
        return ws

    ws = asyncio.run(_spawn())

    assert ws.workspace_id == "ws_pool_001"
    assert ws.status == "running"
    assert ws.agent_id == "test-agent"
    assert ws.stream_url == "ws://1.2.3.4:6080"
    assert ws.workspace_id in mgr._workspaces


def test_warm_pool_empty_falls_back_to_direct_spawn():
    """spawn_workspace falls back to direct pod creation when pool is empty."""
    mgr, Workspace = _make_gke_manager()
    # Pool is empty — no pre-booted pods

    # Mock the kubernetes calls that direct spawn needs
    mgr._core.create_namespaced_pod = MagicMock()
    mgr._core.create_namespaced_service = MagicMock()

    async def _spawn():
        # Patch _resolve_ip background task to be a no-op
        original_create_task = asyncio.get_event_loop().create_task

        async def _noop_resolve():
            pass

        ws = await mgr.spawn_workspace(agent_id="fallback-agent", auto_kill_timeout_minutes=5)
        if ws._kill_task:
            ws._kill_task.cancel()
        return ws

    ws = asyncio.run(_spawn())

    assert ws.workspace_id.startswith("ws_")
    assert ws.agent_id == "fallback-agent"
    # A fresh pod must have been created (warm-pool claim skips these calls)
    mgr._core.create_namespaced_pod.assert_called_once()
    mgr._core.create_namespaced_service.assert_called_once()


def test_warm_pool_claim_resets_created_at():
    """Claimed pod gets a fresh created_at so cost accounting is correct."""
    import time as _time
    mgr, Workspace = _make_gke_manager()

    old_pod = _make_workspace(Workspace, "ws_pool_002")
    old_pod.status = "ready"
    old_pod.created_at = _time.time() - 3600   # 1 hour ago (idle pod)
    asyncio.run(mgr._pool.put(old_pod))

    before = _time.time()

    async def _spawn():
        async def _noop_refill(): pass
        mgr._refill_pool = _noop_refill
        ws = await mgr.spawn_workspace(agent_id="agent-x", auto_kill_timeout_minutes=10)
        if ws._kill_task:
            ws._kill_task.cancel()
        return ws

    ws = asyncio.run(_spawn())
    assert ws.created_at >= before, "created_at should be reset to now, not the idle pod's birth time"


def test_gke_exec_timeout():
    """A client that never closes should cause exit_code=124."""
    mgr, Workspace = _make_gke_manager()
    ws = _make_workspace(Workspace)
    mgr._workspaces[ws.workspace_id] = ws

    fake_resp = NeverEndingWSClient()
    _kube_stream_mod.stream = MagicMock(return_value=fake_resp)

    result = asyncio.run(mgr.execute_command(
        workspace_id=ws.workspace_id,
        command="sleep 9999",
        timeout_seconds=1,  # very short — will time out immediately
    ))

    assert result.exit_code == 124, (
        f"Expected exit_code=124 (timeout) but got {result.exit_code}"
    )
    assert "timed out" in result.stderr.lower()
