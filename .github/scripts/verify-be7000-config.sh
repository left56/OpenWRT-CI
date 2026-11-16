#!/usr/bin/env bash
# Validate the real OpenWrt .config after make defconfig, not just the fragments.
set -euo pipefail

CONFIG="$1"
if [[ ! -f "$CONFIG" ]]; then
  echo "::error::Missing OpenWrt config file: $CONFIG"
  exit 1
fi

# Required selections must survive make defconfig.
while IFS= read -r setting; do
  [[ -n "$setting" ]] || continue
  if ! grep -Fxq -- "$setting" "$CONFIG"; then
    echo "::error::Required config was not selected after defconfig: $setting"
    exit 1
  fi
done <<'REQUIRED'
CONFIG_TARGET_qualcommbe=y
CONFIG_TARGET_qualcommbe_ipq95xx=y
CONFIG_TARGET_DEVICE_qualcommbe_ipq95xx_DEVICE_xiaomi_be7000=y
CONFIG_PACKAGE_honk=y
CONFIG_PACKAGE_luci-app-honk=y
CONFIG_PACKAGE_luci-app-mosdns=y
CONFIG_PACKAGE_mosdns=y
CONFIG_PACKAGE_nlbwmon=y
CONFIG_PACKAGE_luci-app-nlbwmon=y
CONFIG_PACKAGE_luci-i18n-nlbwmon-zh-cn=y
CONFIG_PACKAGE_kmod-nf-conntrack-netlink=y
CONFIG_KERNEL_DEBUG_INFO_BTF=y
CONFIG_KERNEL_XDP_SOCKETS=y
REQUIRED

# Exactly one device profile must be selected: Xiaomi BE7000 (non-ES).
expected_device="CONFIG_TARGET_DEVICE_qualcommbe_ipq95xx_DEVICE_xiaomi_be7000=y"
selected_devices="$(grep -E '^CONFIG_TARGET_DEVICE_[^=]+=y$' "$CONFIG" || true)"
if [[ "$selected_devices" != "$expected_device" ]]; then
  echo "::error::Expected only Xiaomi BE7000 (non-ES), got:"
  printf '%s\n' "$selected_devices"
  exit 1
fi

# Fail early if a custom or feed package source was not installed.
for package_makefile in \
  package/openwrt-honk/honk/Makefile \
  package/openwrt-honk/luci-app-honk/Makefile \
  feeds/packages/net/nlbwmon/Makefile \
  feeds/luci/applications/luci-app-nlbwmon/Makefile \
  feeds/luci/applications/luci-app-nlbwmon/po/zh_Hans/nlbwmon.po; do
  if [[ ! -f "$package_makefile" ]]; then
    echo "::error::Required package source is missing: $package_makefile"
    exit 1
  fi
done

# Validate the BE7000-only exclusion list against the resolved Kconfig.
EXCLUSIONS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/Config/IPQ95XX-REMOVE.txt"
if [[ ! -f "$EXCLUSIONS" ]]; then
  echo "::error::Missing BE7000 exclusion list: $EXCLUSIONS"
  exit 1
fi
while IFS= read -r setting || [[ -n "$setting" ]]; do
  [[ -z "$setting" || "$setting" == \#* ]] && continue
  if [[ ! "$setting" =~ ^CONFIG_PACKAGE_([[:alnum:]_-]+)=n$ ]]; then
    echo "::error::Invalid BE7000 exclusion: $setting"
    exit 1
  fi
  pkg="${BASH_REMATCH[1]}"
  if grep -Eq "^CONFIG_PACKAGE_${pkg}=[ym]$" "$CONFIG"; then
    echo "::error::Excluded BE7000 package selected after defconfig: $pkg"
    exit 1
  fi
done < "$EXCLUSIONS"

# Reject unwanted alternatives.
if grep -Eq '^CONFIG_PACKAGE_[^=]*easytier[^=]*=y$' "$CONFIG"; then
  echo "::error::EasyTier must not be selected for BE7000"
  grep -E '^CONFIG_PACKAGE_[^=]*easytier[^=]*=y$' "$CONFIG"
  exit 1
fi

if grep -Eq '^CONFIG_PACKAGE_(luci-app-homeproxy|dae|daed|luci-app-daede(_dae|_daed)?)=y$' "$CONFIG"; then
  echo "::error::Legacy daed/dae/HomeProxy packages were selected unexpectedly"
  exit 1
fi

# The firmware provides BTF itself; do not embed a separate BTF image.
if grep -Fxq 'CONFIG_PACKAGE_vmlinux-btf=y' "$CONFIG"; then
  echo "::error::Unexpected external vmlinux-btf package with built-in kernel BTF"
  exit 1
fi

printf 'Verified: IPQ95XX Xiaomi BE7000 (non-ES), honk + LuCI, MosDNS, no EasyTier, nlbwmon, Chinese LuCI, BTF + XDP.\n'
