# ASRSub Health / Readiness Contract

This document defines the local health and readiness story for the ASRSub
pipeline: a single Rust `asrsub` binary running as one compose service (the
daemon serves both the dashboard and the API on `WEBHOOK_PORT`, default
`:8085`). It is the authoritative contract for deployment and monitoring.

## Endpoints (actually served)

- `GET /health` — **Liveness** (unauthenticated, always open). Returns `200
  {"ok": true}` if the process is running. Does **not** check any
  dependency; use it for container liveness probes. (Also `/api2/health`.)
- `GET /ready` — **Readiness** (unauthenticated). Evaluates the local
  prerequisites below and returns `200` when ready, `503` otherwise. It
  never makes remote calls and never gates on Sonarr/Bazarr/Jellyfin, so a
  slow or down integration cannot flap readiness. (Also `/api2/ready`.)
- `GET /status` — Daemon state (unauthenticated): `paused`, `uptime_s`,
  `last_pass{at,scanned,done,failed}`, `current`, `started_at`,
  `run_once_requested`, `media_ok` (the configured media root is present — a
  dead mount no longer looks like "idle").
- `GET /config` — Merged config with secrets masked.
- `POST /pause /resume /run-once /wake /webhook` — Control (require
  `X-API-Key` (or the `X-Control-Key` alias that media-server notification
  plugins send): `<control key>` from `/run/secrets/control_api_key`;
  env `CONTROL_API_KEY` is test fallback only).
- `/api2/*` — Telemetry + episode actions (same auth rule for POSTs).

### `/ready` contract

```jsonc
{
  "ready": true,                       // 200 when true, 503 when false
  "checks": {
    "media_root": {"ok": true, "path": "/mnt/nas/share/media"},
    "providers":  {"ok": true, "llm": 14, "whisper": 1},
    "state_dir":  {"ok": true, "path": "/home/user/.config/asr-pipeline"}
  },
  "integrations": {                    // diagnostics ONLY — never gate ready
    "sonarr": true,
    "bazarr": true,
    "jellyfin": false,
    "jellyfin_misconfigured": false    // API key set but JELLYFIN_URL empty
  }
}
```

Readiness requires all three gating checks:

1. **`media_root`** — the directory named by `NAS_MEDIA_PREFIX` exists and is
   a directory. This is where `/data/…` paths from Sonarr/Radarr are mapped
   and what the daemon reads; it must be the mounted media tree.
2. **`providers`** — at least one LLM model and at least one Whisper
   endpoint are configured in `asrsub_providers.json`. The daemon refuses to
   start without an LLM, so a running-but-LLM-less process is impossible;
   the Whisper count catches a file that omits STT.
3. **`state_dir`** — the directory containing `STATE_FILE` is writable (a
   short-lived probe file is created and removed). State lives here.

`integrations` reports whether the optional `SONARR_URL`, `BAZARR_URL` and
`JELLYFIN_URL`/key pairs are configured. `jellyfin_misconfigured` is `true`
whenever a Jellyfin key is set without a URL (no site-specific default
ships in code, so refresh is silently off until the URL is set).

Use `/health` for liveness and `/ready` for readiness:

```bash
curl -sf http://127.0.0.1:8085/health | jq .
curl -sf http://127.0.0.1:8085/ready  | jq .   # non-zero exit when 503
```

## Operational prerequisites

Checked by `/ready` (media root, state dir, providers) or by the operator:

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

### 2. Media Mount

- **What**: the directory named by `NAS_MEDIA_PREFIX` (default
  `/mnt/nas/share/media`) must be mounted and contain the media tree. The
  compose file mounts the host media directory (`MEDIA_HOST_PATH`) there.
- **Jellyfin mapping**: `JELLYFIN_MEDIA_ROOT` (default `/media`) is the path
  prefix the **Jellyfin server** reports for the same files. It is a string
  mapping used only for the refresh lookup — the daemon does not read it
  from disk. Keep it aligned with the Jellyfin server's container path.
- **Why**: Missing media must never trigger destructive cleanup — episodes
  without files on disk error the pass item (`failed`, state row `error`)
  and are retried next pass; nothing is pruned.
- **How**:
  ```bash
  mountpoint -q "${NAS_MEDIA_PREFIX:-/mnt/nas/share/media}" || \
    grep -q " ${NAS_MEDIA_PREFIX:-/mnt/nas/share/media} " /proc/mounts
  curl -sf http://127.0.0.1:8085/ready | jq .checks.media_root
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
# To resume: POST /resume (or /api2/resume)
```

## Operational Notes

- **No secret values** appear in health/readiness responses, logs, or dashboards. `CONTROL_API_KEY` is loaded from `/run/secrets/control_api_key` (or `CONTROL_API_KEY_FILE`); `pipeline.env` at `/home/user/.config/asr-pipeline/pipeline.env` holds non-control settings (BAZARR_URL, SONARR_URL, JELLYFIN_URL, …) and is mounted as a volume, not injected as environment variables.
- **One daemon serves the dashboard**: exactly one `orchestrator` service runs the daemon, which serves the UI at `/` and the API at `/api2/*` on `WEBHOOK_PORT` (default 8085). There is no separate dashboard replica: running a second full daemon on a read-only state mount caused an `EROFS` restart loop and risked competing state writers.
- **Build vs deploy**: images are built by CI and pushed to GHCR as immutable `ghcr.io/bedasrv/asrsub:<full-40-char-git-sha>` (`build.sh` is local/dev builds only and never pushes); deploy pulls an explicit tag (`docker compose pull`, then `up -d --no-build`). The compose file fails closed if `ASRSUB_IMAGE` is unset. Deployment detail lives in `DEPLOY.md`.
- **Probes** (compose ships this healthcheck; `/health` for liveness, `/ready` for readiness):
  ```yaml
  healthcheck:
    test: ["CMD", "curl", "-sf", "http://127.0.0.1:8085/ready"]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 20s
  ```
  Release gating: `/ready` 200. Rollout gating (e.g. `depends_on:
  condition: service_healthy`) should also use `/ready`.

## Verification (local)

```bash
cargo test            # Rust suite (offline; includes full-program simulation)
python3 -m unittest tests.test_immutable_release_contract tests.test_release_descriptor_execution
curl -sf http://127.0.0.1:8085/health | jq .
curl -sf http://127.0.0.1:8085/ready  | jq .
curl -sf http://127.0.0.1:8085/status | jq '{paused, media_ok, last_pass}'
# Authenticated config (secrets masked):
curl -H "X-API-Key: $(cat /run/secrets/control_api_key 2>/dev/null || echo $CONTROL_API_KEY)" http://127.0.0.1:8085/config | jq .
```
