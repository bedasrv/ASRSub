#!/usr/bin/env bash
# Deployment smoke test: liveness, readiness, and media-mapping sanity.
#
# Usage:
#   scripts/deploy_smoke.sh
#   BASE_URL=http://127.0.0.1:8085 scripts/deploy_smoke.sh
#   MEDIA_SAMPLE=Shows/Test/Season\ 1/Ep.mkv scripts/deploy_smoke.sh
#
# Environment:
#   BASE_URL            control API base (default http://127.0.0.1:8085)
#   NAS_MEDIA_PREFIX    host/NAS prefix the daemon reads (default /mnt/nas/share/media)
#   JELLYFIN_MEDIA_ROOT Jellyfin container path prefix (default /media)
#   MEDIA_SAMPLE        optional relative path under the media root to verify
#                       is visible at BOTH prefixes
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8085}"
NAS_MEDIA_PREFIX="${NAS_MEDIA_PREFIX:-/mnt/nas/share/media}"
JELLYFIN_MEDIA_ROOT="${JELLYFIN_MEDIA_ROOT:-/media}"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$1" || true; }

# 1. Liveness — cheap process probe.
[ "$(code "$BASE_URL/health")" = "200" ] || fail "GET /health did not return 200"
pass "GET /health 200"

# 2. Readiness — dependency-aware 200/503.
ready_body="$(curl -s "$BASE_URL/ready")"
[ "$(code "$BASE_URL/ready")" = "200" ] || fail "GET /ready not ready: $ready_body"
pass "GET /ready 200"

python3 - "$ready_body" <<'PY' || fail "readiness payload missing required checks"
import json, sys
payload = json.loads(sys.argv[1])
checks = payload.get("checks", {})
for name in ("media_root", "providers", "state_dir"):
    assert checks.get(name, {}).get("ok") is True, f"{name} not ok: {checks.get(name)}"
summary = {k: v.get("ok") for k, v in checks.items()}
print("  checks:", summary)
print("  integrations:", payload.get("integrations", {}))
PY

# 3. Media mapping sanity: the configured host prefix must exist, and a known
# sample must be visible at the Jellyfin prefix too (the refresh lookup maps
# host path -> Jellyfin path).
[ -d "$NAS_MEDIA_PREFIX" ] || fail "media root $NAS_MEDIA_PREFIX is not a directory"
pass "media root present: $NAS_MEDIA_PREFIX"

if [ -n "${MEDIA_SAMPLE:-}" ]; then
  [ -r "$NAS_MEDIA_PREFIX/$MEDIA_SAMPLE" ] \
    || fail "sample not readable at $NAS_MEDIA_PREFIX/$MEDIA_SAMPLE"
  pass "sample readable at $NAS_MEDIA_PREFIX/$MEDIA_SAMPLE"
  if [ -d "$JELLYFIN_MEDIA_ROOT" ]; then
    [ -r "$JELLYFIN_MEDIA_ROOT/$MEDIA_SAMPLE" ] \
      || fail "sample not visible at Jellyfin root $JELLYFIN_MEDIA_ROOT/$MEDIA_SAMPLE"
    pass "sample visible at Jellyfin root $JELLYFIN_MEDIA_ROOT/$MEDIA_SAMPLE"
  else
    echo "note: $JELLYFIN_MEDIA_ROOT not present locally (Jellyfin runs elsewhere); skipping"
  fi
fi

echo "smoke test passed against $BASE_URL"
