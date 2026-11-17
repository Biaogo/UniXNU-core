#!/usr/bin/env python3
"""Resume-identity regression for explicit member selection.

Synthetic files are fixtures. They are not metallibs, not a Mac producer, and
not an A830 or pack result. The checks call stamp() and resumable_result().
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# Fixture digests. Not the MentalHealthUI outer or any member of it.
FIXTURE_OUTER = "11" * 32
FIXTURE_OUTER_OTHER = "22" * 32
FIXTURE_MEMBER_0 = "33" * 32
FIXTURE_MEMBER_1 = "44" * 32
FIXTURE_MEMBER_OTHER = "55" * 32


def load_generator():
    spec = importlib.util.spec_from_file_location(
        "pipeline_cache_member_identity_generator", SCRIPTS / "make-pipeline-cache.py"
    )
    if spec is None or spec.loader is None:
        raise SystemExit("FAIL: could not load make-pipeline-cache.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def require(condition: object, message: str) -> None:
    if not condition:
        raise SystemExit("FAIL: " + message)


def member_argv(order: str, outer: str, member: str) -> list[str]:
    return [
        "--member-file-order", order,
        "--member-outer-sha256", outer,
        "--member-sha256", member,
    ]


def stamp_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write_primed(directory: Path, path: str, identity: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.log").write_text("fixture run log\n")
    (directory / "prime-done").write_text(path + "\tfixed\n")
    payload = {
        "fixture": True,
        "index": 0,
        "path": path,
        "status": "primed",
        "requested": 1,
        "primed": 1,
        "refused": 0,
        "absent": 0,
        "returncode": 0,
        "producer_summary": {"requested": 1, "primed": 1, "refused": 0, "absent": 0},
        "stamp_id": identity,
    }
    (directory / "result.json").write_text(json.dumps(payload))


generator = load_generator()
checks = 0


def check(condition: object, message: str) -> None:
    global checks
    require(condition, message)
    checks += 1


with tempfile.TemporaryDirectory(prefix="fixture-member-identity-") as temporary:
    root = Path(temporary)
    one = root / "fixture-one"
    one.write_bytes(b"fixture-not-a-metallib\n")
    producer = root / "fixture-producer"
    producer.write_bytes(b"fixture producer\n")
    inputs = SimpleNamespace(
        producer=producer,
        host_turnip=one,
        target_turnip=one,
        peer=one,
        bridge=one,
        prime_list=one,
        host_profile=one,
        target_profile=one,
        peer_original=one,
        peer_source=one,
        peer_patch=one,
        peer_helper=one,
        peer_manifest=one,
    )
    environment = {
        "DYLD_INSERT_LIBRARIES": "fixture",
        "HOSTSIM_GPU": "a830",
        "HOSTSIM_PAGE_SIZE": "4096",
        "MESA_LOG": "",
        "MESA_SHADER_CACHE_DISABLE": "true",
        "TU_DEBUG": "sysmem,nocb",
    }
    absent = generator.stamp(producer, inputs, environment, "fixture-os")
    empty = generator.stamp(producer, inputs, environment, "fixture-os")
    inputs.member_argv = []
    explicit_empty = generator.stamp(producer, inputs, environment, "fixture-os")
    check(absent == empty == explicit_empty, "no-selection stamp is not stable")
    check("member_argv=" not in absent, "no-selection stamp gained a member line")
    check(absent.endswith("os_build=fixture-os\n"), "no-selection stamp no longer ends at os_build")
    prefixes = [line.split("=", 1)[0].split(" ")[0] for line in absent.splitlines()]
    check(prefixes[-1] == "os_build", "no-selection last field changed")
    check(all(not line.startswith("member_argv=") for line in absent.splitlines()), "member line in default stamp")

    def stamped(argv: list[str]) -> str:
        inputs.member_argv = argv
        return generator.stamp(producer, inputs, environment, "fixture-os")

    member0 = stamped(member_argv("0", FIXTURE_OUTER, FIXTURE_MEMBER_0))
    member0_again = stamped(member_argv("0", FIXTURE_OUTER, FIXTURE_MEMBER_0))
    member1 = stamped(member_argv("1", FIXTURE_OUTER, FIXTURE_MEMBER_1))
    other_outer = stamped(member_argv("0", FIXTURE_OUTER_OTHER, FIXTURE_MEMBER_0))
    other_member = stamped(member_argv("0", FIXTURE_OUTER, FIXTURE_MEMBER_OTHER))
    check(member0 == member0_again, "same explicit request did not reprint the same stamp")
    check("member_argv=" in member0, "explicit stamp omitted member_argv")
    check(FIXTURE_OUTER in member0 and FIXTURE_MEMBER_0 in member0 and "\t0\t" in member0,
          "explicit stamp does not cover file order and both digests")
    for label, text in (
        ("member0", member0),
        ("member1", member1),
        ("other_outer", other_outer),
        ("other_member", other_member),
    ):
        check(text != absent, label + " stamp collapsed onto the no-selection stamp")
    check(len({absent, member0, member1, other_outer, other_member}) == 5,
          "default/member0/member1/outer/member stamps are not isolated")

    ids = {
        "default": stamp_id(absent),
        "member0": stamp_id(member0),
        "member1": stamp_id(member1),
        "other_outer": stamp_id(other_outer),
        "other_member": stamp_id(other_member),
    }
    check(len(set(ids.values())) == 5, "stamp_id did not isolate the five requests")

    path = "/System/Library/Fixture/archive.metallib"
    work = root / "fixture-work"
    inputs.work = work
    directory = work / "runs" / "000"
    write_primed(directory, path, ids["member0"])
    reused = generator.resumable_result(0, path, inputs, ids["member0"])
    check(reused is not None and reused.get("resumed") is True and reused.get("stamp_id") == ids["member0"],
          "same request was not reused through resumable_result")
    reused_again = generator.resumable_result(0, path, inputs, ids["member0"])
    check(reused_again is not None and reused_again.get("resumed") is True,
          "same request was not reusable on a second resumable_result call")
    for label in ("default", "member1", "other_outer", "other_member"):
        denied = generator.resumable_result(0, path, inputs, ids[label])
        check(denied is None, label + " reused a member0 primed result")

    write_primed(directory, path, ids["default"])
    check(generator.resumable_result(0, path, inputs, ids["default"]) is not None,
          "default request could not reuse its own primed result")
    check(generator.resumable_result(0, path, inputs, ids["member0"]) is None,
          "member0 reused a default primed result")

print("PASS fixture member identity checks=%d" % checks)
