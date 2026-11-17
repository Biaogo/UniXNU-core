#!/usr/bin/env python3
"""Synthetic regressions for bounded pipeline-library format classification.

These fixtures contain no Apple bytes and do not open Metal, compile shaders or
claim that a container can execute.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pipeline_library_format", ROOT / "scripts/pipeline_library_format.py"
)
assert SPEC and SPEC.loader
format_audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = format_audit
SPEC.loader.exec_module(format_audit)


def require(condition: object, message: str) -> None:
    if not condition:
        raise SystemExit("FAIL: " + message)


def _segment_command(
    name: bytes,
    *,
    fileoff: int,
    filesize: int,
    section: bytes | None = None,
) -> bytes:
    section_count = 1 if section is not None else 0
    return (
        struct.pack(
            "<II16sQQQQIIII",
            0x19,
            72 + (80 if section is not None else 0),
            name.ljust(16, b"\0"),
            0,
            filesize,
            fileoff,
            filesize,
            7,
            5,
            section_count,
            0,
        )
        + (section or b"")
    )


def _section_command(
    name: bytes,
    segment: bytes,
    *,
    offset: int,
    size: int,
) -> bytes:
    return struct.pack(
        "<16s16sQQIIIIIIII",
        name.ljust(16, b"\0"),
        segment.ljust(16, b"\0"),
        offset,
        size,
        offset,
        4,
        0,
        0,
        0,
        0,
        0,
        0,
    )


def _embedded_metallib() -> bytes:
    wrapper = (
        b"\xde\xc0\x17\x0b"
        + struct.pack("<4I", 0, 20, 4, 0xFFFFFFFF)
        + b"BC\xc0\xde"
    )
    declared_size = 88 + len(wrapper)
    header = bytearray(88)
    header[:4] = b"MTLB"
    struct.pack_into("<HHHBBHH", header, 4, 1, 1, 0, 0, 1, 12, 0)
    struct.pack_into("<Q", header, 16, declared_size)
    for at in (24, 40, 56):
        struct.pack_into("<QQ", header, at, 88, 0)
    struct.pack_into("<QQ", header, 72, 88, len(wrapper))
    return bytes(header) + wrapper


def macho_gpu_file(
    *,
    filetype: int = 2,
    bad_command: bool = False,
    bad_section_range: bool = False,
    misaligned_command: bool = False,
    no_metallib: bool = False,
) -> bytes:
    embedded = _embedded_metallib()
    text_offset = 0x200
    air = _segment_command(b"__AIR_DATA", fileoff=0, filesize=text_offset)
    section_offset = text_offset - 8 if bad_section_range else text_offset
    metallib_section = (
        None
        if no_metallib
        else _section_command(
            b"__metallib", b"__TEXT", offset=section_offset, size=len(embedded)
        )
    )
    text = _segment_command(
        b"__TEXT",
        fileoff=text_offset,
        filesize=len(embedded),
        section=metallib_section,
    )
    commands = air + text
    if bad_command:
        commands = (
            commands[:76]
            + struct.pack("<I", 160)
            + commands[80:]
        )
    command_bytes = len(commands)
    if misaligned_command:
        commands = commands[:76] + struct.pack("<I", 156) + commands[80:]
        command_bytes = len(commands) + 8
    header = struct.pack(
        "<8I",
        format_audit.MH_MAGIC_64,
        format_audit.CPU_TYPE_GPU,
        97,
        filetype,
        2,
        command_bytes,
        0,
        0,
    )
    data = bytearray(header + commands)
    data.extend(b"\0" * (text_offset - len(data)))
    data.extend(b"\0" * len(embedded) if no_metallib else embedded)
    return bytes(data)


def counted_bundle(*, corrupt_wrapper: bool = False) -> bytes:
    names = [b"secret.first\0", b"secret.second\0"]
    table_size = 4 + sum(len(name) + 4 for name in names)
    payloads = []
    for index in range(2):
        wrapper_magic = (
            b"XXXX" if corrupt_wrapper and index == 1 else b"\xde\xc0\x17\x0b"
        )
        wrapper = (
            wrapper_magic
            + struct.pack("<4I", 0, 20, 4, 0xFFFFFFFF)
            + b"BC\xc0\xde"
        )
        payloads.append(struct.pack("<I", len(wrapper)) + wrapper)
    table = bytearray(struct.pack("<I", len(names)))
    payload_offset = table_size
    for name, payload in zip(names, payloads):
        table.extend(name)
        table.extend(struct.pack("<I", payload_offset))
        payload_offset += len(payload)
    assert len(table) == table_size
    return bytes(table) + b"".join(payloads)


def universal_with_air(magic: int) -> bytes:
    air = b"MTLB" + b"\0" * 84
    header = struct.pack(">II", magic, 2)
    gpu = struct.pack(">5I", format_audit.CPU_TYPE_GPU, 97, 48, 8, 0)
    air_slice = struct.pack(
        ">5I", format_audit.AIR_SLICE_CPU_TYPE, 0, 64, len(air), 0
    )
    return header + gpu + air_slice + b"\0" * (64 - 48) + air


with tempfile.TemporaryDirectory(prefix="pipeline-format-") as temporary:
    root = Path(temporary)

    require(format_audit.MH_OBJECT == 1 and format_audit.MH_EXECUTE == 2,
            "Mach-O file type constants do not match the fixed ABI")
    require(format_audit.UNIVERSAL_MAGIC == b"\xcb\xfe\xba\xbe",
            "universal metallib magic is not the fixed CB FE BA BE")

    source = root / "synthetic.metallib"
    source.write_bytes(b"MTLB" + b"\0" * 60)
    audit = format_audit.inspect_library(source, load_status="loaded")
    require(audit["group"] == "valid_source",
            "loaded MTLB was not a valid source")
    require(audit["evidence"]["grade"] == "load_accepted",
            "valid source does not retain load acceptance evidence")
    require(audit["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest(),
            "whole-file digest was not recorded")

    large = root / "synthetic-large.metallib"
    large.write_bytes(b"MTLB" + b"\0" * format_audit.PREFIX_LIMIT + b"tail")
    audit = format_audit.inspect_library(large, load_status="load_refused")
    require(audit["prefix_bytes_read"] == format_audit.PREFIX_LIMIT
            and audit["size"] == len(large.read_bytes())
            and audit["sha256"] == hashlib.sha256(large.read_bytes()).hexdigest(),
            "bounded prefix and whole-file digest scopes were confused")

    air_fat = root / "synthetic-universal.metallib"
    air_fat.write_bytes(universal_with_air(0xCBFEBABE))
    audit = format_audit.inspect_library(air_fat, load_status="load_refused")
    require(audit["container"] == "universal_metallib",
            "literal CB FE BA BE universal header was not recognized")
    require(audit["group"] == "unknown"
            and audit["evidence"]["grade"] == "structural",
            "structural universal evidence bypassed load acceptance")

    not_fat = root / "synthetic-cafe-negative.metallib"
    not_fat.write_bytes(universal_with_air(0xCAFEBABE))
    audit = format_audit.inspect_library(not_fat, load_status="load_refused")
    require(audit["container"] != "universal_metallib",
            "CA FE BA BE was incorrectly recognized as universal metallib")

    executable = root / "synthetic-gpu-executable.metallib"
    executable.write_bytes(macho_gpu_file(filetype=2))
    audit = format_audit.inspect_library(executable, load_status="load_refused")
    evidence = audit["evidence"]
    require(audit["group"] == "unknown"
            and audit["container"] == "gpu_executable_candidate"
            and evidence["file_type"] == 2
            and evidence["file_type_name"] == "MH_EXECUTE",
            "MH_EXECUTE GPU candidate was misnamed or overclassified")
    require(evidence["load_command_count"] == 2
            and len(evidence["load_commands"]) == 2
            and all(command["aligned"] for command in evidence["load_commands"])
            and [segment["name"] for segment in evidence["segments"]]
            == ["__AIR_DATA", "__TEXT"],
            "complete load-command and segment metadata was not retained")
    require(evidence["sections"][0]["segment"] == "__TEXT"
            and evidence["sections"][0]["name"] == "__metallib"
            and evidence["sections"][0]["parent_segment_found"]
            and evidence["sections"][0]["in_parent_segment"],
            "section was not checked against its parent segment")
    embedded = evidence["embedded_metallib"]
    require(embedded["mtlb_magic_verified"]
            and embedded["header_complete"]
            and embedded["declared_file_size_in_file"]
            and embedded["declared_file_size_in_section"]
            and embedded["bitcode_range_in_header"]
            and embedded["bitcode_range_in_file"]
            and embedded["bitcode_start_magic"] == "bitcode_wrapper"
            and embedded["wrapper"]["range_in_bitcode"]
            and embedded["wrapper"]["inner_bitcode_magic_verified"],
            "bounded embedded MTLB/wrapper header checks were incomplete")
    require("embedded source candidate requires parser/load qualification"
            in audit["reason"],
            "embedded source candidate did not retain the conservative reason")

    relocatable = root / "synthetic-gpu-object.metallib"
    relocatable.write_bytes(macho_gpu_file(filetype=1))
    audit = format_audit.inspect_library(relocatable, load_status="load_refused")
    require(audit["group"] == "unknown"
            and audit["container"] == "gpu_object_candidate"
            and audit["evidence"]["file_type_name"] == "MH_OBJECT",
            "MH_OBJECT=1 candidate was lost or overclassified")

    no_metallib = root / "synthetic-gpu-no-metallib.metallib"
    no_metallib.write_bytes(macho_gpu_file(filetype=2, no_metallib=True))
    audit = format_audit.inspect_library(no_metallib, load_status="load_refused")
    require(audit["group"] == "unknown"
            and "does not prove absence of an embedded source" in audit["reason"]
            and "embedded_metallib" not in audit["evidence"],
            "a GPU Mach-O without __TEXT,__metallib invented a non-source label")

    broken = root / "synthetic-broken-gpu-object.metallib"
    broken.write_bytes(macho_gpu_file(filetype=2, bad_command=True))
    audit = format_audit.inspect_library(broken, load_status="load_refused")
    require(audit["group"] == "unknown"
            and "invalid boundary" in audit["reason"],
            "an invalid load-command boundary was accepted")

    misaligned = root / "synthetic-misaligned-gpu-object.metallib"
    misaligned.write_bytes(macho_gpu_file(filetype=2, misaligned_command=True))
    audit = format_audit.inspect_library(misaligned, load_status="load_refused")
    require(audit["group"] == "unknown"
            and "not 8-byte aligned" in audit["reason"],
            "a misaligned load command was not reported")

    outside_parent = root / "synthetic-outside-parent.metallib"
    outside_parent.write_bytes(macho_gpu_file(filetype=2, bad_section_range=True))
    audit = format_audit.inspect_library(outside_parent, load_status="load_refused")
    require(audit["group"] == "unknown"
            and "outside its parent segment" in audit["reason"],
            "a section outside its parent segment was not reported")

    header_only = macho_gpu_file(filetype=2)[:64]
    audit = format_audit.inspect_library(
        root / "absent.metallib",
        load_status="load_refused",
        header64_hex=header_only.hex(),
        declared_size=len(macho_gpu_file(filetype=2)),
    )
    require(audit["group"] == "unknown"
            and audit["container"] == "gpu_executable_candidate"
            and "complete Mach-O load commands" in audit["evidence"]["missing"],
            "64-byte Mach-O evidence was overclassified")
    audit = format_audit.inspect_library(
        root / "absent-loaded.metallib",
        load_status="loaded",
        header64_hex=header_only.hex(),
    )
    require(audit["group"] == "valid_source"
            and audit["evidence"]["grade"] == "load_accepted",
            "load acceptance was lost when local bytes were unavailable")

    table = root / "synthetic-counted-bitcode-bundle.metallib"
    table.write_bytes(counted_bundle())
    audit = format_audit.inspect_library(table, load_status="load_refused")
    evidence = audit["evidence"]
    require(audit["group"] == "unknown"
            and audit["container"] == "counted_bitcode_bundle_candidate"
            and evidence["records_complete"]
            and evidence["first_offset_is_table_end"]
            and evidence["offsets_strictly_increasing"]
            and evidence["record_count"] == 2
            and evidence["length_exact_count"] == 2
            and evidence["wrapper_header_count"] == 2
            and evidence["wrapper_magic_count"] == 2
            and evidence["inner_bitcode_count"] == 2,
            "valid counted bundle metadata was not aggregated")
    require("linked-runtime semantics" in audit["reason"]
            and "secret.first" not in json.dumps(audit),
            "counted bundle was overclassified or leaked a symbol name")

    corrupt_table = root / "synthetic-corrupt-counted-bundle.metallib"
    corrupt_table.write_bytes(counted_bundle(corrupt_wrapper=True))
    audit = format_audit.inspect_library(corrupt_table, load_status="load_refused")
    evidence = audit["evidence"]
    require(audit["group"] == "unknown"
            and evidence["length_exact_count"] == 2
            and evidence["wrapper_magic_count"] == 1
            and not evidence["wrappers_complete"]
            and evidence["issues"],
            "a corrupt counted wrapper was not reported conservatively")

    excessive_count = root / "synthetic-excessive-count.metallib"
    excessive_count.write_bytes(
        struct.pack("<I", format_audit.MAX_SYMBOL_RECORDS + 1) + b"x\0"
    )
    audit = format_audit.inspect_library(
        excessive_count, load_status="load_refused"
    )
    require(audit["group"] == "unknown"
            and audit["container"] == "counted_bitcode_bundle_candidate"
            and not audit["evidence"]["records_complete"],
            "an excessive counted record table was not reported conservatively")

    records = [
        {
            "index": 0,
            "path": "/synthetic.metallib",
            "status": "loaded",
            "header64_hex": source.read_bytes()[:64].hex(),
        },
        {
            "index": 1,
            "path": "/synthetic-gpu-executable.metallib",
            "status": "load_refused",
            "header64_hex": executable.read_bytes()[:64].hex(),
        },
        {
            "index": 2,
            "path": "/synthetic-counted-bitcode-bundle.metallib",
            "status": "load_refused",
            "header64_hex": table.read_bytes()[:64].hex(),
        },
    ]
    coverage = format_audit.annotate_records(
        records, os_mount=root, cryptex_mount=root
    )
    require(coverage["manifest_total"] == 3
            and coverage["group_counts"]["valid_source"] == 1
            and coverage["group_counts"]["known_non_source"] == 0
            and coverage["group_counts"]["unknown"] == 2,
            "manifest grouping invented a non-source or lost an unknown entry")
    require(coverage["acceptance_effect"].startswith("report-only"),
            "format audit silently changed acceptance")
    require(records[2]["header64_hex"] is None
            and records[2].get("header64_redaction")
            and "secret." not in json.dumps(records),
            "manifest annotation leaked counted bundle symbol names")

print(
    "PASS: synthetic format audit keeps load-accepted sources distinct from "
    "bounded Mach-O and counted-bitcode candidates"
)
print(
    "NOT COVERED: no Apple library, Metal runtime, macOS load, linked bundle "
    "semantics or shader compilation was exercised; synthetic labels are not "
    "production results"
)
