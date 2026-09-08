# ASRSub Health / Readiness Contract

This document defines the local health and readiness story for the ASRSub
pipeline: a single Rust `asrsub` binary running as two compose services
(orchestrator on :8085, dashboard/API replica on :8080). It is the
authoritative contract for deployment and monitoring.

## Endpoints (actually served)

- `GET /health` — **Liveness** (unauthenticated, always open). Returns `200
  {"ok": true}` if the process is running. Does not check external
  dependencies. Use for container liveness probes. (Also at `/api2/health`.)
- `GET /status` — Daemon state (unauthenticated): `paused`, `uptime_s`,
  `last_pass{at,scanned,done,failed}`, `current`, `started_at`. This is the
  readiness signal today: healthy = 200 + a recent `last_pass.at` (see below).
- `GET /config` — Merged config with secrets masked.
- `POST /pause /resume /run-once /wake` — Control (require
  `X-API-Key: <control key>` from `/run/secrets/control_api_key`;
  env `CONTROL_API_KEY` is test fallback only). No `X-Control-Key` alias.
- `/api2/*` — Telemetry + episode actions (same auth rule for POSTs).

There is **no `/ready` endpoint yet** (planned). `deploy.sh`'s `/ready`
probe is best-effort and tolerates the 404; gate releases on `/health`
plus `/status` instead:

```bash
curl -sf http://127.0.0.1:8085/health | jq .
curl -sf http://127.0.0.1:8085/status | jq '{paused, last_pass}'
```

## Operational prerequisites

Still true operationally, checked by the operator (not yet gated in code —
the daemon logs and degrades instead of refusing to start):

### 1. Local State Directory

- **What**: `/home/user/.config/asr-pipeline` (or `$STATE_FILE` dirname)
  should be local, writable storage with headroom. State files
  (`state.jsonl`, `subtitle_registry.jsonl`, `actions.jsonl`) are
  append-or-atomic-replace under `flock` sidecars — never on NFS/tmpfs.
- **How**:
  ```bash
  df -T /home/user/.config/asr-pipeline
  stat -f -c %T /home/user/.config/asr-pipeline  # expected: btrfs
  test -w /home/user/.config/asr-pipeline
  ```

### 2. NFS Media Mount

- **What**: `/mnt/nas/share/media` must be mounted and contain the media tree.
- **Why**: Missing media must never trigger destructive cleanup — episodes
  without files on disk error the pass item (`failed`, state row `error`)
  and are retried next pass; nothing is pruned.
- **How**:
  ```bash
  mountpoint -q /mnt/nas/share/media || grep -q " /mnt/nas/share/media " /proc/mounts
  test -d /mnt/nas/share/media/jellyfin
  ```

### 3. Webhooks (no inbox ledger)

The retired Python daemon used a SQLite webhook inbox
(`webhook_inbox.db` + integrity checks). The Rust daemon has **no inbox
DB**: `POST /webhook` (authenticated like every other control POST — send
`X-API-Key` or `X-Control-Key: <CONTROL_API_KEY>`, otherwise 401) wakes the
pass loop and extracts embedded subtitles on a spawned task, with
concurrent duplicates for the same file collapsed to one extraction.
A POST without a `file`/`filePath`/`path` field is a no-op.
There is nothing to `PRAGMA
integrity_check` — if you migrated from the Python deployment, the stale
`.db` files under the state dir are inert and can be archived away.

> OPS: the Tdarr/Sonarr notification that POSTs `/webhook` MUST carry the
> key header — without it webhooks 401 and new episodes wait for the next
> periodic pass (30–120s) instead of starting immediately.

### 4. Runtime Pause (no paused boot)

The daemon always starts **unpaused** (no `paused` file, no `PAUSED=1` —
those belonged to the retired Python daemon). Hold/resume at runtime:

```bash
curl -H "X-API-Key: $(cat /run/secrets/control_api_key)" -X POST http://127.0.0.1:8085/pause
curl -H "X-API-Key: $(cat /run/secrets/control_api_key)" http://127.0.0.1:8085/status | jq .paused
# To resume: POST /resume (or /api2/resume on :8080)
```

## Operational Notes

- **No secret values** appear in health/readiness responses, logs, or dashboards. `CONTROL_API_KEY` is loaded from `/run/secrets/control_api_key` (or `CONTROL_API_KEY_FILE`); `pipeline.env` at `/home/user/.config/asr-pipeline/pipeline.env` holds non-control settings (BAZARR_URL, SONARR_URL, etc.) and is mounted as a volume, not injected as environment variables.
- **Dashboard vs orchestrator**: Dashboard mounts the state volume read-only (`:ro`) to prevent unintended state writes. Both services share the immutable `${ASRSUB_IMAGE}` (`asrsub:<full-git-sha>`) image but use distinct commands; the compose file retains all data mounts and fails closed if `ASRSUB_IMAGE` is unset.
- **Build vs deploy**: `build.sh` is build-only and immutable (`asrsub:<full-40-char-git-sha>` via `docker build`) and emits a non-secret release descriptor (`.release.env` / `release.json` with `ASRSUB_IMAGE`/`GIT_SHA`). It never runs `down`, `rmi`, or `builder prune` and never tags `latest`. Deployment is explicit via `deploy.sh` (requires `ASRSUB_IMAGE` and uses `--no-build`) or `DEPLOY.md`.
- **Probes** (orchestrator `:8085`; dashboard replica on `:8080` serves the same):
  ```yaml
  healthcheck:
    test: ["CMD", "curl", "-sf", "http://127.0.0.1:8085/health"]
    interval: 30s
    timeout: 5s
    retries: 3
  ```
  Release gating: `/health` 200 plus a fresh `/status` `last_pass.at`.

## Verification (local)

```bash
cargo test            # Rust suite (offline; includes full-program simulation)
python3 -m unittest tests.test_immutable_release_contract tests.test_release_descriptor_execution
curl -sf http://127.0.0.1:8085/health | jq .
curl -sf http://127.0.0.1:8085/status | jq '{paused, last_pass}'
# Authenticated config (secrets masked):
curl -H "X-API-Key: $(cat /run/secrets/control_api_key 2>/dev/null || echo $CONTROL_API_KEY)" http://127.0.0.1:8085/config | jq .
```
