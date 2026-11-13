#!/usr/bin/env python3
"""Analyze BE7000 factory UBI volume table and estimate post-flash Overlay."""
import argparse
import os
import struct
import sys
from pathlib import Path

MIB = 1024 * 1024
PEB_SIZE = 128 * 1024
LEB_SIZE = 126976
DEVICE_TOTAL_LEBS = 640
DEVICE_VOLUME_LEB_BUDGET = 616
RESERVED_PEBS = 20
BASELINE_DATA_LEBS = 154
BASELINE_OVERLAY_FREE_MIB = 13.4
MIN_OVERLAY_FREE_MIB = 5.0
LAYOUT_VOLUME_ID = 0x7FFFEFFF
VTBL_RECORD_SIZE = 172
VTBL_RECORD_COUNT = 128
AUTORESIZE_FLAG = 1
EXPECTED = {"kernel", "rootfs", "rootfs_data"}


def mib(value):
    return value / MIB


def parse_table(data):
    required = VTBL_RECORD_SIZE * VTBL_RECORD_COUNT
    if len(data) < required:
        raise ValueError(f"Truncated UBI volume table: {len(data)} < {required}")
    volumes = {}
    for index in range(VTBL_RECORD_COUNT):
        record = data[index * VTBL_RECORD_SIZE:(index + 1) * VTBL_RECORD_SIZE]
        reserved_pebs = struct.unpack_from(">I", record, 0)[0]
        name_len = struct.unpack_from(">H", record, 14)[0]
        if reserved_pebs == 0 or name_len == 0:
            continue
        if name_len > 127:
            raise ValueError(f"Invalid UBI volume name length: {name_len}")
        name = record[16:16 + name_len].decode("utf-8", errors="strict")
        if name in volumes:
            raise ValueError(f"Duplicate UBI volume name: {name}")
        volumes[name] = {
            "id": index,
            "reserved_pebs": reserved_pebs,
            "autoresize": bool(record[144] & AUTORESIZE_FLAG),
        }
    missing = EXPECTED - volumes.keys()
    extra = volumes.keys() - EXPECTED
    if missing:
        raise ValueError("Missing UBI volumes: " + ", ".join(sorted(missing)))
    if extra:
        raise ValueError("Unexpected UBI volumes: " + ", ".join(sorted(extra)))
    return volumes


def read_layout(path):
    size = path.stat().st_size
    if size <= 0 or size % PEB_SIZE:
        raise ValueError(f"Factory UBI size {size} is not a multiple of {PEB_SIZE}")
    image_pebs = size // PEB_SIZE
    physical_lnums = {}
    layout_lnums = set()
    volumes = None

    with path.open("rb") as image:
        for peb_index in range(image_pebs):
            base = peb_index * PEB_SIZE
            image.seek(base)
            ec = image.read(64)
            if len(ec) != 64 or ec[:4] != b"UBI#":
                continue
            vid_offset = struct.unpack_from(">I", ec, 16)[0]
            data_offset = struct.unpack_from(">I", ec, 20)[0]
            if not (64 <= vid_offset <= PEB_SIZE - 64 and
                    vid_offset + 64 <= data_offset < PEB_SIZE):
                raise ValueError(f"Invalid UBI EC offsets at PEB {peb_index}")
            if PEB_SIZE - data_offset != LEB_SIZE:
                raise ValueError(f"Unexpected PEB/LEB geometry at PEB {peb_index}")
            image.seek(base + vid_offset)
            vid = image.read(64)
            if len(vid) != 64 or vid[:4] != b"UBI!":
                continue
            volume_id, lnum = struct.unpack_from(">II", vid, 8)
            if volume_id == LAYOUT_VOLUME_ID:
                layout_lnums.add(lnum)
                if volumes is None:
                    image.seek(base + data_offset)
                    volumes = parse_table(image.read(VTBL_RECORD_SIZE * VTBL_RECORD_COUNT))
            else:
                physical_lnums.setdefault(volume_id, set()).add(lnum)

    if volumes is None or not layout_lnums:
        raise ValueError("UBI layout volume / volume table not found")

    physical_counts = {}
    for name, volume in volumes.items():
        count = len(physical_lnums.get(volume["id"], set()))
        if count > volume["reserved_pebs"]:
            raise ValueError(f"{name}: physical PEB count exceeds volume-table capacity")
        if name in {"kernel", "rootfs"} and count == 0:
            raise ValueError(f"Factory image has no physical data PEBs for {name}")
        physical_counts[name] = count

    if volumes["rootfs_data"]["reserved_pebs"] <= 0:
        raise ValueError("rootfs_data has no reserved PEB capacity")
    if not volumes["rootfs_data"]["autoresize"]:
        raise ValueError("rootfs_data is not marked autoresize")
    return volumes, physical_counts, image_pebs, len(layout_lnums)


def write_env(path, values, summary):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")
        output.write("LAYOUT_RELEASE_SUMMARY<<BE7000_LAYOUT_EOF\n")
        output.write(summary.rstrip() + "\n")
        output.write("BE7000_LAYOUT_EOF\n")


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
        if not args.factory_image.is_file() or not args.sysupgrade_image.is_file():
            raise ValueError("Factory UBI or sysupgrade image is missing")
        volumes, physical, image_pebs, layout_copies = read_layout(args.factory_image)
        kernel_lebs = volumes["kernel"]["reserved_pebs"]
        rootfs_lebs = volumes["rootfs"]["reserved_pebs"]
        table_data_lebs = volumes["rootfs_data"]["reserved_pebs"]

        # reserved_pebs in the volume table is the capacity required on-device.
        # rootfs_data is autoresize, so only its final capacity is derived from
        # the measured BE7000 user-volume budget.
        kernel_rootfs_lebs = kernel_lebs + rootfs_lebs
        data_lebs = DEVICE_VOLUME_LEB_BUDGET - kernel_rootfs_lebs
        capacity_errors = []
        if data_lebs <= 0:
            capacity_errors.append(
                f"Kernel + RootFS require {kernel_lebs} + {rootfs_lebs} = "
                f"{kernel_rootfs_lebs} LEBs, but the BE7000 named-volume budget "
                f"is {DEVICE_VOLUME_LEB_BUDGET} LEBs (excess {kernel_rootfs_lebs - DEVICE_VOLUME_LEB_BUDGET} LEBs). "
                "No RootFS Data capacity remains."
            )
        elif table_data_lebs > data_lebs:
            capacity_errors.append(
                f"rootfs_data table reservation {table_data_lebs} LEBs exceeds "
                f"autoresize capacity {data_lebs} LEBs."
            )

        overlay_free = None
        if data_lebs > 0:
            overlay_free = BASELINE_OVERLAY_FREE_MIB + (
                data_lebs - BASELINE_DATA_LEBS
            ) * (LEB_SIZE / MIB)
            if overlay_free < MIN_OVERLAY_FREE_MIB:
                capacity_errors.append(
                    f"Estimated Overlay free space {overlay_free:.2f} MiB is below "
                    f"the {MIN_OVERLAY_FREE_MIB:.2f} MiB minimum."
                )
        status = "PASS" if not capacity_errors else "FAIL"

        factory_bytes = args.factory_image.stat().st_size
        sysupgrade_bytes = args.sysupgrade_image.stat().st_size
        kernel_bytes = kernel_lebs * LEB_SIZE
        rootfs_bytes = rootfs_lebs * LEB_SIZE
        table_data_bytes = table_data_lebs * LEB_SIZE
        data_bytes = max(data_lebs, 0) * LEB_SIZE
        other_lebs = DEVICE_TOTAL_LEBS - DEVICE_VOLUME_LEB_BUDGET
        overlay_free_text = (
            f"{overlay_free:.2f} MiB" if overlay_free is not None
            else "N/A (no RootFS Data capacity)"
        )
        capacity_error_text = "\n".join(f"- {error}" for error in capacity_errors) or "- None"

        summary = "\n".join([
            "| Metric | Result |",
            "|---|---:|",
            f"| Capacity gate | **{status}** (minimum {MIN_OVERLAY_FREE_MIB:.1f} MiB free) |",
            f"| Factory UBI image | {mib(factory_bytes):.2f} MiB ({image_pebs} PEBs) |",
            f"| Sysupgrade image | {mib(sysupgrade_bytes):.2f} MiB |",
            f"| Kernel volume | {mib(kernel_bytes):.2f} MiB ({kernel_lebs} LEBs) |",
            f"| RootFS volume | {mib(rootfs_bytes):.2f} MiB ({rootfs_lebs} LEBs) |",
            f"| Kernel + RootFS allocation | {kernel_rootfs_lebs} / {DEVICE_VOLUME_LEB_BUDGET} LEBs |",
            f"| RootFS Data after autoresize | {mib(data_bytes):.2f} MiB ({max(data_lebs, 0)} LEBs) |",
            f"| Estimated Overlay free space | **{overlay_free_text}** |",
        ])
        report = "\n".join([
            "# Xiaomi BE7000 Firmware Layout",
            "",
            f"**Capacity gate: {status}** — minimum estimated Overlay free space: {MIN_OVERLAY_FREE_MIB:.1f} MiB.",
            "",
            "## Capacity calculation",
            "",
            f"- Kernel volume table allocation: {kernel_lebs} LEBs ({mib(kernel_bytes):.2f} MiB); physical PEBs in factory image: {physical['kernel']}.",
            f"- RootFS volume table allocation: {rootfs_lebs} LEBs ({mib(rootfs_bytes):.2f} MiB); physical PEBs in factory image: {physical['rootfs']}.",
            f"- Combined Kernel + RootFS: **{kernel_rootfs_lebs} / {DEVICE_VOLUME_LEB_BUDGET} LEBs**.",
            f"- RootFS Data table initial reservation: {table_data_lebs} LEBs.",
            f"- RootFS Data after autoresize: {max(data_lebs, 0)} LEBs.",
            f"- Capacity errors: {capacity_error_text}.",
            "",
            "## Device UBI geometry",
            "",
            "| Property | Value |",
            "|---|---:|",
            f"| PEB size | {PEB_SIZE // 1024} KiB |",
            f"| LEB size | {LEB_SIZE // 1024} KiB |",
            f"| Device total LEBs | {DEVICE_TOTAL_LEBS} |",
            f"| Named-volume LEB budget | {DEVICE_VOLUME_LEB_BUDGET} |",
            f"| Difference outside named-volume budget | {other_lebs} LEBs |",
            f"| Reserved physical eraseblocks | {RESERVED_PEBS} PEBs |",
            "",
            "Geometry is calibrated from the supplied live BE7000 ubinfo/df output. The 24-LEB difference is not counted as Overlay space; reserved PEBs are reported separately.",
            "",
            "## Factory UBI volume table vs physical image",
            "",
            "| Volume | Table capacity | Physical PEBs in image |",
            "|---|---:|---:|",
            f"| Kernel | {kernel_lebs} LEBs ({mib(kernel_bytes):.2f} MiB) | {physical['kernel']} |",
            f"| RootFS | {rootfs_lebs} LEBs ({mib(rootfs_bytes):.2f} MiB) | {physical['rootfs']} |",
            f"| RootFS Data | {table_data_lebs} LEBs ({mib(table_data_bytes):.2f} MiB) | {physical['rootfs_data']} |",
            f"| UBI layout copies found | {layout_copies} |",
            f"| Factory UBI file | {mib(factory_bytes):.2f} MiB ({image_pebs} PEBs) |",
            f"| Sysupgrade file | {mib(sysupgrade_bytes):.2f} MiB |",
            "",
            "reserved_pebs is volume capacity, not the count of PEBs physically present in the factory image. rootfs_data is autoresize; its final capacity is estimated from the measured device volume budget.",
            "",
            "## Post-flash Overlay estimate",
            "",
            f"- RootFS Data after autoresize: **{mib(data_bytes):.2f} MiB** ({max(data_lebs, 0)} LEBs).",
            f"- Estimated Overlay free space: **{overlay_free_text}**.",
            f"- Minimum allowed Overlay free space: **{MIN_OVERLAY_FREE_MIB:.2f} MiB**.",
            "- Baseline: 154 RootFS Data LEBs corresponded to 13.4 MiB free on the supplied live device. Each extra RootFS Data LEB changes the estimate by one LEB. UBIFS metadata and runtime writes can affect final df output.",
            "",
            f"**Result: {status}**",
            "",
        ])
        args.report.write_text(report, encoding="utf-8")
        values = {
            "LAYOUT_STATUS": status,
            "LAYOUT_FACTORY_UBI_MIB": f"{mib(factory_bytes):.2f}",
            "LAYOUT_SYSUPGRADE_MIB": f"{mib(sysupgrade_bytes):.2f}",
            "LAYOUT_FACTORY_UBI_PEBS": str(image_pebs),
            "LAYOUT_UBI_LAYOUT_PEBS": str(layout_copies),
            "LAYOUT_KERNEL_LEBS": str(kernel_lebs),
            "LAYOUT_ROOTFS_LEBS": str(rootfs_lebs),
            "LAYOUT_ROOTFS_DATA_TABLE_LEBS": str(table_data_lebs),
            "LAYOUT_ROOTFS_DATA_LEBS": str(max(data_lebs, 0)),
            "LAYOUT_KERNEL_ROOTFS_TOTAL_LEBS": str(kernel_rootfs_lebs),
            "LAYOUT_KERNEL_PHYSICAL_PEBS": str(physical["kernel"]),
            "LAYOUT_ROOTFS_PHYSICAL_PEBS": str(physical["rootfs"]),
            "LAYOUT_OVERLAY_AVAILABLE_MIB": f"{overlay_free:.2f}" if overlay_free is not None else "N/A",
        }
        write_env(args.github_env, values, summary)
        print(report)
        if status == "FAIL":
            for error in capacity_errors:
                print(f"::error::{error}")
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
        print(f"::error::BE7000 UBI layout analysis failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
