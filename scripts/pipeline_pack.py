#!/usr/bin/env python3
"""Build and validate the pipeline-cache pack shared by the cache generator and GPU addon packer.

The outer format is ``SHEXP001`` and the zlib payload is ``M2VCACH1`` as
defined by drivers/AdrenoMetal/CacheExport.h.  Validation checks the complete
container and entry bounds; it does not establish that entries were compiled
by the target driver.
"""
from __future__ import annotations

import shutil
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

PACK_MAGIC = b"SHEXP001"
STREAM_MAGIC = b"M2VCACH1"
MAX_PACK_BYTES = 1 << 30
MAX_STREAM_BYTES = 1 << 30
MAX_ROOT_BYTES = 4096
MAX_PATH_BYTES = 1024
MAX_FILES = 1 << 20


@dataclass(frozen=True)
class PackSummary:
    stream_bytes: int
    packed_bytes: int
    root: str
    files: tuple[str, ...]


def _archive_files(
    root: str, records: Iterable[tuple[str, bytes]]
) -> Iterator[tuple[str, bytes, int, int]]:
    root_bytes = root.encode("utf-8")
    if not root.startswith("/") or len(root_bytes) > MAX_ROOT_BYTES:
        raise ValueError("pipeline pack root must be an absolute UTF-8 path within 4096 bytes")
    seen: set[str] = set()
    count = 0
    for path, data in records:
        count += 1
        if count > MAX_FILES:
            raise ValueError("pipeline pack exceeds its file-count bound")
        encoded = path.encode("utf-8")
        parts = path.split("/")
        if (
            not path
            or len(encoded) > MAX_PATH_BYTES
            or path.startswith("/")
            or "\0" in path
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise ValueError("unsafe pipeline pack path: %r" % path)
        if path in seen:
            raise ValueError("duplicate pipeline pack path: " + path)
        seen.add(path)
        yield path, data, len(encoded), len(data)


def encode_pipeline_pack(root: str, records: Iterable[tuple[str, bytes]]) -> bytes:
    """Return a complete pack from in-memory entries; intended for labelled format fixtures."""
    ordered = sorted(records, key=lambda item: (item[0] == "prime-done", item[0]))
    entries = list(_archive_files(root, ordered))
    if not entries:
        raise ValueError("refuse an empty pipeline pack")
    raw = bytearray(STREAM_MAGIC)
    raw += struct.pack("<I", len(root.encode("utf-8"))) + root.encode("utf-8")
    raw += struct.pack("<I", len(entries))
    for path, data, path_bytes, size in entries:
        raw += struct.pack("<IIIIQ", path_bytes, 0o600, 501, 501, size)
        raw += path.encode("utf-8") + data
    if len(raw) > MAX_STREAM_BYTES:
        raise ValueError("pipeline stream exceeds its 1 GiB bound")
    packed = zlib.compress(bytes(raw), 9)
    if 16 + len(packed) > MAX_PACK_BYTES:
        raise ValueError("pipeline pack exceeds its 1 GiB bound")
    return PACK_MAGIC + struct.pack("<II", len(raw), len(packed)) + packed


def write_pipeline_pack(
    output: Path, root: str, files: Iterable[tuple[Path, str]]
) -> PackSummary:
    """Stream cache files into ``output`` without holding the full pack in memory."""
    root_bytes = root.encode("utf-8")
    if not root.startswith("/") or len(root_bytes) > MAX_ROOT_BYTES:
        raise ValueError("pipeline pack root must be an absolute UTF-8 path within 4096 bytes")
    ordered = sorted(files, key=lambda item: (item[1] == "prime-done", item[1]))
    if not ordered:
        raise ValueError("refuse an empty pipeline pack")
    seen: set[str] = set()
    normalized: list[tuple[Path, str, int]] = []
    for source, path in ordered:
        encoded = path.encode("utf-8")
        parts = path.split("/")
        if (
            not path
            or len(encoded) > MAX_PATH_BYTES
            or path.startswith("/")
            or "\0" in path
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise ValueError("unsafe pipeline pack path: %r" % path)
        if path in seen:
            raise ValueError("duplicate pipeline pack path: " + path)
        seen.add(path)
        if len(normalized) >= MAX_FILES:
            raise ValueError("pipeline pack exceeds its file-count bound")
        normalized.append((source, path, source.stat().st_size))

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    compressed_path = output.with_name(output.name + ".stream")
    packed_path = output.with_name(output.name + ".tmp")
    raw_bytes = 0
    packed_bytes = 0
    compressor = zlib.compressobj(9)
    try:
        with compressed_path.open("wb") as packed:
            def put(data: bytes) -> None:
                nonlocal raw_bytes, packed_bytes
                raw_bytes += len(data)
                chunk = compressor.compress(data)
                packed.write(chunk)
                packed_bytes += len(chunk)

            put(STREAM_MAGIC)
            put(struct.pack("<I", len(root_bytes)) + root_bytes)
            put(struct.pack("<I", len(normalized)))
            chunk = bytearray(1 << 20)
            for source, path, size in normalized:
                encoded = path.encode("utf-8")
                put(
                    struct.pack("<IIIIQ", len(encoded), 0o600, 501, 501, size)
                    + encoded
                )
                copied = 0
                with source.open("rb") as stream:
                    while data := stream.read(len(chunk)):
                        put(data)
                        copied += len(data)
                if copied != size:
                    raise ValueError("pipeline cache file changed while packing: " + str(source))
            tail = compressor.flush()
            packed.write(tail)
            packed_bytes += len(tail)
        if raw_bytes > MAX_STREAM_BYTES or 16 + packed_bytes > MAX_PACK_BYTES:
            raise ValueError("pipeline pack exceeds its 1 GiB format bound")
        with packed_path.open("wb") as output_stream:
            output_stream.write(PACK_MAGIC)
            output_stream.write(struct.pack("<II", raw_bytes, packed_bytes))
            with compressed_path.open("rb") as packed:
                shutil.copyfileobj(packed, output_stream, length=1 << 20)
        summary = PackSummary(
            raw_bytes, packed_bytes, root, tuple(path for _source, path, _size in normalized)
        )
        packed_path.replace(output)
        return summary
    finally:
        for path in (compressed_path, packed_path):
            path.unlink(missing_ok=True)


def validate_pipeline_pack(data: bytes, source: str = "pipeline pack") -> PackSummary:
    """Validate an entire SHEXP001/M2VCACH1 payload and return its structural metadata."""
    if len(data) < 16 or len(data) > MAX_PACK_BYTES or data[:8] != PACK_MAGIC:
        raise ValueError(source + ": expected a SHEXP001 pack")
    stream_bytes, packed_bytes = struct.unpack_from("<II", data, 8)
    if stream_bytes == 0 or stream_bytes > MAX_STREAM_BYTES:
        raise ValueError(source + ": invalid stream length")
    if packed_bytes == 0 or packed_bytes != len(data) - 16:
        raise ValueError(source + ": invalid compressed length")
    inflater = zlib.decompressobj()
    try:
        stream = inflater.decompress(data[16:], stream_bytes + 1)
        if len(stream) > stream_bytes or inflater.unconsumed_tail:
            raise ValueError(source + ": stream exceeds its declared length")
        stream += inflater.flush(stream_bytes + 1 - len(stream))
    except (ValueError, zlib.error) as error:
        raise ValueError(source + ": invalid zlib stream: " + str(error)) from error
    if not inflater.eof or inflater.unused_data or len(stream) != stream_bytes:
        raise ValueError(source + ": compressed stream does not match its header")
    if len(stream) < 16 or stream[:8] != STREAM_MAGIC:
        raise ValueError(source + ": expected an M2VCACH1 stream")
    root_bytes = struct.unpack_from("<I", stream, 8)[0]
    if not 0 < root_bytes <= MAX_ROOT_BYTES or 12 + root_bytes + 4 > len(stream):
        raise ValueError(source + ": invalid cache root length")
    try:
        root = stream[12:12 + root_bytes].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(source + ": cache root is not UTF-8") from error
    if not root.startswith("/"):
        raise ValueError(source + ": cache root is not absolute")
    at = 12 + root_bytes
    count = struct.unpack_from("<I", stream, at)[0]
    at += 4
    if not 0 < count <= MAX_FILES:
        raise ValueError(source + ": pack has no files or exceeds its file-count bound")
    paths: list[str] = []
    seen: set[str] = set()
    for _ in range(count):
        if at + 24 > len(stream):
            raise ValueError(source + ": truncated entry header")
        path_bytes, _mode, _uid, _gid, size = struct.unpack_from("<IIIIQ", stream, at)
        at += 24
        if not 0 < path_bytes <= MAX_PATH_BYTES or at + path_bytes + size > len(stream):
            raise ValueError(source + ": invalid entry bounds")
        try:
            path = stream[at:at + path_bytes].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(source + ": entry path is not UTF-8") from error
        at += path_bytes
        parts = path.split("/")
        if path.startswith("/") or "\0" in path or any(part in ("", ".", "..") for part in parts):
            raise ValueError(source + ": unsafe entry path " + repr(path))
        if path in seen:
            raise ValueError(source + ": duplicate entry path " + path)
        seen.add(path)
        paths.append(path)
        at += size
    if at != len(stream):
        raise ValueError(source + ": trailing bytes after the last entry")
    return PackSummary(stream_bytes, packed_bytes, root, tuple(paths))
