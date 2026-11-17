#!/usr/bin/env python3
"""Linux-runnable source/format regression for the dodge GPU pipeline chain.

The synthetic M2VCACH1 entry below is explicitly a format fixture.  It is not
a compiler product and does not qualify pipeline compatibility with A830.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
import textwrap
from types import SimpleNamespace
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def require(value: object, message: str) -> None:
    if not value:
        raise SystemExit("FAIL: " + message)


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def checked_git(
    repo: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    require(
        result.returncode == 0,
        "git %s failed in %s: %s"
        % (" ".join(arguments), repo, (result.stdout + result.stderr).strip()),
    )
    return result.stdout.strip()


def parse_addon(data: bytes) -> dict[str, bytes]:
    require(data[:8] == b"NGPUADD2" and len(data) >= 24, "addon magic/header")
    count, _reserved, total = struct.unpack_from("<IIQ", data, 8)
    require(total == len(data), "addon total length")
    at = 24
    files: dict[str, bytes] = {}
    for _ in range(count):
        path_bytes, _mode, size, compressed = struct.unpack_from("<IIQQ", data, at)
        at += 24
        path_end = at + path_bytes
        require(path_end <= len(data), "addon path bounds")
        name = data[at:path_end].rstrip(b"\0").decode()
        at = (path_end + 7) & ~7
        packed_end = at + compressed
        require(packed_end <= len(data), "addon payload bounds")
        payload = zlib.decompress(data[at:packed_end])
        require(len(payload) == size, "addon payload length")
        require(name not in files, "duplicate addon path")
        files[name] = payload
        at = (packed_end + 7) & ~7
    require(at == len(data), "addon trailing bytes")
    return files


pipeline_pack = load_script("pipeline_pack", "pipeline_pack.py")
generator = load_script("make_pipeline_cache", "make-pipeline-cache.py")

pins = json.loads((SCRIPTS / "data/adreno-compiler-inputs.json").read_text())
expected_patches = [
    "patches/mesa/freedreno-tools-nir.patch",
    "patches/mesa/ir3-darwin-build-id.patch",
    "patches/mesa/ir3-portable-asm-roundtrip.patch",
    "patches/mesa/offline-adreno-compiler-probe.patch",
    "patches/mesa/turnip-darwin-kmd.patch",
    "patches/mesa/turnip-graphics-cache-feedback.patch",
    "patches/mesa/turnip-empty-scissor.patch",
    "patches/mesa/turnip-builtin-shader-pack.patch",
    "patches/mesa/turnip-host-import-images.patch",
    "patches/mesa/turnip-render-mode-for-attachment-memory.patch",
    "patches/mesa/turnip-gmem-mutable-swap.patch",
    "patches/mesa/turnip-frame-diagnostics.patch",
    "patches/mesa/turnip-submit-backpressure.patch",
    "patches/mesa/turnip-current-queue-priorities.patch",
]
require(pins["patches"] == expected_patches, "priority patch added without changing the rest; actual " + repr(pins["patches"]))
require(pins["mesa"]["commit"] == "85fda3b36405bcaea8bffc64aa4f2651dc90770d", "Mesa pin changed")
translator = json.loads((SCRIPTS / "data/gpu-metal-vulkan-inputs.json").read_text())
require(translator["translator"]["commit"] == "624bab3918de5b978e1101517abca2b4fa426565", "translator pin changed")
priority = ROOT / "patches/mesa/turnip-current-queue-priorities.patch"
require(
    hashlib.sha256(priority.read_bytes()).hexdigest()
    == "25a64d842b26336b052720ababd5fa00ef4c6507351e5b75f51492b768593588",
    "priority patch differs from the reviewed history",
)
priority_text = priority.read_text()
require("dw_priority_known && dw.open" in priority_text and "lowering releases an old boost" in priority_text,
        "priority patch does not carry live-queue aggregation")

turnip_build = (SCRIPTS / "build-turnip-darwin.py").read_text()
air_build = (SCRIPTS / "build-air-nir-ios.py").read_text()
require("-Dshader-cache=enabled" in turnip_build, "Turnip shader cache is not enabled")
require("-Dshader-cache=disabled" not in turnip_build, "Turnip still disables its shader cache")
require("-Dshader-cache=disabled" in air_build, "AIR tool build unexpectedly changed")

# The host peer links the real preemption core and supplies the C-ABI kernel
# boot-argument KPI. Keep both host entry points wired to those dependencies.
driver = ROOT / "drivers/Adreno740"
require((driver / "NativeAdrenoPreempt.cpp").is_file(), "NativeAdrenoPreempt.cpp is missing")
host_entries = {
    "Linux": (SCRIPTS / "test-dodge-host-peer.py").read_text(),
    "macOS": (SCRIPTS / "check-turnip-darwin-host.py").read_text(),
}
host_peer = load_script("host_peer", "host_peer.py")
expected_host_headers = {
    "IOKit/IOTypes.h",
    "IOKit/IOReturn.h",
    "IOKit/IOBufferMemoryDescriptor.h",
    "IOKit/IOLib.h",
    "kern/task.h",
    "sys/resource_private.h",
}
with tempfile.TemporaryDirectory(prefix="dodge-host-includes-") as temporary:
    includes = host_peer.write_host_includes(Path(temporary))
    generated = {
        path.relative_to(includes).as_posix(): path.read_text()
        for path in includes.rglob("*")
        if path.is_file()
    }
    require(
        set(generated) == expected_host_headers,
        "shared host include helper does not generate the complete kernel header set",
    )
    require(
        all(text == '#include "XnuMemoryFixture.h"\n' for text in generated.values()),
        "shared host header shim does not use the real XnuMemoryFixture",
    )
for name, text in host_entries.items():
    require(
        re.search(r"from host_peer import [^\n]*\bwrite_host_includes\b", text) is not None,
        name + " host entry does not import the shared header helper",
    )
    require(
        re.search(r"includes\s*=\s*write_host_includes\((?:out|work)\)", text) is not None,
        name + " host entry does not call the shared header helper",
    )
    require(
        re.search(r"from host_peer import [^\n]*\bHOST_CLOCK_SHIM\b", text) is not None,
        name + " host entry does not import the shared clock shim",
    )
    require(
        "HOST_CLOCK_SHIM" in text and "host-clock-shim.o" in text,
        name + " host entry does not compile the shared clock shim",
    )
clock_shim = SCRIPTS / "host-clock-shim.cpp"
require(clock_shim.is_file(), "shared host clock shim is missing")
clock_shim_text = clock_shim.read_text()
require(
    'extern "C" uint64_t native_adreno_now_us(void)' in clock_shim_text
    and "steady_clock::now().time_since_epoch()" in clock_shim_text
    and "microseconds" in clock_shim_text,
    "shared host clock shim is not the C-ABI steady-clock implementation",
)
require(
    '"-Wl,-z,defs"' in host_entries["Linux"] and '"-Wl,--no-undefined"' in host_entries["Linux"],
    "Linux host entry does not reject unresolved shared-library symbols",
)
require(
    'DRIVER / "NativeAdrenoPreempt.cpp"' in host_entries["Linux"],
    "Linux host entry omits NativeAdrenoPreempt.cpp",
)
require(
    "'NativeAdrenoPreempt'" in host_entries["macOS"],
    "macOS host entry omits NativeAdrenoPreempt.cpp",
)
host_peer_patch = (ROOT / "patches/adreno740/hostsim-a830-peer.patch").read_text()
require(
    "hostA740ClientVaStart=0x40000000" in host_peer_patch
    and "hostA740ClientVaSize=0x40000000" in host_peer_patch
    and "client.setScope(0,windowStart,windowBytes)" in host_peer_patch,
    "A740 host client window does not avoid the kernel preemption mappings",
)
require(
    "method BoAlloc" in host_peer_patch
    and "method BoMap" in host_peer_patch
    and "iova=0x%llx bytes=%llu" in host_peer_patch,
    "host peer does not report bounded NativeAdreno allocation/map failures",
)
require(
    "for gpu in (None, 'a830')" in host_entries["macOS"]
    and "HOSTSIM_TRACE='1'" in host_entries["macOS"],
    "macOS smoke does not cover unset A740 and A830 with failure tracing",
)
require(host_peer.HOSTSIM_PAGE_SIZE == "4096", "host simulation page size is not the tablet contract")
for host_check in (
    "check-turnip-darwin-host.py",
    "check-turnip-pipeline-cache-host.py",
    "check-quartzcore-turnip-cache-host.py",
    "check-priming-cache-host.py",
    "check-native-metal-frontend-host.py",
    "check-texture-atlas-host.py",
):
    text = (SCRIPTS / host_check).read_text()
    require("hostsim_environment" in text, host_check + " bypasses the shared 4 KiB runtime contract")
kpi_signature = re.compile(
    r'extern\s+"C"\s+(?:'
    r'int\s+PE_parse_boot_argn\s*\(\s*const\s+char\s*\*,\s*void\s*\*,\s*int\s*\)'
    r'|\{(?:[^{}]|\{[^{}]*\})*?int\s+PE_parse_boot_argn\s*\(\s*const\s+char\s*\*,\s*void\s*\*,\s*int\s*\)'
    r')'
)
require(kpi_signature.search(host_entries["Linux"]), "Linux host entry omits the PE_parse_boot_argn KPI")
shim = (driver / "tests/hostsim/host_iokit_shim.cpp").read_text()
require(kpi_signature.search(shim), "host IOKit shim omits the PE_parse_boot_argn KPI")
require(
    "peer.shim_source" in host_entries["macOS"] and "sim / 'host_iokit_shim.cpp'" not in host_entries["macOS"]
    and "service, shim_obj, kpi_obj" in host_entries["macOS"],
    "macOS host entry does not compile the prepared host IOKit shim",
)
stall_patch = (SCRIPTS / "data/hostsim-iokit-stall.patch").read_text()
require(
    "hostsimStall(header[1],\"tunnel\")" in stall_patch
    and "hostsimStall(selector,\"direct\")" in stall_patch
    and "clock_gettime(CLOCK_MONOTONIC" in stall_patch
    and "claimed.exchange" in stall_patch
    and "sleep(seconds)" in stall_patch,
    "host IOKit stall patch does not share one deadline-bounded injection across both owned transports",
)
require(
    "peer.shim_source" in host_entries["macOS"]
    and "HOSTSIM_STALL='17'" in host_entries["macOS"]
    and "requested_ms=17000" in host_entries["macOS"],
    "macOS watchdog control does not run and verify the prepared 17 s stall",
)
require(
    "__attribute__((weak)) int proc_selfpid" in host_entries["macOS"],
    "macOS host entry changed the weak proc_selfpid fallback",
)

b3a8 = ROOT / "patches/adreno-metal/0001-scanout-b3a8-10bit.patch"
require(
    hashlib.sha256(b3a8.read_bytes()).hexdigest()
    == "4c5d06171e0e106e215e2d35835ff77a97d5bf6052d5104e3a895409d0f5ba7d",
    "scanout_b3a8 patch changed",
)
require("scanout_b3a8" in b3a8.read_text(), "scanout_b3a8 missing")
watchdog_patch = ROOT / "patches/adreno-metal/0002-turnip-watchdog-monotonic.patch"
watchdog_patch_text = watchdog_patch.read_text()
require(
    "runWatchedDetailed" in watchdog_patch_text
    and "NativeTurnipWatchdog.h" in watchdog_patch_text
    and "using Clock = std::chrono::steady_clock" in watchdog_patch_text
    and "if (done() || !due(started, now)) return false" in watchdog_patch_text
    and "pollReport(std::chrono::duration_cast" in watchdog_patch_text,
    "Turnip watchdog patch does not route both production and host timing through the real deadline policy",
)
require(
    "test-turnip-watchdog-deadline.cpp" in host_entries["Linux"]
    and "prepare_watchdog_include(work)" in host_entries["Linux"],
    "Linux host entry does not compile the shared watchdog deadline regression",
)
require(
    "prepare_watchdog_include" in host_entries["macOS"]
    and "'-I', watchdog_include" in host_entries["macOS"],
    "macOS host entry does not compile the reviewed watchdog source",
)
plugin_smoke_source = (SCRIPTS / "check-turnip-watchdog-host.cpp").read_text()
require(
    "runWatchedDetailed" in plugin_smoke_source
    and '"watchdog poll: elapsed_ms=' in plugin_smoke_source
    and '"watchdog process: wall_ms=' in plugin_smoke_source,
    "host plugin smoke does not preserve real poll and process wall-clock diagnostics",
)
require(
    "plugin_watchdog.stdout.log" in host_entries["macOS"]
    and "plugin_watchdog.stderr.log" in host_entries["macOS"],
    "macOS watchdog failure path does not preserve raw stdout and stderr",
)
prime = (SCRIPTS / "data/prime-list.txt").read_text().splitlines()
require(len(prime) == 166 and all(path.endswith(".metallib") for path in prime), "prime list is not the expected 166 entries")

component_manifest_path = SCRIPTS / "data/dodge-addon-components.json"
component_manifest = json.loads(component_manifest_path.read_text())
require(component_manifest.get("format") == 1, "addon component manifest format changed")
component_specs = {
    component["name"]: component for component in component_manifest["components"]
}
require(
    set(component_specs) == {"AdrenoMetal", "GopFramebuffer", "QcomGeniSpi"},
    "addon component manifest does not describe the three pinned components",
)
for name, component in component_specs.items():
    component_repo = ROOT / component["path"]
    base_commit = component["base_commit"]
    base_tree = component["base_tree"]
    require(
        checked_git(component_repo, "rev-parse", base_commit + "^{tree}") == base_tree,
        name + " public base commit does not resolve to the recorded base tree",
    )
    local_head = checked_git(component_repo, "rev-parse", "HEAD")
    gitlink_line = checked_git(ROOT, "ls-files", "-s", "--", component["path"])
    fields = gitlink_line.split()
    require(
        len(fields) == 4 and fields[0] == "160000",
        name + " is not recorded as a gitlink",
    )
    gitlink = fields[1]
    require(
        gitlink in (base_commit, local_head),
        name + " gitlink is neither the public base nor the approved local patched commit",
    )
    if gitlink == base_commit:
        require(
            checked_git(component_repo, "rev-parse", gitlink + "^{tree}") == base_tree,
            name + " base gitlink does not resolve to the public base tree",
        )
    else:
        require(gitlink == local_head, name + " gitlink is not the approved local checkout")
    for patch in component["patches"]:
        patch_path = ROOT / patch["path"]
        require(
            hashlib.sha256(patch_path.read_bytes()).hexdigest() == patch["sha256"],
            name + " patch identity changed: " + patch["path"],
        )

workflow = (ROOT / ".github/workflows/addon.yml").read_text()
for needle in (
    "scripts/prepare-dodge-addon-components.py",
    "python3 scripts/prepare-dodge-addon-components.py",
    "GENI_SPI_CS_CLK_DELAY",
    "python3 scripts/build-adreno-metal-plugin.py --target ios --chip a830 --no-replay",
    'test "$(wc -l < build/prime-list.txt)" -eq 166',
    'args=(--prime-list build/prime-list.txt --pipeline-pack "$PIPELINE_PACK")',
    "python3 scripts/ci-dodge-pipeline-cache.py --mode generate",
    "python3 scripts/ci-dodge-pipeline-cache.py --mode extract --drop-archive",
    'files.get("addons/NativeAdrenoMetal.m2vpack") == expected',
    "build/dodge.m2vpack.json",
    "build/dodge-os-input.json",
    "Build host Turnip",
    "Build host Metal bridge",
):
    require(needle in workflow, "workflow missing " + needle)
for inline_apply in (
    "git -C drivers/AdrenoMetal apply",
    "git -C drivers/GopFramebuffer apply",
    "git -C drivers/QcomGeniSpi apply",
):
    require(inline_apply not in workflow, "workflow retains inline component apply: " + inline_apply)
require("pipeline pack: omitted" not in workflow, "workflow still silently omits the pipeline pack")
require("pipeline_pack_url" not in workflow, "removed external-pack input still controls generation")
require("external-pipeline-pack" not in workflow, "removed external-pack path remains in the workflow")
require(
    re.search(
        r"name: Generate the real pinned 21H461 pipeline pack\n"
        r"\s+run: \|\n"
        r"\s+set -euo pipefail\n"
        r"\s+python3 scripts/ci-dodge-pipeline-cache\.py --mode generate",
        workflow,
    )
    is not None,
    "workflow does not unconditionally generate the pinned pack",
)
require(
    "python3 scripts/check-turnip-darwin-host.py" not in workflow,
    "workflow still runs the host smoke outside the mounted-input lifecycle",
)

# The generated-pack validation heredoc remains the workflow stdin fixture.
positions = {
    name: workflow.index(name)
    for name in (
        "Build Turnip for iOS",
        "Prepare the metal2vulkan translator",
        "Prepare pinned addon components from public bases",
        "Stage the 166-entry priming list",
        "Build host Turnip",
        "Build host Metal bridge",
        "Download and verify the pinned official OS input",
        "Generate the real pinned 21H461 pipeline pack",
        "Build the Metal plugin",
        "Pack the addon with the pipeline cache",
    )
}
ordered = list(positions.values())
require(ordered == sorted(ordered), "workflow step order changed")
require("--target ios" in workflow and "--target host" in workflow, "workflow lost ios/host Turnip targets")
require("--target host\n" in workflow and "aarch64-apple-darwin" in workflow, "host bridge is not arm64 Darwin")
require('test "$(uname -m)" = arm64' in workflow, "workflow does not assert an arm64 runner")
require("timeout-minutes: 240" in workflow, "workflow timeout is not 240 minutes")

# Rebuild all three component checkouts from public trees in scratch, then run
# the single preparation entry point. This never treats a local unpublished
# patched HEAD as the checkout source.
with tempfile.TemporaryDirectory(prefix="dodge-component-replay-") as temporary:
    scratch = Path(temporary) / "tree"
    scratch_repos: dict[str, Path] = {}
    for name, component in component_specs.items():
        source_repo = ROOT / component["path"]
        destination = scratch / component["path"]
        destination.mkdir(parents=True)
        archive = subprocess.run(
            ["git", "-C", str(source_repo), "archive", component["base_commit"]],
            capture_output=True,
            timeout=30,
        )
        require(
            archive.returncode == 0,
            name + " public base archive failed: " + archive.stderr.decode(errors="replace"),
        )
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as source:
            source.extractall(destination, filter="data")
        checked_git(destination, "init", "-q")
        checked_git(destination, "add", "--all", "--", ".")
        archived_tree = checked_git(destination, "write-tree")
        require(
            archived_tree == component["base_tree"],
            name + " git archive does not reproduce the recorded base tree",
        )
        commit_env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Dodge source replay",
            GIT_AUTHOR_EMAIL="dodge-source-replay@invalid",
            GIT_COMMITTER_NAME="Dodge source replay",
            GIT_COMMITTER_EMAIL="dodge-source-replay@invalid",
        )
        baseline_commit = checked_git(
            destination,
            "commit-tree",
            archived_tree,
            env=commit_env,
            input_text=name + " public base tree\n",
        )
        checked_git(destination, "symbolic-ref", "HEAD", "refs/heads/main")
        checked_git(destination, "update-ref", "refs/heads/main", baseline_commit)
        require(
            checked_git(destination, "status", "--porcelain=v1", "--untracked-files=all")
            == "",
            name + " scratch baseline is not clean",
        )
        scratch_repos[name] = destination

    prepare_command = [
        sys.executable,
        str(SCRIPTS / "prepare-dodge-addon-components.py"),
        "--manifest",
        str(component_manifest_path),
        "--root",
        str(scratch),
        "--allow-synthetic-baseline",
    ]
    prepared = subprocess.run(
        prepare_command, capture_output=True, text=True, timeout=120
    )
    require(
        prepared.returncode == 0,
        "scratch component replay failed: "
        + (prepared.stdout + prepared.stderr).strip(),
    )
    for name, component in component_specs.items():
        require(
            checked_git(scratch_repos[name], "rev-parse", "HEAD^{tree}")
            == component["base_tree"],
            name + " scratch replay changed the baseline commit",
        )
        require(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(scratch_repos[name]),
                    "diff",
                    "--quiet",
                    "--",
                ],
                timeout=30,
            ).returncode
            == 1,
            name + " scratch replay did not produce worktree changes",
        )
    repeated = subprocess.run(
        prepare_command, capture_output=True, text=True, timeout=120
    )
    require(
        repeated.returncode == 0 and "(prepared)" in repeated.stdout,
        "component preparation is not idempotent after exact tree verification",
    )

    (scratch_repos["AdrenoMetal"] / "untracked.tmp").write_text("unexpected\n")
    untracked = subprocess.run(
        prepare_command, capture_output=True, text=True, timeout=120
    )
    require(
        untracked.returncode != 0 and "untracked files" in untracked.stderr,
        "component preparation accepted an untracked source file",
    )
    (scratch_repos["AdrenoMetal"] / "untracked.tmp").unlink()
    spi_source = scratch_repos["QcomGeniSpi"] / "GeniSpi.c"
    spi_source.write_bytes(spi_source.read_bytes() + b"\n")
    dirty = subprocess.run(
        prepare_command, capture_output=True, text=True, timeout=120
    )
    require(
        dirty.returncode != 0 and "dirty" in dirty.stderr,
        "component preparation swallowed an unexpected dirty source change",
    )


def workflow_run_scripts(text: str) -> list[str]:
    lines = text.splitlines()
    scripts = []
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)run: \|\s*$", line)
        if match is None:
            continue
        parent_indent = len(match.group(1))
        body = []
        for child in lines[index + 1:]:
            if child.strip() and len(child) - len(child.lstrip()) <= parent_indent:
                break
            body.append(child)
        scripts.append(textwrap.dedent("\n".join(body)) + "\n")
    return scripts


run_scripts = workflow_run_scripts(workflow)
require(run_scripts, "workflow has no shell run blocks")
for index, script in enumerate(run_scripts):
    shell_check = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True, timeout=30
    )
    require(shell_check.returncode == 0, "workflow run block %d is not valid bash: %s" % (index, shell_check.stderr))

artifact_blocks: list[str] = []
workflow_lines = workflow.splitlines()
for index, line in enumerate(workflow_lines):
    if "uses: actions/upload-artifact@v4" not in line:
        continue
    body = []
    for child in workflow_lines[index + 1:]:
        if child.strip() and len(child) - len(child.lstrip()) <= 6:
            break
        body.append(child)
    artifact_blocks.append("\n".join(body))
require(len(artifact_blocks) == 3, "workflow does not have exactly pack, addon, and log artifact uploads")
for block in artifact_blocks:
    require(".ipsw" not in block and ".dmg" not in block and ".cache/dodge-ipsw" not in block,
            "artifact upload includes an Apple OS input")
require(
    any("build/turnip-darwin-host/*.log" in block for block in artifact_blocks),
    "host compilation logs are not in an artifact",
)
for pipeline_metadata in (
    "build/pipeline-cache/load-preflight.log",
    "build/pipeline-cache/load-preflight.json",
    "build/pipeline-cache/runs/*/prime.log",
    "build/pipeline-cache/runs/*/prime-current",
    "build/pipeline-cache/runs/*/prime-fn",
    "build/pipeline-cache/runs/*/prime-done",
    "build/pipeline-cache/runs/*/result.json",
    "build/pipeline-cache/inputs.sha256",
):
    require(
        any(pipeline_metadata in block for block in artifact_blocks),
        "pipeline failure metadata is not uploaded: " + pipeline_metadata,
    )

pipeline_host_source = (SCRIPTS / "pipeline-cache-host.mm").read_text()
require(
    "unsigned primed = 0, refused = 0, absent = 0;" in pipeline_host_source
    and 'argc == 11 && strcmp(argv[10], "--validate-loads") == 0' in pipeline_host_source
    and "[device newLibraryWithURL:url error:&error]" in pipeline_host_source
    and '"header64_hex\\":\\"%s\\"' in pipeline_host_source
    and '"LOAD_RESULT: requested=%zu loaded=%zu refused=%zu absent=%zu "' in pipeline_host_source
    and '"pipelines_compiled=0 shader_pack_produced=0\\n"' in pipeline_host_source
    and 'std::fprintf(stderr, "%s\\n", result.c_str());' in pipeline_host_source
    and '"RESULT: requested=%zu primed=%u refused=%u absent=%u' in pipeline_host_source
    and '"FAIL: requested=%zu primed=%u refused=%u absent=%u\\n"' in pipeline_host_source,
    "pipeline producer does not preserve refusal reasons and failure status",
)
require(
    pipeline_host_source.index("if (validateOnly)")
    < pipeline_host_source.index("prime::library("),
    "load validation is not isolated ahead of the shader priming call",
)
generator_source = (SCRIPTS / "make-pipeline-cache.py").read_text()
require(
    "for future in as_completed(futures):" in generator_source
    and "LOAD_PREFLIGHT_TIMEOUT_SECONDS = 180" in generator_source
    and '"--validate-loads"' in generator_source
    and generator_source.index("load_preflight = run_load_preflight(inputs, identity, env)")
    < generator_source.index("pool.submit(prime_one")
    and 'inputs.work / "load-preflight.json"' in generator_source
    and 'if not load_preflight["passed"]:' in generator_source
    and '"load_preflight": (' in generator_source
    and '"producer_execution_attempted": producer_attempted' in generator_source
    and '"host_pipeline_compiler_subprocess": {' in generator_source
    and '"native_mac_execution": False' not in generator_source
    and '"host_compilation":' not in generator_source
    and '"timeout": 1 if status == "timeout" else 0' in generator_source
    and 'status == "primed"' in generator_source
    and "len(identity[\"paths\"])" in generator_source,
    "pipeline generator does not collect all futures and persist classified results",
)

ci_cache = load_script("ci_dodge_pipeline_cache", "ci-dodge-pipeline-cache.py")
require(ci_cache.parser().parse_args([]).mode == "all", "CI cache helper default mode is not generation")
ci_config = ci_cache.load_config(SCRIPTS / "data/dodge-pipeline-cache.json")
require(ci_config.source.bytes == 6835124840, "official IPSW byte pin changed")
require(
    ci_config.source.sha256
    == "c84ad24b6981a859eec499b1802be9ac593acfd3dc0a185a76d06e4d767a1451",
    "official IPSW SHA256 pin changed",
)
ci_cache.validate_download_url(ci_config.source.url, ci_config.source.url)
for trusted_url in (
    ci_config.source.url.replace("updates.cdn-apple.com", "updates.cdn-apple.com:443"),
    ci_config.source.url.replace("updates.cdn-apple.com", "apple.com"),
    ci_config.source.url.replace("updates.cdn-apple.com", "cdn.apple.com"),
):
    ci_cache.validate_download_url(trusted_url, ci_config.source.url)
for rejected_url in (
    ci_config.source.url.replace("https://", "http://", 1),
    ci_config.source.url.replace("updates.cdn-apple.com", "updates.cdn-apple.com:444"),
    ci_config.source.url.replace("https://", "https://ci-user@", 1),
    ci_config.source.url.replace("updates.cdn-apple.com", "updates.cdn-apple.com.evil.test"),
    ci_config.source.url.replace("updates.cdn-apple.com", "apple.com.evil.test"),
):
    try:
        ci_cache.validate_download_url(rejected_url, ci_config.source.url)
    except ci_cache.CiError:
        pass
    else:
        raise SystemExit("FAIL: download URL policy accepted " + rejected_url)
require(
    ci_config.images["os"].filename == "044-76040-038.dmg"
    and ci_config.images["os"].bytes == 5157406069
    and ci_config.images["os"].kind == "udif",
    "OS image identity changed",
)
require(
    ci_config.images["cryptex"].filename == "044-76034-038.dmg"
    and ci_config.images["cryptex"].bytes == 3303014400
    and ci_config.images["cryptex"].kind == "raw-apfs",
    "cryptex image identity changed",
)
require(
    ci_config.product_build == "21H461"
    and ci_config.product_version == "17.7.11"
    and ci_config.supported_products == ("iPad7,1", "iPad7,2", "iPad7,3", "iPad7,4")
    and ci_config.device == "iPad7,1"
    and ci_config.device_class == "j120ap"
    and ci_config.restore_behavior == "Erase",
    "BuildManifest device/build selection changed",
)

download_fixture = b"synthetic download identity fixture\n"
download_sink = io.BytesIO()
ci_cache.copy_verified_stream(
    io.BytesIO(download_fixture),
    download_sink,
    len(download_fixture),
    hashlib.sha256(download_fixture).hexdigest(),
    2,
)
require(download_sink.getvalue() == download_fixture, "download stream fixture was not copied exactly")
for bad_stream, bad_bytes, bad_sha in (
    (io.BytesIO(download_fixture + b"x"), len(download_fixture), hashlib.sha256(download_fixture).hexdigest()),
    (io.BytesIO(download_fixture), len(download_fixture), "0" * 64),
):
    try:
        ci_cache.copy_verified_stream(bad_stream, io.BytesIO(), bad_bytes, bad_sha, 2)
    except ci_cache.CiError:
        pass
    else:
        raise SystemExit("FAIL: download identity fixture accepted a length/SHA mismatch")


def fixture_identity(device_class: str) -> dict:
    return {
        "Info": {"DeviceClass": device_class, "RestoreBehavior": "Erase"},
        "Manifest": {
            "OS": {
                "Info": {"Path": "044-76040-038.dmg"},
                "Digest": {"SHA1": bytes.fromhex("11" * 20)},
            },
            "Cryptex1,SystemOS": {
                "Info": {"Path": "044-76034-038.dmg"},
                "Digest": {"SHA1": bytes.fromhex("22" * 20)},
            },
        },
    }


manifest_fixture = {
    "ProductVersion": "17.7.11",
    "ProductBuildVersion": "21H461",
    "SupportedProductTypes": ["iPad7,1", "iPad7,2", "iPad7,3", "iPad7,4"],
    "BuildIdentities": [fixture_identity("j120ap"), fixture_identity("j121ap")],
}
selection = ci_cache.select_build_images(manifest_fixture, ci_config)
require(
    selection.device == "iPad7,1"
    and selection.matching_identities == 1
    and selection.images["os"].path == "044-76040-038.dmg"
    and selection.images["cryptex"].path == "044-76034-038.dmg",
    "synthetic BuildManifest did not select the pinned device/images",
)

with tempfile.TemporaryDirectory(prefix="dodge-zip-guard-") as temporary:
    archive_path = Path(temporary) / "traversal.ipsw"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../BuildManifest.plist", b"not a manifest")
    with zipfile.ZipFile(archive_path, "r") as archive:
        try:
            ci_cache.validate_archive_names(archive)
        except ci_cache.CiError:
            pass
        else:
            raise SystemExit("FAIL: ZIP path traversal entry was accepted")
    safe_archive = Path(temporary) / "safe.ipsw"
    with zipfile.ZipFile(safe_archive, "w") as archive:
        archive.writestr("BuildManifest.plist", b"fixture")
    with zipfile.ZipFile(safe_archive, "r") as archive:
        for bad_name in ("../BuildManifest.plist", "wrong.dmg"):
            try:
                ci_cache.extract_member(
                    archive, bad_name, Path(temporary) / "x", 7, 2
                )
            except ci_cache.CiError:
                pass
            else:
                raise SystemExit("FAIL: unsafe or wrong ZIP extraction target was accepted")

detached: list[str] = []
with tempfile.TemporaryDirectory(prefix="dodge-mount-finally-") as temporary:
    mount_root = Path(temporary) / "mounts"

    def fake_attach(image: Path, mountpoint: Path, kind: str, timeout: int):
        return ci_cache.AttachedImage("", image, mountpoint, "/dev/fixture-" + mountpoint.name)

    def fake_detach(device: str, timeout: int) -> None:
        detached.append(device)

    def fail_after_mounts(mounts) -> None:
        require(set(mounts) == {"os", "cryptex"}, "mount fixture did not expose both inputs")
        raise RuntimeError("synthetic generator failure")

    try:
        ci_cache.with_readonly_mounts(
            [
                ("os", Path("/fixture/os.dmg"), "udif"),
                ("cryptex", Path("/fixture/cryptex.dmg"), "raw-apfs"),
            ],
            mount_root,
            fake_attach,
            fake_detach,
            fail_after_mounts,
            2,
            2,
        )
    except RuntimeError:
        pass
    else:
        raise SystemExit("FAIL: mount cleanup fixture swallowed the generator failure")
require(detached == ["/dev/fixture-cryptex", "/dev/fixture-os"], "mount fixture did not detach own devices in reverse order")
require(not mount_root.exists(), "mount fixture did not remove its own mount root")

with tempfile.TemporaryDirectory(prefix="dodge-attach-finally-") as temporary:
    mountpoint = Path(temporary) / "partial"
    commands: list[list[str]] = []
    real_run_captured = ci_cache.run_captured

    def fake_attach_run(command, timeout_seconds):
        commands.append([str(item) for item in command])
        if command[1] == "attach":
            plist = ci_cache.plistlib.dumps({
                "system-entities": [{
                    "dev-entry": "/dev/fixture-partial",
                    "mount-point": str(mountpoint),
                }]
            })
            return subprocess.CompletedProcess(command, 1, plist.decode(), "synthetic attach failure\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    try:
        ci_cache.run_captured = fake_attach_run
        try:
            ci_cache.attach_image(Path("/fixture/os.dmg"), mountpoint, "udif", 2)
        except ci_cache.CiError:
            pass
        else:
            raise SystemExit("FAIL: synthetic hdiutil attach failure was accepted")
    finally:
        ci_cache.run_captured = real_run_captured
    require(
        commands[0][1] == "attach"
        and commands[1] == ["hdiutil", "detach", "/dev/fixture-partial"],
        "attach failure did not detach only the device from its own plist",
    )

raw_command = ci_cache.attach_command(Path("/fixture/cryptex.dmg"), Path("/fixture/mount"), "raw-apfs")
udif_command = ci_cache.attach_command(Path("/fixture/os.dmg"), Path("/fixture/mount"), "udif")
require(
    raw_command[-3:-1] == ["-imagekey", "diskimage-class=CRawDiskImage"]
    and "-imagekey" not in udif_command
    and "-readonly" in raw_command
    and "-nobrowse" in raw_command
    and "-owners" in raw_command,
    "hdiutil attach command does not select the pinned image type",
)

# The host smoke is no longer a standalone workflow step. Exercise the CI helper
# with fixture mounts to prove that it runs the complete smoke before generation
# and supplies only the mounted 21H461 QuartzCore library.
with tempfile.TemporaryDirectory(prefix="dodge-host-smoke-order-") as temporary:
    root = Path(temporary)
    os_mount = root / "os"
    cryptex_mount = root / "cryptex"
    quartzcore = os_mount / "System/Library/Frameworks/QuartzCore.framework/default.metallib"
    quartzcore.parent.mkdir(parents=True)
    quartzcore.write_bytes(b"mounted target metallib fixture; not macOS QuartzCore")
    cryptex_mount.mkdir()
    mounts = {
        "os": ci_cache.AttachedImage("os", Path("/fixture/os.dmg"), os_mount, "/dev/os"),
        "cryptex": ci_cache.AttachedImage(
            "cryptex", Path("/fixture/cryptex.dmg"), cryptex_mount, "/dev/cryptex"
        ),
    }
    stage_calls = []

    def fake_stage_run(command, timeout_seconds, env=None):
        stage_calls.append(([str(item) for item in command], timeout_seconds, env))
        return subprocess.CompletedProcess(command, 0, "fixture PASS\n", "")

    stage_log = root / "pipeline-stage.log"
    staged = ci_cache.run_host_smoke_and_generator(
        ci_config, mounts, stage_log, run=fake_stage_run
    )
    require(len(stage_calls) == 2, "host-smoke/generator helper did not run exactly two stages")
    require(
        Path(stage_calls[0][0][1]).name == "check-turnip-darwin-host.py"
        and Path(stage_calls[1][0][1]).name == "make-pipeline-cache.py",
        "host smoke did not precede pipeline generation inside the mount callback",
    )
    require(
        stage_calls[0][2].get("NMB_TEST_QUARTZCORE_LIBRARY") == str(quartzcore)
        and stage_calls[0][2].get("NMB_TEST_QUARTZCORE_LIBRARY") != "/System/Library/Frameworks/QuartzCore.framework/default.metallib",
        "host smoke was not given the mounted 21H461 QuartzCore library",
    )
    require(
        stage_calls[1][2] is None,
        "the generator unexpectedly inherited the QuartzCore smoke override",
    )
    require(
        staged["combined_timeout_seconds"] == ci_config.generator["timeout_seconds"]
        and stage_calls[0][1] > 0
        and stage_calls[1][1] > 0,
        "host smoke and generator do not share the configured bounded deadline",
    )
    stage_text = stage_log.read_text()
    require(
        stage_text.index("complete host smoke") < stage_text.index("make-pipeline-cache.py"),
        "combined stage log does not preserve smoke-before-generator order",
    )

arguments = generator.parser().parse_args(
    ["--os-mount", "/fixture/os", "--cryptex-mount", "/fixture/cryptex", "--output", "/fixture/out"]
)
require(arguments.target == "ios" and arguments.gpu == "a830", "generator defaults are not ios/a830")
resolved = generator.resolve_inputs(arguments)
require(resolved.target_turnip == ROOT / "build/mesa-tu-ios/src/freedreno/vulkan/libvulkan_freedreno.dylib",
        "target ios does not select build/mesa-tu-ios")
with tempfile.TemporaryDirectory(prefix="dodge-generator-env-") as temporary:
    arguments.work = Path(temporary)
    previous_gpu = os.environ.get("HOSTSIM_GPU")
    previous_debug = os.environ.get("TU_DEBUG")
    os.environ["HOSTSIM_GPU"] = "a740"
    os.environ["TU_DEBUG"] = "wrong"
    try:
        env = generator.runtime_environment(arguments, resolved)
    finally:
        if previous_gpu is None:
            os.environ.pop("HOSTSIM_GPU", None)
        else:
            os.environ["HOSTSIM_GPU"] = previous_gpu
        if previous_debug is None:
            os.environ.pop("TU_DEBUG", None)
        else:
            os.environ["TU_DEBUG"] = previous_debug
require(
    env["HOSTSIM_GPU"] == "a830"
    and env["TU_DEBUG"] == "sysmem,nocb"
    and env["MESA_SHADER_CACHE_DISABLE"] == "true",
    "generator runtime environment is not A830 dodge with the independent m2v cache",
)

# Exercise the generator's real verify_host_peer control flow with mocked
# preparation, build-manifest verification, Xcode compilation, and probe
# execution. This checks Python wiring only; it does not prove that the macOS
# compiler or ABI probe succeeds.
with tempfile.TemporaryDirectory(prefix="dodge-generator-host-peer-") as temporary:
    work = Path(temporary) / "work"
    peer_parent = Path(temporary) / "peer"
    peer_parent.mkdir()
    peer = SimpleNamespace(
        original=peer_parent / "host_gpu_service.cpp",
        patch=peer_parent / "hostsim-a830-peer.patch",
        helper=peer_parent / "host_peer.py",
        source=peer_parent / "peer-source/tests/hostsim/host_gpu_service.cpp",
        build_dir=peer_parent,
        manifest=peer_parent / "peer-build.json",
    )
    for path in (peer.original, peer.patch, peer.helper, peer.source, peer.manifest):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture identity\n")
    product = peer_parent / "libhostiokit.dylib"
    product.write_bytes(b"mock host peer product")
    inputs = SimpleNamespace(peer=product, work=work)
    arguments = SimpleNamespace(gpu="a830")
    events: list[tuple] = []
    run_calls: list[tuple[list[str], dict[str, str], int | None]] = []

    def fake_prepare(build_dir: Path):
        events.append(("prepare", build_dir))
        return peer

    def fake_verify_manifest(prepared, dylib: Path) -> None:
        events.append(("manifest", prepared, dylib))

    def fake_run(command, env=None, capture_output=False, text=False, timeout=None):
        rendered = [str(item) for item in command]
        run_calls.append((rendered, dict(env or {}), timeout))
        events.append(("run", rendered[0]))
        return subprocess.CompletedProcess(command, 0, "mock probe PASS\n", "")

    real_prepare = generator.prepare
    real_verify_manifest = generator.verify_build_manifest
    real_subprocess = generator.subprocess
    try:
        generator.prepare = fake_prepare
        generator.verify_build_manifest = fake_verify_manifest
        generator.subprocess = SimpleNamespace(run=fake_run)
        generator.verify_host_peer(arguments, inputs)
    finally:
        generator.prepare = real_prepare
        generator.verify_build_manifest = real_verify_manifest
        generator.subprocess = real_subprocess

    require(
        [event[0] for event in events] == ["prepare", "manifest", "run", "run"],
        "verify_host_peer did not prepare, verify the manifest, compile, then probe",
    )
    require(events[0][1] == inputs.peer.parent, "verify_host_peer prepared the wrong peer directory")
    require(
        events[1][1] is peer and events[1][2] == inputs.peer,
        "verify_host_peer did not verify the prepared peer product manifest",
    )
    require(
        inputs.peer_source == peer.source
        and inputs.peer_patch == peer.patch
        and inputs.peer_helper == peer.helper
        and inputs.peer_original == peer.original
        and inputs.peer_manifest == peer.manifest,
        "verify_host_peer did not preserve prepared peer identity on inputs",
    )
    compile_command = run_calls[0][0]
    require(
        compile_command[:2] == ["xcrun", "clang++"]
        and str(SCRIPTS / "host-peer-probe.cpp") in compile_command
        and str(ROOT / "drivers/Adreno740") in compile_command,
        "verify_host_peer did not compile the identity probe with the driver headers",
    )
    probe_command, probe_environment, probe_timeout = run_calls[1]
    expected_probe = [str(work / "host-peer-abi-probe"), "info", str(product)]
    expected_probe.extend(host_peer.CONTRACTS["a830"].probe_arguments())
    require(probe_command == expected_probe, "verify_host_peer probed the wrong A830 product or arguments")
    require(
        probe_environment.get("HOSTSIM_GPU") == "a830"
        and probe_environment.get("HOSTSIM_PAGE_SIZE") == host_peer.HOSTSIM_PAGE_SIZE == "4096"
        and "DYLD_INSERT_LIBRARIES" not in probe_environment
        and probe_timeout == 120,
        "verify_host_peer ABI probe did not receive the shared A830/4 KiB environment",
    )
    require(
        (work / "host-peer-abi.log").read_text() == "mock probe PASS\n",
        "verify_host_peer did not record the ABI probe output",
    )

with tempfile.TemporaryDirectory(prefix="dodge-generator-fail-") as temporary:
    output = Path(temporary) / "must-not-exist.m2vpack"
    run = subprocess.run(
        [
            sys.executable, str(SCRIPTS / "make-pipeline-cache.py"), "--target", "ios", "--gpu", "a830",
            "--os-mount", str(Path(temporary) / "missing-os"),
            "--cryptex-mount", str(Path(temporary) / "missing-cryptex"),
            "--output", str(output),
        ],
        capture_output=True, text=True, timeout=30,
    )
    require(run.returncode != 0 and not output.exists(), "generator accepted missing macOS/OS inputs")
    require("requires macOS" in run.stderr or "missing" in run.stderr, "generator prerequisite failure is unclear")

fixture_root = "/fixture/mobile/Library/Caches/com.apple.backboardd/m2v"
fixture_records = [
    ("v1/FIXTURE-not-a-real-pipeline", b"format-only pipeline cache fixture\n"),
    ("prime-done", b"/fixture/FORMAT-ONLY.metallib\t1\t0\tfixture-identity\n"),
]
fixture_pack = pipeline_pack.encode_pipeline_pack(fixture_root, fixture_records)
workflow_pack_check_match = re.search(
    r'^ {10}python3 - "\$PIPELINE_PACK" build/gpu-addon\.bin <<\'PY\'\n(?P<script>.*?)^ {10}PY$',
    workflow,
    re.MULTILINE | re.DOTALL,
)
require(workflow_pack_check_match is not None, "workflow pipeline pack check heredoc not found")
workflow_pack_check = textwrap.dedent(workflow_pack_check_match.group("script"))
with tempfile.TemporaryDirectory(prefix="dodge-workflow-pack-") as temporary:
    base = Path(temporary)
    fixture_path = base / "fixture.m2vpack"
    fixture_path.write_bytes(fixture_pack)
    plugin = base / "plugin/NativeAdrenoMetal.bundle"
    plugin.mkdir(parents=True)
    (plugin / "NativeAdrenoMetal").write_bytes(b"format fixture executable, not a plugin")
    addon_path = base / "fixture-addon.bin"
    build_fixture = subprocess.run(
        [
            sys.executable, str(SCRIPTS / "make-gpu-addon.py"), "--no-daemon",
            "--pipeline-pack", str(fixture_path),
            "--prime-list", str(SCRIPTS / "data/prime-list.txt"),
            str(addon_path),
        ],
        env=dict(os.environ, ADRENO_PLUGIN_DIR=str(base / "plugin")),
        capture_output=True,
        text=True,
        timeout=30,
    )
    require(build_fixture.returncode == 0 and addon_path.is_file(),
            "workflow pack validation addon fixture failed: " + build_fixture.stderr)
    run = subprocess.run(
        [sys.executable, "-", str(fixture_path), str(addon_path)],
        cwd=ROOT,
        input=workflow_pack_check,
        capture_output=True,
        text=True,
        timeout=30,
    )
    require(run.returncode == 0, "workflow pipeline pack validation failed: " + run.stderr)
    require(
        "addon pipeline pack is byte-identical and the priming list has 166 entries" in run.stdout,
        "workflow pipeline pack validation did not parse the fixture",
    )
summary = pipeline_pack.validate_pipeline_pack(fixture_pack, "format fixture")
require(summary.root == fixture_root and summary.files[-1] == "prime-done", "fixture pack does not parse")
try:
    pipeline_pack.validate_pipeline_pack(fixture_pack[:-1], "truncated fixture")
except ValueError:
    pass
else:
    raise SystemExit("FAIL: truncated fixture pack was accepted")

with tempfile.TemporaryDirectory(prefix="dodge-addon-fixture-") as temporary:
    base = Path(temporary)
    plugin = base / "plugin/NativeAdrenoMetal.bundle"
    plugin.mkdir(parents=True)
    (plugin / "NativeAdrenoMetal").write_bytes(b"format fixture executable, not a plugin")
    pack_path = base / "fixture.m2vpack"
    pack_path.write_bytes(fixture_pack)
    addon = base / "fixture-addon.bin"
    run = subprocess.run(
        [sys.executable, str(SCRIPTS / "make-gpu-addon.py"), "--no-daemon", "--pipeline-pack", str(pack_path), str(addon)],
        env=dict(os.environ, ADRENO_PLUGIN_DIR=str(base / "plugin")),
        capture_output=True, text=True, timeout=30,
    )
    require(run.returncode == 0 and addon.is_file(), "fixture pack -> addon failed: " + run.stderr)
    files = parse_addon(addon.read_bytes())
    require(files.get("addons/NativeAdrenoMetal.m2vpack") == fixture_pack,
            "addon did not carry the fixture pack byte for byte")

host_smoke_dependencies = [
    SCRIPTS / "check-quartzcore-turnip-cache-host.py",
    SCRIPTS / "quartzcore_turnip_cache.py",
    SCRIPTS / "test-quartzcore-cache-loader.py",
    SCRIPTS / "data/quartzcore-cache-host-instrument.patch",
    SCRIPTS / "check-turnip-pipeline-cache-host.py",
    SCRIPTS / "check-priming-cache-host.py",
    SCRIPTS / "gen-turnip-smoke-shaders.py",
    SCRIPTS / "host-peer-probe.cpp",
    SCRIPTS / "pipeline-cache-host.mm",
    ROOT / "drivers/AdrenoMetal/bridge/quartzcore_cache.mm",
    ROOT / "drivers/AdrenoMetal/bridge/graphics_cache_loader.cpp",
    ROOT / "drivers/AdrenoMetal/bridge/NativeMetalObjects.h",
]
missing_host_smoke_dependencies = [str(path.relative_to(ROOT)) for path in host_smoke_dependencies if not path.is_file()]
require(
    not missing_host_smoke_dependencies,
    "host smoke dependency is missing: " + ", ".join(missing_host_smoke_dependencies),
)

quartzcore_checker = (SCRIPTS / "check-quartzcore-turnip-cache-host.py").read_text()
for needle in (
    "EXPECTED_BYTES = 9670832",
    "606f913adf520420f9b4bb666efb782a68c830f082f1e559e17b20d0d13adbc9",
    "NMB_TEST_QUARTZCORE_LIBRARY",
    "graphics_cache_loader.cpp",
    "quartzcore_cache.mm",
    "quartzcore-cache-host-instrument.patch",
    "'-framework', 'Metal'",
    "validate_produce(produced)",
    "validate_consume(consumed)",
    "validate_cold(produced)",
    "empty-driver",
):
    require(needle in quartzcore_checker, "QuartzCore checker missing " + needle)
instrument_patch = (SCRIPTS / "data/quartzcore-cache-host-instrument.patch").read_text()
require(
    "strictBlocked" not in instrument_patch
    and "QC vk phase=" in instrument_patch
    and "VK_STRUCTURE_TYPE_PIPELINE_CREATE_FLAGS_2_CREATE_INFO" in instrument_patch
    and "flags2Present[i]?flags2[i]:copies[i].flags" in instrument_patch
    and "(copies[i].flags|flags2[i])" not in instrument_patch
    and "(effectiveFlags&VK_PIPELINE_CREATE_LIBRARY_BIT_KHR)" in instrument_patch
    and (instrument_patch.index('phase("recipe",index);') <
         instrument_patch.index("archived=nmb_binary_archive_add_render")),
    "QuartzCore instrument can suppress or mislabel a real Vulkan call",
)
loader_test = (SCRIPTS / "test-quartzcore-cache-loader.py").read_text()
require(
    "FAKE_DRIVER" in loader_test
    and "validate_cold" in loader_test
    and "legacy-overridden" in loader_test,
    "QuartzCore loader regression does not exercise the fake-driver boundary",
)
failure_flow_test = (SCRIPTS / "test-pipeline-cache-failures.py").read_text()
require(
    "len(report[\"libraries\"]) == 166" in failure_flow_test
    and "failure[\"merge_calls\"] == 0" in failure_flow_test
    and 'failure["prime_one_calls"] == []' in failure_flow_test
    and '"load_refused"' in failure_flow_test
    and "load_timeout=True" in failure_flow_test
    and "subprocess.TimeoutExpired" in failure_flow_test,
    "pipeline load/prime failure regression lost its stop, persistence, or no-pack controls",
)
failure_flow = subprocess.run(
    [sys.executable, str(SCRIPTS / "test-pipeline-cache-failures.py")],
    capture_output=True,
    text=True,
    timeout=60,
)
require(
    failure_flow.returncode == 0
    and "PASS: 166-library control flow" in failure_flow.stdout,
    "pipeline failure control-flow regression failed: "
    + (failure_flow.stdout + failure_flow.stderr).strip(),
)

print("PASS: source guards, public-base component scratch replay, pinned CI input identity, manifest/ZIP guards, workflow shell/default-generation checks, verify_host_peer Python wiring with mocked compiler/probe, mount cleanup, and labelled SHEXP001 -> NGPUADD2 fixture parsing")
print("NOT COVERED: mocked verify_host_peer compilation/ABI execution does not prove macOS compilation or peer ABI compatibility; Linux host-peer execution (scripts/test-dodge-host-peer.py), real 21H461 mount/load-preflight/generation, target cache identity/hits, native Apple GPU execution, physical GPU execution, and hardware compatibility are also not covered")
