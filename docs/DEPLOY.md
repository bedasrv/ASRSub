# ASRSub Deploy Instructions (Explicit, Immutable Release)

Images are built by CI and published to GHCR; deployment pulls an explicit
image by git SHA and starts it. There is no `deploy.sh` (deleted
2026-09-09) and no mutable `latest` tag — the release image must always be
set explicitly via `ASRSUB_IMAGE`.

## What's in the image

Single statically-built `asrsub` Rust binary + ffmpeg + ca-certs on
Debian slim. **No Python, no model weights, no GPU runtime** — all Whisper
and LLM inference is remote via `asrsub_providers.json`, of which the image
bakes only the **keyless template** (`asrsub_providers.json.example`):
images never carry keys. At runtime leave `api_key` empty and export the
`key_env` vars (e.g. from
`~/.config/asr-pipeline/secrets/provider_keys.env`, `chmod 600`),
which the daemon reads at call time so rotated keys apply without restart.
The dashboard UI (`assets/dashboard.html`) is served by the daemon itself.

## Prerequisites

- GHCR read access on the host (one time):
  ```bash
  docker login ghcr.io   # PAT with read:packages
  ```

- Secret file present on host (not in repo, not in compose env):
  ```bash
  mkdir -p /home/user/.config/asr-pipeline/secrets
  chmod 700 /home/user/.config/asr-pipeline/secrets
  # Create secret file with raw key (no newline, chmod 600)
  printf '%s' '<CONTROL_API_KEY>' > /home/user/.config/asr-pipeline/secrets/control_api_key
  chmod 600 /home/user/.config/asr-pipeline/secrets/control_api_key
  # Verify compose picks it up (requires ASRSUB_IMAGE to be set for config rendering):
  ASRSUB_IMAGE=ghcr.io/bedasrv/asrsub:$(git rev-parse HEAD) CONTROL_API_KEY_FILE_HOST=/home/user/.config/asr-pipeline/secrets/control_api_key docker compose config | grep -A2 control_api_key
  ```
  The file is mounted as a Docker Compose secret at `/run/secrets/control_api_key` inside containers. Environment `CONTROL_API_KEY` is only for hermetic tests.

- Non-control settings remain in `/home/user/.config/asr-pipeline/pipeline.env` (mounted as a volume, not injected as env). Do not put `CONTROL_API_KEY` there.

- Check health prerequisites (see `HEALTH.md`):
  ```bash
  df -T /home/user/.config/asr-pipeline
  mountpoint -q /mnt/nas/share/media && echo "NFS mounted" || echo "NFS missing"
  ls -lh /home/user/.config/asr-pipeline/*.jsonl 2>/dev/null || echo "no state ledgers yet (first boot is fine)"
  ```

## Build (CI-owned, immutable, versioned)

Every push to `main` triggers `.github/workflows/release.yml`: it builds
the image and pushes **two tags** to GHCR:
`ghcr.io/bedasrv/asrsub:<full-40-char-git-sha>` (immutable, authoritative)
and `ghcr.io/bedasrv/asrsub:latest` (a convenience alias tracking the
newest `main` build, so public pulls get the current Rust image — it
previously held the retired Python image). Production must still pin the
SHA tag; compose requires an explicit `${ASRSUB_IMAGE}`. The workflow
emits the non-secret release descriptor as an artifact
(`release.json`: `asrsub_image` / `git_sha` / `build_time` — no secrets).

`build.sh` remains for local/dev builds only: it tags the same
`ghcr.io/bedasrv/asrsub:<git-sha>` scheme (override the registry with
`ASRSUB_REGISTRY=` for mirrors), emits `.release.env` + `release.json`
locally, and **never pushes**. It never runs `docker compose down`,
`docker rmi`, or `docker builder prune`.

- Never tags or deploys a mutable `latest` tag — no `asrsub:` + `latest` default exists for production.
- Retains volumes: `/home/user/.config/asr-pipeline`, `/home/user/.cache/asr-pipeline`, `/mnt/nas/share/media`.
- Dashboard mounts config read-only to prevent unintended state conflict.
- Release descriptor contains only `ASRSUB_IMAGE` / `GIT_SHA` / `BUILD_TIME` — no secrets.

## Deploy (explicit, immutable, pull-based)

Deploy fails closed if `ASRSUB_IMAGE` is not set to an immutable
`ghcr.io/bedasrv/asrsub:<git-sha>` tag:

```bash
export ASRSUB_IMAGE=ghcr.io/bedasrv/asrsub:<full-40-char-sha>
docker compose config                    # review secrets, mounts, and resolved ASRSUB_IMAGE
docker compose pull                      # fetch the exact image from GHCR
docker compose up -d --no-build          # start/restart without deleting volumes, never building
docker compose ps
curl -sf http://127.0.0.1:8085/health | jq .
# No /ready endpoint yet (planned, see docs/HEALTH.md); readiness today is
# "health 200 + /status shows recent last_pass".
curl -H "X-API-Key: $(cat /home/user/.config/asr-pipeline/secrets/control_api_key)" http://127.0.0.1:8085/status | jq .paused
```

Without `ASRSUB_IMAGE`, `docker compose config` and `docker compose up` will error:
`ASRSUB_IMAGE must be set to immutable release tag (e.g. asrsub:<git-sha> — see docs/DEPLOY.md / .release.env)`.

## Rollback (immutable: pull previous SHA)

```bash
docker images | grep asrsub
# Inspect the previous release descriptor (CI artifact: release.json).
# Set the explicit previous immutable tag and redeploy:
ASRSUB_IMAGE=ghcr.io/bedasrv/asrsub:<previous-40-char-sha> docker compose pull
ASRSUB_IMAGE=ghcr.io/bedasrv/asrsub:<previous-40-char-sha> docker compose up -d --no-build
```

Do NOT use `docker tag ... :latest` — the mutable latest tag is intentionally absent from the immutable release flow (always use explicit `ghcr.io/bedasrv/asrsub:<sha>`).

## Paused Startup

Not implemented at boot: the daemon always starts unpaused (no `paused`
file, no `PAUSED=1` handling — those belonged to the retired Python
daemon). To hold the backlog for inspection after boot:

```bash
ASRSUB_IMAGE=ghcr.io/bedasrv/asrsub:$(git rev-parse HEAD) docker compose up -d --no-build
# then immediately pause via:
curl -X POST -H "X-API-Key: $(cat /home/user/.config/asr-pipeline/secrets/control_api_key)" http://127.0.0.1:8085/pause
# ...inspect...
curl -X POST -H "X-API-Key: $(cat /home/user/.config/asr-pipeline/secrets/control_api_key)" http://127.0.0.1:8085/resume
```

## Notes

- No secret values in code, docs, tests, or logs. Release descriptor (`.release.env` / `release.json`) is non-secret by construction.
- Build and deploy logs must redact `CONTROL_API_KEY`.
- Verify after deploy: `HEALTH.md` probes, `docker compose ps`, and `pipeline.env` contains no control key.
