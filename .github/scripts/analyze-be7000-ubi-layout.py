#!/usr/bin/env python3
"""Parse a BE7000 factory UBI image and estimate post-flash Overlay capacity.

The device geometry and baseline are calibrated from the supplied live BE7000
ubinfo -a and df -h output. Volume sizes are read from the actual UBI volume
table; rootfs_data is calculated after its autoresize flag is applied.
"""
import argparse
import os
import struct
import sys
from pathlib import Path

MIB = 1024 * 1024
PEB = 128 * 1024
LEB = 126976
TOTAL_LEBS = 640
VOLUME_LEB_BUDGET = 616
RESERVED_PEBS = 20
BASELINE_DATA_LEBS = 154
BASELINE_OVERLAY_FREE = 13.4
MIN_OVERLAY_FREE = 5.0
LAYOUT_ID = 0x7FFFEFFF
RECORD_SIZE = 172
RECORD_COUNT = 128
AUTORESIZE_FLAG = 1
EXPECTED = {"kernel", "rootfs", "rootfs_data"}


def mib(value):
    return value / MIB


def read_ubi_volumes(path):
    size = path.stat().st_size
    if size <= 0 or size % PEB:
        raise ValueError("Factory UBI size is not a positive multiple of 128 KiB")
    image_pebs = size // PEB
    with path.open("rb") as image:
        for index in range(image_pebs):
            base = index * PEB
            image.seek(base)
            ec = image.read(64)
            if len(ec) != 64 or ec[:4] != b"UBI#":
                continue
            vid_off = struct.unpack_from(">I", ec, 16)[0]
            data_off = struct.unpack_from(">I", ec, 20)[0]
            if not (64 <= vid_off <= PEB - 64 and vid_off + 64 <= data_off < PEB):
                continue
            if PEB - data_off != LEB:
                raise ValueError("Unexpected PEB/LEB geometry in UBI EC header")
            image.seek(base + vid_off)
            vid = image.read(64)
            if len(vid) != 64 or vid[:4] != b"UBI!":
                continue
            vol_id, lnum = struct.unpack_from(">II", vid, 8)
            if vol_id != LAYOUT_ID or lnum != 0:
                continue
            image.seek(base + data_off)
            table = image.read(RECORD_SIZE * RECORD_COUNT)
            if len(table) != RECORD_SIZE * RECORD_COUNT:
                raise ValueError("Truncated UBI volume table")
            volumes = {}
            for i in range(RECORD_COUNT):
                rec = table[i * RECORD_SIZE:(i + 1) * RECORD_SIZE]
                reserved = struct.unpack_from(">I", rec, 0)[0]
                name_len = struct.unpack_from(">H", rec, 14)[0]
                if reserved == 0 or name_len == 0 or name_len > 127:
                    continue
                name = rec[16:16 + name_len].decode("utf-8", errors="strict")
                volumes[name] = {
                    "lebs": reserved,
                    "autoresize": bool(rec[144] & AUTORESIZE_FLAG),
                }
            missing = EXPECTED - volumes.keys()
            extra = volumes.keys() - EXPECTED
            if missing:
                raise ValueError("Missing UBI volumes: " + ", ".join(sorted(missing)))
            if extra:
                detail = ", ".join(f"{n}={volumes[n]['lebs']} LEBs" for n in sorted(extra))
                raise ValueError("Unexpected UBI volumes change RootFS Data budget: " + detail)
            initial = sum(volumes[n]["lebs"] for n in EXPECTED)
            if image_pebs < initial + 2:
                raise ValueError("Factory image is smaller than volume allocation plus two layout PEBs")
            return volumes, image_pebs
    raise ValueError("UBI layout volume not found in factory image")


def write_env(path, values, summary):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        for key, value in values.items():
            out.write(f"{key}={value}\n")
        out.write("LAYOUT_RELEASE_SUMMARY<<BE7000_LAYOUT_EOF\n")
        out.write(summary.rstrip() + "\n")
        out.write("BE7000_LAYOUT_EOF\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factory-image", required=True, type=Path)
    parser.add_argument("--sysupgrade-image", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--github-env", type=Path,
                        default=Path(os.environ["GITHUB_ENV"]) if os.environ.get("GITHUB_ENV") else None)
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)

    try:
        if not args.factory_image.is_file():
            raise ValueError("Factory UBI image not found: " + str(args.factory_image))
        if not args.sysupgrade_image.is_file():
            raise ValueError("Sysupgrade image not found: " + str(args.sysupgrade_image))

        volumes, image_pebs = read_ubi_volumes(args.factory_image)
        kernel_lebs = volumes["kernel"]["lebs"]
        rootfs_lebs = volumes["rootfs"]["lebs"]
        initial_data_lebs = volumes["rootfs_data"]["lebs"]
        if not volumes["rootfs_data"]["autoresize"]:
            raise ValueError("rootfs_data volume is not marked autoresize")

        data_lebs = VOLUME_LEB_BUDGET - kernel_lebs - rootfs_lebs
        if data_lebs < initial_data_lebs or data_lebs <= 0:
            raise ValueError("Post-flash rootfs_data allocation is invalid or smaller than factory image allocation")

        kernel_bytes = kernel_lebs * LEB
        rootfs_bytes = rootfs_lebs * LEB
        data_bytes = data_lebs * LEB
        overlay_free = BASELINE_OVERLAY_FREE + (data_lebs - BASELINE_DATA_LEBS) * (LEB / MIB)
        status = "PASS" if overlay_free >= MIN_OVERLAY_FREE else "FAIL"
        factory_bytes = args.factory_image.stat().st_size
        sysupgrade_bytes = args.sysupgrade_image.stat().st_size
        non_volume_lebs = TOTAL_LEBS - VOLUME_LEB_BUDGET

        summary = "\n".join([
            "| Metric | Result |",
            "|---|---:|",
            f"| Capacity gate | **{status}** (minimum {MIN_OVERLAY_FREE:.1f} MiB free) |",
            f"| Factory UBI image | {mib(factory_bytes):.2f} MiB ({image_pebs} PEBs) |",
            f"| Sysupgrade image | {mib(sysupgrade_bytes):.2f} MiB |",
            f"| Kernel volume | {mib(kernel_bytes):.2f} MiB ({kernel_lebs} LEBs) |",
            f"| RootFS volume | {mib(rootfs_bytes):.2f} MiB ({rootfs_lebs} LEBs) |",
            f"| RootFS Data after autoresize | {mib(data_bytes):.2f} MiB ({data_lebs} LEBs) |",
            f"| Estimated Overlay free space | **{overlay_free:.2f} MiB** |",
        ])
        report = "\n".join([
            "# Xiaomi BE7000 Firmware Layout",
            "",
            f"**Capacity gate: {status}** — minimum estimated Overlay free space: {MIN_OVERLAY_FREE:.1f} MiB.",
            "",
            "## Device UBI geometry",
            "",
            "| Property | Value |",
            "|---|---:|",
            f"| Physical eraseblock (PEB) | {PEB // 1024} KiB |",
            f"| Logical eraseblock (LEB) | {LEB // 1024} KiB |",
            f"| Device total logical eraseblocks | {TOTAL_LEBS} |",
            f"| Measured named-volume LEB budget | {VOLUME_LEB_BUDGET} |",
            f"| Difference outside named-volume budget | {non_volume_lebs} LEBs |",
            f"| Reserved physical eraseblocks | {RESERVED_PEBS} PEBs |",
            "",
            "The volume budget and Overlay baseline are calibrated from the supplied live BE7000 ubinfo and df output: kernel 49 LEBs, rootfs 413 LEBs, rootfs_data 154 LEBs, and 13.4 MiB Overlay available. The 24-LEB difference is not treated as free Overlay capacity. The 20 reserved PEBs are reported separately and are not subtracted a second time. This profile expects exactly the three named UBI volumes; unexpected volumes fail the gate.",
            "",
            "## Actual factory image",
            "",
            "| Image / volume | Allocated size | Detail |",
            "|---|---:|---:|",
            f"| Factory UBI file | {mib(factory_bytes):.2f} MiB | {factory_bytes:,} bytes; {image_pebs} PEBs |",
            f"| Sysupgrade file | {mib(sysupgrade_bytes):.2f} MiB | {sysupgrade_bytes:,} bytes |",
            f"| Kernel volume | {mib(kernel_bytes):.2f} MiB | {kernel_lebs} LEBs |",
            f"| RootFS volume | {mib(rootfs_bytes):.2f} MiB | {rootfs_lebs} LEBs |",
            f"| RootFS Data in factory image | {mib(initial_data_lebs * LEB):.2f} MiB | {initial_data_lebs} initial LEBs; autoresize=true |",
            f"| RootFS Data after autoresize | {mib(data_bytes):.2f} MiB | {data_lebs} estimated LEBs |",
            "",
            "## Overlay estimate and capacity gate",
            "",
            f"- Estimated Overlay free space: **{overlay_free:.2f} MiB**.",
            f"- Minimum allowed Overlay free space: **{MIN_OVERLAY_FREE:.2f} MiB**.",
            f"- Estimated RootFS Data volume after autoresize: {mib(data_bytes):.2f} MiB.",
            "- Estimate is calibrated from the supplied live device snapshot: 154 RootFS Data LEBs corresponded to 13.4 MiB free. Each additional RootFS LEB reduces the autoresized RootFS Data volume by one LEB. Actual UBIFS metadata, reserved blocks, and post-boot writes can change the final df result.",
            "",
            f"**Result: {status}**",
            "",
        ])
        args.report.write_text(report, encoding="utf-8")
        values = {
            "LAYOUT_STATUS": status,
            "LAYOUT_FACTORY_UBI_MIB": f"{mib(factory_bytes):.2f}",
            "LAYOUT_SYSUPGRADE_MIB": f"{mib(sysupgrade_bytes):.2f}",
            "LAYOUT_KERNEL_LEBS": str(kernel_lebs),
            "LAYOUT_ROOTFS_LEBS": str(rootfs_lebs),
            "LAYOUT_ROOTFS_DATA_LEBS": str(data_lebs),
            "LAYOUT_OVERLAY_AVAILABLE_MIB": f"{overlay_free:.2f}",
        }
        write_env(args.github_env, values, summary)
        print(report)
        if status == "FAIL":
            print(f"::error::BE7000 Overlay estimate {overlay_free:.2f} MiB is below {MIN_OVERLAY_FREE:.2f} MiB")
            return 1
        print(f"::notice title=BE7000 capacity gate::PASS - estimated Overlay free space {overlay_free:.2f} MiB")
        return 0
    except Exception as error:
        report = "\n".join([
            "# Xiaomi BE7000 Firmware Layout",
            "",
            "**Capacity gate: ERROR**",
            "",
            "Analysis failed: " + str(error),
            "",
            "Firmware must not be published until the image or layout analysis is corrected.",
            "",
        ])
        args.report.write_text(report, encoding="utf-8")
        write_env(args.github_env, {"LAYOUT_STATUS": "ERROR"},
                  "| Metric | Result |\n|---|---|\n| Capacity gate | **ERROR** — layout analysis failed; firmware not published |")
        print(report, file=sys.stderr)
        print("::error::BE7000 UBI layout analysis failed: " + str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
