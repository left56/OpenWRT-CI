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
CONFIG_PACKAGE_luci-app-daede=y
CONFIG_PACKAGE_luci-app-daede_daed=y
CONFIG_PACKAGE_daed=y
CONFIG_PACKAGE_luci-app-mosdns=y
CONFIG_PACKAGE_mosdns=y
CONFIG_PACKAGE_easytier-noweb=y
CONFIG_PACKAGE_luci-app-easytier=y
CONFIG_PACKAGE_luci-i18n-easytier-zh-cn=y
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
  package/luci-app-easytier/easytier-noweb/Makefile \
  package/luci-app-easytier/luci-app-easytier/Makefile \
  feeds/packages/net/nlbwmon/Makefile \
  feeds/luci/applications/luci-app-nlbwmon/Makefile \
  feeds/luci/applications/luci-app-nlbwmon/po/zh_Hans/nlbwmon.po; do
  if [[ ! -f "$package_makefile" ]]; then
    echo "::error::Required package source is missing: $package_makefile"
    exit 1
  fi
done

# Reject unwanted alternatives.
if grep -Fxq 'CONFIG_PACKAGE_easytier=y' "$CONFIG"; then
  echo "::error::The Web-enabled EasyTier core must not be selected; use easytier-noweb"
  exit 1
fi

if grep -Eq '^CONFIG_PACKAGE_(luci-app-homeproxy|dae)=y$' "$CONFIG"; then
  echo "::error::HomeProxy or standalone dae was selected unexpectedly"
  exit 1
fi

if grep -Fxq 'CONFIG_PACKAGE_luci-app-daede_dae=y' "$CONFIG"; then
  echo "::error::luci-app-daede selected standalone dae instead of daed"
  exit 1
fi

# The firmware provides BTF itself; do not select a detached BTF image.
if grep -Fxq 'CONFIG_DAED_USE_VMLINUX_BTF=y' "$CONFIG"; then
  echo "::error::Unexpected detached vmlinux-btf selection with built-in kernel BTF"
  exit 1
fi

printf 'Verified: IPQ95XX Xiaomi BE7000 (non-ES), daed-only, MosDNS, EasyTier-noweb, nlbwmon, Chinese LuCI, BTF + XDP.\n'
