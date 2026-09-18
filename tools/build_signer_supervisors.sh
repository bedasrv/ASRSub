#!/usr/bin/bash
# Build and install the two fixed-role ASRSub signer supervisors.
set -euo pipefail

ROOT="$(cd -- "$(/usr/bin/dirname -- "$0")/.." && /usr/bin/pwd -P)"
SOURCE="$ROOT/tools/asrsub_signer_supervisor.c"
OUTPUT_DIR="/usr/local/sbin"
OUTPUT_OVERRIDE=0
CC_BIN="/usr/bin/cc"
INSTALL_BIN="/usr/bin/install"
MKTEMP_BIN="/usr/bin/mktemp"
REALPATH_BIN="/usr/bin/realpath"
RM_BIN="/usr/bin/rm"
STAT_BIN="/usr/bin/stat"

usage() {
    echo "usage: tools/build_signer_supervisors.sh [--output-dir TEST_DIR]" >&2
    exit 2
}

fail() {
    echo "asrsub-signer-build: $1" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --output-dir)
            (($# >= 2)) || usage
            OUTPUT_DIR=$2
            OUTPUT_OVERRIDE=1
            shift 2
            ;;
        *)
            usage
            ;;
    esac
done

[[ -n "$OUTPUT_DIR" ]] || usage
[[ "$OUTPUT_DIR" == /* ]] || fail "output directory must be absolute"
case "$OUTPUT_DIR" in
    *"/../"*|*/..|../*|..)
        fail "output directory contains traversal"
        ;;
esac

check_directory_metadata() {
    local directory=$1
    local uid mode type
    [[ -d "$directory" && ! -L "$directory" ]] || fail "output directory component is unsafe"
    read -r uid mode type < <("$STAT_BIN" -c '%u %a %F' -- "$directory") || fail "cannot inspect output directory"
    [[ "$uid" == 0 && "$type" == "directory" ]] || fail "production output directory is not root-owned"
    (( (8#$mode & 022) == 0 )) || fail "production output directory is writable by group or other"
}

check_production_destination() {
    [[ "$OUTPUT_DIR" == "/usr/local/sbin" ]] || fail "production output directory is fixed at /usr/local/sbin"
    local directory
    for directory in / /usr /usr/local /usr/local/sbin; do
        check_directory_metadata "$directory"
    done
}

check_test_destination() {
    case "$OUTPUT_DIR" in
        /tmp/agent-scratch/*|/var/tmp/*)
            ;;
        *)
            fail "test output directory must be below /tmp/agent-scratch or /var/tmp"
            ;;
    esac
    local parent resolved
    parent="$(/usr/bin/dirname -- "$OUTPUT_DIR")"
    [[ -d "$parent" && ! -L "$parent" ]] || fail "test output parent is unsafe or absent"
    resolved="$($REALPATH_BIN -e -- "$parent")" || fail "cannot resolve test output parent"
    [[ "$resolved" == "$parent" ]] || fail "test output parent contains a symlink"
    if [[ -e "$OUTPUT_DIR" || -L "$OUTPUT_DIR" ]]; then
        [[ -d "$OUTPUT_DIR" && ! -L "$OUTPUT_DIR" ]] || fail "test output directory is not a directory"
    else
        "$INSTALL_BIN" -d -m 0700 -- "$OUTPUT_DIR"
    fi
    resolved="$($REALPATH_BIN -e -- "$OUTPUT_DIR")" || fail "cannot resolve test output directory"
    [[ "$resolved" == "$OUTPUT_DIR" ]] || fail "test output directory contains a symlink"
}

if [[ "$OUTPUT_OVERRIDE" == 0 || "$OUTPUT_DIR" == "/usr/local/sbin" ]]; then
    [[ "${ASRSUB_ALLOW_HOST_INSTALL:-}" == "1" ]] || fail \
        "host installation is disabled; set ASRSUB_ALLOW_HOST_INSTALL=1 only on an isolated target"
    check_production_destination
else
    check_test_destination
fi

BUILD_DIR="$($MKTEMP_BIN -d -- "$OUTPUT_DIR/.asrsub-signer-build.XXXXXX")"
cleanup() {
    "$RM_BIN" -rf -- "$BUILD_DIR"
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

for name in asrsub-bundle-signer asrsub-approval-signer; do
    destination="$OUTPUT_DIR/$name"
    if [[ -L "$destination" || ( -e "$destination" && ! -f "$destination" ) ]]; then
        fail "refusing unsafe existing destination"
    fi
done
for name in asrsub-bundle-signer asrsub-approval-signer; do
    destination="$OUTPUT_DIR/$name"
    "$INSTALL_BIN" -m 0755 -- "$BUILD_DIR/$name" "$destination"
    read -r uid mode type < <("$STAT_BIN" -c '%u %a %F' -- "$destination") || fail "cannot inspect installed supervisor"
    [[ ! -L "$destination" && "$type" == "regular file" && "$mode" == 755 ]] || fail "installed supervisor metadata is unsafe"
    if [[ "$OUTPUT_OVERRIDE" == 0 || "$OUTPUT_DIR" == "/usr/local/sbin" ]]; then
        [[ "$uid" == 0 ]] || fail "installed supervisor is not root-owned"
    fi
done
