# 高质量<免费>交流群

[IPQ技术讨论群](https://qm.qq.com/q/v7nMhzB4oU)

# 高质量<付费>中转站

[Krill-Code](https://www.krill-code.com/register?invite=959NHQCZGT)

# 本地编译器

https://github.com/VIKINGYFY/OWRT-Tools.git

# 自用修改版插件

https://github.com/VIKINGYFY/packages.git

# OpenWRT-CI

官方版：

https://github.com/immortalwrt/immortalwrt.git

自用版：

https://github.com/VIKINGYFY/immortalwrt.git

# U-BOOT

高通版-沉心：

https://github.com/chenxin527/uboot-qsdk12.5-build.git

高通版-小猪：

https://github.com/1980490718/u-boot-2016.git

联发科-全新版：

https://github.com/VIKINGYFY/UBOOT-CI/releases

联发科-官方版：

https://drive.wrt.moe/uboot/mediatek

# 固件简要说明

本 Fork 只编译 `VIKINGYFY/immortalwrt` 的 `owrt` 分支。

- 构建目标：Qualcomm IPQ95XX / Xiaomi BE7000（非 ES），`qualcommbe-ipq95xx-xiaomi_be7000`；另提供 x86_64 通用 PC / 虚拟机镜像。
- 软件包：禁用 `luci-app-homeproxy`；启用 `luci-app-daede`（只选择 `daed` 后端）、`luci-app-mosdns`，BE7000 已移除 EasyTier 和 Samba4（x86_64 仍保留 EasyTier）；同时内置 `nlbwmon` + `luci-app-nlbwmon` + 简体中文翻译，用于按主机统计流量。
- 额外内核支持：BTF / XDP，用于 daed eBPF CO-RE。
- 工作流：每日约 05:21（北京时间）通过 `Auto-Clean` 完成后触发 `IPQ95XX-BE7000`；也可以手动运行。
- 测试工作流：`IPQ95XX-TEST` 默认仅验证并发布最终 `.config`；正式编译请用 `IPQ95XX-BE7000`。
- 容量门禁：构建后解析 factory UBI 卷表，按 BE7000 实测 UBI 几何估算 autoresize 后 Overlay 空间；预计可用空间低于 5 MiB 时阻止发布。
- Release：BE7000 附带 UBI 布局报告；x86_64 使用 ext4 根文件系统、1 GiB 虚拟磁盘，输出 BIOS / UEFI、QCOW2 / VMDK 镜像。

固件信息里的时间为编译开始时间，便于核对上游源码提交。

# 目录简要说明

workflows——自定义CI配置

Scripts——自定义脚本

Config——自定义配置

## Star History

<a href="https://www.star-history.com/?repos=vikingyfy%2Fopenwrt-ci&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=vikingyfy/openwrt-ci&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=vikingyfy/openwrt-ci&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=vikingyfy/openwrt-ci&type=date&legend=top-left" />
 </picture>
</a>
