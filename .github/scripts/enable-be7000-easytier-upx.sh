#!/usr/bin/env bash
# Compress EasyTier Core and CLI with UPX -9 for BE7000.
set -euo pipefail
UPX_VERSION="5.3.0"
UPX_ARCHIVE="upx-${UPX_VERSION}-amd64_linux.tar.xz"
UPX_SHA256="79cea8a50b6fa102d10c7b79a419cd77dead3cc2d78661de2dc5e5815dc505c1"
UPX_URL="https://github.com/upx/upx/releases/download/v${UPX_VERSION}/${UPX_ARCHIVE}"
UPX_BIN="$PWD/.ci-tools/upx"
MAKEFILE="$PWD/package/luci-app-easytier/easytier-noweb/Makefile"
test -f "$MAKEFILE"
temp_dir="$(mktemp -d)"
trap 'rm -rf "$temp_dir"' EXIT
curl -fL --retry 3 --connect-timeout 30 "$UPX_URL" -o "$temp_dir/$UPX_ARCHIVE"
printf '%s  %s\n' "$UPX_SHA256" "$temp_dir/$UPX_ARCHIVE" | sha256sum -c -
tar -xJf "$temp_dir/$UPX_ARCHIVE" -C "$temp_dir"
install -D -m 0755 "$temp_dir/upx-${UPX_VERSION}-amd64_linux/upx" "$UPX_BIN"
# Upstream Build/Compile is empty because the package ships prebuilt ELF files.
python3 - "$MAKEFILE" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text()
old = "define Build/Compile\nendef\n"
new = ("define Build/Compile\n"
       "\t@if $(TOPDIR)/.ci-tools/upx -l $(PKG_BUILD_DIR)/easytier-core >/dev/null 2>&1; then echo 'easytier-core already UPX-packed; skip'; else $(TOPDIR)/.ci-tools/upx -9 $(PKG_BUILD_DIR)/easytier-core; fi\n"
       "\t@if $(TOPDIR)/.ci-tools/upx -l $(PKG_BUILD_DIR)/easytier-cli >/dev/null 2>&1; then echo 'easytier-cli already UPX-packed; skip'; else $(TOPDIR)/.ci-tools/upx -9 $(PKG_BUILD_DIR)/easytier-cli; fi\n"
       "endef\n")
if text.count(old) != 1:
    raise SystemExit("Unexpected EasyTier Makefile: cannot install UPX -9 hook")
path.write_text(text.replace(old, "".join(new), 1))
PY
echo "EasyTier Core and CLI: UPX -9 enabled for BE7000."
