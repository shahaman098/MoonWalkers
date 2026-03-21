#!/usr/bin/env bash
# =============================================================================
# SwarmOS GKE Autopilot Cluster Setup
#
# Creates the GKE cluster, service accounts, Artifact Registry,
# IAM bindings, and billing alerts for the SwarmOS backend.
#
# Usage: bash infra/gke/cluster-setup.sh
# =============================================================================
set -euo pipefail

PROJECT_ID="gdm-hacks2603-lon-2812"
PROJECT_NUMBER="980802873588"
REGION="europe-west2"
CLUSTER_NAME="swarm-os-cluster"
AR_REPO="swarm-os"

echo "============================================="
echo "SwarmOS GKE Cluster Setup"
echo "Project:  ${PROJECT_ID}"
echo "Region:   ${REGION}"
echo "Cluster:  ${CLUSTER_NAME}"
echo "============================================="

# ---------------------------------------------------------------------------
# 0. Set project
# ---------------------------------------------------------------------------
gcloud config set project "$PROJECT_ID"

# ---------------------------------------------------------------------------
# 1. Enable required APIs
# ---------------------------------------------------------------------------
echo "[1/7] Enabling GCP APIs..."
gcloud services enable \
    container.googleapis.com \
    artifactregistry.googleapis.com \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    billingbudgets.googleapis.com \
    --quiet

# ---------------------------------------------------------------------------
# 2. Create Artifact Registry repository
# ---------------------------------------------------------------------------
echo "[2/7] Creating Artifact Registry repo '${AR_REPO}'..."
gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --description="SwarmOS container images" \
    --quiet 2>/dev/null || echo "  (repo already exists)"

# ---------------------------------------------------------------------------
# 3. Create service accounts
# ---------------------------------------------------------------------------
echo "[3/7] Creating service accounts..."

# API service account (used by Cloud Run)
gcloud iam service-accounts create swarm-api \
    --display-name="SwarmOS API Service Account" \
    --quiet 2>/dev/null || echo "  (swarm-api SA already exists)"

# Worker service account (used by GKE agent pods)
gcloud iam service-accounts create swarm-worker \
    --display-name="SwarmOS Worker Service Account" \
    --quiet 2>/dev/null || echo "  (swarm-worker SA already exists)"

# ---------------------------------------------------------------------------
# 4. IAM role bindings
# ---------------------------------------------------------------------------
echo "[4/7] Setting IAM bindings..."

SA_API="swarm-api@${PROJECT_ID}.iam.gserviceaccount.com"
SA_WORKER="swarm-worker@${PROJECT_ID}.iam.gserviceaccount.com"

# API SA needs: run containers, manage GKE workloads, read AR
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_API}" \
    --role="roles/container.developer" \
    --quiet

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_API}" \
    --role="roles/artifactregistry.reader" \
    --quiet

# Worker SA needs: pull images, write logs
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_WORKER}" \
    --role="roles/artifactregistry.reader" \
    --quiet

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_WORKER}" \
    --role="roles/logging.logWriter" \
    --quiet

# ---------------------------------------------------------------------------
# 5. Create GKE Autopilot cluster
# ---------------------------------------------------------------------------
echo "[5/7] Creating GKE Autopilot cluster '${CLUSTER_NAME}'..."
gcloud container clusters create-auto "$CLUSTER_NAME" \
    --region="$REGION" \
    --release-channel=rapid \
    --enable-private-nodes \
    --quiet 2>/dev/null || echo "  (cluster already exists)"

# Get credentials
gcloud container clusters get-credentials "$CLUSTER_NAME" \
    --region="$REGION" --quiet

# ---------------------------------------------------------------------------
# 6. Create Kubernetes service account + Workload Identity binding
# ---------------------------------------------------------------------------
echo "[6/7] Setting up Workload Identity..."

kubectl create serviceaccount swarm-worker-ksa \
    --namespace=default 2>/dev/null || echo "  (KSA already exists)"

gcloud iam service-accounts add-iam-policy-binding "${SA_WORKER}" \
    --role="roles/iam.workloadIdentityUser" \
    --member="serviceAccount:${PROJECT_ID}.svc.id.goog[default/swarm-worker-ksa]" \
    --quiet

kubectl annotate serviceaccount swarm-worker-ksa \
    --namespace=default \
    "iam.gke.io/gcp-service-account=${SA_WORKER}" \
    --overwrite

# NOTE: swarm-secrets is intentionally NOT created here.
# The API key is generated and synced by deploy.sh so Cloud Run and GKE
# always share the same key. Run deploy.sh after this script.

# ---------------------------------------------------------------------------
# 7. Set billing budget alert
# ---------------------------------------------------------------------------
echo "[7/7] Billing alert..."
echo "  NOTE: Billing budgets require the billing account ID."
echo "  Apply infra/billing/budget.yaml via gcloud or the Console."
echo "  Monthly budget: \$50 with alerts at 50%, 80%, 100%."

echo ""
echo "============================================="
echo "Setup complete!"
echo ""
echo "Cluster:    ${CLUSTER_NAME}"
echo "API SA:     ${SA_API}"
echo "Worker SA:  ${SA_WORKER}"
echo "AR Repo:    ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"
echo ""
echo "Next: run deploy.sh to build images, deploy Cloud Run, and sync the API key to this cluster."
echo "============================================="
