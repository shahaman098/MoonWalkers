"""Workspace lifecycle management.

Handles spawning, executing commands in, and destroying workspaces.
Each workspace gets its own virtual desktop (Xvfb + fluxbox + x11vnc +
websockify) so agents can run GUI apps and viewers can watch live via
noVNC over the API WebSocket.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import shutil
import signal
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional

from app import config

logger = logging.getLogger("swarm.workspace")

# Display numbers start at 10 to avoid collision with any host display.
_next_display = 10
_display_lock = Lock()

def _alloc_display() -> int:
    global _next_display
    with _display_lock:
        d = _next_display
        _next_display += 1
        return d


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
    directory: str
    created_at: float = field(default_factory=time.time)
    status: str = "booting"
    display_num: int = 0
    vnc_port: int = 0
    vnc_ws_port: int = 0
    _kill_task: Optional[asyncio.Task] = field(default=None, repr=False)
    _activity_subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)
    _desktop_procs: list = field(default_factory=list, repr=False)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    execution_time_ms: int


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
class WorkspaceManager:
    """Thread-safe workspace tracker with async auto-kill."""

    def __init__(self) -> None:
        self._workspaces: dict[str, Workspace] = {}
        self._lock = Lock()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _generate_id() -> str:
        return f"ws_{random.randint(100_000_000, 999_999_999)}"

    @staticmethod
    def _resolve_local_path(ws_dir: str, container_path: str) -> str:
        """Map a container-side path to the local workspace directory."""
        CONTAINER_ROOT = "/home/agent/workspace"
        normalized_container = os.path.normpath(container_path)
        if normalized_container != CONTAINER_ROOT and not normalized_container.startswith(
            f"{CONTAINER_ROOT}/"
        ):
            raise ValueError(
                f"Path must stay within {CONTAINER_ROOT}: {container_path!r}"
            )
        rel = os.path.relpath(normalized_container, CONTAINER_ROOT)
        local_path = ws_dir if rel == "." else os.path.normpath(
            os.path.join(ws_dir, rel)
        )
        if os.path.commonpath([ws_dir, local_path]) != ws_dir:
            raise ValueError(f"Path escapes workspace root: {container_path!r}")
        return local_path

    @staticmethod
    def _is_blocked(command: str) -> bool:
        """Return True if *command* matches any entry on the blocklist."""
        cmd_lower = command.lower().strip()
        for blocked in config.BLOCKED_COMMANDS:
            if blocked.lower() in cmd_lower:
                return True
        return False

    # -- auto-kill ---------------------------------------------------------

    async def _auto_kill(self, workspace_id: str, timeout_minutes: int) -> None:
        """Background coroutine that destroys a workspace after *timeout_minutes*.

        This is THE most critical safety feature.  Even if the rest of the
        code is buggy, this must fire.
        """
        total_seconds = timeout_minutes * 60
        warn_5 = max(total_seconds - 300, 0)
        warn_1 = max(total_seconds - 60, 0)

        try:
            if warn_5 > 0:
                await asyncio.sleep(warn_5)
                logger.warning(
                    "Workspace %s: 5 minutes until auto-kill", workspace_id
                )
            remaining_to_1 = warn_1 - warn_5
            if remaining_to_1 > 0:
                await asyncio.sleep(remaining_to_1)
            logger.warning(
                "Workspace %s: 1 minute until auto-kill", workspace_id
            )
            remaining = total_seconds - warn_1
            if remaining > 0:
                await asyncio.sleep(remaining)

            logger.critical(
                "AUTO-KILL triggered for workspace %s after %d minutes",
                workspace_id,
                timeout_minutes,
            )
            await self.destroy_workspace(workspace_id)
        except asyncio.CancelledError:
            logger.info("Auto-kill cancelled for workspace %s", workspace_id)

    # -- activity stream ---------------------------------------------------

    def subscribe(self, workspace_id: str) -> asyncio.Queue:
        """Return a new queue that receives real-time activity events."""
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise KeyError(f"Workspace {workspace_id} not found")
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        ws._activity_subscribers.append(q)
        return q

    def unsubscribe(self, workspace_id: str, q: asyncio.Queue) -> None:
        ws = self.get_workspace(workspace_id)
        if ws is not None:
            try:
                ws._activity_subscribers.remove(q)
            except ValueError:
                pass

    def _broadcast(self, workspace_id: str, event: dict) -> None:
        """Push an event to all subscribers. Drop if queue is full."""
        ws = self.get_workspace(workspace_id)
        if ws is None:
            return
        dead: list[asyncio.Queue] = []
        for q in ws._activity_subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            try:
                ws._activity_subscribers.remove(q)
            except ValueError:
                pass

    # -- public API --------------------------------------------------------

    def _start_desktop(self, ws: Workspace) -> None:
        """Start a virtual desktop stack for the workspace.

        Launches Xvfb, fluxbox, x11vnc, and websockify. If any binary is
        missing (e.g. running tests on macOS), the workspace still works
        but without a screen.
        """
        display_num = _alloc_display()
        vnc_port = 5900 + display_num
        vnc_ws_port = 6080 + display_num
        ws.display_num = display_num
        ws.vnc_port = vnc_port
        ws.vnc_ws_port = vnc_ws_port

        display = f":{display_num}"
        devnull = subprocess.DEVNULL

        try:
            # 1. Xvfb — virtual framebuffer
            xvfb = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1280x720x24",
                 "-ac", "+extension", "GLX", "+render", "-noreset"],
                stdout=devnull, stderr=devnull,
            )
            ws._desktop_procs.append(xvfb)
            time.sleep(0.5)  # let Xvfb initialise

            env = {**os.environ, "DISPLAY": display}

            # 2. fluxbox — lightweight window manager
            fluxbox = subprocess.Popen(
                ["fluxbox"],
                stdout=devnull, stderr=devnull, env=env,
            )
            ws._desktop_procs.append(fluxbox)

            # 3. x11vnc — captures the display
            x11vnc = subprocess.Popen(
                ["x11vnc", "-display", display, "-forever", "-nopw",
                 "-shared", "-rfbport", str(vnc_port), "-quiet"],
                stdout=devnull, stderr=devnull,
            )
            ws._desktop_procs.append(x11vnc)
            time.sleep(0.3)  # let VNC bind

            # 4. websockify — WebSocket proxy to VNC
            websockify = subprocess.Popen(
                ["websockify", "--web", "/usr/share/novnc",
                 str(vnc_ws_port), f"localhost:{vnc_port}"],
                stdout=devnull, stderr=devnull,
            )
            ws._desktop_procs.append(websockify)

            logger.info(
                "Desktop started for %s — display=%s vnc=:%d ws=:%d",
                ws.workspace_id, display, vnc_port, vnc_ws_port,
            )
        except FileNotFoundError as exc:
            logger.warning(
                "Desktop binaries not available (%s) — workspace %s "
                "will run without a screen",
                exc.filename, ws.workspace_id,
            )

    def _stop_desktop(self, ws: Workspace) -> None:
        """Kill all desktop processes for a workspace."""
        for proc in reversed(ws._desktop_procs):
            try:
                proc.terminate()
            except OSError:
                pass
        # Give them a moment then force-kill
        for proc in reversed(ws._desktop_procs):
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
        ws._desktop_procs.clear()

    async def spawn_workspace(
        self,
        agent_id: str,
        environment_type: str = "dev",
        tools_preloaded: list[str] | None = None,
        auto_kill_timeout_minutes: int | None = None,
    ) -> Workspace:
        """Create a new workspace with a virtual desktop and start the auto-kill timer."""
        if tools_preloaded is None:
            tools_preloaded = []
        if auto_kill_timeout_minutes is None:
            auto_kill_timeout_minutes = config.AUTO_KILL_DEFAULT

        ws_id = self._generate_id()
        ws_dir = os.path.join(config.WORKSPACE_ROOT, ws_id)
        os.makedirs(ws_dir, exist_ok=True)

        ws = Workspace(
            workspace_id=ws_id,
            agent_id=agent_id,
            environment_type=environment_type,
            tools_preloaded=tools_preloaded,
            auto_kill_timeout_minutes=auto_kill_timeout_minutes,
            directory=ws_dir,
        )

        # Start virtual desktop (Xvfb + fluxbox + x11vnc + websockify)
        self._start_desktop(ws)

        # Start auto-kill timer (critical safety net)
        loop = asyncio.get_running_loop()
        ws._kill_task = loop.create_task(
            self._auto_kill(ws_id, auto_kill_timeout_minutes)
        )

        with self._lock:
            self._workspaces[ws_id] = ws

        ws.status = "running"
        logger.info(
            "Spawned workspace %s for agent %s (display=:%d, auto-kill in %d min)",
            ws_id,
            agent_id,
            ws.display_num,
            auto_kill_timeout_minutes,
        )
        return ws

    async def execute_command(
        self,
        workspace_id: str,
        command: str,
        args: list[str] | None = None,
        working_directory: str = "/home/agent/workspace",
        timeout_seconds: int | None = None,
    ) -> ExecResult:
        """Run a command inside a workspace via subprocess.

        Broadcasts real-time activity events to any connected stream
        subscribers so viewers can watch agent terminal output live.
        """
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise KeyError(f"Workspace {workspace_id} not found")

        if self._is_blocked(command):
            self._broadcast(workspace_id, {
                "type": "command_blocked",
                "command": command,
                "timestamp": time.time(),
            })
            return ExecResult(
                exit_code=126,
                stdout="",
                stderr=f"Blocked: command matches blocklist entry. "
                       f"Refusing to execute: {command!r}",
                execution_time_ms=0,
            )

        if timeout_seconds is None:
            timeout_seconds = config.EXEC_TIMEOUT

        try:
            cwd = self._resolve_local_path(ws.directory, working_directory)
        except ValueError as exc:
            return ExecResult(
                exit_code=1,
                stdout="",
                stderr=str(exc),
                execution_time_ms=0,
            )
        if not os.path.isdir(cwd):
            return ExecResult(
                exit_code=1,
                stdout="",
                stderr=f"Working directory not found: {working_directory}",
                execution_time_ms=0,
            )

        full_cmd = command
        if args:
            full_cmd = f"{command} {' '.join(args)}"

        # Broadcast: command starting
        self._broadcast(workspace_id, {
            "type": "command_start",
            "command": full_cmd,
            "working_directory": working_directory,
            "timestamp": time.time(),
        })

        start = time.monotonic()
        try:
            # Set DISPLAY so GUI apps render on this workspace's virtual screen
            env = {**os.environ, "DISPLAY": f":{ws.display_num}"}
            proc = await asyncio.create_subprocess_shell(
                full_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            # Stream stdout/stderr line-by-line in real time
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            async def _read_stream(
                stream: asyncio.StreamReader, dest: list[str], channel: str
            ) -> None:
                async for raw_line in stream:
                    line = raw_line.decode(errors="replace")
                    dest.append(line)
                    self._broadcast(workspace_id, {
                        "type": "command_output",
                        "channel": channel,
                        "line": line.rstrip("\n"),
                        "timestamp": time.time(),
                    })

            await asyncio.wait_for(
                asyncio.gather(
                    _read_stream(proc.stdout, stdout_lines, "stdout"),
                    _read_stream(proc.stderr, stderr_lines, "stderr"),
                    proc.wait(),
                ),
                timeout=timeout_seconds,
            )

            elapsed_ms = int((time.monotonic() - start) * 1000)
            stdout_text = "".join(stdout_lines)
            stderr_text = "".join(stderr_lines)

            self._broadcast(workspace_id, {
                "type": "command_end",
                "exit_code": proc.returncode or 0,
                "execution_time_ms": elapsed_ms,
                "timestamp": time.time(),
            })

            return ExecResult(
                exit_code=proc.returncode or 0,
                stdout=stdout_text,
                stderr=stderr_text,
                execution_time_ms=elapsed_ms,
            )
        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self._broadcast(workspace_id, {
                "type": "command_end",
                "exit_code": 124,
                "execution_time_ms": elapsed_ms,
                "timed_out": True,
                "timestamp": time.time(),
            })
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"Command timed out after {timeout_seconds}s",
                execution_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self._broadcast(workspace_id, {
                "type": "command_end",
                "exit_code": 1,
                "execution_time_ms": elapsed_ms,
                "error": str(exc),
                "timestamp": time.time(),
            })
            return ExecResult(
                exit_code=1,
                stdout="",
                stderr=str(exc),
                execution_time_ms=elapsed_ms,
            )

    async def destroy_workspace(self, workspace_id: str) -> dict:
        """Tear down a workspace, kill desktop processes, and return cost summary."""
        with self._lock:
            ws = self._workspaces.pop(workspace_id, None)

        if ws is None:
            raise KeyError(f"Workspace {workspace_id} not found")

        # Cancel auto-kill timer
        if ws._kill_task and not ws._kill_task.done():
            ws._kill_task.cancel()

        # Stop virtual desktop
        self._stop_desktop(ws)

        uptime_seconds = time.time() - ws.created_at
        uptime_minutes = round(uptime_seconds / 60, 2)
        cost = round((uptime_seconds / 3600) * config.COST_PER_HOUR, 6)

        # Clean up filesystem
        if os.path.isdir(ws.directory):
            shutil.rmtree(ws.directory, ignore_errors=True)

        logger.info(
            "Destroyed workspace %s (uptime=%.1fm, cost=$%.4f)",
            workspace_id,
            uptime_minutes,
            cost,
        )
        return {
            "workspace_id": workspace_id,
            "status": "destroyed",
            "uptime_minutes": uptime_minutes,
            "cost_incurred_usd": cost,
        }

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        with self._lock:
            return self._workspaces.get(workspace_id)

    async def start_warm_pool(self) -> None:
        """No-op for local mode — spawns are instant."""
        pass

    def list_workspaces(self) -> list[Workspace]:
        with self._lock:
            return list(self._workspaces.values())

    def download_workspace_bytes(
        self, workspace_id: str, path: str = "/home/agent/workspace"
    ) -> bytes:
        """Zip the local workspace directory and return raw bytes."""
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise KeyError(f"Workspace {workspace_id} not found")
        try:
            target_dir = self._resolve_local_path(ws.directory, path)
        except ValueError as exc:
            raise FileNotFoundError(str(exc)) from exc
        if not os.path.isdir(target_dir):
            raise FileNotFoundError(f"Directory not found: {target_dir}")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(target_dir):
                for fname in files:
                    full = os.path.join(root, fname)
                    arcname = os.path.relpath(full, target_dir)
                    zf.write(full, arcname)
        buf.seek(0)
        return buf.read()

    def list_workspace_files(self, workspace_id: str) -> list[dict]:
        """Walk the local workspace and return list of {path, size, modified}."""
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise KeyError(f"Workspace {workspace_id} not found")
        directory = ws.directory
        entries: list[dict] = []
        if not os.path.isdir(directory):
            return entries
        for root, _dirs, files in os.walk(directory):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, directory)
                try:
                    st = os.stat(full)
                    entries.append({
                        "path": rel,
                        "size": st.st_size,
                        "modified": st.st_mtime,
                    })
                except OSError:
                    pass
        return entries
