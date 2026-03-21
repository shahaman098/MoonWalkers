#!/usr/bin/env bash
# =============================================================================
# SwarmOS One-Click Deploy Script
#
# Builds images, pushes to Artifact Registry, deploys Cloud Run, and
# optionally creates the GKE cluster.
#
# Usage: bash deploy.sh
# =============================================================================
set -euo pipefail

PROJECT_ID="project-6f8d46a0-9fca-4197-96d"
REGION="europe-west2"
AR_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/swarm-os"
API_IMAGE="${AR_REPO}/api:latest"
RUNTIME_IMAGE="${AR_REPO}/agent-runtime:latest"
SERVICE_NAME="swarm-os-api"
CLUSTER_NAME="swarm-os-cluster"

echo "============================================="
echo "SwarmOS Deploy"
echo "Project:  ${PROJECT_ID}"
echo "Region:   ${REGION}"
echo "============================================="

# ---------------------------------------------------------------------------
# 0. Pre-flight checks
# ---------------------------------------------------------------------------
echo "[0/6] Checking prerequisites..."

if ! command -v gcloud &>/dev/null; then
    echo "ERROR: gcloud CLI not found. Install: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null || true)
if [ -z "$ACCOUNT" ]; then
    echo "ERROR: Not authenticated. Run: gcloud auth login"
    exit 1
fi
echo "  Authenticated as: ${ACCOUNT}"

gcloud config set project "$PROJECT_ID" --quiet

# ---------------------------------------------------------------------------
# 1. Create Artifact Registry repo (idempotent)
# ---------------------------------------------------------------------------
echo "[1/6] Ensuring Artifact Registry repo exists..."
gcloud artifacts repositories create swarm-os \
    --repository-format=docker \
    --location="$REGION" \
    --description="SwarmOS container images" \
    --quiet 2>/dev/null || echo "  (already exists)"

# Configure Docker auth for AR
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

# ---------------------------------------------------------------------------
# 2. Build and push API image
# ---------------------------------------------------------------------------
echo "[2/6] Building API image..."
docker buildx build --platform linux/amd64 -t "$API_IMAGE" -f Dockerfile.api . --push
echo "  Pushed to Artifact Registry."

# ---------------------------------------------------------------------------
# 3. Build and push agent runtime image (full desktop environment)
# ---------------------------------------------------------------------------
echo "[3/6] Building agent runtime image..."
docker buildx build --platform linux/amd64 -t "$RUNTIME_IMAGE" -f Dockerfile.agent . --push
echo "  Pushed to Artifact Registry."

# ---------------------------------------------------------------------------
# 4. Deploy Cloud Run service
# ---------------------------------------------------------------------------
echo "[4/6] Deploying Cloud Run service..."

# Generate API key (single source of truth — also synced to GKE secret below)
SWARM_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

gcloud run deploy "$SERVICE_NAME" \
    --image="$API_IMAGE" \
    --region="$REGION" \
    --platform=managed \
    --allow-unauthenticated \
    --port=8080 \
    --memory=512Mi \
    --cpu=1 \
    --min-instances=0 \
    --max-instances=10 \
    --timeout=300 \
    --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},GCP_REGION=${REGION},SWARM_API_KEY=${SWARM_KEY},GKE_MODE=false" \
    --quiet

# Get the deployed URL
API_URL=$(gcloud run services describe "$SERVICE_NAME" \
    --region="$REGION" \
    --format="value(status.url)" 2>/dev/null)

# ---------------------------------------------------------------------------
# 5. Summary (GKE skipped — running in local/subprocess mode)
# ---------------------------------------------------------------------------
echo ""
echo "============================================="
echo "Deploy complete!"
echo "============================================="
echo ""
echo "API URL:   ${API_URL:-"(check Cloud Run console)"}"
echo "API Key:   ${SWARM_KEY}"
echo ""
echo "Test it:"
echo "  curl -s ${API_URL:-https://YOUR_URL}/docs"
echo ""
echo "  curl -X POST ${API_URL:-https://YOUR_URL}/v1/workspaces/spawn \\"
echo "    -H 'Authorization: Bearer ${SWARM_KEY}' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"agent_id\": \"test-agent\"}'"
echo ""
echo "Save this API key - you will need it for the orchestrator!"
echo "============================================="
