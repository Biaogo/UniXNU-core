#!/usr/bin/env python3
"""Prepare the pinned 21H461 OS inputs and generate the dodge pipeline pack in CI.

The download path is an exact-size, exact-SHA256 stream.  The IPSW is reduced on
disk to BuildManifest.plist plus the two image entries named by that manifest.
While those images are mounted read-only, the complete host smoke runs against
the mounted 21H461 QuartzCore metallib and then ``make-pipeline-cache.py`` uses
the same mounts.  Both are detached in a ``finally`` cleanup path.

Use ``--mode download`` before saving the IPSW to an Actions cache, then
``--mode extract --drop-archive``, ``--mode generate``, and ``--mode cleanup``.
``--mode verify-members`` reuses the mounted OS/cryptex contract to compile the
producer and run eight explicit members. It does not generate or pack.
The default ``all`` mode performs the same sequence without an Actions cache.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import time
from typing import BinaryIO, Callable, Mapping, Sequence
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "scripts/data/dodge-pipeline-cache.json"
STAGING = ROOT / ".cache/dodge-ipsw"
REPORT = ROOT / "build/dodge-os-input.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
READ_BLOCK = 8 << 20
MAX_MANIFEST_BYTES = 64 << 20
TRUSTED_APPLE_REDIRECT_DOMAINS = ("apple.com",)


class CiError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourcePin:
    url: str
    bytes: int
    sha256: str
    archive_name: str
    public: bool


@dataclass(frozen=True)
class ImagePin:
    role: str
    manifest_key: str
    filename: str
    bytes: int
    kind: str


@dataclass(frozen=True)
class Config:
    source: SourcePin
    manifest_entry: str
    product_version: str
    product_build: str
    supported_products: tuple[str, ...]
    device: str
    device_class: str
    restore_behavior: str
    images: Mapping[str, ImagePin]
    generator: Mapping[str, object]
    download_timeout: int
    extract_timeout: int
    mount_timeout: int
    detach_timeout: int
    raw_config: Mapping[str, object]


@dataclass(frozen=True)
class SelectedImage:
    path: str
    bytes: int
    sha1: str | None


@dataclass(frozen=True)
class BuildSelection:
    product_build: str
    device: str
    supported_products: tuple[str, ...]
    matching_identities: int
    images: Mapping[str, SelectedImage]


@dataclass(frozen=True)
class AttachedImage:
    role: str
    image: Path
    mountpoint: Path
    owned_device: str


def fail(message: str) -> None:
    raise CiError(message)


_RUN = subprocess.run


def establish_host_peer(os_mount: Path, peer: Path, log_path: Path) -> None:
    """Build the host peer with the real smoke entry before any peer verify.

    The QuartzCore input is the metallib inside this OS mount. A parent
    directory is not a built peer. stdout/stderr/exit are recorded on the
    runner and are not an upload path.
    """
    quartzcore = os_mount / "System/Library/Frameworks/QuartzCore.framework/default.metallib"
    if not quartzcore.is_file():
        fail("mounted OS lacks the target QuartzCore metallib")
    command = [sys.executable, str(ROOT / "scripts/check-turnip-darwin-host.py")]
    env = dict(os.environ)
    env["NMB_TEST_QUARTZCORE_LIBRARY"] = str(quartzcore.resolve())
    result = _RUN(command, capture_output=True, text=True, env=env)
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(stdout + stderr + "\nexit=%d\n" % result.returncode)
    if result.returncode:
        fail("host peer build/smoke failed with exit %d; see %s" % (result.returncode, log_path))
    if not peer.is_file():
        fail("host peer build did not produce the peer library")


def _mapping(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        fail(what + " must be an object")
    return value


def _string(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        fail(what + " must be a non-empty string")
    return value


def _positive_int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        fail(what + " must be a positive integer")
    return value


def _relative_path(value: object, what: str) -> str:
    text = _string(value, what)
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in ("", ".", "..") for part in text.split("/")):
        fail(what + " must be a safe relative path")
    return path.as_posix()


def _safe_archive_name(value: object, what: str) -> str:
    name = _string(value, what)
    if "\0" in name or "\\" in name or name.startswith("/"):
        fail(what + " is not a safe ZIP entry")
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    if not trimmed or any(part in ("", ".", "..") for part in parts):
        fail(what + " is not a safe ZIP entry")
    return PurePosixPath(*parts).as_posix()


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        fail("cannot read CI input pin %s: %s" % (path, error))
    root = _mapping(raw, "CI input pin")
    if root.get("schema") != 1:
        fail("CI input pin schema must be 1")

    source_raw = _mapping(root.get("source"), "source")
    url = _string(source_raw.get("url"), "source.url")
    parsed = urllib.parse.urlsplit(url)
    try:
        source_port = parsed.port
    except ValueError:
        fail("source.url has an invalid port")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "updates.cdn-apple.com"
        or parsed.username is not None
        or parsed.password is not None
        or source_port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        fail("source.url must be the credential-free official Apple HTTPS URL")
    source = SourcePin(
        url=url,
        bytes=_positive_int(source_raw.get("bytes"), "source.bytes"),
        sha256=_string(source_raw.get("sha256"), "source.sha256").lower(),
        archive_name=_safe_archive_name(source_raw.get("archive_name"), "source.archive_name"),
        public=source_raw.get("public", False) is True,
    )
    if not SHA256_RE.fullmatch(source.sha256):
        fail("source.sha256 must be 64 lowercase hexadecimal characters")

    build_raw = _mapping(root.get("build"), "build")
    products = build_raw.get("supported_product_types")
    if not isinstance(products, list) or not products or not all(isinstance(item, str) and item for item in products):
        fail("build.supported_product_types must be a non-empty string list")
    if len(products) != len(set(products)):
        fail("build.supported_product_types contains duplicates")
    device = _string(build_raw.get("device"), "build.device")
    if device not in products:
        fail("build.device must be one of build.supported_product_types")
    device_class = _string(build_raw.get("device_class"), "build.device_class")
    restore_behavior = _string(build_raw.get("restore_behavior"), "build.restore_behavior")

    images_raw = _mapping(root.get("images"), "images")
    images: dict[str, ImagePin] = {}
    for role in ("os", "cryptex"):
        item = _mapping(images_raw.get(role), "images." + role)
        kind = _string(item.get("kind"), "images.%s.kind" % role)
        if kind not in ("udif", "raw-apfs"):
            fail("images.%s.kind must be udif or raw-apfs" % role)
        pin = ImagePin(
            role=role,
            manifest_key=_string(item.get("manifest_key"), "images.%s.manifest_key" % role),
            filename=_safe_archive_name(item.get("filename"), "images.%s.filename" % role),
            bytes=_positive_int(item.get("bytes"), "images.%s.bytes" % role),
            kind=kind,
        )
        if "/" in pin.filename:
            fail("images.%s.filename must be a top-level archive name" % role)
        images[role] = pin
    if images["os"].kind != "udif":
        fail("images.os must use the UDIF attach path")
    if images["cryptex"].kind != "raw-apfs":
        fail("images.cryptex must use the raw APFS attach path")

    generator_raw = _mapping(root.get("generator"), "generator")
    target = _string(generator_raw.get("target"), "generator.target")
    gpu = _string(generator_raw.get("gpu"), "generator.gpu")
    jobs = _positive_int(generator_raw.get("jobs"), "generator.jobs")
    if target != "ios" or gpu != "a830" or jobs != 2:
        fail("generator must use target ios, gpu a830, and exactly 2 workers")
    product_version = _string(build_raw.get("product_version"), "build.product_version")
    product_build = _string(build_raw.get("product_build_version"), "build.product_build_version")
    if _string(generator_raw.get("os_build"), "generator.os_build") != product_build:
        fail("generator.os_build must match the BuildManifest product build")

    timeouts_raw = _mapping(root.get("timeouts"), "timeouts")
    timeouts = {
        name: _positive_int(timeouts_raw.get(name), "timeouts." + name)
        for name in ("download_seconds", "extract_seconds", "mount_seconds", "detach_seconds")
    }
    if timeouts["download_seconds"] > 4 * 3600 or timeouts["extract_seconds"] > 4 * 3600:
        fail("transfer/extract timeouts must stay within four hours")
    for name in ("prime_list", "output", "work"):
        _relative_path(generator_raw.get(name), "generator." + name)
    _positive_int(generator_raw.get("timeout_seconds"), "generator.timeout_seconds")

    return Config(
        source=source,
        manifest_entry=_safe_archive_name(root.get("build_manifest_entry"), "build_manifest_entry"),
        product_version=product_version,
        product_build=product_build,
        supported_products=tuple(products),
        device=device,
        device_class=device_class,
        restore_behavior=restore_behavior,
        images=images,
        generator=generator_raw,
        download_timeout=timeouts["download_seconds"],
        extract_timeout=timeouts["extract_seconds"],
        mount_timeout=timeouts["mount_seconds"],
        detach_timeout=timeouts["detach_seconds"],
        raw_config=root,
    )


def _nested(value: object, *keys: str) -> object | None:
    current = value
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _identity_product_type(identity: Mapping[str, object]) -> str | None:
    for value in (
        identity.get("ProductType"),
        _nested(identity, "Info", "ProductType"),
        _nested(identity, "Manifest", "OS", "Info", "ProductType"),
    ):
        if isinstance(value, str) and value:
            return value
    return None


def _identity_info_string(identity: Mapping[str, object], key: str) -> str | None:
    value = _nested(identity, "Info", key)
    return value if isinstance(value, str) and value else None


def _identity_image(identity: Mapping[str, object], key: str) -> tuple[object | None, object | None]:
    info = _nested(identity, "Manifest", key, "Info")
    if not isinstance(info, dict):
        return None, None
    return info.get("Path"), _nested(identity, "Manifest", key, "Digest", "SHA1")


def select_build_images(manifest: Mapping[str, object], config: Config) -> BuildSelection:
    product_version = manifest.get("ProductVersion")
    if product_version != config.product_version:
        fail("BuildManifest ProductVersion is %r, expected %r" % (product_version, config.product_version))
    product_build = manifest.get("ProductBuildVersion")
    if product_build != config.product_build:
        fail("BuildManifest ProductBuildVersion is %r, expected %r" % (product_build, config.product_build))
    supported = manifest.get("SupportedProductTypes")
    if not isinstance(supported, list) or not all(isinstance(item, str) for item in supported):
        fail("BuildManifest SupportedProductTypes is missing or malformed")
    if set(supported) != set(config.supported_products):
        fail("BuildManifest SupportedProductTypes differs from the pinned iPad7,1-iPad7,4 set")
    if config.device not in supported:
        fail("BuildManifest does not support selected device " + config.device)
    identities = manifest.get("BuildIdentities")
    if not isinstance(identities, list) or not identities:
        fail("BuildManifest has no BuildIdentities")

    identities = [value for value in identities if isinstance(value, dict)]
    selected_identities = [
        value for value in identities
        if _identity_info_string(value, "DeviceClass") == config.device_class
        and _identity_info_string(value, "RestoreBehavior") == config.restore_behavior
    ]
    if len(selected_identities) != 1:
        fail(
            "expected one %s/%s BuildIdentity for %s, found %d"
            % (config.device_class, config.restore_behavior, config.device, len(selected_identities))
        )
    selected_identity = selected_identities[0]
    identity_product = _identity_product_type(selected_identity)
    if identity_product is not None and identity_product != config.device:
        fail("selected BuildIdentity ProductType is %r, expected %r" % (identity_product, config.device))
    for pin in config.images.values():
        path, _ = _identity_image(selected_identity, pin.manifest_key)
        if path is None:
            fail("selected BuildIdentity has no %s image" % pin.manifest_key)

    selected: dict[str, SelectedImage] = {}
    for role, pin in config.images.items():
        paths: set[str] = set()
        digests: set[str] = set()
        for identity in identities:
            path, digest = _identity_image(identity, pin.manifest_key)
            if path is not None:
                paths.add(_string(path, "BuildIdentity %s Path" % pin.manifest_key))
            if isinstance(digest, bytes):
                digests.add(digest.hex())
            elif isinstance(digest, str) and digest:
                digests.add(digest.lower())
        if paths != {pin.filename}:
            fail(
                "BuildManifest %s paths are %r, expected exactly %r"
                % (pin.manifest_key, sorted(paths), pin.filename)
            )
        if len(digests) > 1:
            fail("BuildManifest %s identities disagree on the image digest" % pin.manifest_key)
        selected[role] = SelectedImage(
            path=pin.filename,
            bytes=pin.bytes,
            sha1=next(iter(digests)) if digests else None,
        )
    return BuildSelection(
        product_build=config.product_build,
        device=config.device,
        supported_products=tuple(supported),
        matching_identities=len(selected_identities),
        images=selected,
    )


def require_ci_host() -> None:
    if sys.platform != "darwin":
        fail("the real pipeline-cache chain requires macOS, not " + sys.platform)
    machine = platform.machine().lower()
    if machine not in ("arm64", "aarch64"):
        fail("the macOS runner must be arm64/aarch64, not " + machine)
    for tool in ("xcrun", "hdiutil"):
        if shutil.which(tool) is None:
            fail(tool + " is required on the macOS runner")


def archive_path(config: Config) -> Path:
    return STAGING / "source" / (config.source.sha256 + ".ipsw")


def extracted_path(config: Config, role: str) -> Path:
    return STAGING / "images" / config.source.sha256[:16] / config.images[role].filename


def _check_deadline(deadline: float, what: str) -> None:
    if time.monotonic() > deadline:
        fail(what + " timed out")


def copy_verified_stream(
    source: BinaryIO,
    sink: BinaryIO,
    expected_bytes: int,
    expected_sha256: str,
    timeout_seconds: int,
) -> tuple[int, str]:
    """Stream bytes while enforcing exact length and SHA256 before success."""
    deadline = time.monotonic() + timeout_seconds
    digest = hashlib.sha256()
    total = 0
    while True:
        _check_deadline(deadline, "download")
        chunk = source.read(READ_BLOCK)
        if not chunk:
            break
        total += len(chunk)
        if total > expected_bytes:
            fail("downloaded input exceeds pinned byte count %d" % expected_bytes)
        digest.update(chunk)
        sink.write(chunk)
    if total != expected_bytes:
        fail("downloaded input has %d bytes, expected %d" % (total, expected_bytes))
    actual = digest.hexdigest()
    if actual != expected_sha256:
        fail("downloaded input SHA256 is %s, expected %s" % (actual, expected_sha256))
    return total, actual


def _redact_url(message: object, url: str) -> str:
    return str(message).replace(url, "<official source URL>")


def _credential_free_https_origin(url: str, what: str) -> tuple[str, int]:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
        host = (parsed.hostname or "").lower()
    except ValueError:
        fail(what + " has an invalid host or port")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        fail(what + " must be credential-free HTTPS on port 443")
    return host, port or 443


def validate_download_url(final_url: str, source_url: str) -> None:
    """Accept the verified source origin or an explicitly trusted Apple redirect."""
    source_origin = _credential_free_https_origin(source_url, "official source URL")
    final_origin = _credential_free_https_origin(final_url, "official input redirect")
    if final_origin == source_origin:
        return
    final_host = final_origin[0]
    if any(
        final_host == domain or final_host.endswith("." + domain)
        for domain in TRUSTED_APPLE_REDIRECT_DOMAINS
    ):
        return
    fail("official input download redirected outside an Apple HTTPS host")


def verify_file_identity(path: Path, expected_bytes: int, expected_sha256: str, timeout_seconds: int) -> None:
    if not path.is_file():
        fail("missing verified input: " + str(path))
    if path.stat().st_size != expected_bytes:
        fail(
            "cached input %s has %d bytes, expected %d; remove this stale cache entry"
            % (path, path.stat().st_size, expected_bytes)
        )
    with path.open("rb") as source, open(os.devnull, "wb") as sink:
        copy_verified_stream(source, sink, expected_bytes, expected_sha256, timeout_seconds)


def ensure_archive(config: Config) -> Path:
    output = archive_path(config)
    if output.exists():
        verify_file_identity(output, config.source.bytes, config.source.sha256, config.download_timeout)
        return output

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".part")
    partial.unlink(missing_ok=True)
    try:
        request = urllib.request.Request(config.source.url, headers={"User-Agent": "UniXNU-addon-ci/1"})
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as sink:
            validate_download_url(response.geturl(), config.source.url)
            copy_verified_stream(
                response,
                sink,
                config.source.bytes,
                config.source.sha256,
                config.download_timeout,
            )
        partial.replace(output)
    except CiError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as error:
        partial.unlink(missing_ok=True)
        fail("official input download failed: " + _redact_url(error, config.source.url))
    return output


def validate_archive_names(archive: zipfile.ZipFile) -> None:
    seen: set[str] = set()
    for info in archive.infolist():
        name = _safe_archive_name(info.filename, "ZIP entry")
        if not info.is_dir() and name in seen:
            fail("duplicate ZIP file entry: " + name)
        seen.add(name)


def _archive_member(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    matches = [info for info in archive.infolist() if info.filename == name]
    if len(matches) != 1 or matches[0].is_dir():
        fail("IPSW must contain exactly one file entry named " + name)
    return matches[0]


def read_manifest(archive: zipfile.ZipFile, config: Config) -> Mapping[str, object]:
    info = _archive_member(archive, config.manifest_entry)
    if info.file_size <= 0 or info.file_size > MAX_MANIFEST_BYTES:
        fail("BuildManifest.plist has an invalid size")
    with archive.open(info, "r") as stream:
        data = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(data) != info.file_size:
        fail("BuildManifest.plist length changed while reading")
    try:
        value = plistlib.loads(data)
    except Exception as error:
        fail("cannot parse BuildManifest.plist: " + str(error))
    return _mapping(value, "BuildManifest.plist")


def manifest_and_selection(
    config: Config, archive_file: Path | None = None
) -> tuple[Mapping[str, object], BuildSelection]:
    try:
        with zipfile.ZipFile(archive_file or ensure_archive(config), "r") as archive:
            validate_archive_names(archive)
            manifest = read_manifest(archive, config)
    except CiError:
        raise
    except (OSError, zipfile.BadZipFile) as error:
        fail("cannot open the verified IPSW: " + str(error))
    return manifest, select_build_images(manifest, config)


def extract_member(
    archive: zipfile.ZipFile,
    member_name: str,
    output: Path,
    expected_bytes: int,
    timeout_seconds: int,
) -> Path:
    safe_name = _safe_archive_name(member_name, "requested ZIP entry")
    info = _archive_member(archive, safe_name)
    if info.file_size != expected_bytes:
        fail(
            "IPSW entry %s has %d bytes, expected %d"
            % (safe_name, info.file_size, expected_bytes)
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and output.stat().st_size == expected_bytes:
        return output
    partial = output.with_name(output.name + ".part")
    partial.unlink(missing_ok=True)
    deadline = time.monotonic() + timeout_seconds
    total = 0
    try:
        with archive.open(info, "r") as source, partial.open("wb") as sink:
            while True:
                _check_deadline(deadline, "IPSW extraction")
                chunk = source.read(READ_BLOCK)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_bytes:
                    fail("IPSW entry %s exceeds its pinned size" % safe_name)
                sink.write(chunk)
        if total != expected_bytes:
            fail("IPSW entry %s produced %d bytes, expected %d" % (safe_name, total, expected_bytes))
        partial.replace(output)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return output


def extract_images(config: Config, archive_file: Path | None = None) -> tuple[dict[str, Path], BuildSelection]:
    outputs = {role: extracted_path(config, role) for role in ("os", "cryptex")}
    try:
        with zipfile.ZipFile(archive_file or ensure_archive(config), "r") as archive:
            validate_archive_names(archive)
            manifest = read_manifest(archive, config)
            selection = select_build_images(manifest, config)
            deadline = time.monotonic() + config.extract_timeout
            for role in ("os", "cryptex"):
                pin = config.images[role]
                extract_member(
                    archive,
                    pin.filename,
                    outputs[role],
                    pin.bytes,
                    max(1, int(deadline - time.monotonic())),
                )
    except CiError:
        raise
    except (OSError, zipfile.BadZipFile) as error:
        fail("cannot extract the two selected IPSW entries: " + str(error))
    return outputs, selection


def report_base(config: Config, status: str, selection: BuildSelection | None = None) -> dict[str, object]:
    source: dict[str, object] = {
        "archive_name": config.source.archive_name,
        "bytes": config.source.bytes,
        "sha256": config.source.sha256,
    }
    source["url"] = config.source.url if config.source.public else "[redacted]"
    result: dict[str, object] = {
        "schema": 1,
        "status": status,
        "source": source,
        "archive": str(archive_path(config).relative_to(ROOT)),
    }
    if selection is not None:
        result["build_manifest"] = {
            "entry": config.manifest_entry,
            "product_version": config.product_version,
            "product_build_version": selection.product_build,
            "selected_device": selection.device,
            "selected_device_class": config.device_class,
            "selected_restore_behavior": config.restore_behavior,
            "supported_product_types": list(selection.supported_products),
            "matching_build_identities": selection.matching_identities,
            "images": {
                role: {
                    "path": image.path,
                    "bytes": image.bytes,
                    "manifest_sha1": image.sha1,
                }
                for role, image in selection.images.items()
            },
        }
    return result


def write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def read_prepared_report(config: Config) -> dict[str, object]:
    try:
        report = json.loads(REPORT.read_text())
    except (OSError, json.JSONDecodeError) as error:
        fail("missing or invalid prepared OS identity report %s: %s" % (REPORT, error))
    if not isinstance(report, dict):
        fail("prepared OS identity report is not an object")
    source = report.get("source")
    build = report.get("build_manifest")
    if not isinstance(source, dict) or source.get("sha256") != config.source.sha256:
        fail("prepared OS report does not match the pinned IPSW SHA256")
    if (
        not isinstance(build, dict)
        or build.get("product_version") != config.product_version
        or build.get("product_build_version") != config.product_build
    ):
        fail("prepared OS report does not match the pinned OS version/build")
    images = build.get("images")
    if not isinstance(images, dict):
        fail("prepared OS report has no selected image identities")
    for role, pin in config.images.items():
        item = images.get(role)
        if not isinstance(item, dict) or item.get("path") != pin.filename or item.get("bytes") != pin.bytes:
            fail("prepared OS report does not match the pinned %s image" % role)
    return report


def attach_command(image: Path, mountpoint: Path, kind: str) -> list[str]:
    command = [
        "hdiutil",
        "attach",
        "-readonly",
        "-nobrowse",
        "-owners",
        "off",
        "-mountpoint",
        str(mountpoint),
        "-plist",
    ]
    if kind == "raw-apfs":
        command.extend(["-imagekey", "diskimage-class=CRawDiskImage"])
    command.append(str(image))
    return command


def run_captured(
    command: Sequence[str],
    timeout_seconds: int,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(item) for item in command],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=None if env is None else dict(env),
        check=False,
    )


def _attach_owned_device(stdout: str, mountpoint: Path) -> str:
    try:
        value = plistlib.loads(stdout.encode())
    except Exception as error:
        fail("hdiutil attach returned an invalid plist: " + str(error))
    entities = value.get("system-entities") if isinstance(value, dict) else None
    if not isinstance(entities, list):
        fail("hdiutil attach plist has no system entities")
    expected = mountpoint.resolve()
    mounted = [
        entity for entity in entities
        if isinstance(entity, dict)
        and isinstance(entity.get("mount-point"), str)
        and Path(entity["mount-point"]).resolve() == expected
    ]
    if len(mounted) != 1:
        fail("hdiutil attach did not report exactly one entity at " + str(mountpoint))
    device = mounted[0].get("dev-entry")
    if not isinstance(device, str) or not device:
        fail("hdiutil attach entity has no dev-entry")
    return device


def detach_owned(device: str, timeout_seconds: int) -> None:
    try:
        result = run_captured(["hdiutil", "detach", device], timeout_seconds)
    except subprocess.TimeoutExpired:
        fail("hdiutil detach timed out for " + device)
    if result.returncode:
        fail(
            "hdiutil detach failed for %s: %s"
            % (device, (result.stdout + result.stderr).strip()[-2000:])
        )


def _partial_attach_target(stdout: str, mountpoint: Path) -> str:
    try:
        value = plistlib.loads(stdout.encode())
    except Exception:
        return str(mountpoint)
    entities = value.get("system-entities") if isinstance(value, dict) else None
    if not isinstance(entities, list):
        return str(mountpoint)
    expected = mountpoint.resolve()
    mounted = [
        entity for entity in entities
        if isinstance(entity, dict)
        and isinstance(entity.get("mount-point"), str)
        and Path(entity["mount-point"]).resolve() == expected
        and isinstance(entity.get("dev-entry"), str)
        and entity["dev-entry"]
    ]
    if mounted:
        return mounted[-1]["dev-entry"]
    return str(mountpoint)


def attach_image(image: Path, mountpoint: Path, kind: str, timeout_seconds: int) -> AttachedImage:
    mountpoint.mkdir(parents=True, exist_ok=False)
    try:
        result = run_captured(attach_command(image, mountpoint, kind), timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            detach_owned(str(mountpoint), timeout_seconds)
        except CiError as cleanup:
            fail("hdiutil attach timed out at %s; partial-mount detach also failed: %s" % (mountpoint, cleanup))
        fail("hdiutil attach timed out at " + str(mountpoint))
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()[-3000:]
        try:
            detach_owned(_partial_attach_target(result.stdout, mountpoint), timeout_seconds)
        except CiError as cleanup:
            detail += "; partial-mount detach also failed: " + str(cleanup)
        fail("hdiutil attach failed at %s: %s" % (mountpoint, detail))
    try:
        owned = _attach_owned_device(result.stdout, mountpoint)
    except CiError as error:
        try:
            detach_owned(_partial_attach_target(result.stdout, mountpoint), timeout_seconds)
        except CiError as cleanup:
            fail("%s; attach-output cleanup also failed: %s" % (error, cleanup))
        raise
    return AttachedImage(role="", image=image, mountpoint=mountpoint, owned_device=owned)


def with_readonly_mounts(
    specs: Sequence[tuple[str, Path, str]],
    mount_root: Path,
    attach: Callable[[Path, Path, str, int], AttachedImage],
    detach: Callable[[str, int], None],
    action: Callable[[Mapping[str, AttachedImage]], object],
    mount_timeout: int,
    detach_timeout: int,
) -> object:
    attached: list[AttachedImage] = []
    failure: BaseException | None = None
    cleanup_errors: list[str] = []
    result: object = None
    try:
        mount_root.mkdir(parents=True, exist_ok=False)
        for role, image, kind in specs:
            mounted = attach(image, mount_root / role, kind, mount_timeout)
            attached.append(
                AttachedImage(
                    role=role,
                    image=mounted.image,
                    mountpoint=mounted.mountpoint,
                    owned_device=mounted.owned_device,
                )
            )
        result = action({item.role: item for item in attached})
    except BaseException as error:
        failure = error
    finally:
        for item in reversed(attached):
            try:
                detach(item.owned_device, detach_timeout)
            except BaseException as error:
                cleanup_errors.append(str(error))
        if mount_root.exists():
            try:
                shutil.rmtree(mount_root)
            except OSError as error:
                cleanup_errors.append("cannot remove own mount root %s: %s" % (mount_root, error))
    if failure is not None:
        if cleanup_errors:
            print("WARNING: mount cleanup also failed: " + "; ".join(cleanup_errors), file=sys.stderr)
        raise failure
    if cleanup_errors:
        fail("mount cleanup failed: " + "; ".join(cleanup_errors))
    return result


def generator_command(config: Config, os_mount: Path, cryptex_mount: Path) -> list[str]:
    generator = config.generator
    return [
        sys.executable,
        str(ROOT / "scripts/make-pipeline-cache.py"),
        "--target",
        _string(generator["target"], "generator.target"),
        "--gpu",
        _string(generator["gpu"], "generator.gpu"),
        "--os-mount",
        str(os_mount),
        "--cryptex-mount",
        str(cryptex_mount),
        "--os-build",
        config.product_build,
        "--prime-list",
        str(ROOT / _relative_path(generator["prime_list"], "generator.prime_list")),
        "--output",
        str(ROOT / _relative_path(generator["output"], "generator.output")),
        "--work",
        str(ROOT / _relative_path(generator["work"], "generator.work")),
        "--jobs",
        str(_positive_int(generator["jobs"], "generator.jobs")),
    ]


def run_host_smoke_and_generator(
    config: Config,
    mounts: Mapping[str, AttachedImage],
    log_path: Path,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_captured,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Run the full host smoke and generator under one mounted-input deadline."""
    quartzcore = (
        mounts["os"].mountpoint
        / "System/Library/Frameworks/QuartzCore.framework/default.metallib"
    )
    if not quartzcore.is_file():
        fail("the mounted 21H461 OS lacks the target QuartzCore metallib: " + str(quartzcore))

    smoke_command = [sys.executable, str(ROOT / "scripts/check-turnip-darwin-host.py")]
    generate_command = generator_command(
        config, mounts["os"].mountpoint, mounts["cryptex"].mountpoint
    )
    timeout_seconds = int(config.generator["timeout_seconds"])
    deadline = clock() + timeout_seconds
    smoke_env = dict(os.environ)
    smoke_env["NMB_TEST_QUARTZCORE_LIBRARY"] = str(quartzcore)
    staged: dict[str, dict[str, object]] = {}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("")

    def execute(
        label: str,
        what: str,
        command: list[str],
        env: Mapping[str, str] | None = None,
    ) -> None:
        remaining = int(deadline - clock())
        if remaining <= 0:
            fail("%s cannot start: the combined host-smoke/generator timeout expired" % what)
        header = "===== %s =====\n$ %s\n" % (what, " ".join(command))
        with log_path.open("a") as log:
            log.write(header)
        try:
            result = run(command, remaining, env=env)
        except subprocess.TimeoutExpired as error:
            detail = (error.stdout or "") + (error.stderr or "")
            if isinstance(detail, bytes):
                detail = detail.decode(errors="replace")
            with log_path.open("a") as log:
                log.write(detail)
                log.write("\n%s exceeded the remaining %d seconds\n" % (what, remaining))
            fail("%s timed out; see %s\n%s" % (what, log_path, detail))
        output = result.stdout + result.stderr
        with log_path.open("a") as log:
            log.write(output)
            if output and not output.endswith("\n"):
                log.write("\n")
            log.write("exit=%d\n" % result.returncode)
        staged[label] = {
            "command": command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
        if result.returncode:
            fail("%s failed with exit %d; see %s\n%s" %
                 (what, result.returncode, log_path, output))

    execute("host_smoke", "complete host smoke", smoke_command, smoke_env)
    execute("generator", "make-pipeline-cache.py", generate_command)
    return {
        "quartzcore": str(quartzcore),
        "quartzcore_bytes": quartzcore.stat().st_size,
        "quartzcore_sha256": hashlib.sha256(quartzcore.read_bytes()).hexdigest(),
        "combined_timeout_seconds": timeout_seconds,
        "stages": staged,
    }


def generate_pipeline_pack(config: Config, report: dict[str, object]) -> dict[str, object]:
    images = {
        role: extracted_path(config, role)
        for role in ("os", "cryptex")
    }
    for role, path in images.items():
        expected = config.images[role].bytes
        if not path.is_file() or path.stat().st_size != expected:
            fail("prepared %s image is missing or has the wrong size: %s" % (role, path))

    output = ROOT / _relative_path(config.generator["output"], "generator.output")
    output_report = output.with_name(output.name + ".json")
    command_template = generator_command(config, images["os"], images["cryptex"])
    log_path = ROOT / "build/dodge-pipeline-cache.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def action(mounts: Mapping[str, AttachedImage]) -> None:
        captured.update(run_host_smoke_and_generator(config, mounts, log_path))

    mount_root = STAGING / ("mounts-" + str(os.getpid()))
    with_readonly_mounts(
        [
            ("os", images["os"], config.images["os"].kind),
            ("cryptex", images["cryptex"], config.images["cryptex"].kind),
        ],
        mount_root,
        attach_image,
        detach_owned,
        action,
        config.mount_timeout,
        config.detach_timeout,
    )

    if not output.is_file():
        fail("make-pipeline-cache.py succeeded without producing " + str(output))
    with output.open("rb") as stream:
        magic = stream.read(8)
    if magic != b"SHEXP001":
        fail("generator output is not a SHEXP001 pack")
    try:
        generator_report = json.loads(output_report.read_text())
    except (OSError, json.JSONDecodeError) as error:
        fail("generator identity report is missing or invalid: " + str(error))
    if not isinstance(generator_report, dict):
        fail("generator identity report is not an object")
    for key, expected in (
        ("target", "ios"),
        ("gpu", "a830"),
        ("os_build", config.product_build),
        ("prime_list_entries", 166),
        ("pack", str(output.resolve())),
    ):
        if generator_report.get(key) != expected:
            fail("generator report %s is %r, expected %r" % (key, generator_report.get(key), expected))

    stages = captured.get("stages", {})
    smoke = stages.get("host_smoke", {}) if isinstance(stages, Mapping) else {}
    generator_stage = stages.get("generator", {}) if isinstance(stages, Mapping) else {}
    report["status"] = "pipeline-pack-generated"
    report["host_smoke"] = {
        "command": smoke.get("command", []),
        "library": captured.get("quartzcore", ""),
        "library_bytes": captured.get("quartzcore_bytes"),
        "library_sha256": captured.get("quartzcore_sha256"),
        "log": str(log_path.relative_to(ROOT)),
        "stdout_tail": str(smoke.get("stdout", ""))[-2000:],
        "combined_timeout_seconds": captured.get("combined_timeout_seconds"),
        "validation_boundary": (
            "The complete host smoke and pack generation ran in one read-only OS/cryptex "
            "mount lifecycle. The QuartzCore input was the mounted 21H461 metallib, not "
            "the runner's macOS library."
        ),
    }
    report["generator"] = {
        "command": " ".join(generator_stage.get("command", [])),
        "command_template": command_template,
        "log": str(log_path.relative_to(ROOT)),
        "stdout_tail": str(generator_stage.get("stdout", ""))[-2000:],
        "report": str(output_report.relative_to(ROOT)),
        "pack": str(output.relative_to(ROOT)),
        "report_identity": generator_report,
        "host_compilation": True,
        "gpu_execution": False,
        "target_device_cache_hit_verified": False,
        "validation_boundary": (
            "Real macOS host compilation and SHEXP001 generation over the pinned "
            "21H461 inputs; physical A830 execution and target cache reuse are not proven."
        ),
    }
    return report


def _load_script(name: str, relative: str):
    import importlib.util

    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        fail("cannot load " + relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _adapt_prime_result(result: Mapping[str, object]) -> dict[str, object]:
    summary = result.get("producer_summary")
    return {
        "producer_invoked": result.get("producer_invoked") is True,
        "returncode": result.get("returncode"),
        "status": result.get("status"),
        "resumed": result.get("resumed") is True,
        "stdout": result.get("stdout") or "",
        "stderr": result.get("stderr") or "",
        "reason": result.get("reason") or "",
        "summary": summary if isinstance(summary, dict) else {},
    }


def _verify_members_mounted(config: Config, mounts: Mapping[str, AttachedImage], members, _prepared: Mapping[str, object]) -> dict[str, object]:
    """Compile the real producer and prime eight members. Never packs."""
    from types import SimpleNamespace

    generator = _load_script("make_pipeline_cache", "scripts/make-pipeline-cache.py")
    manifest = members.load_member_manifest()
    os_mount = mounts["os"].mountpoint
    cryptex_mount = mounts["cryptex"].mountpoint
    generator.read_build(os_mount, config.product_build)
    if not (cryptex_mount / "System/Library").is_dir():
        fail("cryptex mount lacks System/Library; members-only keeps the OS/cryptex contract")
    work = members.WORK.resolve()
    if work.parent != (ROOT / "build").resolve() or work.name != "explicit-members":
        fail("refusing to reset an unexpected member work directory")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    library = os_mount / str(manifest["library_path"]).lstrip("/")
    args = argparse.Namespace(
        target="ios",
        gpu="a830",
        os_build=config.product_build,
        os_mount=os_mount,
        cryptex_mount=cryptex_mount,
        prime_list=work / "unused-full-list.txt",
        output=work / "not-a-pack",
        pack="^$",
        jobs=1,
        work=work,
        member_file_order=None,
        member_outer_sha256=None,
        member_sha256=None,
    )
    inputs = generator.resolve_inputs(args)
    inputs.work = work
    inputs.producer = work / "pipeline-cache-host"

    def verify_peer() -> None:
        for path, what in (
            (inputs.host_turnip, "host Turnip"),
            (inputs.target_turnip, "target Turnip"),
            (inputs.host_profile, "host Turnip profile"),
            (inputs.target_profile, "target Turnip profile"),
            (inputs.bridge, "host Metal bridge"),
        ):
            generator.require_path(path, what)
        _uuid, platform_id = generator.macho_identity(inputs.target_turnip)
        if platform_id != generator.PLATFORM_IOS:
            fail("target Turnip is not an iOS dylib")
        host_profile = generator.profile_identity(inputs.host_profile, "host")
        target_profile = generator.profile_identity(inputs.target_profile, "ios")
        for key in ("mesa_revision", "source_diff_sha256", "extra_sources"):
            if host_profile.get(key) != target_profile.get(key):
                fail("host and target Turnip compiler-source identities differ")
        pins = json.loads((ROOT / "scripts/data/adreno-compiler-inputs.json").read_text())
        if target_profile.get("mesa_revision") != pins["mesa"]["commit"]:
            fail("target Turnip profile is not from the pinned Mesa revision")
        establish_host_peer(os_mount, inputs.peer, work / "host-peer-build.log")
        generator.verify_host_peer(args, inputs)

    def compile_real() -> None:
        generator.compile_producer(args, inputs)
        if not inputs.producer.is_file():
            fail("compile_producer did not write the producer binary")

    def environment() -> dict[str, str]:
        return generator.runtime_environment(args, inputs)

    def prime(run_dir: Path, command: list[str], env: Mapping[str, str], prime_list: Path) -> dict[str, object]:
        member_work = run_dir.parents[1]
        argv = list(command[-6:])
        cloned = SimpleNamespace(**vars(inputs))
        cloned.work = member_work
        cloned.prime_list = prime_list
        cloned.member_argv = argv
        cloned.producer = inputs.producer
        expected = [
            str(cloned.producer), str(cloned.host_turnip), str(member_work / "runs" / "000"),
            str(prime_list), str(os_mount), str(cryptex_mount), str(cloned.target_turnip),
            env["HOSTSIM_GPU"], "0", "1", *argv,
        ]
        if [str(item) for item in command] != expected:
            fail("member command does not match prime_one argv")
        identity = {
            "paths": [manifest["library_path"]],
            "os_mount": str(os_mount),
            "cryptex_mount": str(cryptex_mount),
        }
        stamp_id = generator.stamp(cloned.producer, cloned, dict(env), config.product_build)
        return _adapt_prime_result(generator.prime_one(0, cloned, identity, dict(env), stamp_id))

    def refuse_pack(*_args: object, **_kwargs: object) -> None:
        fail("explicit member mode attempted to pack")

    return members.run_explicit_members(
        manifest,
        os_mount=os_mount,
        cryptex_mount=cryptex_mount,
        work=work,
        library_file=library,
        producer=inputs.producer,
        host_turnip=inputs.host_turnip,
        target_turnip=inputs.target_turnip,
        peer=inputs.peer,
        gpu="a830",
        verify_peer=verify_peer,
        compile_producer=compile_real,
        prime_one=prime,
        environment=environment,
        pack_writer=refuse_pack,
        claim_host_execution=True,
        execution_source="observed",
    )


def verify_explicit_members(config: Config, report_path: Path | None = None) -> None:
    members = _load_script("explicit_member_verify", "scripts/explicit-member-verify.py")
    destination = report_path or members.REPORT
    if sys.platform != "darwin":
        members.write_report(destination, members.unverified_report(
            "real member producer requires macOS arm64; this host is " + sys.platform
        ))
        fail("unverified: real member producer requires macOS arm64; not claiming a Mac result")
    require_ci_host()
    prepared = read_prepared_report(config)
    images = {role: extracted_path(config, role) for role in ("os", "cryptex")}
    for role, path in images.items():
        expected = config.images[role].bytes
        if not path.is_file() or path.stat().st_size != expected:
            fail("prepared %s image is missing or has the wrong size: %s" % (role, path))
    captured: dict[str, object] = {}

    def action(mounts: Mapping[str, AttachedImage]) -> None:
        try:
            captured["report"] = _verify_members_mounted(config, mounts, members, prepared)
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            failed = members.unverified_report(str(error))
            failed["status"] = "explicit-members-failed"
            failed["pack_written"] = False
            members.write_report(destination, failed)
            if isinstance(error, CiError):
                raise
            fail("explicit member verification failed: " + str(error))

    mount_root = STAGING / ("member-mounts-" + str(os.getpid()))
    with_readonly_mounts(
        [
            ("os", images["os"], config.images["os"].kind),
            ("cryptex", images["cryptex"], config.images["cryptex"].kind),
        ],
        mount_root,
        attach_image,
        detach_owned,
        action,
        config.mount_timeout,
        config.detach_timeout,
    )
    report = captured.get("report")
    if not isinstance(report, dict):
        fail("explicit member verification did not return a report")
    members.write_report(destination, report)
    members.LOG.write_text(
        "status=%s ci_executed=%s mac_producer_verified=%s pack_written=false\n"
        % (report.get("status"), report.get("ci_executed"), report.get("mac_producer_verified"))
    )
    members.assert_no_pack([ROOT / "build/dodge.m2vpack", ROOT / "build/gpu-addon.bin"])
    summary = report.get("summary")
    if not isinstance(summary, dict) or summary.get("passed") is not True:
        fail("explicit member verification failed; no pack was written. see " + str(destination))
    if report.get("pack_written") or report.get("pack_verified") or report.get("driver_claimed"):
        fail("explicit member verification claimed a pack or driver result")
    print("PASS: observed eight explicit members; no pack was produced")


def cleanup_staging() -> None:
    target = STAGING.resolve()
    expected_parent = (ROOT / ".cache").resolve()
    if target.parent != expected_parent or target.name != "dodge-ipsw":
        fail("refusing to clean an unexpected path: " + str(target))
    if target.exists():
        try:
            shutil.rmtree(target)
        except OSError as error:
            fail("cannot remove OS input staging %s: %s" % (target, error))


def run_all(config: Config) -> None:
    archive = ensure_archive(config)
    _, selection = extract_images(config, archive)
    write_json(REPORT, report_base(config, "images-extracted", selection))
    report = read_prepared_report(config)
    generate_pipeline_pack(config, report)
    write_json(REPORT, report)
    output = ROOT / _relative_path(config.generator["output"], "generator.output")
    print("PASS: generated and identity-checked " + str(output))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    result.add_argument(
        "--mode",
        choices=("all", "download", "extract", "generate", "cleanup", "verify-members"),
        default="all",
    )
    result.add_argument(
        "--drop-archive",
        action="store_true",
        help="after a successful extraction, remove the local IPSW because Actions already cached it",
    )
    result.add_argument("--report", type=Path, default=None, help="optional explicit-member report path")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        config = load_config(args.config.resolve())
        if args.mode == "cleanup":
            cleanup_staging()
            print("PASS: removed dodge OS input staging")
            return 0
        if args.mode == "verify-members":
            verify_explicit_members(config, args.report)
            return 0

        require_ci_host()
        if args.mode == "download":
            archive = ensure_archive(config)
            _, selection = manifest_and_selection(config, archive)
            report = report_base(config, "ipsw-verified", selection)
            write_json(REPORT, report)
            print(
                "PASS: official IPSW stream identity verified (%d bytes, sha256 %s); "
                "BuildManifest selects %s for %s"
                % (config.source.bytes, config.source.sha256, config.product_build, config.device)
            )

        if args.mode == "extract":
            archive = ensure_archive(config)
            _, selection = extract_images(config, archive)
            write_json(REPORT, report_base(config, "images-extracted", selection))
            print("PASS: extracted exactly the pinned OS and cryptex image entries")
            if args.drop_archive:
                try:
                    archive_path(config).unlink()
                except OSError as error:
                    fail("cannot remove the cached IPSW after extraction: " + str(error))

        if args.mode == "generate":
            report = read_prepared_report(config)
            generate_pipeline_pack(config, report)
            write_json(REPORT, report)
            output = ROOT / _relative_path(config.generator["output"], "generator.output")
            print("PASS: generated and identity-checked " + str(output))

        if args.mode == "all":
            failure: BaseException | None = None
            cleanup_failure: CiError | None = None
            try:
                run_all(config)
            except BaseException as error:
                failure = error
            try:
                cleanup_staging()
            except CiError as error:
                cleanup_failure = error
            if failure is not None:
                if cleanup_failure is not None:
                    print("WARNING: " + str(cleanup_failure), file=sys.stderr)
                raise failure
            if cleanup_failure is not None:
                raise cleanup_failure
        return 0
    except CiError as error:
        print("FAIL: " + str(error), file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as error:
        print("FAIL: timed out: " + " ".join(str(item) for item in (error.cmd or [])), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
