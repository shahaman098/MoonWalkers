"""GKE-backed workspace manager.

When GKE_MODE=true this replaces the subprocess WorkspaceManager.
Each workspace = one GKE Pod + one LoadBalancer Service (noVNC on port 6080).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import random
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional

from app import config

logger = logging.getLogger("swarm.gke")


# ---------------------------------------------------------------------------
# Kubernetes client init
# ---------------------------------------------------------------------------

def _init_kube() -> None:
    """Configure kubernetes client using ambient GCP credentials."""
    try:
        from kubernetes import config as k8s_config
        k8s_config.load_incluster_config()
        logger.info("kubernetes: using in-cluster config")
        return
    except Exception:
        pass

    try:
        from kubernetes import config as k8s_config
        k8s_config.load_kube_config()
        logger.info("kubernetes: using kubeconfig")
        return
    except Exception:
        pass

    # Dynamically build config from GKE cluster credentials
    import google.auth
    from google.auth.transport.requests import Request
    from google.cloud import container_v1
    from kubernetes import client as k8s_client

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(Request())

    gke = container_v1.ClusterManagerClient(credentials=credentials)
    cluster = gke.get_cluster(
        name=(
            f"projects/{config.GCP_PROJECT_ID}"
            f"/locations/{config.GCP_REGION}"
            f"/clusters/{config.GKE_CLUSTER_NAME}"
        )
    )

    ca_cert = base64.b64decode(cluster.master_auth.cluster_ca_certificate)
    ca_file = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
    ca_file.write(ca_cert)
    ca_file.close()

    cfg = k8s_client.Configuration()
    cfg.host = f"https://{cluster.endpoint}"
    cfg.ssl_ca_cert = ca_file.name
    cfg.verify_ssl = True
    cfg.api_key = {"authorization": f"Bearer {credentials.token}"}
    k8s_client.Configuration.set_default(cfg)
    logger.info("kubernetes: configured via GKE cluster endpoint")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Workspace:
    workspace_id: str
    agent_id: str
    environment_type: str
    tools_preloaded: list[str]
    auto_kill_timeout_minutes: int
    directory: str          # = workspace_id in GKE mode (interface compat)
    created_at: float = field(default_factory=time.time)
    status: str = "booting"
    pod_name: str = ""
    service_name: str = ""
    stream_url: str = ""    # filled in once LoadBalancer gets an external IP
    _kill_task: Optional[asyncio.Task] = field(default=None, repr=False)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    execution_time_ms: int


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class GKEWorkspaceManager:
    """Manages agent workspaces as isolated GKE pods."""

    def __init__(self) -> None:
        _init_kube()
        from kubernetes import client as k8s_client
        self._core = k8s_client.CoreV1Api()
        self._workspaces: dict[str, Workspace] = {}
        self._lock = Lock()
        self._pool: asyncio.Queue[Workspace] = asyncio.Queue()

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _generate_id() -> str:
        return f"ws_{random.randint(100_000_000, 999_999_999)}"

    @staticmethod
    def _resource_name(workspace_id: str) -> str:
        """Map the public workspace ID to a DNS-safe Kubernetes resource name."""
        return workspace_id.replace("_", "-")

    @classmethod
    def _service_name(cls, workspace_id: str) -> str:
        return f"svc-{cls._resource_name(workspace_id)}"

    @staticmethod
    def _is_blocked(command: str) -> bool:
        cmd_lower = command.lower().strip()
        return any(b.lower() in cmd_lower for b in config.BLOCKED_COMMANDS)

    def _pod_manifest(
        self,
        ws_id: str,
        agent_id: str,
        environment_type: str,
        tools_preloaded: list[str],
        timeout_min: int,
    ) -> dict:
        pod_name = self._resource_name(ws_id)
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": config.GKE_NAMESPACE,
                "labels": {
                    "app": "swarm-agent",
                    "workspace_id": ws_id,
                    "agent_id": agent_id[:63],
                },
                "annotations": {
                    "swarm-os/agent_id": agent_id,
                    "swarm-os/environment_type": environment_type,
                    "swarm-os/tools_preloaded": json.dumps(tools_preloaded),
                    "swarm-os/auto_kill_timeout_minutes": str(timeout_min),
                },
            },
            "spec": {
                "serviceAccountName": "swarm-worker-ksa",
                "activeDeadlineSeconds": timeout_min * 60,
                "restartPolicy": "Never",
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000},
                "containers": [{
                    "name": "agent",
                    "image": config.AGENT_IMAGE,
                    "ports": [
                        {"containerPort": 8080, "name": "api"},
                        {"containerPort": 6080, "name": "novnc"},
                        {"containerPort": 5900, "name": "vnc"},
                    ],
                    "resources": {
                        "requests": {"cpu": "500m", "memory": "1Gi"},
                        "limits":   {"cpu": "2",    "memory": "4Gi"},
                    },
                    "env": [
                        {"name": "AUTO_KILL_TIMEOUT", "value": str(timeout_min * 60)},
                        {"name": "SWARM_API_KEY",     "value": config.SWARM_API_KEY},
                    ],
                    "livenessProbe": {
                        "httpGet": {"path": "/healthz", "port": 8080},
                        "initialDelaySeconds": 30,
                        "periodSeconds": 30,
                    },
                }],
            },
        }

    def _service_manifest(self, ws_id: str) -> dict:
        service_name = self._service_name(ws_id)
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": service_name,
                "namespace": config.GKE_NAMESPACE,
                "labels": {"workspace_id": ws_id},
            },
            "spec": {
                "type": "LoadBalancer",
                "selector": {"workspace_id": ws_id},
                "ports": [
                    {"name": "novnc", "port": 6080, "targetPort": 6080},
                    {"name": "api",   "port": 8080, "targetPort": 8080},
                ],
            },
        }

    @staticmethod
    def _stream_url_from_service(service) -> str:
        load_balancer = getattr(getattr(service, "status", None), "load_balancer", None)
        ingress = getattr(load_balancer, "ingress", None) or []
        if not ingress:
            return ""
        host = getattr(ingress[0], "ip", None) or getattr(ingress[0], "hostname", None)
        return f"ws://{host}:6080" if host else ""

    def _workspace_from_resources(self, pod, service=None) -> Workspace:
        metadata = pod.metadata
        labels = getattr(metadata, "labels", {}) or {}
        annotations = getattr(metadata, "annotations", {}) or {}
        workspace_id = labels.get("workspace_id") or metadata.name
        created_at_obj = getattr(metadata, "creation_timestamp", None)
        created_at = (
            created_at_obj.timestamp() if created_at_obj is not None else time.time()
        )
        raw_tools = annotations.get("swarm-os/tools_preloaded", "[]")
        try:
            tools_preloaded = json.loads(raw_tools) if isinstance(raw_tools, str) else []
            if not isinstance(tools_preloaded, list):
                tools_preloaded = []
        except json.JSONDecodeError:
            tools_preloaded = []
        timeout_raw = annotations.get("swarm-os/auto_kill_timeout_minutes", "30")
        try:
            timeout_minutes = int(timeout_raw)
        except ValueError:
            timeout_minutes = config.AUTO_KILL_DEFAULT
        phase = (getattr(getattr(pod, "status", None), "phase", "") or "").lower()
        status = "running" if phase == "running" else "booting"
        ws = Workspace(
            workspace_id=workspace_id,
            agent_id=annotations.get("swarm-os/agent_id", labels.get("agent_id", "")),
            environment_type=annotations.get(
                "swarm-os/environment_type", "ubuntu_python_node"
            ),
            tools_preloaded=tools_preloaded,
            auto_kill_timeout_minutes=timeout_minutes,
            directory=workspace_id,
            created_at=created_at,
            status=status,
            pod_name=metadata.name,
            service_name=self._service_name(workspace_id),
            stream_url=self._stream_url_from_service(service) if service else "",
        )
        return ws

    def _read_workspace_resources(self, workspace_id: str) -> Workspace | None:
        from kubernetes.client.rest import ApiException

        pod_name = self._resource_name(workspace_id)
        try:
            pod = self._core.read_namespaced_pod(pod_name, config.GKE_NAMESPACE)
        except ApiException as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise

        service = None
        service_name = self._service_name(workspace_id)
        try:
            service = self._core.read_namespaced_service(
                service_name, config.GKE_NAMESPACE
            )
        except ApiException as exc:
            if getattr(exc, "status", None) != 404:
                raise

        ws = self._workspace_from_resources(pod, service)
        with self._lock:
            cached = self._workspaces.get(workspace_id)
            if cached is not None and cached._kill_task is not None:
                ws._kill_task = cached._kill_task
            self._workspaces[workspace_id] = ws
        return ws

    async def _wait_for_service_ip(self, svc_name: str, timeout: int = 180) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            svc = self._core.read_namespaced_service(svc_name, config.GKE_NAMESPACE)
            ingress = svc.status.load_balancer.ingress
            if ingress and ingress[0].ip:
                return ingress[0].ip
            await asyncio.sleep(5)
        raise TimeoutError(f"{svc_name} did not get an external IP within {timeout}s")

    # -- auto-kill -----------------------------------------------------------

    async def _auto_kill(self, workspace_id: str, timeout_minutes: int) -> None:
        total = timeout_minutes * 60
        try:
            warn5 = max(total - 300, 0)
            if warn5:
                await asyncio.sleep(warn5)
                logger.warning("Workspace %s: 5 min until auto-kill", workspace_id)
            warn1 = max(total - 60, warn5)
            if warn1 > warn5:
                await asyncio.sleep(warn1 - warn5)
                logger.warning("Workspace %s: 1 min until auto-kill", workspace_id)
            remaining = total - warn1
            if remaining > 0:
                await asyncio.sleep(remaining)
            logger.critical("AUTO-KILL triggered for %s", workspace_id)
            await self.destroy_workspace(workspace_id)
        except asyncio.CancelledError:
            pass

    # -- warm pool -----------------------------------------------------------

    async def _wait_for_pod_running(self, pod_name: str, timeout: int = 300) -> None:
        """Poll until the pod phase is Running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pod = self._core.read_namespaced_pod(pod_name, config.GKE_NAMESPACE)
            phase = pod.status.phase if pod.status else None
            if phase == "Running":
                return
            if phase in ("Failed", "Succeeded"):
                raise RuntimeError(f"Pod {pod_name} ended with phase {phase}")
            await asyncio.sleep(5)
        raise TimeoutError(f"Pod {pod_name} did not reach Running within {timeout}s")

    async def _boot_idle_pod(self) -> Workspace:
        """Create one pod+service, wait for it to be Running and have an IP."""
        ws_id = self._generate_id()
        idle_timeout = config.WARM_POOL_IDLE_TIMEOUT_MINUTES

        self._core.create_namespaced_pod(
            body=self._pod_manifest(ws_id, "__pool_idle__", idle_timeout),
            namespace=config.GKE_NAMESPACE,
        )
        self._core.create_namespaced_service(
            body=self._service_manifest(ws_id),
            namespace=config.GKE_NAMESPACE,
        )

        ws = Workspace(
            workspace_id=ws_id,
            agent_id="__pool_idle__",
            environment_type="dev",
            tools_preloaded=[],
            auto_kill_timeout_minutes=idle_timeout,
            directory=ws_id,
            pod_name=ws_id,
            service_name=f"svc-{ws_id}",
            status="booting",
        )

        await self._wait_for_pod_running(ws_id)

        try:
            ip = await self._wait_for_service_ip(ws.service_name)
            ws.stream_url = f"ws://{ip}:6080"
        except TimeoutError:
            logger.warning("Pool pod %s: LB IP not ready, stream_url unavailable", ws_id)

        ws.status = "ready"
        logger.info("Pool pod %s ready and waiting", ws_id)
        return ws

    async def start_warm_pool(self) -> None:
        """Boot WARM_POOL_SIZE idle pods concurrently at startup."""
        n = config.WARM_POOL_SIZE
        if n <= 0:
            return
        logger.info("Warming pool: booting %d idle pods...", n)
        tasks = [asyncio.create_task(self._boot_idle_pod()) for _ in range(n)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        ready = 0
        for r in results:
            if isinstance(r, Exception):
                logger.error("Failed to boot warm pool pod: %s", r)
            else:
                await self._pool.put(r)
                ready += 1
        logger.info("Warm pool ready: %d/%d pods available", ready, n)

    async def _refill_pool(self) -> None:
        """Boot enough idle pods to bring pool back to WARM_POOL_SIZE."""
        needed = config.WARM_POOL_SIZE - self._pool.qsize()
        for _ in range(needed):
            try:
                ws = await self._boot_idle_pod()
                await self._pool.put(ws)
                logger.info("Pool refilled: %s ready", ws.workspace_id)
            except Exception as exc:
                logger.error("Failed to refill pool: %s", exc)

    # -- public API ----------------------------------------------------------

    async def spawn_workspace(
        self,
        agent_id: str,
        environment_type: str = "ubuntu_python_node",
        tools_preloaded: list[str] | None = None,
        auto_kill_timeout_minutes: int | None = None,
    ) -> Workspace:
        if tools_preloaded is None:
            tools_preloaded = []
        if auto_kill_timeout_minutes is None:
            auto_kill_timeout_minutes = config.AUTO_KILL_DEFAULT

        loop = asyncio.get_running_loop()

        # -- Fast path: claim a pre-booted pod from the warm pool ------------
        try:
            ws = self._pool.get_nowait()
            logger.info("Claimed warm pod %s for agent %s", ws.workspace_id, agent_id)

            ws.agent_id = agent_id
            ws.environment_type = environment_type
            ws.tools_preloaded = tools_preloaded
            ws.auto_kill_timeout_minutes = auto_kill_timeout_minutes
            ws.created_at = time.time()   # reset for accurate cost tracking
            ws.status = "running"

            ws._kill_task = loop.create_task(
                self._auto_kill(ws.workspace_id, auto_kill_timeout_minutes)
            )

            with self._lock:
                self._workspaces[ws.workspace_id] = ws

            # Refill pool in background so the next spawn is also fast
            loop.create_task(self._refill_pool())
            return ws

        except asyncio.QueueEmpty:
            logger.warning(
                "Warm pool empty — falling back to direct spawn for agent %s", agent_id
            )

        # -- Slow path: boot a fresh pod on demand ---------------------------
        ws_id = self._generate_id()

        self._core.create_namespaced_pod(
            body=self._pod_manifest(
                ws_id,
                agent_id,
                environment_type,
                tools_preloaded,
                auto_kill_timeout_minutes,
            ),
            namespace=config.GKE_NAMESPACE,
        )
        pod_name = self._resource_name(ws_id)
        logger.info("Created GKE pod %s for workspace %s", pod_name, ws_id)

        self._core.create_namespaced_service(
            body=self._service_manifest(ws_id),
            namespace=config.GKE_NAMESPACE,
        )
        service_name = self._service_name(ws_id)
        logger.info("Created LoadBalancer service %s", service_name)

        ws = Workspace(
            workspace_id=ws_id,
            agent_id=agent_id,
            environment_type=environment_type,
            tools_preloaded=tools_preloaded,
            auto_kill_timeout_minutes=auto_kill_timeout_minutes,
            directory=ws_id,
            pod_name=pod_name,
            service_name=service_name,
        )

        ws._kill_task = loop.create_task(
            self._auto_kill(ws_id, auto_kill_timeout_minutes)
        )

        with self._lock:
            self._workspaces[ws_id] = ws

        # Resolve external IP in background; stream_url is set when ready
        async def _resolve_ip() -> None:
            try:
                ip = await self._wait_for_service_ip(ws.service_name)
                ws.stream_url = f"ws://{ip}:6080"
                ws.status = "running"
                logger.info("Workspace %s stream ready at %s", ws_id, ws.stream_url)
            except Exception as exc:
                logger.error("Could not get IP for %s: %s", ws_id, exc)
                ws.status = "running"

        loop.create_task(_resolve_ip())
        return ws

    async def execute_command(
        self,
        workspace_id: str,
        command: str,
        args: list[str] | None = None,
        working_directory: str = "/home/agent/workspace",
        timeout_seconds: int | None = None,
    ) -> ExecResult:
        from kubernetes.stream import stream as kube_stream
        from kubernetes.client.rest import ApiException

        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise KeyError(f"Workspace {workspace_id} not found")

        if self._is_blocked(command):
            return ExecResult(
                exit_code=126, stdout="",
                stderr=f"Blocked: refusing to execute {command!r}",
                execution_time_ms=0,
            )

        if timeout_seconds is None:
            timeout_seconds = config.EXEC_TIMEOUT

        full_cmd = ["sh", "-c",
                    f"cd {working_directory} && {command} {' '.join(args or [])}"]
        start = time.monotonic()
        try:
            resp = kube_stream(
                self._core.connect_get_namespaced_pod_exec,
                ws.pod_name, config.GKE_NAMESPACE,
                command=full_cmd,
                stderr=True, stdin=False, stdout=True, tty=False,
                _preload_content=False,
            )
            stdout_parts: list[str] = []
            stderr_parts: list[str] = []
            deadline = time.monotonic() + timeout_seconds
            timed_out = False
            while resp.is_open():
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                resp.update(timeout=1)
                if resp.peek_stdout():
                    stdout_parts.append(resp.read_stdout())
                if resp.peek_stderr():
                    stderr_parts.append(resp.read_stderr())
            if timed_out:
                resp.close()
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return ExecResult(
                    exit_code=124,
                    stdout="".join(stdout_parts),
                    stderr=f"Command timed out after {timeout_seconds}s",
                    execution_time_ms=elapsed_ms,
                )
            resp.close()
            exit_code = resp.returncode if resp.returncode is not None else 0
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ExecResult(
                exit_code=exit_code,
                stdout="".join(stdout_parts),
                stderr="".join(stderr_parts),
                execution_time_ms=elapsed_ms,
            )
        except ApiException as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ExecResult(
                exit_code=1, stdout="",
                stderr=f"kubectl exec failed: {exc.reason}",
                execution_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ExecResult(exit_code=1, stdout="", stderr=str(exc),
                              execution_time_ms=elapsed_ms)

    async def destroy_workspace(self, workspace_id: str) -> dict:
        from kubernetes.client.rest import ApiException

        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise KeyError(f"Workspace {workspace_id} not found")

        with self._lock:
            cached = self._workspaces.pop(workspace_id, None)
        if cached is not None and cached._kill_task is not None:
            ws._kill_task = cached._kill_task

        if ws._kill_task and not ws._kill_task.done():
            ws._kill_task.cancel()

        uptime_seconds = time.time() - ws.created_at
        uptime_minutes = round(uptime_seconds / 60, 2)
        cost = round((uptime_seconds / 3600) * config.COST_PER_HOUR, 6)

        for fn, name in [
            (self._core.delete_namespaced_pod,     ws.pod_name),
            (self._core.delete_namespaced_service,  ws.service_name),
        ]:
            try:
                fn(name, config.GKE_NAMESPACE, grace_period_seconds=10)
            except ApiException:
                pass

        logger.info("Destroyed GKE workspace %s (uptime=%.1fm, $%.4f)",
                    workspace_id, uptime_minutes, cost)
        return {
            "workspace_id": workspace_id,
            "status": "destroyed",
            "uptime_minutes": uptime_minutes,
            "cost_incurred_usd": cost,
        }

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        with self._lock:
            cached = self._workspaces.get(workspace_id)
        if cached is not None:
            return cached
        return self._read_workspace_resources(workspace_id)

    def list_workspaces(self) -> list[Workspace]:
        pods = self._core.list_namespaced_pod(
            config.GKE_NAMESPACE,
            label_selector="app=swarm-agent",
        )
        return [self._workspace_from_resources(pod) for pod in pods.items]

    def download_workspace_bytes(
        self, workspace_id: str, path: str = "/home/agent/workspace"
    ) -> bytes:
        """Tar+gz the remote workspace path and return as zip bytes."""
        from kubernetes.stream import stream as kube_stream

        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise KeyError(f"Workspace {workspace_id} not found")

        cmd = ["sh", "-c", f"tar czf - -C {path} . | base64 -w 0"]
        resp = kube_stream(
            self._core.connect_get_namespaced_pod_exec,
            ws.pod_name, config.GKE_NAMESPACE,
            command=cmd,
            stderr=True, stdin=False, stdout=True, tty=False,
            _preload_content=False,
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                stdout_parts.append(resp.read_stdout())
            if resp.peek_stderr():
                stderr_parts.append(resp.read_stderr())
        resp.close()

        b64_data = "".join(stdout_parts).strip()
        if not b64_data:
            raise FileNotFoundError(f"No data returned from pod for path: {path}")
        tar_bytes = base64.b64decode(b64_data)

        # Convert tar.gz → zip in memory
        zip_buf = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for member in tf.getmembers():
                    if member.isfile():
                        f = tf.extractfile(member)
                        if f is not None:
                            arcname = member.name.lstrip("./")
                            zf.writestr(arcname, f.read())
        zip_buf.seek(0)
        return zip_buf.read()

    def list_workspace_files(self, workspace_id: str) -> list[dict]:
        """List files in the remote workspace pod."""
        from kubernetes.stream import stream as kube_stream

        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise KeyError(f"Workspace {workspace_id} not found")

        cmd = ["find", "/home/agent/workspace", "-type", "f",
               "-printf", r"%P\t%s\t%T@\n"]
        resp = kube_stream(
            self._core.connect_get_namespaced_pod_exec,
            ws.pod_name, config.GKE_NAMESPACE,
            command=cmd,
            stderr=True, stdin=False, stdout=True, tty=False,
            _preload_content=False,
        )
        stdout_parts: list[str] = []
        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                stdout_parts.append(resp.read_stdout())
        resp.close()

        entries: list[dict] = []
        for line in "".join(stdout_parts).splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) == 3:
                try:
                    entries.append({
                        "path": parts[0],
                        "size": int(parts[1]),
                        "modified": float(parts[2]),
                    })
                except (ValueError, IndexError):
                    pass
        return entries
