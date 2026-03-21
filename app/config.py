"""SwarmOS GCP Backend Configuration.

All settings can be overridden via environment variables.
"""

import os
import secrets

# ---------------------------------------------------------------------------
# GCP
# ---------------------------------------------------------------------------
GCP_PROJECT_ID: str = os.getenv("GCP_PROJECT_ID", "project-6f8d46a0-9fca-4197-96d")
GCP_PROJECT_NUMBER: str = os.getenv("GCP_PROJECT_NUMBER", "1045588482905")
GCP_REGION: str = os.getenv("GCP_REGION", "europe-west2")

# ---------------------------------------------------------------------------
# Artifact Registry
# ---------------------------------------------------------------------------
ARTIFACT_REGISTRY: str = (
    f"{GCP_REGION}-docker.pkg.dev/{GCP_PROJECT_ID}/swarm-os"
)

# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------
SA_API: str = os.getenv(
    "SA_API", f"swarm-api@{GCP_PROJECT_ID}.iam.gserviceaccount.com"
)
SA_WORKER: str = os.getenv(
    "SA_WORKER", f"swarm-worker@{GCP_PROJECT_ID}.iam.gserviceaccount.com"
)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
# Generate a default token so local dev "just works".  Override in prod.
_DEFAULT_API_KEY = secrets.token_urlsafe(32)
SWARM_API_KEY: str = os.getenv("SWARM_API_KEY", _DEFAULT_API_KEY)

# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------
WORKSPACE_ROOT: str = os.getenv(
    "WORKSPACE_ROOT", "/tmp/swarm-os-gcp/workspaces"
)

# ---------------------------------------------------------------------------
# URLs  (Cloud Run auto-generated URLs)
# ---------------------------------------------------------------------------
_CLOUD_RUN_HOST = os.getenv(
    "CLOUD_RUN_HOST",
    f"swarm-os-api-{GCP_PROJECT_NUMBER}.{GCP_REGION}.run.app",
)
BASE_URL: str = os.getenv("BASE_URL", f"https://{_CLOUD_RUN_HOST}")
STREAM_URL: str = os.getenv("STREAM_URL", f"wss://{_CLOUD_RUN_HOST}")
SYNC_URL: str = os.getenv("SYNC_URL", f"wss://{_CLOUD_RUN_HOST}")

# ---------------------------------------------------------------------------
# Safety & limits
# ---------------------------------------------------------------------------
AUTO_KILL_DEFAULT: int = int(os.getenv("AUTO_KILL_DEFAULT", "30"))  # minutes
EXEC_TIMEOUT: int = int(os.getenv("EXEC_TIMEOUT", "300"))          # seconds
COST_PER_HOUR: float = float(os.getenv("COST_PER_HOUR", "0.085714"))

BLOCKED_COMMANDS: list[str] = [
    "rm -rf /",
    "shutdown",
    "reboot",
    "poweroff",
    "halt",
    "mkfs",
    "fdisk",
    "dd",
    ":(){ :|:& };:",
]

STREAM_HEARTBEAT: int = int(os.getenv("STREAM_HEARTBEAT", "5"))  # seconds
SPAWN_ESTIMATED_READY_SEC: int = int(os.getenv("SPAWN_ESTIMATED_READY_SEC", "180"))  # GKE only

# ---------------------------------------------------------------------------
# GKE mode
# ---------------------------------------------------------------------------
GKE_MODE: bool = os.getenv("GKE_MODE", "false").lower() == "true"
GKE_CLUSTER_NAME: str = os.getenv("GKE_CLUSTER_NAME", "swarm-os-cluster")
GKE_NAMESPACE: str = os.getenv("GKE_NAMESPACE", "default")
AGENT_IMAGE: str = os.getenv(
    "AGENT_IMAGE",
    f"{ARTIFACT_REGISTRY}/agent-runtime:latest",
)

# ---------------------------------------------------------------------------
# Warm pool
# ---------------------------------------------------------------------------
# Number of pods to keep pre-booted and ready to claim instantly.
WARM_POOL_SIZE: int = int(os.getenv("WARM_POOL_SIZE", "2"))
# How long an idle pool pod lives before its Kubernetes activeDeadlineSeconds
# kills it (and it is replaced). Should be >= AUTO_KILL_DEFAULT.
WARM_POOL_IDLE_TIMEOUT_MINUTES: int = int(os.getenv("WARM_POOL_IDLE_TIMEOUT_MINUTES", "60"))
