#!/usr/bin/env bash
# Immutable release build — non-destructive, versioned, fail-closed.
# Retains data mounts and never stops/removes containers, images, or builder cache.
# Local/dev builds only: image pushes happen exclusively in CI
# (.github/workflows/release.yml). Deployment is pull-based: set ASRSUB_IMAGE
# to a GHCR tag and `docker compose pull && docker compose up -d --no-build`
# (see docs/DEPLOY.md). Emits a non-secret release descriptor
# (.release.env + release.json) for reference.
set -euo pipefail
cd "$(dirname "$0")"

# Resolve full 40-char git SHA; fail closed if not available.
GIT_SHA="${1:-}"
if [[ -z "${GIT_SHA}" ]]; then
  GIT_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
fi
if [[ ! "${GIT_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  # Allow passing short SHA accidentally? Require full SHA.
  if [[ "${GIT_SHA}" =~ ^[0-9a-f]{7,39}$ ]]; then
    echo "ERROR: build.sh requires full 40-char git SHA, got '${GIT_SHA}' (${#GIT_SHA} chars). Resolve via 'git rev-parse HEAD'." >&2
    exit 1
  fi
  echo "ERROR: Unable to resolve full 40-char git SHA (got '${GIT_SHA}'). Ensure this is a git repo with commits." >&2
  exit 1
fi

IMAGE="${ASRSUB_REGISTRY:-ghcr.io/bedasrv}/asrsub:${GIT_SHA}"
RELEASE_ENV=".release.env"
RELEASE_JSON="release.json"

echo "==> Building immutable release ${IMAGE} (build-only, non-destructive, no push)"
echo "    - No 'docker compose down', no 'docker rmi', no 'builder prune', no 'up -d'"
echo "    - Data mounts retained: /home/user/.config/asr-pipeline, /home/user/.cache/asr-pipeline, host media (MEDIA_HOST_PATH)"
echo "    - Single daemon serves the dashboard/API; no separate read-only dashboard replica (see docker-compose.yml)"
echo "    - Secret handling: CONTROL_API_KEY via Docker Compose secret file /run/secrets/control_api_key (or CONTROL_API_KEY_FILE), env only for tests"

# Build explicit immutable tag directly (no mutable latest, no compose build fallback).
docker build -t "${IMAGE}" .

# Emit non-secret release descriptor for deploy (no secrets, no API keys).
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "${RELEASE_ENV}" <<EOF
ASRSUB_IMAGE=${IMAGE}
GIT_SHA=${GIT_SHA}
BUILD_TIME=${BUILD_TIME}
EOF

cat > "${RELEASE_JSON}" <<EOF
{
  "asrsub_image": "${IMAGE}",
  "git_sha": "${GIT_SHA}",
  "build_time": "${BUILD_TIME}"
}
EOF

echo ""
echo "==> Built ${IMAGE} (local only — CI owns GHCR pushes)"
echo "    Release descriptor: ${RELEASE_ENV} and ${RELEASE_JSON} (non-secret, safe to commit in CI artifacts)"
echo "    Contents:"
cat "${RELEASE_ENV}"
echo "    Inspect: docker images | grep asrsub"
echo "    Deploy from GHCR when ready: ASRSUB_IMAGE=${IMAGE} docker compose pull && ASRSUB_IMAGE=${IMAGE} docker compose up -d --no-build — see docs/DEPLOY.md"
