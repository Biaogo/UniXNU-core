#!/usr/bin/env python3
"""Compile the render server's shader priming on a Mac into a pipeline pack.

The producer drives the current PrimeShaders.h through the native Metal
bridge and a host Turnip build over the software NativeAdreno peer.  It
requires the A830 peer to report the requested GPU, the target iOS Turnip to
be a thin ARM64/iOS dylib, the host and target Turnip builds to share one
compiler-source identity, and the supplied OS build/mounts to be real.  The
result is a structural SHEXP001/M2VCACH1 pack; shader execution and physical
A830 execution remain unverified.

Optional ``--member-file-order``, ``--member-outer-sha256`` and
``--member-sha256`` together select one MTLB member. Omitting all three keeps
``newLibraryWithURL``. A partial set is rejected. File order 0 is an explicit
index, not a default. Explicit selection requires a prime list of exactly one
library; a longer list is refused and not packed, and the 166-entry list is
not filtered or given a default member. These flags do not auto-select a
member and do not by themselves produce a shader pack.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import struct
import subprocess
import sys
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from host_peer import CONTRACTS, HOSTSIM_PAGE_SIZE, hostsim_environment, prepare, verify_build_manifest
from pipeline_library_format import annotate_records
from pipeline_pack import write_pipeline_pack

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "drivers/Adreno740"
KIT = ROOT / "drivers/AdrenoMetal"
BUILD_DIRECTORIES = {
    "ios": ROOT / "build/mesa-tu-ios",
    "ios27": ROOT / "build/mesa-tu-ios27",
}
CACHE_ROOT = "/var/mobile/Library/Caches/com.apple.backboardd/m2v"
PLATFORM_IOS = 2
CPU_TYPE_ARM64 = 0x0100000C
PRIME_TIMEOUT_SECONDS = 3600
LOAD_PREFLIGHT_TIMEOUT_SECONDS = 180
PRIME_RESULT = re.compile(
    r"^RESULT: requested=(\d+) primed=(\d+) refused=(\d+) absent=(\d+)(?:\s|$)",
    re.MULTILINE,
)
LOAD_RESULT = re.compile(
    r"^LOAD_RESULT: requested=(\d+) loaded=(\d+) refused=(\d+) absent=(\d+)"
    r"(?:\s|$)",
    re.MULTILINE,
)
LOAD_LIBRARY_PREFIX = "LOAD_LIBRARY: "


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--target", choices=("ios", "ios27"), default="ios",
                        help="target Turnip build (dodge 21H461 uses ios)")
    result.add_argument("--gpu", choices=("a740", "a830"), default="a830",
                        help="device identity the host peer must report (default a830, never silently a740)")
    result.add_argument("--os-mount", type=Path, required=True,
                        help="macOS mount of the target OS volume")
    result.add_argument("--cryptex-mount", type=Path, required=True,
                        help="mount of the matching System/Cryptexes/OS volume")
    result.add_argument("--os-build", default=None,
                        help="required ProductBuildVersion (default 21H461 for ios)")
    result.add_argument("--prime-list", type=Path, default=ROOT / "build/prime-list.txt")
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--pack", default=r"/(QuartzCore|CoreImage|CoreUI|RenderBox)\.framework/",
                        help="regular expression selecting target libraries whose cache entries enter the pack")
    result.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    result.add_argument("--work", type=Path, default=ROOT / "build/pipeline-cache")
    result.add_argument("--member-file-order", default=None,
                        help="explicit MTLB file-order index; requires both sha256 flags")
    result.add_argument("--member-outer-sha256", default=None,
                        help="64 hex digits of the outer file; not a default member")
    result.add_argument("--member-sha256", default=None,
                        help="64 hex digits of the selected member")
    return result


def member_argv(args: argparse.Namespace) -> list[str]:
    """Forward an explicit member triple, or nothing. Never invent a default member."""
    names = ("member_file_order", "member_outer_sha256", "member_sha256")
    flags = ("--member-file-order", "--member-outer-sha256", "--member-sha256")
    values = [getattr(args, name, None) for name in names]
    if all(value is None for value in values):
        return []
    if any(value is None or str(value) == "" for value in values):
        fail("explicit member selection requires --member-file-order, --member-outer-sha256, and --member-sha256")
    return [token for pair in zip(flags, (str(value) for value in values)) for token in pair]


def explicit_member_list_error(member_argv, path_count: int) -> str | None:
    """Refuse to apply one explicit member to any list that is not exactly one library.

    An empty selection is the historical multi-library path and is not an error here.
    This does not filter, rewrite, or choose a default member.
    """
    if not member_argv:
        return None
    if path_count != 1:
        return (
            "explicit member selection requires a prime list of exactly one library, got %d; "
            "refusing to apply one member to other libraries; no pack" % path_count
        )
    return None


def resolve_inputs(args: argparse.Namespace) -> SimpleNamespace:
    target_build = BUILD_DIRECTORIES[args.target]
    work = args.work.resolve()
    return SimpleNamespace(
        host_turnip=ROOT / "build/mesa-tu/src/freedreno/vulkan/libvulkan_freedreno.dylib",
        target_turnip=target_build / "src/freedreno/vulkan/libvulkan_freedreno.dylib",
        host_profile=ROOT / "build/mesa-tu/builtin-build-profile.json",
        target_profile=target_build / "builtin-build-profile.json",
        peer=ROOT / "build/turnip-darwin-host/libhostiokit.dylib",
        bridge=ROOT / "build/adreno-metal-bridge/host/libadreno_metal_bridge.a",
        prime_list=args.prime_list.resolve(),
        work=work,
        producer=work / "pipeline-cache-host",
        member_argv=member_argv(args),
    )


def runtime_environment(args: argparse.Namespace, inputs: SimpleNamespace) -> dict[str, str]:
    prefixes = ("IR3_", "FD_MESA_", "NIR_", "TU_", "HOSTSIM_", "MESA_", "NMB_", "METAL2VULKAN_")
    env = {name: value for name, value in os.environ.items() if not name.startswith(prefixes)}
    env.update(
        DYLD_INSERT_LIBRARIES=str(inputs.peer),
        MESA_SHADER_CACHE_DISABLE="true",
        MESA_LOG="",
        TU_DEBUG="sysmem,nocb",
        HOSTSIM_GPU=args.gpu,
        HOSTSIM_PAGE_SIZE=HOSTSIM_PAGE_SIZE,
    )
    return env


def fail(message: str) -> None:
    raise SystemExit("pipeline-cache preflight: " + message)


def require_path(path: Path, what: str) -> Path:
    if not path.exists():
        fail("missing %s: %s" % (what, path))
    return path


def output_text(value: object) -> str:
    """Normalize captured output, including TimeoutExpired's bytes with text=True."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except FileNotFoundError:
        return ""


def producer_summary(stdout: str) -> dict[str, int] | None:
    matches = list(PRIME_RESULT.finditer(stdout))
    if not matches:
        return None
    requested, primed, refused, absent = matches[-1].groups()
    return {
        "requested": int(requested),
        "primed": int(primed),
        "refused": int(refused),
        "absent": int(absent),
    }


def load_summary(stdout: str) -> dict[str, int] | None:
    matches = list(LOAD_RESULT.finditer(stdout))
    if not matches:
        return None
    requested, loaded, refused, absent = matches[-1].groups()
    return {
        "requested": int(requested),
        "loaded": int(loaded),
        "load_refused": int(refused),
        "absent": int(absent),
    }


def work_relative(path: Path, work: Path) -> str:
    try:
        return path.relative_to(work).as_posix()
    except ValueError:
        return str(path)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def verify_host_peer(args: argparse.Namespace, inputs: SimpleNamespace) -> None:
    try:
        peer = prepare(inputs.peer.parent)
        verify_build_manifest(peer, inputs.peer)
    except RuntimeError as error:
        fail(str(error))
    inputs.peer_source = peer.source
    inputs.peer_patch = peer.patch
    inputs.peer_helper = peer.helper
    inputs.peer_original = peer.original
    inputs.peer_manifest = peer.manifest
    inputs.work.mkdir(parents=True, exist_ok=True)
    probe = inputs.work / "host-peer-abi-probe"
    command = [
        "xcrun", "clang++", "-std=c++17", "-O1", "-I", DRIVER, "-I", ROOT / "kits/AdrenoPM4",
        ROOT / "scripts/host-peer-probe.cpp", "-o", probe,
    ]
    result = subprocess.run([str(value) for value in command], capture_output=True, text=True)
    if result.returncode:
        fail("host peer ABI probe compilation failed: " + (result.stdout + result.stderr)[-2000:])
    environment = hostsim_environment(HOSTSIM_GPU=args.gpu)
    environment.pop("DYLD_INSERT_LIBRARIES", None)
    result = subprocess.run(
        [str(probe), "info", str(inputs.peer), *CONTRACTS[args.gpu].probe_arguments()],
        env=environment, capture_output=True, text=True, timeout=120,
    )
    log = inputs.work / "host-peer-abi.log"
    log.write_text(result.stdout + result.stderr)
    if result.returncode:
        fail("host peer %s GetDeviceInfo/context check failed; see %s" % (args.gpu, log))


def macho_identity(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    if len(data) < 32:
        fail("truncated Mach-O: " + str(path))
    magic, cpu, _subtype, _filetype, commands, command_bytes, _flags, _reserved = struct.unpack_from("<8I", data)
    if magic != 0xFEEDFACF or cpu != CPU_TYPE_ARM64:
        fail("expected a thin little-endian ARM64 Mach-O: " + str(path))
    if 32 + command_bytes > len(data):
        fail("truncated Mach-O load commands: " + str(path))
    at = 32
    uuid = None
    platform = 0
    for _ in range(commands):
        if at + 8 > 32 + command_bytes:
            fail("invalid Mach-O load command: " + str(path))
        command, size = struct.unpack_from("<II", data, at)
        if size < 8 or at + size > 32 + command_bytes:
            fail("invalid Mach-O load command size: " + str(path))
        if command == 0x1B and size >= 24:
            uuid = data[at + 8:at + 24].hex()
        elif command == 0x32 and size >= 24:
            platform = struct.unpack_from("<I", data, at + 8)[0]
        at += size
    if at != 32 + command_bytes or not uuid:
        fail("Mach-O has no exact LC_UUID: " + str(path))
    return uuid, platform


def profile_identity(path: Path, expected_target: str) -> dict:
    value = json.loads(path.read_text())
    profile = value.get("profile", {})
    if profile.get("target") != expected_target:
        fail("%s was built for target %r, not %r" % (path, profile.get("target"), expected_target))
    if "-Dshader-cache=enabled" not in profile.get("options", []):
        fail("%s does not record -Dshader-cache=enabled" % path)
    return profile


def read_build(mount: Path, expected: str) -> str:
    version = mount / "System/Library/CoreServices/SystemVersion.plist"
    if not version.is_file():
        fail("OS mount lacks " + str(version))
    try:
        value = plistlib.loads(version.read_bytes())
    except Exception as error:
        fail("cannot parse %s: %s" % (version, error))
    build = value.get("ProductBuildVersion")
    if build != expected:
        fail("OS mount is build %r, expected %r" % (build, expected))
    return build


def library_paths(prime_list: Path, os_mount: Path, cryptex_mount: Path, target: str, os_build: str,
                  member_argv=()) -> list[str]:
    paths = [line.strip() for line in prime_list.read_text().splitlines() if line.strip()]
    scope_error = explicit_member_list_error(member_argv, len(paths))
    if scope_error:
        fail(scope_error)
    if not member_argv and target == "ios" and os_build == "21H461" and len(paths) != 166:
        fail("21H461 prime list has %d entries, expected 166" % len(paths))
    if not paths or len(paths) != len(set(paths)):
        fail("prime list is empty or contains duplicate paths")
    cryptex = "/System/Cryptexes/OS"
    for path in paths:
        if not path.startswith("/") or not path.endswith(".metallib") or ".." in path.split("/"):
            fail("invalid prime-list path: " + path)
        local = cryptex_mount / path[len(cryptex):].lstrip("/") if path.startswith(cryptex) else os_mount / path.lstrip("/")
        if not local.is_file():
            fail("prime-list library is absent: %s -> %s" % (path, local))
    return paths


def preflight(args: argparse.Namespace, inputs: SimpleNamespace) -> dict:
    if sys.platform != "darwin":
        fail("requires macOS with Xcode; this host is " + sys.platform)
    if not shutil.which("xcrun"):
        fail("xcrun is required")
    if args.jobs < 1:
        fail("--jobs must be positive")
    if not args.os_build:
        if args.target == "ios":
            args.os_build = "21H461"
        else:
            fail("--os-build is required for target " + args.target)
    report_path = args.output.with_name(args.output.name + ".json")
    if args.output.exists() or report_path.exists():
        fail("refusing to replace an existing output/report; choose a fresh --output")
    os_mount, cryptex_mount = args.os_mount.resolve(), args.cryptex_mount.resolve()
    read_build(os_mount, args.os_build)
    if not (cryptex_mount / "System/Library").is_dir():
        fail("cryptex mount lacks System/Library: " + str(cryptex_mount))
    for path, what in (
        (inputs.host_turnip, "host Turnip"), (inputs.target_turnip, "target Turnip"),
        (inputs.host_profile, "host Turnip profile"), (inputs.target_profile, "target Turnip profile"),
        (inputs.peer, "host NativeAdreno peer"), (inputs.bridge, "host Metal bridge"),
        (inputs.prime_list, "prime list"), (KIT / "PrimeShaders.h", "current PrimeShaders.h"),
    ):
        require_path(path, what)
    target_uuid, platform = macho_identity(inputs.target_turnip)
    if platform != PLATFORM_IOS:
        fail("target Turnip is not an iOS dylib (LC_BUILD_VERSION platform %d)" % platform)
    host_profile = profile_identity(inputs.host_profile, "host")
    target_profile = profile_identity(inputs.target_profile, args.target)
    identity_keys = ("mesa_revision", "source_diff_sha256", "extra_sources")
    if any(host_profile.get(key) != target_profile.get(key) for key in identity_keys):
        fail("host and target Turnip compiler-source identities differ")
    pins = json.loads((ROOT / "scripts/data/adreno-compiler-inputs.json").read_text())
    if target_profile.get("mesa_revision") != pins["mesa"]["commit"]:
        fail("target Turnip profile is not from the pinned Mesa revision")
    build_id = hashlib.sha256(json.dumps({
        "mesa_revision": target_profile["mesa_revision"],
        "source_diff_sha256": target_profile["source_diff_sha256"],
        "extra_sources": target_profile["extra_sources"],
    }, sort_keys=True).encode()).hexdigest()[:40]
    paths = library_paths(inputs.prime_list, os_mount, cryptex_mount, args.target, args.os_build,
                          getattr(inputs, "member_argv", ()))
    try:
        pack_pattern = re.compile(args.pack)
    except re.error as error:
        fail("invalid --pack expression: " + str(error))
    selected = [path for path in paths if pack_pattern.search(path)]
    if not selected:
        fail("--pack selects no libraries from the prime list")
    verify_host_peer(args, inputs)
    return {
        "os_build": args.os_build,
        "target_uuid": target_uuid,
        "turnip_build_id": build_id,
        "paths": paths,
        "selected": selected,
        "os_mount": os_mount,
        "cryptex_mount": cryptex_mount,
    }


def producer_compile_commands(inputs: SimpleNamespace) -> tuple[list[object], list[object]]:
    """Compile the C parser as C, then link it into the Objective-C++ producer.

    clang++ -std=c++17 -Werror rejects a .c input as a deprecated C-as-C++
    compile. The object is what the C++ link receives.
    """
    selection = inputs.work / "producer-member-selection.o"
    c_command = [
        "xcrun", "clang", "-std=c11", "-O1", "-Wall", "-Wextra", "-Werror",
        "-I", ROOT / "scripts",
        "-c", ROOT / "scripts/producer-member-selection.c",
        "-o", selection,
    ]
    cxx_command = [
        "xcrun", "clang++", "-std=c++17", "-fobjc-arc", "-O1", "-Wall", "-Wextra", "-Werror",
        "-Wno-unused-parameter", "-Wno-unused-function", "-Wno-deprecated-declarations",
        "-I", KIT, "-I", DRIVER, "-I", ROOT / "kits/AdrenoPM4",
        "-I", ROOT / "scripts",
        "-I", ROOT / ".cache/mesa-work/include", "-I", ROOT / "build/turnip-smoke",
        KIT / "NativeBridgeMetalFrontend.mm", ROOT / "scripts/pipeline-cache-host.mm",
        selection, inputs.bridge, "-framework", "Foundation", "-framework", "Metal",
        "-framework", "IOSurface", "-framework", "CoreFoundation", "-o", inputs.producer,
    ]
    return c_command, cxx_command


def compile_producer(args: argparse.Namespace, inputs: SimpleNamespace) -> Path:
    inputs.work.mkdir(parents=True, exist_ok=True)
    c_command, command = producer_compile_commands(inputs)
    log_path = inputs.work / "producer-build.log"
    c_result = subprocess.run([str(value) for value in c_command], capture_output=True, text=True)
    log_path.write_text(c_result.stdout + c_result.stderr)
    if c_result.returncode:
        fail("host producer C compilation failed; see %s" % log_path)
    result = subprocess.run([str(value) for value in command], capture_output=True, text=True)
    with log_path.open("a") as log:
        log.write(result.stdout + result.stderr)
    if result.returncode:
        fail("host producer compilation failed; see %s" % log_path)
    return inputs.producer


def stamp_inputs(inputs: SimpleNamespace) -> list[Path]:
    return [
        inputs.producer,
        inputs.host_turnip,
        inputs.target_turnip,
        inputs.peer,
        inputs.bridge,
        inputs.prime_list,
        inputs.host_profile,
        inputs.target_profile,
        Path(__file__).resolve(),
        ROOT / "scripts/pipeline-cache-host.mm",
        ROOT / "scripts/pipeline-cache-format.h",
        ROOT / "scripts/prime-member-policy.h",
        ROOT / "scripts/producer-member-selection.c",
        ROOT / "scripts/producer-member-selection.h",
        KIT / "PrimeShaders.h",
        KIT / "NativeBridgeMetalFrontend.mm",
        KIT / "NativeBridgeMetalFrontend.h",
        KIT / "bridge/NativeMetalObjects.rs",
        KIT / "bridge/NativeMetalObjects.h",
        inputs.peer_original,
        inputs.peer_source,
        inputs.peer_patch,
        inputs.peer_helper,
        inputs.peer_manifest,
        ROOT / "scripts/host-peer-probe.cpp",
    ]


def stamp(producer: Path, inputs: SimpleNamespace, env: dict[str, str], os_build: str) -> str:
    del producer  # inputs.producer is part of the ordered semantic stamp
    files = stamp_inputs(inputs)
    values = [
        str(path) + " " + hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    ]
    for name in (
        "DYLD_INSERT_LIBRARIES", "HOSTSIM_GPU", "HOSTSIM_PAGE_SIZE", "MESA_LOG",
        "MESA_SHADER_CACHE_DISABLE", "TU_DEBUG",
    ):
        values.append(name + "=" + env.get(name, ""))
    values.append("os_build=" + os_build)
    # Explicit member selection is resume identity. An absent or empty
    # member_argv adds no line, so the no-selection stamp stays the historical text.
    member = [str(token) for token in (getattr(inputs, "member_argv", None) or [])]
    if member:
        values.append("member_argv=" + "\t".join(member))
    return "\n".join(values) + "\n"


def run_result(
    index: int,
    path: str,
    status: str,
    inputs: SimpleNamespace,
    *,
    stdout: str = "",
    stderr: str = "",
    prime_log: str = "",
    summary: dict[str, int] | None = None,
    returncode: int | None = None,
    reason: str = "",
    timeout_seconds: int | None = None,
    stamp_id: str = "",
    resumed: bool = False,
    producer_invoked: bool = False,
) -> dict:
    directory = inputs.work / "runs" / ("%03d" % index)
    return {
        "index": index,
        "path": path,
        "status": status,
        "requested": 1,
        "primed": 1 if status == "primed" else 0,
        "refused": 1 if status == "refused" else 0,
        "absent": 1 if status == "absent" else 0,
        "timeout": 1 if status == "timeout" else 0,
        "returncode": returncode,
        "producer_summary": summary,
        "reason": reason,
        "timeout_seconds": timeout_seconds,
        "stamp_id": stamp_id,
        "resumed": resumed,
        "producer_invoked": producer_invoked,
        "log": work_relative(directory / "run.log", inputs.work),
        "prime_log": (
            work_relative(directory / "prime.log", inputs.work)
            if (directory / "prime.log").is_file() or prime_log
            else None
        ),
        "stdout": stdout,
        "stderr": stderr,
        "producer_prime_log": prime_log,
    }


def resumable_result(
    index: int,
    path: str,
    inputs: SimpleNamespace,
    stamp_id: str,
) -> dict | None:
    directory = inputs.work / "runs" / ("%03d" % index)
    result_path = directory / "result.json"
    if not result_path.is_file():
        return None
    try:
        previous = json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    expected = {"requested": 1, "primed": 1, "refused": 0, "absent": 0}
    done = read_text(directory / "prime-done")
    if not (
        isinstance(previous, dict)
        and previous.get("status") == "primed"
        and previous.get("index") == index
        and previous.get("path") == path
        and previous.get("returncode") == 0
        and previous.get("producer_summary") == expected
        and previous.get("stamp_id") == stamp_id
        and any(line.startswith(path + "\t") for line in done.splitlines())
        and (directory / "run.log").is_file()
    ):
        return None
    previous["resumed"] = True
    return previous


def prime_one(
    index: int,
    inputs: SimpleNamespace,
    identity: dict,
    env: dict[str, str],
    stamp_id: str,
) -> dict:
    path = identity["paths"][index]
    directory = inputs.work / "runs" / ("%03d" % index)
    resumed = resumable_result(index, path, inputs, stamp_id)
    if resumed is not None:
        return resumed
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    log = directory / "run.log"
    command = [
        inputs.producer, inputs.host_turnip, directory, inputs.prime_list, identity["os_mount"],
        identity["cryptex_mount"], inputs.target_turnip, env["HOSTSIM_GPU"], index, 1,
        *getattr(inputs, "member_argv", []),
    ]
    try:
        run = subprocess.run(
            [str(value) for value in command],
            env=env,
            capture_output=True,
            text=True,
            timeout=PRIME_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        stdout = output_text(error.stdout)
        stderr = output_text(error.stderr)
        prime_log = read_text(directory / "prime.log")
        log.write_text(stdout + stderr)
        result = run_result(
            index, path, "timeout", inputs, stdout=stdout, stderr=stderr, prime_log=prime_log,
            reason="producer timed out after %d seconds" % PRIME_TIMEOUT_SECONDS,
            timeout_seconds=PRIME_TIMEOUT_SECONDS, stamp_id=stamp_id,
            producer_invoked=True,
        )
        write_json(directory / "result.json", result)
        return result
    except Exception as error:
        reason = "%s: %s" % (type(error).__name__, error)
        prime_log = read_text(directory / "prime.log")
        log.write_text(reason + "\n")
        result = run_result(
            index, path, "worker_error", inputs, stderr=reason + "\n", prime_log=prime_log,
            reason=reason, stamp_id=stamp_id, producer_invoked=True,
        )
        write_json(directory / "result.json", result)
        return result

    stdout, stderr = output_text(run.stdout), output_text(run.stderr)
    log.write_text(stdout + stderr)
    summary = producer_summary(stdout)
    prime_log = read_text(directory / "prime.log")
    done = read_text(directory / "prime-done")
    done_ok = any(line.startswith(path + "\t") for line in done.splitlines())
    status = "protocol_failure"
    reason = ""
    if summary is None:
        if run.returncode != 0:
            status = "exit_failure"
            reason = (
                stderr.strip()
                or prime_log.strip()
                or "producer exited with status %d before RESULT" % run.returncode
            )
        else:
            reason = "producer emitted no RESULT summary"
    elif summary["requested"] != 1:
        reason = "producer requested=%d for one-library invocation" % summary["requested"]
    elif summary["refused"]:
        status = "refused"
        reason = stderr.strip() or prime_log.strip() or "producer reported a refused library"
    elif summary["absent"]:
        status = "absent"
        reason = stderr.strip() or prime_log.strip() or "producer reported an absent library"
    elif run.returncode != 0:
        status = "exit_failure"
        reason = (
            stderr.strip()
            or prime_log.strip()
            or "producer exited with status %d" % run.returncode
        )
    elif summary["primed"] == 1 and done_ok:
        status = "primed"
    else:
        reason = (
            "producer summary=%r but prime-done entry=%s and returncode=%r"
            % (summary, done_ok, run.returncode)
        )
    result = run_result(
        index, path, status, inputs, stdout=stdout, stderr=stderr,
        prime_log=prime_log, summary=summary, returncode=run.returncode,
        reason=reason, stamp_id=stamp_id, producer_invoked=True,
    )
    write_json(directory / "result.json", result)
    return result


def worker_failure(
    index: int,
    inputs: SimpleNamespace,
    identity: dict,
    error: BaseException,
    stamp_id: str,
) -> dict:
    path = identity["paths"][index]
    reason = "%s: %s" % (type(error).__name__, error)
    directory = inputs.work / "runs" / ("%03d" % index)
    directory.mkdir(parents=True, exist_ok=True)
    result = run_result(
        index, path, "worker_error", inputs, stderr=reason + "\n",
        reason=reason, stamp_id=stamp_id, producer_invoked=True,
    )
    write_json(directory / "result.json", result)
    return result


def blank_load_record(index: int, path: str, reason: str) -> dict:
    return {
        "index": index,
        "path": path,
        "status": "not_run",
        "reason": reason,
        "header64_hex": None,
        "header_bytes": 0,
        "error_domain": None,
        "error_code": None,
        "error_description": None,
        "pipelines_compiled": False,
        "shader_pack_produced": False,
    }


def parse_load_records(stdout: str, identity: dict, reason: str) -> list[dict]:
    records = [
        blank_load_record(index, path, reason)
        for index, path in enumerate(identity["paths"])
    ]
    for line in stdout.splitlines():
        if not line.startswith(LOAD_LIBRARY_PREFIX):
            continue
        try:
            value = json.loads(line[len(LOAD_LIBRARY_PREFIX):])
            index = int(value["index"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if index < 0 or index >= len(records):
            continue
        record = dict(records[index])
        record.update(value)
        record["pipelines_compiled"] = False
        record["shader_pack_produced"] = False
        if record.get("status") == "load_refused":
            record["reason"] = (
                "NSError domain=%s code=%s description=%s"
                % (
                    record.get("error_domain"),
                    record.get("error_code"),
                    record.get("error_description"),
                )
            )
        elif record.get("status") == "absent":
            record["reason"] = record["path"] + " is absent from the image mount"
        else:
            record["reason"] = ""
        records[index] = record
    return records


def save_load_preflight(
    inputs: SimpleNamespace,
    identity: dict,
    records: list[dict],
    *,
    status: str,
    stdout: str,
    stderr: str,
    returncode: int | None,
    summary: dict[str, int] | None,
    passed: bool,
    reason: str,
    timeout_seconds: int | None = None,
) -> dict:
    log_path = inputs.work / "load-preflight.log"
    json_path = inputs.work / "load-preflight.json"
    log_path.write_text(stdout + stderr)
    format_coverage = annotate_records(
        records,
        os_mount=identity["os_mount"],
        cryptex_mount=identity["cryptex_mount"],
    )
    statuses = Counter(str(record.get("status", "unknown")) for record in records)
    value = {
        "status": status,
        "passed": passed,
        "mode": "validate-only",
        "reason": reason,
        "producer_execution_attempted": True,
        "producer_returncode": returncode,
        "timeout_seconds": timeout_seconds,
        "requested": len(records),
        "loaded": statuses["loaded"],
        "load_refused": statuses["load_refused"],
        "absent": statuses["absent"],
        "not_run": statuses["not_run"],
        "producer_summary": summary,
        "pipelines_compiled": False,
        "shader_pack_produced": False,
        "log": work_relative(log_path, inputs.work),
        "json": work_relative(json_path, inputs.work),
        "format_coverage": format_coverage,
        "libraries": records,
    }
    write_json(json_path, value)
    inputs.load_preflight = value
    return value


def run_load_preflight(
    inputs: SimpleNamespace, identity: dict, env: dict[str, str]
) -> dict:
    command = [
        inputs.producer,
        inputs.host_turnip,
        inputs.work,
        inputs.prime_list,
        identity["os_mount"],
        identity["cryptex_mount"],
        inputs.target_turnip,
        env["HOSTSIM_GPU"],
        0,
        len(identity["paths"]),
        "--validate-loads",
        *getattr(inputs, "member_argv", []),
    ]
    try:
        run = subprocess.run(
            [str(value) for value in command],
            env=env,
            capture_output=True,
            text=True,
            timeout=LOAD_PREFLIGHT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        reason = "load preflight timed out after %d seconds" % LOAD_PREFLIGHT_TIMEOUT_SECONDS
        stdout = output_text(error.stdout)
        stderr = output_text(error.stderr)
        records = parse_load_records(stdout, identity, reason)
        return save_load_preflight(
            inputs,
            identity,
            records,
            status="timeout",
            stdout=stdout,
            stderr=stderr,
            returncode=None,
            summary=load_summary(stdout),
            passed=False,
            reason=reason,
            timeout_seconds=LOAD_PREFLIGHT_TIMEOUT_SECONDS,
        )
    except Exception as error:
        reason = "%s: %s" % (type(error).__name__, error)
        records = parse_load_records("", identity, reason)
        return save_load_preflight(
            inputs,
            identity,
            records,
            status="worker_error",
            stdout="",
            stderr=reason + "\n",
            returncode=None,
            summary=None,
            passed=False,
            reason=reason,
        )

    stdout, stderr = output_text(run.stdout), output_text(run.stderr)
    summary = load_summary(stdout)
    records = parse_load_records(stdout, identity, "load preflight produced no result for this library")
    statuses = Counter(str(record.get("status", "unknown")) for record in records)
    expected = {
        "requested": len(records),
        "loaded": len(records),
        "load_refused": 0,
        "absent": 0,
    }
    passed = (
        run.returncode == 0
        and summary == expected
        and statuses["loaded"] == len(records)
        and not statuses["load_refused"]
        and not statuses["absent"]
        and not statuses["not_run"]
    )
    if passed:
        status = "passed"
        reason = ""
    else:
        status = "failed"
        reason = (
            stderr.strip()
            or "load preflight rejected %d libraries and did not load %d"
            % (statuses["load_refused"], statuses["not_run"])
        )
    return save_load_preflight(
        inputs,
        identity,
        records,
        status=status,
        stdout=stdout,
        stderr=stderr,
        returncode=run.returncode,
        summary=summary,
        passed=passed,
        reason=reason,
    )


def run_libraries(args: argparse.Namespace, inputs: SimpleNamespace, identity: dict) -> list[dict]:
    producer = compile_producer(args, inputs)
    env = runtime_environment(args, inputs)
    current_stamp = stamp(producer, inputs, env, identity["os_build"])
    stamp_id = hashlib.sha256(current_stamp.encode()).hexdigest()
    inputs.work.mkdir(parents=True, exist_ok=True)
    stamp_path = inputs.work / "inputs.sha256"
    if not stamp_path.is_file() or stamp_path.read_text() != current_stamp:
        runs = inputs.work / "runs"
        if runs.exists():
            shutil.rmtree(runs)
        stamp_path.write_text(current_stamp)
    load_preflight = run_load_preflight(inputs, identity, env)
    if not load_preflight["passed"]:
        reason = (
            "load preflight %s; real priming was not started and no pipeline pack was attempted"
            % load_preflight["status"]
        )
        return complete_records(inputs, identity, None, reason, stamp_id)
    records: list[dict | None] = [None] * len(identity["paths"])
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(prime_one, index, inputs, identity, env, stamp_id): index
            for index in range(len(identity["paths"]))
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                records[index] = future.result()
            except BaseException as error:
                records[index] = worker_failure(index, inputs, identity, error, stamp_id)
    return [record for record in records if record is not None]


def merge_cache(args: argparse.Namespace, inputs: SimpleNamespace, identity: dict) -> list[tuple[Path, str]]:
    merged = inputs.work / "cache"
    if merged.exists():
        shutil.rmtree(merged)
    merged.mkdir(parents=True)
    done: list[str] = []
    selected_pattern = re.compile(args.pack)
    for index, path in enumerate(identity["paths"]):
        directory = inputs.work / "runs" / ("%03d" % index)
        done_file = directory / "prime-done"
        if not done_file.is_file() or not done_file.read_text().strip():
            fail("library %d has no prime-done entry: %s" % (index, path))
        done.append(done_file.read_text())
        if not selected_pattern.search(path):
            continue
        for source in sorted(directory.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(directory)
            if relative.as_posix().startswith("v1/"):
                target = merged / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copyfile(source, target)
    (merged / "prime-done").write_text("".join(done))
    entries = [path for path in merged.rglob("*") if path.is_file()]
    cache_entries = [path for path in entries if path.relative_to(merged).as_posix().startswith("v1/")]
    if not cache_entries:
        fail("the selected libraries produced no v1 cache entries; refusing an empty or synthetic pack")
    return [(path, path.relative_to(merged).as_posix()) for path in entries]


def complete_records(
    inputs: SimpleNamespace,
    identity: dict,
    records: list[dict] | None,
    reason: str,
    stamp_id: str = "",
) -> list[dict]:
    records = list(records or [])
    by_index = {
        record["index"]: record
        for record in records
        if isinstance(record, dict) and isinstance(record.get("index"), int)
    }
    completed: list[dict] = []
    for index, path in enumerate(identity["paths"]):
        if index in by_index:
            completed.append(by_index[index])
            continue
        directory = inputs.work / "runs" / ("%03d" % index)
        directory.mkdir(parents=True, exist_ok=True)
        record = run_result(
            index, path, "not_run", inputs, stderr=reason + "\n", reason=reason,
            stamp_id=stamp_id,
        )
        write_json(directory / "result.json", record)
        completed.append(record)
    return completed


def library_counts(records: list[dict]) -> dict[str, int]:
    statuses = Counter(str(record.get("status", "unknown")) for record in records)
    counts = {
        "requested": len(records),
        "primed": statuses["primed"],
        "refused": statuses["refused"],
        "absent": statuses["absent"],
        "timeout": statuses["timeout"],
        "exit_failure": statuses["exit_failure"],
        "worker_error": statuses["worker_error"],
        "protocol_failure": statuses["protocol_failure"],
        "not_run": statuses["not_run"],
    }
    counts["failed"] = counts["requested"] - counts["primed"]
    return counts


def build_report(
    args: argparse.Namespace,
    inputs: SimpleNamespace,
    identity: dict,
    records: list[dict],
    status: str,
    *,
    pack_summary: object | None = None,
    error: str | None = None,
) -> dict:
    counts = library_counts(records)
    load_preflight = getattr(inputs, "load_preflight", None)
    prime_invocations = sum(
        1 for record in records
        if record.get("producer_invoked") is True and record.get("resumed") is not True
    )
    prime_returncode_zero = sum(
        1 for record in records
        if record.get("producer_invoked") is True
        and record.get("resumed") is not True
        and record.get("returncode") == 0
    )
    prime_returncode_nonzero = sum(
        1 for record in records
        if record.get("producer_invoked") is True
        and record.get("resumed") is not True
        and record.get("returncode") is not None
        and record.get("returncode") != 0
    )
    prime_without_returncode = prime_invocations - prime_returncode_zero - prime_returncode_nonzero
    producer_attempted = prime_invocations > 0 or bool(
        load_preflight and load_preflight.get("producer_execution_attempted")
    )
    report = {
        "status": status,
        "target": args.target,
        "gpu": args.gpu,
        "os_build": identity["os_build"],
        "os_mount": str(identity["os_mount"]),
        "cryptex_mount": str(identity["cryptex_mount"]),
        "prime_list": str(inputs.prime_list),
        "prime_list_entries": len(identity["paths"]),
        "selected_libraries": len(identity["selected"]),
        "turnip_build_id": identity["turnip_build_id"],
        "target_turnip_uuid": identity["target_uuid"],
        "requested": counts["requested"],
        "primed": counts["primed"],
        "refused": counts["refused"],
        "absent": counts["absent"],
        "timeout": counts["timeout"],
        "exit_failure": counts["exit_failure"],
        "worker_error": counts["worker_error"],
        "protocol_failure": counts["protocol_failure"],
        "not_run": counts["not_run"],
        "failed": counts["failed"],
        "load_refused": (
            load_preflight.get("load_refused", 0) if load_preflight else 0
        ),
        "pack_produced": pack_summary is not None,
        "pack": str(args.output.resolve()) if pack_summary is not None else None,
        "producer_execution_attempted": producer_attempted,
        "producer_execution": {
            "load_preflight_attempted": bool(
                load_preflight and load_preflight.get("producer_execution_attempted")
            ),
            "load_preflight_returncode": (
                load_preflight.get("producer_returncode") if load_preflight else None
            ),
            "prime_subprocess_invocations": prime_invocations,
        },
        "host_pipeline_compiler_subprocess": {
            "attempted": prime_invocations > 0,
            "invocations": prime_invocations,
            "returncode_zero": prime_returncode_zero,
            "returncode_nonzero": prime_returncode_nonzero,
            "without_returncode": prime_without_returncode,
        },
        "load_preflight": (
            {
                key: load_preflight.get(key)
                for key in (
                    "status", "passed", "requested", "loaded", "load_refused",
                    "absent", "not_run", "producer_returncode", "timeout_seconds",
                    "pipelines_compiled", "shader_pack_produced", "format_coverage",
                    "log", "json",
                )
            }
            if load_preflight
            else None
        ),
        "physical_gpu_execution": False,
        "physical_a830_execution": False,
        "native_apple_gpu_execution": False,
        "target_device_cache_hit": False,
        "target_device_cache_hit_verified": False,
        "validation_boundary": (
            "This report covers only host Metal bridge producer execution; it does not prove "
            "physical GPU execution, native Apple GPU execution, target-device cache "
            "import/hits, or hardware compatibility."
        ),
        "libraries": records,
        "first_producer_result": records[0].get("stdout", "") if records else "",
    }
    if pack_summary is not None:
        report.update(
            pack_stream_bytes=pack_summary.stream_bytes,
            pack_compressed_bytes=pack_summary.packed_bytes,
            pack_files=len(pack_summary.files),
            cache_root=pack_summary.root,
        )
    if error is not None:
        report["error"] = error
    return report


def main() -> int:
    args = parser().parse_args()
    inputs = resolve_inputs(args)
    identity = preflight(args, inputs)
    scope_error = explicit_member_list_error(getattr(inputs, "member_argv", ()), len(identity["paths"]))
    if scope_error:
        fail(scope_error)
    report_path = args.output.with_name(args.output.name + ".json")
    records: list[dict] | None = None
    try:
        records = run_libraries(args, inputs, identity)
        failures = [record for record in records if record.get("status") != "primed"]
        if failures:
            counts = library_counts(records)
            load_preflight = getattr(inputs, "load_preflight", None)
            load_failed = bool(load_preflight and not load_preflight.get("passed"))
            if load_failed:
                error = (
                    "load preflight %s with loaded=%d load_refused=%d absent=%d "
                    "not_run=%d; real priming was not started and no pipeline pack "
                    "was attempted"
                    % (
                        load_preflight["status"],
                        load_preflight["loaded"],
                        load_preflight["load_refused"],
                        load_preflight["absent"],
                        load_preflight["not_run"],
                    )
                )
            else:
                error = "%d libraries did not prime; no pipeline pack was written" % counts["failed"]
            report = build_report(
                args, inputs, identity, records, "host-cache-pack-failed",
                error=error,
            )
            write_json(report_path, report)
            if load_failed:
                print(
                    "FAIL: load_preflight=%s requested=%d loaded=%d load_refused=%d "
                    "absent=%d not_run=%d; real priming was not started; no shader pack "
                    "was produced; this round is expected to FAIL until the reported "
                    "load refusals are resolved; report=%s"
                    % (
                        load_preflight["status"], load_preflight["requested"],
                        load_preflight["loaded"], load_preflight["load_refused"],
                        load_preflight["absent"], load_preflight["not_run"], report_path,
                    ),
                    file=sys.stderr,
                )
                return 1
            print(
                "FAIL: requested=%d primed=%d refused=%d absent=%d timeout=%d "
                "exit_failure=%d not_run=%d failed=%d; no pack produced; report=%s"
                % (
                    counts["requested"], counts["primed"], counts["refused"],
                    counts["absent"], counts["timeout"], counts["exit_failure"],
                    counts["not_run"], counts["failed"], report_path,
                ),
                file=sys.stderr,
            )
            return 1

        entries = merge_cache(args, inputs, identity)
        summary = write_pipeline_pack(args.output, CACHE_ROOT, entries)
        report = build_report(
            args, inputs, identity, records, "host-cache-pack-produced",
            pack_summary=summary,
        )
        write_json(report_path, report)
        print(
            "PASS: requested=%d primed=%d refused=0 absent=0 through %s/%s; "
            "selected %d; %d cache files, %d stream bytes -> %s"
            % (
                len(identity["paths"]), len(records), args.target, args.gpu,
                len(identity["selected"]), len(summary.files),
                summary.stream_bytes, args.output,
            )
        )
        print("REPORT: " + str(report_path))
        return 0
    except (Exception, SystemExit) as error:
        reason = "%s: %s" % (type(error).__name__, error)
        records = complete_records(inputs, identity, records, reason)
        counts = library_counts(records)
        report = build_report(
            args, inputs, identity, records, "host-cache-pack-failed", error=reason
        )
        write_json(report_path, report)
        print(
            "FAIL: requested=%d primed=%d refused=%d absent=%d timeout=%d "
            "exit_failure=%d failed=%d; no pack produced; report=%s; %s"
            % (
                counts["requested"], counts["primed"], counts["refused"],
                counts["absent"], counts["timeout"], counts["exit_failure"],
                counts["failed"], report_path, reason,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
