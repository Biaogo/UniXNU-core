#!/usr/bin/env python3
"""Resolve the reviewed Turnip watchdog source for host builds."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OLD_HEADER = "4d17e263f8e4aa42adc95586efcfa61fb74a0af3"
NEW_HEADER = "d7d01d0e0fb70e63a8d18bc07aca77b9647f579a"
POLICY_HEADER = "051ae763811b07fc89ff8635e57dba98f46bc680"


def checked(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, **kwargs)


def prepare_watchdog_include(work: Path) -> Path:
    """Use the prepared component when new, or stage its exact public-base patch chain."""
    manifest_path = ROOT / "scripts/data/dodge-addon-components.json"
    manifest = json.loads(manifest_path.read_text())
    component = next(item for item in manifest["components"] if item["name"] == "AdrenoMetal")
    component_repo = ROOT / component["path"]
    base_commit = component["base_commit"]
    if checked(
        ["git", "-C", str(component_repo), "rev-parse", base_commit + "^{tree}"],
        capture_output=True,
    ).stdout.strip() != component["base_tree"]:
        raise SystemExit("FAIL: AdrenoMetal public base tree does not match the manifest")

    production_header = component_repo / "NativeTurnipSmoke.h"
    actual_header = checked(
        ["git", "-C", str(component_repo), "hash-object", str(production_header)],
        capture_output=True,
    ).stdout.strip()
    if actual_header not in (OLD_HEADER, NEW_HEADER):
        raise SystemExit("FAIL: AdrenoMetal NativeTurnipSmoke.h has an unknown identity: " + actual_header)
    if actual_header == NEW_HEADER:
        production_policy = component_repo / "NativeTurnipWatchdog.h"
        actual_policy = checked(
            ["git", "-C", str(component_repo), "hash-object", str(production_policy)],
            capture_output=True,
        ).stdout.strip()
        if actual_policy != POLICY_HEADER:
            raise SystemExit("FAIL: prepared NativeTurnipWatchdog.h has an unknown identity: " + actual_policy)
        print("watchdog source: prepared component, new header identity")
        return component_repo

    work.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix="adreno-metal-watchdog-", dir=work))
    archive = subprocess.run(
        ["git", "-C", str(component_repo), "archive", base_commit],
        check=True,
        capture_output=True,
        timeout=120,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(staged, filter="data")
    checked(["git", "-C", str(staged), "init", "-q"])
    for patch in component["patches"]:
        patch_path = ROOT / patch["path"]
        if hashlib.sha256(patch_path.read_bytes()).hexdigest() != patch["sha256"]:
            raise SystemExit("FAIL: AdrenoMetal patch identity changed: " + patch["path"])
        checked(
            [
                "git", "-C", str(staged), "apply", "--binary", "--whitespace=nowarn",
                str(patch_path),
            ]
        )
    checked(["git", "-C", str(staged), "add", "--all", "--", "."])
    patched_tree = checked(
        ["git", "-C", str(staged), "write-tree"], capture_output=True
    ).stdout.strip()
    if patched_tree != component["expected_patched_tree"]:
        raise SystemExit(
            "FAIL: AdrenoMetal staged patches produce %s, expected %s"
            % (patched_tree, component["expected_patched_tree"])
        )
    staged_header = checked(
        ["git", "-C", str(staged), "hash-object", "NativeTurnipSmoke.h"],
        capture_output=True,
    ).stdout.strip()
    staged_policy = checked(
        ["git", "-C", str(staged), "hash-object", "NativeTurnipWatchdog.h"],
        capture_output=True,
    ).stdout.strip()
    if staged_header != NEW_HEADER or staged_policy != POLICY_HEADER:
        raise SystemExit("FAIL: AdrenoMetal staged source identities do not match the reviewed patch chain")
    print(
        "watchdog source: old local header %s staged through the exact new tree %s"
        % (OLD_HEADER[:12], patched_tree[:12])
    )
    return staged
