#!/usr/bin/env bash
# Build and install the two fixed-role ASRSub signer supervisors.
set -euo pipefail
PATH=/usr/bin:/bin
export PATH

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
OUTPUT_DIR="/usr/local/sbin"
CC_BIN="${CC:-/usr/bin/cc}"

usage() {
    echo "usage: tools/build_signer_supervisors.sh [--output-dir DIR] [--cc COMPILER]" >&2
    exit 2
}

while (($#)); do
    case "$1" in
        --output-dir)
            (($# >= 2)) || usage
            OUTPUT_DIR=$2
            shift 2
            ;;
        --cc)
            (($# >= 2)) || usage
            CC_BIN=$2
            shift 2
            ;;
        *)
            usage
            ;;
    esac
done

[[ -n "$OUTPUT_DIR" ]] || usage
if [[ "$OUTPUT_DIR" != /* ]]; then
    OUTPUT_DIR="$ROOT/$OUTPUT_DIR"
fi

SOURCE="$ROOT/tools/asrsub_signer_supervisor.c"
install -d -m 0755 -- "$OUTPUT_DIR"
BUILD_DIR="$(mktemp -d -- "$OUTPUT_DIR/.asrsub-signer-build.XXXXXX")"
cleanup() {
    rm -rf -- "$BUILD_DIR"
}
trap cleanup EXIT

COMMON_CFLAGS=(
    -std=c11
    -O2
    -D_FORTIFY_SOURCE=2
    -fPIE
    -fstack-protector-strong
    -Wall
    -Wextra
    -Werror
    -Wpedantic
)
"$CC_BIN" "${COMMON_CFLAGS[@]}" -DASRSUB_BUNDLE_SIGNER "$SOURCE" \
    -Wl,-z,relro,-z,now -Wl,--build-id=none -pie \
    -o "$BUILD_DIR/asrsub-bundle-signer"
"$CC_BIN" "${COMMON_CFLAGS[@]}" -DASRSUB_APPROVAL_SIGNER "$SOURCE" \
    -Wl,-z,relro,-z,now -Wl,--build-id=none -pie \
    -o "$BUILD_DIR/asrsub-approval-signer"

install -m 0755 -- "$BUILD_DIR/asrsub-bundle-signer" "$OUTPUT_DIR/asrsub-bundle-signer"
install -m 0755 -- "$BUILD_DIR/asrsub-approval-signer" "$OUTPUT_DIR/asrsub-approval-signer"
