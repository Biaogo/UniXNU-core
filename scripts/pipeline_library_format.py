#!/usr/bin/env python3
"""Audit pipeline library containers without invoking Metal or compiling shaders.

The audit reads a bounded prefix for structure, streams the whole file only
through SHA-256, and seeks small metadata headers as needed. It never prints
shader payload bytes. The load result remains the authority for
``valid_source``: a recognized header alone does not turn a refused or failed
library into a successful compile.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import struct


PREFIX_LIMIT = 1024 * 1024
HASH_CHUNK = 1024 * 1024
MAX_LOAD_COMMANDS = 4096
MAX_SEGMENTS = 512
MAX_SECTIONS = 16384
MAX_SYMBOL_RECORDS = 65536
MAX_SYMBOL_BYTES = 4096

MH_MAGIC_64 = 0xFEEDFACF
MH_OBJECT = 1
MH_EXECUTE = 2
LC_SEGMENT_64 = 0x19
CPU_TYPE_GPU = 0x01000013
UNIVERSAL_MAGIC = b"\xcb\xfe\xba\xbe"
AIR_SLICE_CPU_TYPE = 0x01000017
MTLB_MAGIC = b"MTLB"
BITCODE_MAGIC = b"BC\xc0\xde"
WRAPPER_MAGIC = b"\xde\xc0\x17\x0b"
MTLB_HEADER_BYTES = 0x58
WRAPPER_HEADER_BYTES = 20
MAX_COUNTED_TABLE_BYTES = PREFIX_LIMIT
ZERO_FILL_SECTION_TYPES = {0x01, 0x0C, 0x12}


def _fixed_name(value: bytes) -> str:
    return value.split(b"\0", 1)[0].decode("ascii", errors="replace")


def _snapshot(source) -> tuple[bytes, int, str]:
    digest = hashlib.sha256()
    prefix = bytearray()
    size = 0
    while block := source.read(HASH_CHUNK):
        size += len(block)
        digest.update(block)
        room = PREFIX_LIMIT - len(prefix)
        if room > 0:
            prefix.extend(block[:room])
    return bytes(prefix), size, digest.hexdigest()


def _read_at(source, offset: int, length: int, size: int) -> bytes | None:
    """Read one small metadata range without loading the containing payload."""
    if length < 0 or length > MTLB_HEADER_BYTES:
        return None
    if offset < 0 or offset > size or length > size - offset:
        return None
    source.seek(offset)
    value = source.read(length)
    return value if len(value) == length else None


def _range_fits(offset: int, length: int, limit: int) -> bool:
    return offset >= 0 and length >= 0 and offset <= limit and length <= limit - offset


def _prefix_observation(header64_hex: str | None, size: int | None) -> dict:
    value = {
        "size": size,
        "sha256": None,
        "header64_hex": header64_hex,
        "prefix_bytes_read": (
            min(len(header64_hex) // 2, 64) if header64_hex else 0
        ),
        "read_limit_bytes": PREFIX_LIMIT,
        "sha256_scope": "not_available",
        "group": "unknown",
        "container": "unknown",
        "reason": "library bytes were not available to the format audit",
        "evidence": {"grade": "none", "missing": ["local_file"]},
    }
    if not header64_hex:
        return value
    try:
        prefix = bytes.fromhex(header64_hex)
    except ValueError:
        value["reason"] = "header64_hex is not valid hexadecimal"
        value["evidence"]["missing"] = ["valid_header64_hex"]
        return value
    value["prefix_bytes_read"] = len(prefix)
    classified = _classify_bytes(prefix, size)
    value["group"] = classified.pop("group")
    value["container"] = classified.pop("container")
    value["reason"] = classified.pop("reason")
    value["evidence"] = classified.pop("evidence")
    return value


def inspect_library(
    path: Path | None,
    *,
    load_status: str,
    header64_hex: str | None = None,
    declared_size: int | None = None,
) -> dict:
    """Return bounded structural metadata and the coverage classification.

    ``valid_source`` requires an actually loaded library. Structural Mach-O,
    MTLB and counted-bundle evidence remains ``unknown`` until both a dedicated
    parser and the runtime load contract are qualified. Anything not loaded is
    therefore a failure; this round may legitimately report zero
    ``known_non_source`` entries.
    """
    if path is None or not path.is_file():
        observation = _prefix_observation(header64_hex, declared_size)
        if load_status == "loaded":
            observation.update(
                group="valid_source",
                reason="newLibraryWithURL accepted the library",
            )
            observation["evidence"]["grade"] = "load_accepted"
            observation["evidence"]["missing"] = []
        return observation
    with path.open("rb") as source:
        prefix, size, digest = _snapshot(source)
        observation = _classify_bytes(prefix, size, source)
    observation.update(
        size=size,
        sha256=digest,
        header64_hex=prefix[:64].hex(),
        prefix_bytes_read=min(len(prefix), PREFIX_LIMIT),
        read_limit_bytes=PREFIX_LIMIT,
        sha256_scope="whole_file",
    )
    if observation["container"] == "counted_bitcode_bundle_candidate":
        # The first record-table bytes contain symbol names. They are metadata,
        # but the AGX report deliberately keeps the evidence name-free.
        observation["header64_hex"] = None
    if load_status == "loaded":
        observation.update(
            group="valid_source",
            reason="newLibraryWithURL accepted the library",
        )
        observation["evidence"]["grade"] = "load_accepted"
        observation["evidence"]["missing"] = []
    return observation


def _classify_bytes(prefix: bytes, size: int | None, source=None) -> dict:
    if prefix.startswith(MTLB_MAGIC):
        return _result(
            "unknown", "mtlb", "MTLB header recognized; load acceptance is required",
            "header",
            ["newLibraryWithURL acceptance"],
        )
    if prefix.startswith(UNIVERSAL_MAGIC):
        return _parse_universal(prefix, size)
    if prefix.startswith((WRAPPER_MAGIC, BITCODE_MAGIC)):
        return _result(
            "unknown", "air_bitcode", "AIR bitcode recognized; load acceptance is required",
            "header",
            ["newLibraryWithURL acceptance"],
        )
    if len(prefix) >= 4 and struct.unpack_from("<I", prefix)[0] == MH_MAGIC_64:
        return _parse_macho(prefix, size, source)
    if len(prefix) >= 4:
        counted = _parse_counted_records(prefix, size, source)
        if counted is not None:
            return counted
    return _result(
        "unknown", "unrecognized", "container magic is not recognized",
        "header",
        ["recognized container structure"],
    )


def _result(
    group: str,
    container: str,
    reason: str,
    grade: str,
    missing: list[str],
    **evidence: object,
) -> dict:
    return {
        "group": group,
        "container": container,
        "reason": reason,
        "evidence": {"grade": grade, "missing": missing, **evidence},
    }


def _parse_universal(prefix: bytes, size: int | None) -> dict:
    if len(prefix) < 8:
        return _result(
            "unknown", "universal_metallib", "universal header is incomplete",
            "header", ["universal slice table"],
        )
    count = struct.unpack_from(">I", prefix, 4)[0]
    need = 8 + count * 20
    if count > MAX_SEGMENTS:
        return _result(
            "unknown", "universal_metallib", "universal slice count exceeds the audit bound",
            "header", ["bounded universal slice table"], slice_count=count,
        )
    if need > len(prefix):
        return _result(
            "unknown", "universal_metallib", "universal slice table was not read completely",
            "header", ["universal slice table"], slice_count=count,
        )
    slices = []
    air_slice_verified = False
    for index in range(count):
        at = 8 + index * 20
        cpu, subtype, offset, length, align = struct.unpack_from(">5I", prefix, at)
        in_range = offset + length <= len(prefix)
        if size is not None:
            in_range = in_range and offset + length <= size
        slices.append({
            "cpu_type": cpu,
            "cpu_subtype": subtype,
            "offset": offset,
            "size": length,
            "alignment": align,
            "in_file": in_range,
        })
        if cpu == AIR_SLICE_CPU_TYPE and in_range:
            air_slice_verified = prefix[offset:offset + 4] == MTLB_MAGIC
    if not air_slice_verified:
        return _result(
            "unknown", "universal_metallib",
            "universal container has no verified MTLB AIR slice",
            "structural", ["MTLB AIR slice"], slices=slices,
        )
    return _result(
        "unknown", "universal_metallib",
        "universal MTLB AIR slice verified; load acceptance is required",
        "structural",
        ["newLibraryWithURL acceptance"],
        slices=slices,
    )


def _parse_counted_records(prefix: bytes, size: int | None, source) -> dict | None:
    if len(prefix) < 5:
        return None
    count = struct.unpack_from("<I", prefix)[0]
    if count == 0 or count > MAX_SYMBOL_RECORDS:
        return _result(
            "unknown", "counted_bitcode_bundle_candidate",
            "counted record count is outside the audit bound",
            "header", ["bounded counted record count"],
            record_count=count, records_complete=False,
        )
    at = 4
    name_bytes = 0
    offsets = []
    table_limit = min(len(prefix), MAX_COUNTED_TABLE_BYTES)
    for _ in range(count):
        if at >= table_limit:
            return _result(
                "unknown", "counted_bitcode_bundle_candidate",
                "counted string/offset table is incomplete in the bounded read",
                "header", ["complete bounded counted record table"],
                record_count=count, records_complete=False,
            )
        end = prefix.find(b"\0", at, table_limit)
        if end < 0:
            return _result(
                "unknown", "counted_bitcode_bundle_candidate",
                "counted string/offset table is incomplete in the bounded prefix",
                "header", ["complete counted record table"],
                record_count=count, records_complete=False,
            )
        name_length = end - at
        if name_length > MAX_SYMBOL_BYTES or end + 5 > table_limit:
            return _result(
                "unknown", "counted_bitcode_bundle_candidate",
                "counted string/offset table violates the audit bounds",
                "header", ["bounded counted record table"],
                record_count=count, records_complete=False,
            )
        name_bytes += name_length
        offsets.append(struct.unpack_from("<I", prefix, end + 1)[0])
        at = end + 5
    first_offset_exact = bool(offsets) and offsets[0] == at
    strict_offsets = all(
        left < right for left, right in zip(offsets, offsets[1:])
    )
    base = {
        "record_count": count,
        "records_complete": True,
        "table_bytes": at,
        "name_bytes": name_bytes,
        "first_offset_is_table_end": first_offset_exact,
        "offsets_strictly_increasing": strict_offsets,
        "offset_min": min(offsets),
        "offset_max": max(offsets),
    }
    if size is None or source is None:
        return _result(
            "unknown", "counted_bitcode_bundle_candidate",
            "counted_bitcode_bundle_candidate requires local bounded payload metadata",
            "header", ["local file payload metadata"],
            **base,
            records_observed=0,
            length_out_of_bounds_count=0,
            length_mismatch_count=0,
            length_exact_count=0,
            wrapper_header_count=0,
            wrapper_magic_count=0,
            wrapper_bounds_count=0,
            inner_bitcode_count=0,
        )

    issues = []
    records_observed = 0
    length_out_of_bounds_count = 0
    length_mismatch_count = 0
    length_exact_count = 0
    wrapper_header_count = 0
    wrapper_magic_count = 0
    wrapper_bounds_count = 0
    inner_bitcode_count = 0
    for index, (record_offset, next_offset) in enumerate(
        zip(offsets, offsets[1:] + [size])
    ):
        if record_offset + 4 > size:
            issues.append("record %d offset is outside the file" % index)
            continue
        length_bytes = _read_at(source, record_offset, 4, size)
        if length_bytes is None:
            issues.append("record %d length is unreadable" % index)
            continue
        records_observed += 1
        payload_length = struct.unpack("<I", length_bytes)[0]
        payload_end = record_offset + 4 + payload_length
        length_in_bounds = _range_fits(record_offset + 4, payload_length, size)
        if not length_in_bounds:
            length_out_of_bounds_count += 1
        length_lands_exactly = payload_end == next_offset
        if not length_lands_exactly:
            length_mismatch_count += 1
        length_exact = length_in_bounds and length_lands_exactly
        if length_exact:
            length_exact_count += 1
        else:
            issues.append("record %d length does not land on its next offset" % index)
            continue

        if payload_length < WRAPPER_HEADER_BYTES:
            issues.append("record %d payload is shorter than its wrapper" % index)
            continue
        wrapper = _read_at(
            source, record_offset + 4, WRAPPER_HEADER_BYTES, size
        )
        if wrapper is None:
            issues.append("record %d wrapper is unreadable" % index)
            continue
        wrapper_header_count += 1
        wrapper_magic = wrapper[:4] == WRAPPER_MAGIC
        if wrapper_magic:
            wrapper_magic_count += 1
        version, wrapper_start, wrapper_size, wrapper_cpu = struct.unpack_from(
            "<4I", wrapper, 4
        )
        wrapper_bounds = (
            wrapper_start >= WRAPPER_HEADER_BYTES
            and _range_fits(wrapper_start, wrapper_size, payload_length)
        )
        if wrapper_bounds:
            wrapper_bounds_count += 1
        else:
            issues.append("record %d wrapper range exceeds its payload" % index)
        if version != 0 or wrapper_cpu != 0xFFFFFFFF:
            issues.append("record %d wrapper fields are not the observed form" % index)
        if not wrapper_magic:
            issues.append("record %d wrapper magic is unrecognized" % index)
        if not wrapper_bounds:
            continue
        inner_offset = record_offset + 4 + wrapper_start
        inner_magic = _read_at(source, inner_offset, 4, size)
        if inner_magic == BITCODE_MAGIC:
            inner_bitcode_count += 1
        else:
            issues.append("record %d inner bitcode magic is unrecognized" % index)

    complete = (
        first_offset_exact
        and strict_offsets
        and length_exact_count == count
        and wrapper_header_count == count
        and wrapper_magic_count == count
        and wrapper_bounds_count == count
        and inner_bitcode_count == count
    )
    return _result(
        "unknown", "counted_bitcode_bundle_candidate",
        "counted_bitcode_bundle_candidate requires linked-runtime semantics "
        "and API load qualification",
        "structural" if complete else "header",
        ["linked runtime semantics", "newLibraryWithURL acceptance"],
        **base,
        records_observed=records_observed,
        length_out_of_bounds_count=length_out_of_bounds_count,
        length_mismatch_count=length_mismatch_count,
        length_exact_count=length_exact_count,
        wrapper_header_count=wrapper_header_count,
        wrapper_magic_count=wrapper_magic_count,
        wrapper_bounds_count=wrapper_bounds_count,
        inner_bitcode_count=inner_bitcode_count,
        payloads_complete=length_exact_count == count,
        wrappers_complete=complete,
        issues=issues[:16],
    )


def _inspect_embedded_metallib(
    source,
    size: int,
    section: dict,
) -> dict:
    """Read only the embedded MTLB and bitcode headers needed for bounds checks."""
    section_offset = section["offset"]
    section_size = section["size"]
    requested = min(MTLB_HEADER_BYTES, section_size)
    header = _read_at(source, section_offset, requested, size)
    evidence = {
        "offset": section_offset,
        "section_offset": section_offset,
        "section_size": section_size,
        "header_bytes_requested": MTLB_HEADER_BYTES,
        "header_bytes_read": len(header) if header is not None else 0,
        "mtlb_magic_verified": False,
        "header_complete": False,
        "declared_file_size": None,
        "declared_file_size_in_file": False,
        "declared_file_size_in_section": False,
        "bitcode_offset": None,
        "bitcode_size": None,
        "bitcode_range_in_declared_file": False,
        "bitcode_range_in_section": False,
        "bitcode_range_in_file": False,
        "bitcode_range_in_header": False,
        "bitcode_start_hex": None,
        "bitcode_start_magic": "not_read",
        "wrapper": None,
        "issues": [],
    }
    if header is None:
        evidence["issues"].append("embedded MTLB header is unreadable")
        return evidence
    if len(header) < 4:
        evidence["issues"].append("embedded section is shorter than four bytes")
        return evidence
    evidence["header_magic_hex"] = header[:4].hex()
    if header[:4] != MTLB_MAGIC:
        evidence["issues"].append("embedded section does not start with MTLB")
        return evidence
    evidence["mtlb_magic_verified"] = True
    if len(header) < MTLB_HEADER_BYTES:
        evidence["issues"].append("embedded MTLB header is truncated")
        return evidence

    evidence["header_complete"] = True
    (
        platform,
        version_major,
        version_minor,
        file_type,
        os_value,
        os_major,
        os_minor,
    ) = struct.unpack_from("<HHHBBHH", header, 4)
    declared_file_size = struct.unpack_from("<Q", header, 16)[0]
    bitcode_offset, bitcode_size = struct.unpack_from("<QQ", header, 72)
    evidence.update(
        platform=platform,
        version=[version_major, version_minor],
        file_type=file_type,
        os=os_value,
        os_version=[os_major, os_minor],
        declared_file_size=declared_file_size,
        bitcode_offset=bitcode_offset,
        bitcode_size=bitcode_size,
    )
    declared_in_file = _range_fits(section_offset, declared_file_size, size)
    declared_in_section = (
        declared_file_size >= MTLB_HEADER_BYTES
        and declared_file_size <= section_size
    )
    evidence["declared_file_size_in_file"] = declared_in_file
    evidence["declared_file_size_in_section"] = declared_in_section
    if not declared_in_file or not declared_in_section:
        evidence["issues"].append(
            "declared MTLB file size is outside the containing section or file"
        )
    bitcode_in_declared = _range_fits(
        bitcode_offset, bitcode_size, declared_file_size
    )
    bitcode_in_section = _range_fits(
        bitcode_offset, bitcode_size, section_size
    )
    bitcode_in_file = _range_fits(
        section_offset + bitcode_offset, bitcode_size, size
    )
    bitcode_in_header = (
        bitcode_offset >= MTLB_HEADER_BYTES
        and bitcode_in_declared
        and bitcode_in_section
        and bitcode_in_file
    )
    evidence.update(
        bitcode_range_in_declared_file=bitcode_in_declared,
        bitcode_range_in_section=bitcode_in_section,
        bitcode_range_in_file=bitcode_in_file,
    )
    evidence["bitcode_range_in_header"] = bitcode_in_header
    if not bitcode_in_header:
        evidence["issues"].append("MTLB bitcode range is outside the embedded file")
        return evidence
    if bitcode_size < 4:
        evidence["issues"].append("MTLB bitcode range is shorter than four bytes")
        return evidence

    bitcode_absolute = section_offset + bitcode_offset
    start_magic = _read_at(source, bitcode_absolute, 4, size)
    if start_magic is None:
        evidence["issues"].append("MTLB bitcode start is unreadable")
        return evidence
    evidence["bitcode_start_hex"] = start_magic.hex()
    if start_magic == BITCODE_MAGIC:
        evidence["bitcode_start_magic"] = "raw_bitcode"
        return evidence
    if start_magic != WRAPPER_MAGIC:
        evidence["bitcode_start_magic"] = "unrecognized"
        evidence["issues"].append("MTLB bitcode start magic is unrecognized")
        return evidence

    evidence["bitcode_start_magic"] = "bitcode_wrapper"
    if bitcode_size < WRAPPER_HEADER_BYTES:
        evidence["issues"].append("MTLB bitcode wrapper is truncated")
        return evidence
    wrapper_bytes = _read_at(
        source, bitcode_absolute, WRAPPER_HEADER_BYTES, size
    )
    if wrapper_bytes is None:
        evidence["issues"].append("MTLB bitcode wrapper is unreadable")
        return evidence
    version, start, wrapper_size, cpu = struct.unpack_from("<4I", wrapper_bytes, 4)
    wrapper_bounds = (
        start >= WRAPPER_HEADER_BYTES
        and _range_fits(start, wrapper_size, bitcode_size)
    )
    wrapper = {
        "version": version,
        "start": start,
        "size": wrapper_size,
        "cpu_type": cpu,
        "range_in_bitcode": wrapper_bounds,
        "inner_bitcode_magic_verified": False,
    }
    evidence["wrapper"] = wrapper
    if not wrapper_bounds:
        evidence["issues"].append("bitcode wrapper range is outside MTLB bitcode")
        return evidence
    inner_magic = _read_at(source, bitcode_absolute + start, 4, size)
    if inner_magic == BITCODE_MAGIC:
        wrapper["inner_bitcode_magic_verified"] = True
    else:
        evidence["issues"].append("wrapper inner bitcode magic is unrecognized")
    return evidence


def _parse_macho(prefix: bytes, size: int | None, source) -> dict:
    if len(prefix) < 32:
        return _result(
            "unknown", "macho64", "Mach-O header is incomplete", "header",
            ["complete Mach-O header"],
        )
    _, cpu, subtype, filetype, ncmds, sizeofcmds, flags, _ = struct.unpack_from(
        "<8I", prefix
    )
    candidate = cpu == CPU_TYPE_GPU and filetype in (MH_OBJECT, MH_EXECUTE)
    container = {
        MH_OBJECT: "gpu_object_candidate",
        MH_EXECUTE: "gpu_executable_candidate",
    }.get(filetype, "macho64")
    file_type_name = {
        MH_OBJECT: "MH_OBJECT",
        MH_EXECUTE: "MH_EXECUTE",
    }.get(filetype, "unrecognized")
    base = {
        "cpu_type": cpu,
        "cpu_subtype": subtype,
        "file_type": filetype,
        "file_type_name": file_type_name,
        "load_command_count": ncmds,
        "load_command_bytes": sizeofcmds,
        "flags": flags,
    }
    if not candidate:
        return _result(
            "unknown", container,
            "Mach-O is not an Apple GPU object or executable candidate",
            "header", ["recognized GPU object/executable type"], **base,
        )
    if ncmds > MAX_LOAD_COMMANDS:
        return _result(
            "unknown", container, "Mach-O load-command count exceeds the audit bound",
            "header", ["bounded Mach-O load commands"], **base,
        )
    if sizeofcmds % 8:
        return _result(
            "unknown", container,
            "Mach-O load-command region size is not 8-byte aligned",
            "header", ["8-byte aligned Mach-O load commands"], **base,
        )
    command_end = 32 + sizeofcmds
    if size is None:
        return _result(
            "unknown", container, "Mach-O file size and complete load commands are unavailable",
            "header", ["file size", "complete Mach-O load commands"], **base,
        )
    if command_end > size:
        return _result(
            "unknown", container, "Mach-O load commands extend past the file",
            "header", ["exact load-command bounds"], **base,
        )
    if command_end > PREFIX_LIMIT:
        return _result(
            "unknown", container, "Mach-O load commands exceed the bounded read",
            "header", ["Mach-O load commands inside the bounded read"], **base,
        )
    if command_end > len(prefix):
        return _result(
            "unknown", container, "Mach-O load-command region was not read completely",
            "header", ["complete Mach-O load commands"], **base,
        )

    at = 32
    load_commands = []
    segments = []
    sections = []
    problems = []
    for index in range(ncmds):
        if at % 8:
            problems.append("load command %d is not 8-byte aligned" % index)
            break
        if not _range_fits(at, 8, command_end):
            problems.append("load command %d header crosses sizeofcmds" % index)
            break
        command, command_size = struct.unpack_from("<II", prefix, at)
        if command_size < 8:
            problems.append("load command %d is shorter than eight bytes" % index)
            break
        if command_size % 8:
            problems.append("load command %d size is not 8-byte aligned" % index)
            break
        if not _range_fits(at, command_size, command_end):
            problems.append("load command %d has an invalid boundary" % index)
            break
        command_evidence = {
            "index": index,
            "offset": at,
            "command": command,
            "size": command_size,
            "aligned": True,
        }
        load_commands.append(command_evidence)
        if command == LC_SEGMENT_64:
            if command_size < 72:
                problems.append("segment command %d is shorter than 72 bytes" % index)
                break
            segment_name = _fixed_name(prefix[at + 8:at + 24])
            (
                vmaddr, vmsize, fileoff, filesize, maxprot, initprot,
                nsects, segment_flags,
            ) = struct.unpack_from("<QQQQIIII", prefix, at + 24)
            expected_size = 72 + nsects * 80
            if nsects > MAX_SECTIONS or command_size != expected_size:
                problems.append("segment %s has an invalid section count/size" % segment_name)
                break
            if not _range_fits(fileoff, filesize, size):
                problems.append("segment %s has a file range outside the file" % segment_name)
                break
            segment = {
                "name": segment_name,
                "vmaddr": vmaddr,
                "vmsize": vmsize,
                "fileoff": fileoff,
                "filesize": filesize,
                "maxprot": maxprot,
                "initprot": initprot,
                "section_count": nsects,
                "flags": segment_flags,
            }
            segments.append(segment)
            command_evidence["segment"] = segment_name
            section_at = at + 72
            for section_index in range(nsects):
                if not _range_fits(section_at, 80, command_end):
                    problems.append(
                        "section %d in segment %s crosses command bounds"
                        % (section_index, segment_name)
                    )
                    break
                (
                    section_name, section_segment, address, section_size, offset,
                    align, reloff, nreloc, section_flags, reserved1, reserved2,
                    reserved3,
                ) = struct.unpack_from("<16s16sQQIIIIIIII", prefix, section_at)
                # The 16s fields are 16 bytes each, so the section is 80 bytes.
                section_type = section_flags & 0xFF
                file_backed = section_type not in ZERO_FILL_SECTION_TYPES
                section_segment_name = _fixed_name(section_segment)
                section_name_text = _fixed_name(section_name)
                parent_matches = section_segment_name == segment_name
                if not parent_matches:
                    problems.append(
                        "section %s,%s does not belong to its enclosing segment %s"
                        % (
                            section_segment_name,
                            section_name_text,
                            segment_name,
                        )
                    )
                    break
                in_parent_segment = True
                in_file = not file_backed or _range_fits(offset, section_size, size)
                if file_backed:
                    in_parent_segment = (
                        offset >= segment["fileoff"]
                        and _range_fits(
                            offset - segment["fileoff"],
                            section_size,
                            segment["filesize"],
                        )
                    )
                if file_backed and not in_file:
                    problems.append(
                        "section %s,%s has a file range outside the file"
                        % (section_segment_name, section_name_text)
                    )
                    break
                if file_backed and not in_parent_segment:
                    problems.append(
                        "section %s,%s has a file range outside its parent segment"
                        % (section_segment_name, section_name_text)
                    )
                    break
                sections.append({
                    "segment": section_segment_name,
                    "name": section_name_text,
                    "address": address,
                    "size": section_size,
                    "offset": offset,
                    "align": align,
                    "reloff": reloff,
                    "nreloc": nreloc,
                    "flags": section_flags,
                    "reserved": [reserved1, reserved2, reserved3],
                    "file_backed": file_backed,
                    "in_file": in_file,
                    "parent_segment_found": True,
                    "in_parent_segment": in_parent_segment,
                })
                section_at += 80
            if problems:
                break
        at += command_size
    if problems:
        return _result(
            "unknown", container, problems[0], "header",
            ["valid load-command, segment and section boundaries"],
            **base, load_commands=load_commands, segments=segments,
            sections=sections,
        )
    if at != command_end:
        return _result(
            "unknown", container, "load commands do not end exactly at sizeofcmds",
            "header", ["exact load-command bounds"], **base,
            load_commands=load_commands, segments=segments, sections=sections,
        )
    embedded_section = next(
        (
            section for section in sections
            if section["segment"] == "__TEXT"
            and section["name"] == "__metallib"
            and section["file_backed"]
            and section["in_parent_segment"]
        ),
        None,
    )
    embedded = None
    if embedded_section is not None:
        embedded = _inspect_embedded_metallib(source, size, embedded_section)
    else:
        return _result(
            "unknown", container,
            "Mach-O structure does not prove absence of an embedded source; "
            "parser/load qualification is required",
            "structural", ["parser/load qualification"],
            **base, load_commands=load_commands, segments=segments,
            sections=sections,
        )
    return _result(
        "unknown", container,
        "embedded source candidate requires parser/load qualification",
        "structural",
        ["dedicated embedded source parser", "newLibraryWithURL acceptance"],
        cpu_type_meaning=(
            "Apple GPU CPU type 0x01000013; embedded MTLB/header evidence "
            "does not establish parser or runtime load acceptance"
        ),
        **base, load_commands=load_commands, segments=segments,
        sections=sections, embedded_metallib=embedded,
    )


def _local_path(logical: str, os_mount: Path | None, cryptex_mount: Path | None) -> Path | None:
    if os_mount is None:
        return None
    cryptex = "/System/Cryptexes/OS"
    if logical.startswith(cryptex):
        if cryptex_mount is None:
            return None
        return cryptex_mount / logical[len(cryptex):].lstrip("/")
    return os_mount / logical.lstrip("/")


def annotate_records(
    records: list[dict],
    *,
    os_mount: Path | None = None,
    cryptex_mount: Path | None = None,
) -> dict:
    """Add a report-only format classification and return its full manifest."""
    for record in records:
        logical = str(record.get("path", ""))
        local = _local_path(logical, os_mount, cryptex_mount)
        record["format_audit"] = inspect_library(
            local,
            load_status=str(record.get("status", "unknown")),
            header64_hex=record.get("header64_hex"),
        )
        if record["format_audit"]["container"] == "counted_bitcode_bundle_candidate":
            record["header64_hex"] = None
            record["header64_redaction"] = (
                "counted record-table symbol names are not emitted"
            )
    groups = ("valid_source", "known_non_source", "unknown")
    grouped = {group: [] for group in groups}
    for record in records:
        audit = record.get("format_audit") or {}
        group = str(audit.get("group", "unknown"))
        if group not in grouped:
            group = "unknown"
        grouped[group].append({
            "index": record.get("index"),
            "path": record.get("path"),
            "load_status": record.get("status"),
            "size": audit.get("size"),
            "sha256": audit.get("sha256"),
            "container": audit.get("container"),
            "evidence_grade": (audit.get("evidence") or {}).get("grade"),
            "reason": audit.get("reason"),
        })
    counts = Counter({
        group: len(entries) for group, entries in grouped.items()
    })
    return {
        "manifest_total": len(records),
        "group_counts": {group: counts[group] for group in groups},
        "groups": grouped,
        "acceptance_effect": "report-only; load preflight and priming gates are unchanged",
        "pipelines_compiled": False,
        "shader_pack_produced": False,
        "unknown_is_failure": True,
        "read_limit_bytes": PREFIX_LIMIT,
        "output_boundary": "metadata only; no shader payload bytes are emitted",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--os-mount", type=Path, default=None)
    parser.add_argument("--cryptex-mount", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = json.loads(args.preflight.read_text())
    records = value.get("libraries")
    if not isinstance(records, list):
        raise SystemExit("pipeline library format: preflight has no libraries array")
    coverage = annotate_records(
        records,
        os_mount=args.os_mount.resolve() if args.os_mount else None,
        cryptex_mount=args.cryptex_mount.resolve() if args.cryptex_mount else None,
    )
    result = {
        "source_preflight": str(args.preflight.resolve()),
        "source_preflight_status": value.get("status"),
        "coverage": coverage,
        "libraries": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(
        "FORMAT: manifest=%d valid_source=%d known_non_source=%d unknown=%d output=%s"
        % (
            coverage["manifest_total"],
            coverage["group_counts"]["valid_source"],
            coverage["group_counts"]["known_non_source"],
            coverage["group_counts"]["unknown"],
            args.output,
        )
    )
    return 0 if coverage["group_counts"]["unknown"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
