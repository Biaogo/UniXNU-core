#!/usr/bin/env python3
"""Host Rust triple selection. This does not build or run the Metal bridge."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "scripts/build-adreno-metal-bridge.py"
CI = ROOT / "scripts/ci-dodge-pipeline-cache.py"
WORKFLOW = ROOT / ".github/workflows/addon.yml"
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise SystemExit("FAIL: " + message)


def probe(system: str, machine: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable, str(BRIDGE), "--target", "host", "--print-host-triple",
            "--host-system", system, "--host-machine", machine,
        ],
        capture_output=True, text=True,
    )


def main() -> int:
    arm = probe("Darwin", "arm64")
    check(arm.returncode == 0 and arm.stdout.strip() == "aarch64-apple-darwin", "arm64 Darwin host triple")
    aarch = probe("Darwin", "aarch64")
    check(aarch.returncode == 0 and aarch.stdout.strip() == "aarch64-apple-darwin", "aarch64 Darwin host triple")
    intel = probe("Darwin", "x86_64")
    check(intel.returncode == 0 and intel.stdout.strip() == "x86_64-apple-darwin", "x86_64 Darwin host triple")
    linux = probe("Linux", "x86_64")
    check(linux.returncode != 0, "Linux must not select a host triple")
    check("aarch64-apple-darwin" not in linux.stdout, "Linux must not print aarch64-apple-darwin")
    check("refusing to select aarch64-apple-darwin" in (linux.stderr + linux.stdout), "Linux refusal text")
    other = probe("Darwin", "i386")
    check(other.returncode != 0 and "unsupported Darwin host architecture" in (other.stderr + other.stdout), "unknown Darwin arch")
    ios = subprocess.run(
        [sys.executable, str(BRIDGE), "--target", "ios", "--print-host-triple"],
        capture_output=True, text=True,
    )
    check(ios.returncode != 0, "ios target must not print a host triple")
    text = BRIDGE.read_text()
    check("aarch64-apple-ios" in text and "arm64e-apple-ios" in text, "iOS triples remain")
    check("'host': 'aarch64-apple-darwin'" not in text, "host triple is no longer a fixed aarch64 map entry")
    check("/usr/local/opt/llvm/bin/llvm-as" in text and "/opt/homebrew/opt/llvm/bin/llvm-as" in text, "both Homebrew LLVM prefixes")
    workflow = WORKFLOW.read_text()
    check('test "$(uname -m)" = arm64' in workflow, "CI workflow still requires an arm64 runner")
    check("aarch64-apple-darwin" in workflow, "CI workflow still installs the arm64 Darwin Rust target")
    spec = importlib.util.spec_from_file_location("ci_dodge_pipeline_cache", CI)
    if spec is None or spec.loader is None:
        raise SystemExit("FAIL: cannot load CI entry")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.sys.platform = "darwin"
    module.platform.machine = lambda: "x86_64"
    try:
        module.require_ci_host()
    except module.CiError as error:
        check("arm64/aarch64" in str(error) and "x86_64" in str(error), "CI host guard still rejects x86_64: " + str(error))
    else:
        raise SystemExit("FAIL: CI host guard accepted x86_64")
    module.platform.machine = lambda: "arm64"
    module.shutil.which = lambda name: None
    try:
        module.require_ci_host()
    except module.CiError as error:
        check("xcrun" in str(error), "arm64 CI still requires xcrun")
    else:
        raise SystemExit("FAIL: arm64 CI passed without xcrun")
    print("PASS: host bridge triple checks=%d" % CHECKS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
