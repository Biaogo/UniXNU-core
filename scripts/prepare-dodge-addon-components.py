#!/usr/bin/env python3
"""Prepare dodge addon components from public bases and verified patches."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "scripts/data/dodge-addon-components.json"
EXPECTED_COMPONENTS = ("AdrenoMetal", "GopFramebuffer", "QcomGeniSpi")


class PrepareError(Exception):
    pass


def git(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    if check and result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise PrepareError(
            "%s: git %s failed%s"
            % (repo, " ".join(args), ": " + detail if detail else "")
        )
    return result


def require_hex(value: object, field: str, length: int = 40) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PrepareError("%s must be a lowercase %d-character object ID" % (field, length))
    return value


def inside(root: Path, path: Path, description: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise PrepareError("%s escapes %s: %s" % (description, root, path))
    return resolved


def load_manifest(path: Path, root: Path) -> list[dict[str, object]]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PrepareError("cannot read component manifest %s: %s" % (path, error))
    if not isinstance(value, dict) or value.get("format") != 1:
        raise PrepareError("component manifest must be a format 1 object")
    raw_components = value.get("components")
    if not isinstance(raw_components, list):
        raise PrepareError("component manifest has no component list")

    components: list[dict[str, object]] = []
    names: list[str] = []
    root = root.resolve()
    for index, raw in enumerate(raw_components):
        if not isinstance(raw, dict):
            raise PrepareError("components[%d] is not an object" % index)
        name = raw.get("name")
        relative = raw.get("path")
        repository = raw.get("repository")
        if not isinstance(name, str) or not name:
            raise PrepareError("components[%d].name is missing" % index)
        if not isinstance(relative, str) or not relative:
            raise PrepareError("%s path is missing" % name)
        if not isinstance(repository, str) or not repository.startswith("https://"):
            raise PrepareError("%s repository must be an HTTPS URL" % name)
        if Path(relative).is_absolute():
            raise PrepareError("%s path must be relative" % name)

        repo = inside(root, root / relative, name + " component")
        base_commit = require_hex(raw.get("base_commit"), name + ".base_commit")
        base_tree = require_hex(raw.get("base_tree"), name + ".base_tree")
        expected_tree = require_hex(
            raw.get("expected_patched_tree"), name + ".expected_patched_tree"
        )
        raw_patches = raw.get("patches")
        if not isinstance(raw_patches, list) or not raw_patches:
            raise PrepareError("%s must list at least one patch" % name)
        patches: list[tuple[Path, str]] = []
        for patch_index, raw_patch in enumerate(raw_patches):
            if not isinstance(raw_patch, dict):
                raise PrepareError(
                    "%s.patches[%d] is not an object" % (name, patch_index)
                )
            relative_patch = raw_patch.get("path")
            expected_sha = require_hex(
                raw_patch.get("sha256"),
                "%s.patches[%d].sha256" % (name, patch_index),
                length=64,
            )
            if not isinstance(relative_patch, str) or not relative_patch:
                raise PrepareError(
                    "%s.patches[%d].path is missing" % (name, patch_index)
                )
            patch = inside(ROOT, ROOT / relative_patch, name + " patch")
            if not patch.is_file():
                raise PrepareError("%s patch is missing: %s" % (name, patch))
            actual_sha = hashlib.sha256(patch.read_bytes()).hexdigest()
            if actual_sha != expected_sha:
                raise PrepareError(
                    "%s patch differs from manifest: %s (expected %s, found %s)"
                    % (name, patch, expected_sha, actual_sha)
                )
            patches.append((patch, actual_sha))
        if not repo.is_dir():
            raise PrepareError("%s checkout is missing: %s" % (name, repo))
        components.append(
            {
                "name": name,
                "path": relative,
                "repository": repository,
                "repo": repo,
                "base_commit": base_commit,
                "base_tree": base_tree,
                "expected_patched_tree": expected_tree,
                "patches": patches,
            }
        )
        names.append(name)

    if tuple(sorted(names)) != tuple(sorted(EXPECTED_COMPONENTS)):
        raise PrepareError(
            "component manifest must contain exactly %s; found %s"
            % (", ".join(EXPECTED_COMPONENTS), ", ".join(names))
        )
    return components


@contextmanager
def temporary_index(repo: Path, start_tree: str) -> Iterator[tuple[dict[str, str], Path]]:
    with tempfile.TemporaryDirectory(prefix="dodge-component-index-") as temporary:
        index = Path(temporary) / "index"
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = str(index)
        git(repo, "read-tree", start_tree, env=env)
        yield env, index


def patched_tree(
    repo: Path,
    start_tree: str,
    patches: list[tuple[Path, str]],
    *,
    reverse: bool = False,
) -> str:
    with temporary_index(repo, start_tree) as (env, _index):
        ordered = list(reversed(patches)) if reverse else patches
        for patch, _sha256 in ordered:
            arguments = [
                "apply",
                "--cached",
                "--binary",
                "--whitespace=nowarn",
            ]
            if reverse:
                arguments.append("--reverse")
            arguments.append(str(patch))
            git(repo, *arguments, env=env)
        return git(repo, "write-tree", env=env).stdout.strip()


def worktree_tree(repo: Path, base_tree: str) -> str:
    with temporary_index(repo, base_tree) as (env, _index):
        git(repo, "add", "--all", "--", ".", env=env)
        return git(repo, "write-tree", env=env).stdout.strip()


def clean_status(repo: Path) -> str:
    return git(
        repo, "status", "--porcelain=v1", "--untracked-files=all"
    ).stdout


def check_checkout(
    component: dict[str, object],
    allow_synthetic_baseline: bool,
) -> tuple[str, str]:
    name = str(component["name"])
    repo = component["repo"]
    assert isinstance(repo, Path)
    base_commit = str(component["base_commit"])
    base_tree = str(component["base_tree"])
    expected_tree = str(component["expected_patched_tree"])
    patches = component["patches"]
    assert isinstance(patches, list)

    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    head_tree = git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    if head != base_commit:
        if not allow_synthetic_baseline:
            raise PrepareError(
                "%s checkout is %s, expected public base %s"
                % (name, head, base_commit)
            )
        if head_tree != base_tree:
            raise PrepareError(
                "%s synthetic checkout tree is %s, expected base tree %s"
                % (name, head_tree, base_tree)
            )
    elif head_tree != base_tree:
        raise PrepareError(
            "%s public base tree is %s, manifest records %s"
            % (name, head_tree, base_tree)
        )

    status = clean_status(repo)
    if not status:
        prospective = patched_tree(repo, base_tree, patches)
        if prospective != expected_tree:
            raise PrepareError(
                "%s patches produce tree %s, expected %s"
                % (name, prospective, expected_tree)
            )
        return head, "clean"

    actual = worktree_tree(repo, base_tree)
    if actual != expected_tree:
        untracked = any(
            line.startswith("?? ") for line in status.splitlines()
        )
        reason = "unexpected untracked files" if untracked else "dirty"
        raise PrepareError(
            "%s checkout has %s and its tree %s is not the recorded patched tree %s; "
            "refusing to overwrite existing changes:\n%s"
            % (name, reason, actual, expected_tree, status.rstrip())
        )
    reversed_tree = patched_tree(
        repo, expected_tree, patches, reverse=True
    )
    if reversed_tree != base_tree:
        raise PrepareError(
            "%s dirty tree matches the expected result, but its changes do not reverse "
            "to the recorded base tree" % name
        )
    return head, "prepared"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="root containing component checkouts (defaults to this repository)",
    )
    parser.add_argument(
        "--allow-synthetic-baseline",
        action="store_true",
        help="accept a foreign commit only when its tree exactly equals the public base tree",
    )
    args = parser.parse_args()

    try:
        components = load_manifest(args.manifest.resolve(), args.root)
        states: dict[str, str] = {}
        for component in components:
            _head, state = check_checkout(
                component, args.allow_synthetic_baseline
            )
            states[str(component["name"])] = state

        # Every component passed its clean-baseline preflight before any worktree
        # is changed. Existing dirty trees are accepted only when their exact patch
        # set reverses to the recorded base.
        for component in components:
            if states[str(component["name"])] != "clean":
                continue
            repo = component["repo"]
            patches = component["patches"]
            assert isinstance(repo, Path)
            assert isinstance(patches, list)
            for patch, _sha256 in patches:
                git(
                    repo,
                    "apply",
                    "--binary",
                    "--whitespace=nowarn",
                    str(patch),
                )
            actual = worktree_tree(repo, str(component["base_tree"]))
            if actual != component["expected_patched_tree"]:
                raise PrepareError(
                    "%s worktree tree is %s after applying patches, expected %s"
                    % (
                        component["name"],
                        actual,
                        component["expected_patched_tree"],
                    )
                )

        for component in components:
            print(
                "PASS: %s %s tree %s -> %s (%s)"
                % (
                    component["name"],
                    str(component["base_commit"])[:12],
                    component["base_tree"],
                    component["expected_patched_tree"],
                    states[str(component["name"])],
                )
            )
        return 0
    except PrepareError as error:
        print("FAIL: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
