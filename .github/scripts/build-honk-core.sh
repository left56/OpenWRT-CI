#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build the pinned Honk core with the local REALITY client-version patch.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
WRT_ROOT=${1:-"$ROOT/wrt"}
HONK_ROOT="$WRT_ROOT/package/openwrt-honk"
BUILD_SCRIPT="$HONK_ROOT/ci/build-core.sh"
PATCH="$ROOT/.github/patches/honk-reality-client-version.patch"

for path in "$BUILD_SCRIPT" "$HONK_ROOT/ci/pins.env" "$PATCH"; do
	test -f "$path" || { echo "Required Honk build input is missing: $path" >&2; exit 1; }
done

case "${WRT_CONFIG:-}" in
	IPQ95XX) CORE_ARCHS=aarch64 ;;
	X86) CORE_ARCHS=x86_64 ;;
	*) echo "Unsupported Honk build target: ${WRT_CONFIG:-unset}" >&2; exit 1 ;;
esac

PATCH_SHA256=$(sha256sum "$PATCH" | awk '{print $1}')
# Parse the unified diff before starting a cross-compile; catch malformed hunk counts early.
git apply --numstat "$PATCH" >/dev/null
export HONK_REALITY_PATCH="$PATCH"

# The feed's Makefile consumes prebuilt binaries. Inject the patch application
# immediately after the pinned upstream source checkout, before cargo builds it.
python3 - "$BUILD_SCRIPT" <<'PYEOF'
import sys
from pathlib import Path

path = Path(sys.argv[1])
source = path.read_text()
needle = 'git -C "$WORK_DIR/source" checkout --quiet "$CORE_COMMIT"\n'
insertion = needle + (
    'REALITY_VERSION_PATCH="${HONK_REALITY_PATCH:?Honk REALITY patch path is required}"\n'
    'test -f "$REALITY_VERSION_PATCH"\n'
    'git -C "$WORK_DIR/source" apply --check "$REALITY_VERSION_PATCH"\n'
    'git -C "$WORK_DIR/source" apply "$REALITY_VERSION_PATCH"\n'
    'grep -Fq "plain[..3].copy_from_slice(&REALITY_CLIENT_VERSION);" '
    '"$WORK_DIR/source/crates/honk-outbound/src/reality.rs"\n'
)
if source.count(needle) != 1:
    raise SystemExit("Pinned Honk build script changed: source checkout anchor not found exactly once")
if "HONK_REALITY_PATCH" in source:
    raise SystemExit("Pinned Honk build script already contains a REALITY patch hook")
path.write_text(source.replace(needle, insertion))
PYEOF

(
	cd "$HONK_ROOT"
	CORE_ARCHS="$CORE_ARCHS" ci/build-core.sh
)

STAGE="$HONK_ROOT/honk/generated-stage.mk"
PROVENANCE="$HONK_ROOT/honk/generated-provenance.json"
CACHE="$HONK_ROOT/artifacts/cache"
test -s "$STAGE"
test -s "$PROVENANCE"

# Use the freshly built archives instead of kenzok8's unpatched release assets.
printf '\n# Local CI artifacts built with the REALITY compatibility patch.\nPKG_SOURCE_URL:=file://%s/artifacts/cache/\n' "$HONK_ROOT" >> "$STAGE"

for arch in $CORE_ARCHS; do
	key=$(printf '%s' "$arch" | tr '[:lower:]' '[:upper:]')
	asset=$(sed -n "s/^HONK_ASSET_${key}:=//p" "$STAGE")
	expected=$(sed -n "s/^HONK_HASH_${key}:=//p" "$STAGE")
	test -n "$asset" && test -n "$expected"
	test -s "$CACHE/$asset"
	actual=$(sha256sum "$CACHE/$asset" | awk '{print $1}')
	test "$actual" = "$expected" || {
		echo "Honk core archive hash mismatch: $asset" >&2
		exit 1
	}
	echo "Verified patched Honk core: $arch $asset $actual"
done

python3 - "$PROVENANCE" "$PATCH_SHA256" "$CORE_ARCHS" <<'PYEOF'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text())
data["core"]["reality_client_version"] = "26.11.9"
data["core"]["reality_client_version_patch_sha256"] = sys.argv[2]
data["core"]["architectures"] = sys.argv[3].split()
path.write_text(json.dumps(data, indent=2) + "\n")
PYEOF

mkdir -p "$ROOT/build-reports"
cp "$PROVENANCE" "$ROOT/build-reports/honk-core-provenance.json"
{
	echo "REALITY client version marker: 26.11.9"
	echo "Patch SHA256: $PATCH_SHA256"
	echo "Architectures: $CORE_ARCHS"
} > "$ROOT/build-reports/honk-reality-version.txt"

echo "Honk core build completed with REALITY client version 26.11.9."
