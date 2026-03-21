# SwarmOS Deployment Checklist

## YOUR DECISIONS (Local Lead)

### 1. Generate Your API Key
Run this once and paste the output into `.env`:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```
**Value:** `SWARM_API_KEY = ____________________`

### 2. File Sync Scope (MVP Decision)
Choose one:
- [ ] **Listing-only** (current): file_sync only lists/snapshots files, no upload/download
- [ ] **Full sync** (future): add real upload/download/sync semantics

**Decision:** _____________________

### 3. Command Blacklist Strictness
Current strict list: `rm, shutdown, reboot, poweroff, halt, mkfs, fdisk, dd`
- [ ] Keep strict (safer)
- [ ] Relax (add exceptions or allowlist instead)

**Decision:** _____________________

### 4. Stream Placeholder Okay for MVP?
- [ ] Yes, we'll wire real WebRTC/KasmVNC later
- [ ] No, need real streaming now

**Decision:** _____________________

---

## SEND THIS TO YOUR TEAMMATE (Cloud Lead)

### 5. GCP Infrastructure Values

**Required Before Deployment:**

- [ ] GCP Region (e.g., `europe-west1`, `us-central1`, `asia-east1`)
  - **Value:** `_____________________`

- [ ] Cloud Run service account email (format: `svc-cloudrun@PROJECT_ID.iam.gserviceaccount.com`)
  - **Value:** `_____________________`

- [ ] Worker service account email (format: `svc-worker@PROJECT_ID.iam.gserviceaccount.com`)
  - **Value:** `_____________________`

- [ ] Artifact Registry paths (format: `REGION-docker.pkg.dev/PROJECT_ID/REPO/IMAGE:TAG`)
  - **Backend API image:** `_____________________`
  - **Agent runtime image:** `_____________________`

- [ ] Final DNS/subdomains you own
  - **API base:** `_____________________` (e.g., `api.swarm-os.example.com`)
  - **Stream WS:** `_____________________` (e.g., `stream.swarm-os.example.com`)
  - **File sync WS:** `_____________________` (e.g., `sync.swarm-os.example.com`)

- [ ] Worker runtime platform
  - [ ] **GKE Autopilot** (managed, easier)
  - [ ] **Compute Engine Spot VMs** (cheaper, riskier)

- [ ] Visual streaming stack
  - [ ] **WebRTC** (low latency)
  - [ ] **KasmVNC** (simpler setup)

- [ ] Network allowlist
  - Outbound domains agents can reach (e.g., `github.com, npm.org, pypi.org, ...`)
  - **List:** `_____________________`

- [ ] Billing alerts
  - Daily budget cap: `$_____`
  - Alert threshold: `$_____` (e.g., 80% of daily budget)
  - **Values:** `_____________________`

### 6. Shared Decisions

**Environment Types** (work together on these):
- [ ] What does `ubuntu_python_node` include?
  - Tools: `_____________________`
  - Image: `_____________________`

- [ ] What does `ubuntu_browser` include?
  - Tools: `_____________________`
  - Image: `_____________________`

- [ ] Other environment types? List them:
  - `_____________________`

**Cost Tracking:**
- [ ] Use real GCP cost API (more accurate)
- [ ] Use estimated formula: $0.085714/hour (current)

**Workspace Kill Behavior:**
What happens if a workspace is killed while an agent is mid-exec?
- **Decision:** `_____________________`

---

## NEXT STEPS

1. **You**: Fill out your decisions above (API key, file_sync scope, etc.)
2. **Send to teammate**: The "SEND THIS TO YOUR TEAMMATE" section
3. **Together**: Discuss the "Shared Decisions" section
4. **Tell me**: Once you have all values, I'll update:
   - `app/config.py` with real values
   - `infra/cloudrun/service.yaml` with GCP project and region
   - `infra/gke/agent-pod.yaml` with service accounts and images
   - `.env.example` with what needs to be set at runtime
