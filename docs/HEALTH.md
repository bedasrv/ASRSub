# ASRSub Health / Readiness Contract

This document defines the local health and readiness checks for the ASRSub pipeline (orchestrator + dashboard). It is the authoritative contract for deployment and monitoring.

## Endpoints

- `GET /health` — **Liveness** (unauthenticated, always open). Returns `200 {"ok": true}` if the process is running. Does not check external dependencies. Use for container liveness probes.
- `GET /ready` — **Readiness** (unauthenticated). Returns `200` if all readiness checks pass, `503` otherwise with a JSON body describing failures. Use for readiness probes and pre-deployment gating.
- `GET /status` — **Authenticated status** (`X-API-Key` or `X-Control-Key` must match the secret at `/run/secrets/control_api_key` or `CONTROL_API_KEY_FILE`; env `CONTROL_API_KEY` is test fallback only). Returns full pipeline status including `paused`, queue, and `readiness` summary. No secrets are ever returned or logged.

## Readiness Checks

All checks run from repo root or container; none require secrets in output.

### 1. Local btrfs State

- **What**: The local state directory (`/home/user/.config/asr-pipeline` or `$STATE_FILE` dirname) must be on a local btrfs filesystem, writable, with at least 100 MiB free.
- **Why**: State files (`state.jsonl`, `subtitle_registry.jsonl`, `webhook_inbox.db`) are append-only and use `os.replace` + `fsync` + file locks. They must not be on NFS or tmpfs.
- **How**:
  ```bash
  df -T /home/user/.config/asr-pipeline
  stat -f -c %T /home/user/.config/asr-pipeline  # expected: btrfs
  test -w /home/user/.config/asr-pipeline
  ```
- **Failure modes**: Not a mountpoint, wrong fstype, read-only, low space, or path under `/mnt/nas`/`/media`. Liveness stays `200`; readiness returns `503` with `checks.btrfs.ok=false`.

### 2. NFS Media Mount

- **What**: The Jellyfin media root (`/mnt/nas/share/media` or `JELLYFIN_MEDIA_ROOT`) must be a mounted NFS (or at least a mountpoint) and contain the expected media tree.
- **Why**: Media files are multi-GB; extraction probes are header-only but still require the mount. Missing mount must not trigger destructive fileless GC.
- **How**:
  ```bash
  mountpoint -q /mnt/nas/share/media || grep -q " /mnt/nas/share/media " /proc/mounts
  test -d /mnt/nas/share/media/jellyfin
  ```
- **Failure**: Not mounted or inaccessible. Readiness `checks.nfs.ok=false`. The pipeline logs a warning and skips media-dependent sweeps; it does not delete state.

### 3. Ledger Integrity

- **What**: The webhook inbox ledger (`webhook_inbox.db` under the state directory, never NFS) must exist, be a valid SQLite WAL database, and pass `PRAGMA integrity_check`.
- **Why**: The ledger is the durable inbox for Tdarr webhooks; corruption must fail closed without losing sidecars.
- **How**:
  ```bash
  ls -lh /home/user/.config/asr-pipeline/webhook_inbox.db*
  sqlite3 /home/user/.config/asr-pipeline/webhook_inbox.db "PRAGMA integrity_check;"
  # expected: ok
  ```
- **Checks**: File exists, not on NFS (`/mnt/nas`/`/media` rejected), `PRAGMA journal_mode=WAL`, `integrity_check` returns `ok`, and required tables (`operations`) exist.
- **Failure**: Missing or corrupt file, wrong journal mode, or NFS path. Readiness `checks.ledger.ok=false`; webhook handler fails closed (zero worker) and logs.

### 4. Paused Startup

- **What**: The orchestrator may start in paused mode if any of the following is true at startup:
  - File `/home/user/.config/asr-pipeline/paused` exists (empty file or containing `1`/`true`)
  - Environment `PAUSED=1` / `ASRSUB_PAUSED=1`
  - Docker Compose override: `environment: - PAUSED=1`
- **Why**: Allows safe startup for inspection after btrfs/NFS recovery or manual maintenance without immediately processing the backlog.
- **How**:
  ```bash
  test -f /home/user/.config/asr-pipeline/paused && echo "paused startup"
  curl -H "X-API-Key: $(cat /run/secrets/control_api_key)" http://127.0.0.1:8085/status | jq .paused
  # To resume: POST /resume or POST /api2/resume, or remove paused file and POST /resume
  ```
- **Behavior**: When paused, readiness returns `200` with `checks.paused=true` and `ready=false` (or `paused:true` in the payload) so orchestrators can distinguish "up but paused" from "not ready". Liveness remains `200`. A restart without the pause flag resumes normal operation. The flag file is not automatically removed on resume; operator must remove it for the next restart to be unpaused.

## Operational Notes

- **No secret values** appear in health/readiness responses, logs, or dashboards. `CONTROL_API_KEY` is loaded from `/run/secrets/control_api_key` (or `CONTROL_API_KEY_FILE`); `pipeline.env` at `/home/user/.config/asr-pipeline/pipeline.env` holds non-control settings (BAZARR_URL, SONARR_URL, etc.) and is mounted as a volume, not injected as environment variables.
- **Dashboard vs orchestrator**: Dashboard mounts the state volume read-only (`:ro`) to prevent unintended state writes. Both services share the immutable `${ASRSUB_IMAGE}` (`asrsub:<full-git-sha>`) image but use distinct commands; the compose file retains all data mounts and fails closed if `ASRSUB_IMAGE` is unset.
- **Build vs deploy**: `build.sh` is build-only and immutable (`asrsub:<full-40-char-git-sha>` via `docker build`) and emits a non-secret release descriptor (`.release.env` / `release.json` with `ASRSUB_IMAGE`/`GIT_SHA`). It never runs `down`, `rmi`, or `builder prune` and never tags `latest`. Deployment is explicit via `deploy.sh` (requires `ASRSUB_IMAGE` and uses `--no-build`) or `DEPLOY.md`.
- **Probes**:
  ```yaml
  # docker-compose healthcheck example (orchestrator)
  healthcheck:
    test: ["CMD", "curl", "-sf", "http://127.0.0.1:8085/health"]
    interval: 30s
    timeout: 5s
    retries: 3
  # readiness
  test: ["CMD", "curl", "-sf", "http://127.0.0.1:8085/ready"]
  ```

## Verification (local)

```bash
# From repo root (Python sources live under legacy/):
python -m py_compile legacy/orchestrator.py legacy/webhook_ledger.py legacy/control_api_v2.py
curl -sf http://127.0.0.1:8085/health | jq .
curl -sf http://127.0.0.1:8085/ready | jq .
# Authenticated status:
curl -H "X-API-Key: $(cat /run/secrets/control_api_key 2>/dev/null || echo $CONTROL_API_KEY)" http://127.0.0.1:8085/status | jq .
```
