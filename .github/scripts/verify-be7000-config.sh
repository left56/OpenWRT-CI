#!/usr/bin/env bash
# Validate the real OpenWrt .config after make defconfig, not just the fragments.
set -euo pipefail

CONFIG="${1:-.config}"
if [[ ! -f "$CONFIG" ]]; then
  echo "::error::Missing OpenWrt config file: $CONFIG"
  exit 1
fi

required=(
  CONFIG_TARGET_qualcommbe=y
  CONFIG_TARGET_qualcommbe_ipq95xx=y
  CONFIG_TARGET_DEVICE_qualcommbe_ipq95xx_DEVICE_xiaomi_be7000=y
  CONFIG_PACKAGE_luci-app-daede=y
  CONFIG_PACKAGE_luci-app-daede_daed=y
  CONFIG_PACKAGE_daed=y
  CONFIG_PACKAGE_luci-app-mosdns=y
  CONFIG_PACKAGE_mosdns=y
  CONFIG_KERNEL_DEBUG_INFO_BTF=y
  CONFIG_KERNEL_XDP_SOCKETS=y
  CONFIG_DAED_USE_KERNEL_BTF=y
)

for setting in "${required[@]}"; do
  if ! grep -Fxq -- "$setting" "$CONFIG"; then
    echo "::error::Required config was not selected after defconfig: $setting"
    exit 1
  fi
done

expected_device="CONFIG_TARGET_DEVICE_qualcommbe_ipq95xx_DEVICE_xiaomi_be7000=y"
selected_devices="$(grep -E '^CONFIG_TARGET_DEVICE_[^=]+=y$' "$CONFIG" || true)"
if [[ "$selected_devices" != "$expected_device" ]]; then
  echo "::error::Expected only Xiaomi BE7000 (non-ES), got:"
  printf '%s\n' "$selected_devices"
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

printf 'Verified: IPQ95XX Xiaomi BE7000 (non-ES), daed-only, MosDNS, BTF + XDP.\n'
