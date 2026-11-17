#!/usr/bin/env python3
"""Build and run the dodge host peer on Linux with the real kernel core.

Each GPU choice runs in a fresh process: A740 (unset and explicit), A830, and
an invalid value.  The exercised path is the prepared host peer, ArmSmmu4K,
NativeAdreno memory/VM/ring/submit/user-client/context, AdrenoPM4, and the
repository's software XnuMemoryFixture.  It uses no Apple framework, Vulkan,
or GPU execution.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from host_peer import CONTRACTS, HOST_CLOCK_SHIM, hostsim_environment, prepare, verify_build_manifest, write_build_manifest, write_host_includes
from watchdog_source import prepare_watchdog_include

DRIVER = ROOT / "drivers/Adreno740"
SMMU = ROOT / "kits/ArmSmmu4K"
PM4 = ROOT / "kits/AdrenoPM4"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--cxx", default=os.environ.get("CXX", "g++"), help="C++ compiler (default: $CXX or g++)")
    return result


def host_mmio_source(work: Path) -> Path:
    """Return the real MMIO adapter, adapted only for a non-AArch64 host."""
    original = SMMU / "ArmSmmu4KMmio.c"
    if platform.machine().lower() in ("aarch64", "arm64"):
        return original
    source = original.read_text()
    guard = (
        "#if !defined(__aarch64__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__\n"
        "#error Direct SMMUv2 MMIO binding requires little-endian AArch64\n"
        "#endif"
    )
    barrier = '__asm__ volatile("dsb sy":::"memory");'
    replacement_guard = (
        "#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__\n"
        "#error Direct SMMUv2 MMIO host regression requires little-endian host\n"
        "#endif"
    )
    replacement_barrier = "__atomic_thread_fence(__ATOMIC_SEQ_CST);"
    if source.count(guard) != 1 or source.count(barrier) != 1:
        raise RuntimeError("ArmSmmu4KMmio.c host adaptation anchors changed")
    adapted = work / "ArmSmmu4KMmio.host.c"
    adapted.write_text(source.replace(guard, replacement_guard).replace(barrier, replacement_barrier))
    return adapted


def checked(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, text=True, **kwargs)
    except subprocess.CalledProcessError as error:
        if error.stdout:
            print(error.stdout, file=sys.stderr)
        if error.stderr:
            print(error.stderr, file=sys.stderr)
        raise


def verify_prepare_inside_repository() -> None:
    """Exercise the build path used by the macOS check from inside the repo."""
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dodge-host-peer-prepare-", dir=build) as directory:
        peer = prepare(Path(directory) / "host-peer")
        original = peer.original.read_text()
        prepared = peer.source.read_text()
        marker = "const NativeAdrenoAbiDeviceInfo infoA830="
        contract = (
            marker
            + "{0x44050001u,12u<<20,0x4000000ull,0x1fffffffff000ull,0x20000000ull,0xE0000000ull,\n"
            + "    16,6,1,5,NativeAdrenoDeviceClientIova,0};"
        )
        if marker in original:
            raise SystemExit("FAIL: pinned host peer source already contains the A830 contract")
        if contract not in prepared:
            raise SystemExit("FAIL: in-repository prepare did not produce the exact A830 ABI record")
        if peer.source.read_bytes() == peer.original.read_bytes():
            raise SystemExit("FAIL: in-repository prepare did not change the pinned host peer source")
        if (peer.source.parents[2] / ".git").exists():
            raise SystemExit("FAIL: in-repository prepare leaked its staging Git metadata")
    print("in-repo prepare: exact A830 patch applied below build/")


def main() -> int:
    args = parser().parse_args()
    cxx = shutil.which(args.cxx)
    if not cxx:
        raise SystemExit("compiler not found: " + args.cxx)
    verify_prepare_inside_repository()
    temporary_parent = os.environ.get("TMPDIR") or None
    with tempfile.TemporaryDirectory(prefix="dodge-host-peer-", dir=temporary_parent) as directory:
        work = Path(directory)
        peer = prepare(work / "host-peer")
        watchdog_dir = prepare_watchdog_include(work)
        deadline = work / "test-turnip-watchdog-deadline"
        checked([
            cxx, "-std=c++17", "-O1", "-Wall", "-Wextra", "-Werror",
            "-I", str(watchdog_dir), str(SCRIPTS / "test-turnip-watchdog-deadline.cpp"),
            "-o", str(deadline),
        ])
        deadline_result = checked([str(deadline)], capture_output=True)
        print(deadline_result.stdout.strip())
        includes = write_host_includes(work)

        common = [
            "-O1", "-g", "-fPIC", "-DNATIVE_ADRENO_HOST_TEST",
            "-I", str(includes), "-I", str(DRIVER / "tests"), "-I", str(DRIVER),
            "-I", str(SMMU), "-I", str(PM4),
        ]
        mmio_source = host_mmio_source(work)
        objects: list[Path] = []
        for name in ("ArmSmmu4KContext", "ArmSmmu4KProgram", "ArmSmmu4KLive", "ArmSmmu4KStreams", "ArmSmmu4KMmio"):
            obj = work / (name + ".o")
            source = mmio_source if name == "ArmSmmu4KMmio" else SMMU / (name + ".c")
            checked([cxx, "-x", "c", "-std=c11", *common, "-c", str(source), "-o", str(obj)])
            objects.append(obj)
        pm4 = work / "AdrenoPM4.o"
        checked([cxx, "-x", "c", "-std=c11", *common, "-c", str(PM4 / "AdrenoPM4.c"), "-o", str(pm4)])
        objects.append(pm4)
        for source in (
            peer.source,
            DRIVER / "NativeAdrenoMemory.cpp",
            DRIVER / "NativeAdrenoSmmuMemory.cpp",
            DRIVER / "NativeAdrenoVm.cpp",
            DRIVER / "NativeAdrenoRing.cpp",
            DRIVER / "NativeAdrenoPreempt.cpp",
            DRIVER / "NativeAdrenoSubmit.cpp",
            DRIVER / "NativeAdrenoUserClient.cpp",
            DRIVER / "NativeAdrenoTimer.cpp",
            DRIVER / "NativeAdrenoSmmuContext.cpp",
            DRIVER / "NativeAdrenoGpuContext.cpp",
        ):
            obj = work / (source.stem + ".o")
            checked([
                cxx, "-std=c++23", *common, "-Wall", "-Wextra",
                "-Wno-unused-parameter", "-Wno-unused-function", "-Wno-missing-field-initializers",
                "-Wno-class-memaccess", "-c", str(source), "-o", str(obj),
            ])
            objects.append(obj)

        stall_test = work / "test-hostsim-iokit-stall"
        checked([
            cxx, "-std=c++17", "-O1", "-DHOSTSIM_IOKIT_STALL_TEST",
            "-include", str(peer.shim_source), str(SCRIPTS / "test-hostsim-iokit-stall.cpp"),
            "-o", str(stall_test),
        ])
        stall_result = checked([str(stall_test)], capture_output=True)
        diagnostic = stall_result.stdout + stall_result.stderr
        begin = re.findall(r"^shim: stall method=2 route=test requested_ms=1000 result=begin$", diagnostic, re.M)
        complete = re.findall(
            r"^shim: stall method=2 route=test requested_ms=1000 interrupts=1 result=complete elapsed_ms=(\d+)$",
            diagnostic, re.M,
        )
        attempts = re.findall(
            r"^shim: stall method=2 route=test attempt=1 requested_ms=1000 returned_s=\d+ "
            r"errno=4 \(Interrupted system call\) interrupted=1 elapsed_ms=\d+$",
            diagnostic, re.M,
        )
        if (len(begin) != 1 or len(complete) != 1 or int(complete[0]) < 1000
                or len(attempts) != 1):
            print(diagnostic, end="", file=sys.stderr)
            raise SystemExit("FAIL: prepared host IOKit stall diagnostic lacks one interrupted, deadline-bounded injection")
        print(stall_result.stdout.strip())
        print("stall: " + next(line for line in diagnostic.splitlines() if "result=complete" in line))

        kpi = work / "host-kpi.cpp"
        kpi.write_text(
            'extern "C" int PE_parse_boot_argn(const char *, void *, int) { return 0; }\n'
        )
        kpi_object = work / "host-kpi.o"
        checked([cxx, "-std=c++17", "-O1", "-fPIC", "-c", str(kpi), "-o", str(kpi_object)])
        objects.append(kpi_object)
        clock_object = work / "host-clock-shim.o"
        checked([cxx, "-std=c++17", "-O1", "-fPIC", "-c", str(HOST_CLOCK_SHIM), "-o", str(clock_object)])
        objects.append(clock_object)

        library = work / "libhostiokit.so"
        checked([cxx, "-shared", "-Wl,-z,defs", "-Wl,--no-undefined", "-o", str(library), *map(str, objects)])
        write_build_manifest(peer, library)
        verify_build_manifest(peer, library)
        manifest = json.loads(peer.manifest.read_text())
        if (manifest["original_source_sha256"] == manifest["prepared_source_sha256"]
                or manifest["original_shim_source_sha256"] == manifest["prepared_shim_source_sha256"]):
            raise SystemExit("FAIL: host peer manifest did not record both applied source patches")
        print("manifest: original/prepared peer and shim identities match the linked product")
        nm = shutil.which("nm")
        if not nm:
            raise SystemExit("FAIL: nm is required to audit host peer dynamic symbols")
        defined = checked([nm, "-D", "--defined-only", str(library)], capture_output=True).stdout
        if " native_adreno_now_us\n" not in defined:
            raise SystemExit("FAIL: host peer library does not define native_adreno_now_us")
        undefined = checked([nm, "-D", "--undefined-only", str(library)], capture_output=True).stdout
        unexpected = []
        optional_weak = []
        for line in undefined.splitlines():
            fields = line.split()
            if len(fields) < 2 or not fields[-1].startswith("native_"):
                continue
            kind, symbol = fields[-2:]
            if kind in ("w", "v") and symbol == "native_gpu_dcvs":
                optional_weak.append(symbol)
            else:
                unexpected.append("%s %s" % (kind, symbol))
        if unexpected:
            raise SystemExit("FAIL: host peer library has unresolved driver symbols: " + ", ".join(unexpected))
        if optional_weak != ["native_gpu_dcvs"]:
            raise SystemExit(
                "FAIL: optional host KPI has an unexpected binding: %s"
                % (", ".join(optional_weak) or "none")
            )
        print("symbols: native_adreno_now_us defined; only optional weak native_gpu_dcvs remains")
        probe = work / "host-peer-probe"
        checked([
            cxx, "-std=c++17", "-O1", "-DHOST_PEER_DIRECT", "-I", str(DRIVER), "-I", str(PM4),
            str(SCRIPTS / "host-peer-probe.cpp"), "-L", str(work), "-lhostiokit",
            "-Wl,-rpath," + str(work), "-o", str(probe),
        ])

        for name in ("unset", "a740", "a830"):
            environment = hostsim_environment()
            if name == "unset":
                environment.pop("HOSTSIM_GPU", None)
            else:
                environment["HOSTSIM_GPU"] = name
            result = checked(
                [str(probe), "exercise", "direct", *CONTRACTS[name].probe_arguments()],
                env=environment, capture_output=True,
            )
            expected_iova = "0x20000000" if name == "a830" else "0x40000000"
            for evidence in (
                "global BoAlloc bytes=655360 align_log2=12 contiguous=0",
                "iova=" + expected_iova,
                "cpu_bytes=655360",
            ):
                if evidence not in result.stdout:
                    raise SystemExit("FAIL: %s host exercise lacks %s" % (name, evidence))
            print("%s: %s" % (name, result.stdout.strip().splitlines()[-2]))

        environment = hostsim_environment(HOSTSIM_GPU="a830x")
        invalid = subprocess.run(
            [str(probe), "info", "direct", *CONTRACTS["a830"].probe_arguments()],
            env=environment, capture_output=True, text=True,
        )
        if invalid.returncode == 0 or "invalid HOSTSIM_GPU=a830x" not in invalid.stderr:
            raise SystemExit("FAIL: invalid HOSTSIM_GPU was not rejected clearly")
        print("invalid: rejected with %s" % next(
            line for line in invalid.stderr.splitlines() if "invalid HOSTSIM_GPU" in line
        ))

    print("PASS: Linux host peer covered unset/a740/A830 ABI facts, the createDevice 640 KiB BO at "
          "A740 0x40000000 and A830 0x20000000, map/submit/close, and invalid rejection")
    print("NOT COVERED: macOS/Xcode build, Metal producer, hardware execution, or target cache compatibility")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
