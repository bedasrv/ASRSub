# ASRSub Deploy Instructions (Explicit, Immutable Release)

Build and deploy are intentionally separate. `build.sh` never stops containers or deletes images/cache/volumes.
Production deployment is fail-closed: no mutable `latest` fallback; the release image must be explicitly set via `ASRSUB_IMAGE`.

## What's in the image

Single statically-built `asrsub` Rust binary + ffmpeg + ca-certs on
Debian slim. **No Python, no model weights, no GPU runtime** — all Whisper
and LLM inference is remote via `asrsub_providers.json`, which is baked
into the image at `/app/asrsub_providers.json`:
keep the repo copy `chmod 600` and be aware the keys ship inside every
built image (rotate on leak; env-only secrets are a future change).
The dashboard UI (`assets/dashboard.html`) is served by the daemon itself.

## Prerequisites

- Secret file present on host (not in repo, not in compose env):
  ```bash
  mkdir -p /home/user/.config/asr-pipeline/secrets
  chmod 700 /home/user/.config/asr-pipeline/secrets
  # Create secret file with raw key (no newline, chmod 600)
  printf '%s' '<CONTROL_API_KEY>' > /home/user/.config/asr-pipeline/secrets/control_api_key
  chmod 600 /home/user/.config/asr-pipeline/secrets/control_api_key
  # Verify compose picks it up (requires ASRSUB_IMAGE to be set for config rendering):
  ASRSUB_IMAGE=asrsub:$(git rev-parse HEAD) CONTROL_API_KEY_FILE_HOST=/home/user/.config/asr-pipeline/secrets/control_api_key docker compose config | grep -A2 control_api_key
  ```
  The file is mounted as a Docker Compose secret at `/run/secrets/control_api_key` inside containers. Environment `CONTROL_API_KEY` is only for hermetic tests.

- Non-control settings remain in `/home/user/.config/asr-pipeline/pipeline.env` (mounted as a volume, not injected as env). Do not put `CONTROL_API_KEY` there.

- Check health prerequisites (see `HEALTH.md`):
  ```bash
  df -T /home/user/.config/asr-pipeline
  mountpoint -q /mnt/nas/share/media && echo "NFS mounted" || echo "NFS missing"
  ls -lh /home/user/.config/asr-pipeline/*.jsonl 2>/dev/null || echo "no state ledgers yet (first boot is fine)"
  ```

## Build (non-destructive, immutable, versioned)

`build.sh` builds a full git-SHA image tag and emits a non-secret release descriptor for deploy (` .release.env` + `release.json`).

```bash
./build.sh                              # builds asrsub:<full-40-char-git-sha> via 'docker build'
cat .release.env                        # ASRSUB_IMAGE=asrsub:<git-sha>  GIT_SHA=<sha>  BUILD_TIME=<iso8601>
cat release.json                        # same metadata as JSON (non-secret)
docker images | grep asrsub             # verify immutable tag exists
```

- Never runs `docker compose down`, `docker rmi`, or `docker builder prune`.
- Never tags or deploys a mutable `latest` tag — no `asrsub:` + `latest` default exists for production.
- Retains volumes: `/home/user/.config/asr-pipeline`, `/home/user/.cache/asr-pipeline`, `/mnt/nas/share/media`.
- Dashboard mounts config read-only to prevent unintended state conflict.
- Release descriptor contains only `ASRSUB_IMAGE` / `GIT_SHA` / `BUILD_TIME` — no secrets.

## Deploy (explicit, immutable, requires confirmation)

Deploy fails closed if `ASRSUB_IMAGE` is not set to an immutable `asrsub:<git-sha>` tag and always uses `--no-build`.

Option A — `deploy.sh` (interactive, preferred):
```bash
./build.sh && ./deploy.sh
# deploy.sh loads .release.env (ASRSUB_IMAGE) or respects exported ASRSUB_IMAGE,
# validates it is asrsub:<40-char-sha>, verifies the local image exists,
# prompts for confirmation, verifies secret file, checks NFS, then runs:
# docker compose --env-file .release.env up -d --no-build
# curl http://127.0.0.1:8085/health
```

Option B — manual (explicit env):
```bash
export ASRSUB_IMAGE=asrsub:$(git rev-parse HEAD)
# or: export ASRSUB_IMAGE=$(grep ASRSUB_IMAGE .release.env | cut -d= -f2)
docker compose config                    # review secrets, mounts, and resolved ASRSUB_IMAGE
docker compose --env-file .release.env up -d --no-build   # start/restart without deleting volumes, never building
# or: ASRSUB_IMAGE=$ASRSUB_IMAGE docker compose up -d --no-build
docker compose ps
curl -sf http://127.0.0.1:8085/health | jq .
# No /ready endpoint yet (planned, see docs/HEALTH.md); readiness today is
# "health 200 + /status shows recent last_pass". deploy.sh's /ready probe is
# best-effort and already tolerates the 404.
curl -H "X-API-Key: $(cat /home/user/.config/asr-pipeline/secrets/control_api_key)" http://127.0.0.1:8085/status | jq .paused
```

Without `ASRSUB_IMAGE`, `docker compose config` and `docker compose up` will error:
`ASRSUB_IMAGE must be set to immutable release tag (e.g. asrsub:<git-sha> — see docs/DEPLOY.md / .release.env)`.

## Rollback (immutable: redeploy previous SHA)

```bash
docker images | grep asrsub
# Inspect previous release descriptor if archived:
cat .release.env.prev 2>/dev/null || git show HEAD~1:.release.env 2>/dev/null
# Set explicit previous immutable tag and redeploy with --no-build:
ASRSUB_IMAGE=asrsub:<previous-40-char-sha> ./deploy.sh
# Manual equivalent:
ASRSUB_IMAGE=asrsub:<previous-40-char-sha> docker compose up -d --no-build
# Alternative: restore .release.env to previous SHA and deploy:
echo "ASRSUB_IMAGE=asrsub:<previous-40-char-sha>" > .release.env
docker compose --env-file .release.env up -d --no-build
```

Do NOT use `docker tag ... :latest` — the mutable latest tag is intentionally absent from the immutable release flow (always use explicit `asrsub:<sha>`).

## Paused Startup

Not implemented at boot: the daemon always starts unpaused (no `paused`
file, no `PAUSED=1` handling — those belonged to the retired Python
daemon). To hold the backlog for inspection after boot:

```bash
ASRSUB_IMAGE=asrsub:$(git rev-parse HEAD) docker compose up -d --no-build
# then immediately pause via:
curl -X POST -H "X-API-Key: $(cat /home/user/.config/asr-pipeline/secrets/control_api_key)" http://127.0.0.1:8085/pause
# ...inspect...
curl -X POST -H "X-API-Key: $(cat /home/user/.config/asr-pipeline/secrets/control_api_key)" http://127.0.0.1:8085/resume
```

## Notes

- No secret values in code, docs, tests, or logs. Release descriptor (` .release.env` / `release.json`) is non-secret by construction.
- Build and deploy logs must redact `CONTROL_API_KEY`.
- Verify after deploy: `HEALTH.md` probes, `docker compose ps`, and `pipeline.env` contains no control key.
