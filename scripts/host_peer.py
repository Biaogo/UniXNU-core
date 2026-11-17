#!/usr/bin/env python3
"""Prepare the A740 host peer with its top-level host-only GPU contract patch.

The Adreno740 submodule stays unchanged.  Callers copy the checked-in source
into their build directory, apply the patch there, and use that prepared file
for compilation, inspection, and cache-input stamps.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "drivers/Adreno740"
ORIGINAL = DRIVER / "tests/hostsim/host_gpu_service.cpp"
PATCH = ROOT / "patches/adreno740/hostsim-a830-peer.patch"
SHIM_ORIGINAL = DRIVER / "tests/hostsim/host_iokit_shim.cpp"
SHIM_PATCH = ROOT / "scripts/data/hostsim-iokit-stall.patch"
HELPER = Path(__file__).resolve()
HOST_CLOCK_SHIM = ROOT / "scripts/host-clock-shim.cpp"
HOSTSIM_PAGE_SIZE = "4096"
SOURCE_RELATIVE = Path("tests/hostsim/host_gpu_service.cpp")
SHIM_SOURCE_RELATIVE = Path("tests/hostsim/host_iokit_shim.cpp")
HOST_INCLUDE_HEADERS = (
    "IOKit/IOTypes.h",
    "IOKit/IOReturn.h",
    "IOKit/IOBufferMemoryDescriptor.h",
    "IOKit/IOLib.h",
    "kern/task.h",
    "sys/resource_private.h",
)


@dataclass(frozen=True)
class GpuContract:
    chip_id: int
    gmem_bytes: int
    gmem_base: int
    uche_trap_base: int
    va_start: int
    va_size: int
    highest_bank_bit: int
    ubwc_swizzle: int
    macrotile_mode: int
    ubwc_version: int
    flags: int
    context_buffers: int
    smmu_input_bits: int
    smmu_output_bits: int

    def probe_arguments(self) -> list[str]:
        return [
            str(self.chip_id), str(self.gmem_bytes), str(self.gmem_base),
            str(self.uche_trap_base), str(self.va_start), str(self.va_size),
            str(self.highest_bank_bit), str(self.ubwc_swizzle),
            str(self.macrotile_mode), str(self.ubwc_version), str(self.flags),
            str(self.context_buffers), str(self.smmu_input_bits),
            str(self.smmu_output_bits),
        ]


# Values are the device facts reported to Turnip. The A740 host context maps
# its preemption pages at 0x20000000, so its client scope starts above every
# kernel buffer. The A830 VA is the 4 GiB aperture the base context can build
# here, not the device repo's 16 GiB configuration.
CONTRACTS = {
    "unset": GpuContract(0x43050A01, 3 << 20, 0x01000000, 0x1FFFFFFFFF000,
                          0x40000000, 0x40000000, 16, 6, 1, 4, 1, 11, 44, 40),
    "a740": GpuContract(0x43050A01, 3 << 20, 0x01000000, 0x1FFFFFFFFF000,
                        0x40000000, 0x40000000, 16, 6, 1, 4, 1, 11, 44, 40),
    "a830": GpuContract(0x44050001, 12 << 20, 0x04000000, 0x1FFFFFFFFF000,
                         0x20000000, 0xE0000000, 16, 6, 1, 5, 1, 6, 48, 36),
}


@dataclass(frozen=True)
class PreparedPeer:
    original: Path
    patch: Path
    helper: Path
    source: Path
    shim_original: Path
    shim_patch: Path
    shim_source: Path
    build_dir: Path
    manifest: Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hostsim_environment(source: dict[str, str] | None = None, **updates: str) -> dict[str, str]:
    """Return the shared host-sim runtime contract.

    Cached address layouts and BO sizes must model the tablet's 4 KiB granule,
    not the 16 KiB granule of Apple silicon hosts.
    """
    environment = dict(os.environ if source is None else source)
    environment["HOSTSIM_PAGE_SIZE"] = HOSTSIM_PAGE_SIZE
    environment.update(updates)
    return environment


def write_host_includes(build_dir: Path) -> Path:
    """Write every external kernel header used by the real native host core.

    The fixture supplies the declarations those sources need.  In particular,
    it deliberately leaves PRIO_DARWIN_GPU_* undefined so the core keeps its
    existing host no-OS-GPU-role behavior instead of inventing private values.
    """
    includes = build_dir / "host-include"
    for name in HOST_INCLUDE_HEADERS:
        path = includes / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "XnuMemoryFixture.h"\n')
    return includes


def prepare(build_dir: Path) -> PreparedPeer:
    """Apply the peer patch to a copy below build_dir and return its identity.

    The staging tree gets its own Git repository.  Without that boundary,
    git apply in a directory below the enclosing UniXNU checkout can discover
    the parent repository, accept the pathspec, and silently include no file.
    """
    build_dir = build_dir.resolve()
    for name, path in (
        ("host peer source", ORIGINAL),
        ("host peer patch", PATCH),
        ("host IOKit shim source", SHIM_ORIGINAL),
        ("host IOKit stall patch", SHIM_PATCH),
        ("prepare helper", HELPER),
        ("host clock shim", HOST_CLOCK_SHIM),
    ):
        if not path.is_file():
            raise RuntimeError("missing %s: %s" % (name, path))
    git_tool = shutil.which("git")
    if not git_tool:
        raise RuntimeError("git is required to prepare the host peer")

    staged = build_dir / "peer-source.tmp"
    prepared = build_dir / "peer-source"
    if staged.exists():
        shutil.rmtree(staged)
    staged_sources = {}
    for original, relative in ((ORIGINAL, SOURCE_RELATIVE), (SHIM_ORIGINAL, SHIM_SOURCE_RELATIVE)):
        staged_source = staged / relative
        staged_source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, staged_source)
        staged_sources[relative] = staged_source
    git_environment = os.environ.copy()
    for name in (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_WORK_TREE",
    ):
        git_environment.pop(name, None)
    try:
        initialized = subprocess.run(
            [git_tool, "init", "--quiet", str(staged)],
            capture_output=True, text=True, env=git_environment,
        )
        if initialized.returncode:
            detail = (initialized.stdout + initialized.stderr).strip()
            raise RuntimeError("cannot initialize isolated host peer staging: %s" % detail)
        top_level = subprocess.run(
            [git_tool, "-C", str(staged), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, env=git_environment,
        )
        if top_level.returncode or Path(top_level.stdout.strip()).resolve() != staged:
            detail = (top_level.stdout + top_level.stderr).strip()
            raise RuntimeError("host peer staging did not isolate Git from the enclosing checkout: %s" % detail)
        for patch, relative, description in (
            (PATCH, SOURCE_RELATIVE, "host peer"),
            (SHIM_PATCH, SHIM_SOURCE_RELATIVE, "host IOKit stall"),
        ):
            apply_command = [
                git_tool, "-C", str(staged), "apply",
                "--include=" + relative.as_posix(),
            ]
            result = subprocess.run(
                [*apply_command, "--check", str(patch)],
                capture_output=True, text=True, env=git_environment,
            )
            if not result.returncode:
                result = subprocess.run(
                    [*apply_command, str(patch)],
                    capture_output=True, text=True, env=git_environment,
                )
            if result.returncode:
                detail = (result.stdout + result.stderr).strip()
                raise RuntimeError("%s patch does not apply to %s: %s"
                                   % (description, staged_sources[relative], detail))
        expected = "const NativeAdrenoAbiDeviceInfo infoA830="
        if expected not in staged_sources[SOURCE_RELATIVE].read_text():
            raise RuntimeError("prepared host peer lacks the A830 contract")
        if "static bool hostsimStall(" not in staged_sources[SHIM_SOURCE_RELATIVE].read_text():
            raise RuntimeError("prepared host IOKit shim lacks the deadline stall control")
        shutil.rmtree(staged / ".git")
        if prepared.exists():
            shutil.rmtree(prepared)
        staged.rename(prepared)
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return PreparedPeer(
        ORIGINAL,
        PATCH,
        HELPER,
        prepared / SOURCE_RELATIVE,
        SHIM_ORIGINAL,
        SHIM_PATCH,
        prepared / SHIM_SOURCE_RELATIVE,
        build_dir,
        build_dir / "peer-build.json",
    )


def write_build_manifest(peer: PreparedPeer, dylib: Path) -> None:
    if not dylib.is_file():
        raise RuntimeError("host peer library was not produced: %s" % dylib)
    value = {
        "original_source_sha256": sha256(peer.original),
        "original_shim_source_sha256": sha256(peer.shim_original),
        "prepare_helper_sha256": sha256(peer.helper),
        "host_clock_shim_sha256": sha256(HOST_CLOCK_SHIM),
        "patch_sha256": sha256(peer.patch),
        "shim_patch_sha256": sha256(peer.shim_patch),
        "prepared_source_sha256": sha256(peer.source),
        "prepared_shim_source_sha256": sha256(peer.shim_source),
        "peer_product": str(dylib.resolve()),
        "peer_product_sha256": sha256(dylib),
    }
    peer.manifest.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


MMIO_SOURCE = ROOT / "kits/ArmSmmu4K/ArmSmmu4KMmio.c"
MMIO_AARCH64_GUARD = (
    "#if !defined(__aarch64__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__\n"
    "#error Direct SMMUv2 MMIO binding requires little-endian AArch64\n"
    "#endif"
)
MMIO_AARCH64_BARRIER = '__asm__ volatile("dsb sy":::\"memory\");'
MMIO_HOST_GUARD = (
    "#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__\n"
    "#error Direct SMMUv2 MMIO host regression requires little-endian host\n"
    "#endif"
)
MMIO_HOST_BARRIER = "__atomic_thread_fence(__ATOMIC_SEQ_CST);"


def normalize_host_cpu(machine: str) -> str:
    """Map a uname or compiler dumpmachine string to arm64 or x86_64.

    Unknown architectures are refused. This does not rewrite the driver
    sources and is not an iOS target selector.
    """
    token = machine.strip().lower().split("-", 1)[0]
    if token in ("arm64", "aarch64"):
        return "arm64"
    if token in ("x86_64", "amd64"):
        return "x86_64"
    raise RuntimeError("host peer adaptation refuses unknown architecture: " + machine)


def host_core_cflags(machine: str) -> list[str]:
    """Return host-only compile flags for the peer core.

    x86_64 uses the existing NATIVE_ADRENO_HOST_TEST branches, which replace
    dsb with an atomic fence. ARM64 gets no extra flag, so the dsb path used
    by the device and the Apple Silicon host stays unchanged. On Apple the
    same macro does not switch weak_import to ELF weak, because those branches
    also require !defined(__APPLE__).
    """
    if normalize_host_cpu(machine) == "x86_64":
        return ["-DNATIVE_ADRENO_HOST_TEST"]
    return []


def host_mmio_source(work: Path, machine: str) -> Path:
    """Return the real MMIO adapter, adapted only for an x86_64 host copy.

    The kit file is never modified. ARM64 returns that file. The x86_64 copy
    keeps the register algorithm and only replaces the architecture gate and
    the dsb sy barrier already used by the Linux host regression.
    """
    if not MMIO_SOURCE.is_file():
        raise RuntimeError("missing host MMIO source: %s" % MMIO_SOURCE)
    if normalize_host_cpu(machine) == "arm64":
        return MMIO_SOURCE
    source = MMIO_SOURCE.read_text()
    if source.count(MMIO_AARCH64_GUARD) != 1 or source.count(MMIO_AARCH64_BARRIER) != 1:
        raise RuntimeError("ArmSmmu4KMmio.c host adaptation anchors changed")
    adapted = work / "ArmSmmu4KMmio.host.c"
    adapted.parent.mkdir(parents=True, exist_ok=True)
    adapted.write_text(
        source.replace(MMIO_AARCH64_GUARD, MMIO_HOST_GUARD).replace(
            MMIO_AARCH64_BARRIER, MMIO_HOST_BARRIER
        )
    )
    return adapted


def verify_build_manifest(peer: PreparedPeer, dylib: Path) -> None:
    if not dylib.is_file():
        raise RuntimeError("missing host peer product %s; run scripts/check-turnip-darwin-host.py" % dylib)
    if not peer.manifest.is_file():
        raise RuntimeError("host peer has no build manifest; run scripts/check-turnip-darwin-host.py")
    value = json.loads(peer.manifest.read_text())
    expected = {
        "original_source_sha256": sha256(peer.original),
        "original_shim_source_sha256": sha256(peer.shim_original),
        "prepare_helper_sha256": sha256(peer.helper),
        "host_clock_shim_sha256": sha256(HOST_CLOCK_SHIM),
        "patch_sha256": sha256(peer.patch),
        "shim_patch_sha256": sha256(peer.shim_patch),
        "prepared_source_sha256": sha256(peer.source),
        "prepared_shim_source_sha256": sha256(peer.shim_source),
        "peer_product": str(dylib.resolve()),
        "peer_product_sha256": sha256(dylib),
    }
    mismatched = [name for name, digest in expected.items() if value.get(name) != digest]
    if mismatched:
        raise RuntimeError(
            "host peer product is stale for %s; rerun scripts/check-turnip-darwin-host.py"
            % ", ".join(mismatched)
        )
