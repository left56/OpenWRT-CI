#!/usr/bin/env python3
"""Inspect a BE7000 factory UBI image and estimate post-flash Overlay capacity.

The device geometry and Overlay baseline are calibrated from the user's live
BE7000 `ubinfo -a` and `df -h` output. UBI volume-table reserved_pebs values
are treated as volume capacity, not as a count of PEBs physically present in
the factory image. This distinction is required for autoresize volumes.
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

MIB = 1024 * 1024
PEB_SIZE = 128 * 1024
LEB_SIZE = 126976
DEVICE_TOTAL_LEBS = 640
DEVICE_NAMED_VOLUME_LEB_BUDGET = 616
DEVICE_RESERVED_PEBS = 20
BASELINE_ROOTFS_DATA_LEBS = 154
BASELINE_OVERLAY_FREE_MIB = 13.4
MIN_OVERLAY_FREE_MIB = 5.0
UBI_LAYOUT_VOLUME_ID = 0x7FFFEFFF
UBI_VTBL_RECORD_SIZE = 172
UBI_VTBL_RECORD_COUNT = 128
UBI_VTBL_AUTORESIZE_FLG = 1
EXPECTED_VOLUMES = {"kernel", "rootfs", "rootfs_data"}


def mib(value: int | float) -> float:
    return float(value) / MIB


def parse_volume_table(data: bytes) -> dict[str, dict[str, int | bool]]:
    expected_size = UBI_VTBL_RECORD_SIZE * UBI_VTBL_RECORD_COUNT
    if len(data) < expected_size:
        raise ValueError(
            f"Truncated UBI volume table: {len(data)} bytes, expected {expected_size}"
        )

    volumes: dict[str, dict[str, int | bool]] = {}
    for volume_id in range(UBI_VTBL_RECORD_COUNT):
        record = data[
            volume_id * UBI_VTBL_RECORD_SIZE:
            (volume_id + 1) * UBI_VTBL_RECORD_SIZE
        ]
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
            "id": volume_id,
            "reserved_pebs": reserved_pebs,
            "autoresize": bool(record[144] & UBI_VTBL_AUTORESIZE_FLG),
        }

    missing = EXPECTED_VOLUMES - volumes.keys()
    extra = volumes.keys() - EXPECTED_VOLUMES
    if missing:
        raise ValueError("Missing UBI volumes: " + ", ".join(sorted(missing)))
    if extra:
        raise ValueError("Unexpected UBI volumes: " + ", ".join(sorted(extra)))
    return volumes


def read_ubi_layout(path: Path) -> tuple[dict, dict, int, int]:
    image_size = path.stat().st_size
    if image_size <= 0 or image_size % PEB_SIZE:
        raise ValueError(
            f"Factory UBI size {image_size} is not a positive multiple of "
            f"{PEB_SIZE} bytes"
        )

    image_pebs = image_size // PEB_SIZE
    volume_lnums: dict[int, set[int]] = {}
    layout_lnums: set[int] = set()
    volumes = None
    valid_ubi_pebs = 0

    with path.open("rb") as image:
        for peb_index in range(image_pebs):
            peb_offset = peb_index * PEB_SIZE
            image.seek(peb_offset)
            ec_header = image.read(64)
            if len(ec_header) != 64 or ec_header[:4] != b"UBI#":
                continue

            vid_header_offset = struct.unpack_from(">I", ec_header, 16)[0]
            data_offset = struct.unpack_from(">I", ec_header, 20)[0]
            if not (64 <= vid_header_offset <= PEB_SIZE - 64
                and vid_header_offset + 64 <= data_offset < PEB_SIZE):
                raise ValueError(
                    f"Invalid UBI EC offsets in PEB {peb_index}: "
                    f"VID={vid_header_offset}, data={data_offset}"
                )
            if PEB_SIZE - data_offset != LEB_SIZE:
                raise ValueError(
                    f"Unexpected PEB/LEB geometry in PEB {peb_index}: "
                    f"PEB={PEB_SIZE}, data_offset={data_offset}, "
                    f"derived LEB={PEB_SIZE - data_offset}, expected={LEB_SIZE}"
                )

            image.seek(peb_offset + vid_header_offset)
            vid_header = image.read(64)
            if len(vid_header) != 64 or vid_header[:4] != b"UBI!":
                continue

            volume_id, logical_eraseblock = struct.unpack_from(">II", vid_header, 8)
            valid_ubi_pebs += 1
            if volume_id == UBI_LAYOUT_VOLUME_ID:
                layout_lnums.add(logical_eraseblock)
                # The layout volume has two copies. Read the first available copy.
                if volumes is None:
                    image.seek(peb_offset + data_offset)
                    table = image.read(
                        UBI_VTBL_RECORD_SIZE * UBI_VTBL_RECORD_COUNT
                    )
                    volumes = parse_volume_table(table)
            else:
                volume_lnums.setdefault(volume_id, set()).add(logical_eraseblock)

    if volumes is None or not layout_lnums:
        raise ValueError("UBI layout volume / volume table was not found")

    physical_counts: dict[str, int] = {}
    for name, volume in volumes.items():
        volume_id = int(volume["id"])
        count = len(volume_lnums.get(volume_id, set()))
        reserved_pebs = int(volume["reserved_pebs"])
        if count > reserved_pebs:
            raise ValueError(
                f"{name} has {count} physical PEBs in the image but the volume "
                f"table reserves only {reserved_pebs}"
            )
        if name in {"kernel", "rootfs"} and count == 0:
            raise ValueError("Factory image contains no physical data PEBs for " + name)

        physical_counts[name] = count

    if int(volumes["rootfs_data"]["reserved_pebs"]) <= 0:
        raise ValueError("rootfs_data has no reserved PEB capacity")
    if not bool(volumes["rootfs_data"]["autoresize"]):
        raise ValueError("rootfs_data volume is not marked autoresize")

    # Crucial: reserved_pebs is volume capacity. It is NOT a count of the PEBs
    # physically emitted into the factory image, especially for autoresize.
    return volumes, physical_counts, image_pebs, len(layout_lnums)


def write_github_env(path: Path | None, values: dict[str, str], summary: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")
        output.write("LAYOUT_RELEASE_SUMMARY<<BE7000_LAYOUT_EOF\n")
        output.write(summary.rstrip() + "\n")
        output.write("BE7000_LAYOUT_EOF\n")


def main(() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factory-image", required=True, type=Path)
    parser.add_argument("--sysupgrade-image", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--github-env",
        type=Path,
        default=Path(os.environ["GITHUB_ENV"]) if os.environ.get("GITHUB_ENV") else None,
    )
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)

    try:
        if not args.factory_image.is_file():
            raise ValueError("Factory UBI image not found: " + str(args.factory_image))
        if not args.sysupgrade_image.is_file():
            raise ValueError("Sysupgrade image not found: " + str(args.sysupgrade_image))

        volumes, physical_counts, image_pebs, layout_pebs = read_ubi_layout(args.factory_image)

        kernel_lebs = int(volumes["kernel"]["reserved_pebs"])
        rootfs_lebs = int(volumes["rootfs"]["reserved_pebs"])
        table_rootfs_data_lebs = int(volumes["rootfs_data"]["reserved_pebs"])

        if kernel_lebs + rootfs_lebs >= DEVICE_NAMED_VOLUME_LEB_BUDGET:
            raise ValueError(
                "Kernel + RootFS exceed the measured BE7000 named-volume LEB budget: "
                f"{kernel_lebs} + {rootfs_lebs} >= {DEVICE_NAMED_VOLUME_LEB_BUDGET}"
            )

        rootfs_data_lebs = DEVICE_NAMED_VOLUME_LEB_BUDGET = 616 - kernel_lebs - rootfs_lebs
        if table_rootfs_data_lebs > rootfs_data_lebs:
            raise ValueError(
                "rootfs_data volume-table reservation exceeds the BE7000 autoresize "
                f"capacity: table={table_rootfs_data_lebs} LEBs, "
                f"device_budget={rootfs_data_lebs} LEBs"
            )

        overlay_free = BASELINE_OVERLAY_FREE_MIB + (rootfs_data_lebs - BASELINE_ROOTFS_DATA_LEBS) * (LEB_SIZE / MIB)
        status = "PASS" if overlay_free >= MIN_OVERLAY_FREE_MIB else "FAIL"

        factory_bytes = args.factory_image.stat().st_size
        sysupgrade_bytes = args.sysupgrade_image.stat().st_size
        kernel_bytes = kernel_lebs * LEB_SIZE
        rootfs_bytes = rootfs_lebs * LEB_SIZE
        rootfs_data_bytes = rootfs_data_lebs * LEB_SIZE
        table_data_bytes = table_rootfs_data_lebs * LEB_SIZE
        non_volume_lebs = DEVICE_TOTAL_LEBS - DEVICE_NAMED_VOLUME_LEB_BUDGET

        summary = "\n".join([
            "| Metric | Result |",
            "|---|---:|",
            f"| Capacity gate | **{status}** (minimum {MIN_OVERLAY_FREE_MIB:.1f} MiB free) |",
            f"| Factory UBI image | {mib(factory_bytes):.2f} MiB ({image_pebs} PEBs) |",
            f"| Sysupgrade image | {mib(sysupgrade_bytes):.2f} MiB |",
            f"| Kernel volume capacity | {mib(kernel_bytes):.2f} MiB ({kernel_lebs} LEBs) |",
            f"| RootFS volume capacity | {mib(rootfs_bytes):.2f} MiB ({rootfs_lebs} LEBs) |",
             f"| RootFS Data after autoresize | {mib(rootfs_data_bytes):.2f} MiB ({rootfs_data_lebs} LEBs) |",
            f"| Estimated Overlay free space | **{overlay_free:.2f} MiB** |",
        ])

        report = "\n".join([
            "# Xiaomi BE7000 Firmware Layout",
            "",
            f*"Capacity gate: {status}** — minimum estimated Overlay free space: {MIN_OVERLAY_FREE_MIB:.1f} MiB","",
            "## Device UBI geometry",
            "",
            "| Property | Value |",
            "|---|---:|",
             f"| Physical eraseblock (PEB) | {PEB_SIZE // 1024} KiB |",
             f"| Logical eraseblock (LEB) | {LEB_SIZE // 1024} KiB |",
              f"| Device total logical eraseblocks | {DEVICE_TOTAL_LEBS} |",
                f"| Named-volume LEB budget | {DEVICE_NAMED_VOLUME_LEB_BUDGET} |",
                f"| Difference outside named-volume budget | {non_volume_lebs} LEBs |",
                f"| Reserved physical eraseblocks | {DEVICE_RESERVED_PEBS} PEBs |",
                "",
                "The device geometry is calibrated from the supplied live BE7000 `ubinfo -a` and `df -h` output. The 24-LEB difference is not treated as free Overlay capacity. The 20 reserved PEBs are reported separately and are not subtracted a second time.",
                "",
                "## Factory UBI image and volume table",
                "",
                "| Item | Volume-table capacity | Physical PEBs in factory image |",
                "|---|---:|---:|",
                f"| Kernel | {kernel_lebs} LEBs ({mib(kernel_bytes):.2f} MiB) | {physical_counts['kernel']} |",
                f"| RootFS | {rootfs_lebs} LEBs ({mib(rootfs_bytes):.2f} MiB) | {physical_counts['rootfs']} |",
                f"| RootFS Data | {table_rootfs_data_lebs} LEBs ({mib(table_data_bytes):.2f} MiB) | {physical_counts['rootfs_data']} |",
                f"| UBI layout volume copies found | {layout_pebs} |",
                f"| Factory UBI file | {mib(factory_bytes):.2f} MiB ({image_pebs} PEBs) |",
                f"| Sysupgrade file | {mib(sysupgrade_bytes):.2f} MiB |",
                "",
                "`reserved_pebs` is the volume-table capacity, not the number of PEBs physically present in the factory image. The `rootfs_data` volume is marked `autoresize`; its final capacity is estimated from the device's measured named-volume LEB budget, not from the factory file length.",
                "",
                "## Post-flash Overlay estimate",
                "",
                f"- RootFS Data capacity after autoresize: **{mib(rootfs_data_bytes):.2f} MiB** ({rootfs_data_lebs} LEBs).",
                f"- Estimated Overlay filesystem available: **{overlay_free:.2f} MiB**.",
                f"- Minimum allowed Overlay free space: **{MIN_OVERLAY_FREE_MIB:.2f} MiB**.",
                "- Estimate is calibrated from the supplied live device snapshot: 154 RootFS Data LEBs corresponded to 13.4 MiB free. Each additional RootFS LEB reduces the autoresized RootFS Data volume by one LEB. Actual UBIFS metadata and post-boot writes can change the final `df -h` result.",
                "",
                f"**Result: {status}**",
                "",
            ])
        args.report.write_text(report, encoding="utf-8")

        env_values = {
            "LAYOUT_STATUS": status,
            "LAYOUT_FACTORY_UBI_MIB": f"{mib(factory_bytes):.2f}",
            "LAYOUT_SYSUPGRADE_MIB": f"{mib(sysupgrade_bytes):.2f}",
            "LAYOUT_FACTORY_UBI_PEBS": str(image_pebs),
            "LAYOUT_UBI_LAYOUT_PEBS": str(layout_pebs),
            "LAYOUT_KERNEL_LEBS": str(kernel_lebs),
            "LAYOUT_ROOTFS_LEBS": str(rootfs_lebs),
            "LAYOUT_ROOTFS_DATA_TABLE_LEBS": str(table_rootfs_data_lebs),
            "LAYOUT_ROOTFS_DATA_LEBS": str(rootfs_data_lebs),
            "LAYOUT_OVERLAY_AVAILABLE_MIB": f"{overlay_free:.2f}",
        }
        write_github_env(args.github_env, env_values, summary)
        print(report)

        if status == "FAIL":
            print(
                f"::error::BE7000 Overlay estimate {overlay_free:.2f} MiB is below "
                f"{MIN_OVERLAY_FREE_MIB:.2f} MiB"
            )
            return 1

        print(
            f"::notice title=BE7000 capacity gate::PASS - estimated Overlay free "
            f"space {overlay_free:.2f} MiB"
        )
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
        write_github_env(
            args.github_env,
            {"LAYOUT_STATUS": "ERROR"},
            "| Metric | Result |\n|---|---|\n| Capacity gate | **ERROR** — layout analysis failed; firmware not published |",
        )
        print(report, file=sys.stderr)
        print(f"::error::BE7000 UBI layout analysis failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
