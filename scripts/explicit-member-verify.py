#!/usr/bin/env python3
"""Plan and collect the eight explicit member producer runs.

This module does not pack, does not upload an Apple library, and does not
treat a fixture or a non-null priming object as a Mac or driver result.
The CI mode calls the real compiler and prime_one only when the host can
execute them. A Linux run stays unverified.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts/data/dodge-explicit-members.json"
REPORT = ROOT / "build/explicit-member-verify.json"
LOG = ROOT / "build/explicit-member-verify.log"
WORK = ROOT / "build/explicit-members"
EXPECTED_ORDERS = (0, 1, 3, 4, 5, 6, 7, 8)
EXECUTION_FIXTURE = "fixture"
EXECUTION_OBSERVED = "observed"
EXECUTION_REAL = "real"
ATTESTABLE_SOURCES = frozenset({EXECUTION_OBSERVED, EXECUTION_REAL})
ALLOWED_EXECUTION_SOURCES = frozenset({EXECUTION_FIXTURE, *ATTESTABLE_SOURCES})
BINDING_INDEX = 1
LIBRARY_PATH = "/System/Library/PrivateFrameworks/MentalHealthUI.framework/archive.metallib"
FORBIDDEN_KEYS = {"name", "function", "function_name", "shader", "air", "metallib_bytes", "source"}
BINDING_KEYS = {
    "file_order",
    "index",
    "name_sha256",
    "disposition",
    "stage",
    "object_returned",
    "entered_request",
    "layer",
    "driver_pipeline_unproven",
}
ASSESS_ITEMS = (
    "file_order",
    "index_unique",
    "index_consistent",
    "layer",
    "name_sha256",
    "stage",
    "entered_request",
    "object_returned",
)
BINDING_LAYER = "priming-request"


class VerifyError(Exception):
    pass


def fail(message: str) -> None:
    raise VerifyError(message)


def _hex64(value: object, what: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        fail(what + " must be 64 lowercase hex characters")
    return value


def load_member_manifest(path: Path = MANIFEST) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        fail("cannot read explicit member manifest %s: %s" % (path, error))
    if not isinstance(value, dict) or value.get("format") != 1:
        fail("explicit member manifest must be a format 1 object")
    if value.get("library_path") != LIBRARY_PATH:
        fail("explicit member manifest library path changed")
    if value.get("binding_index") != BINDING_INDEX:
        fail("explicit member manifest binding index is not 1")
    if value.get("excluded_file_orders") != [2]:
        fail("member 2 must stay excluded from the eight successes")
    if value.get("outer_bytes") != 768416:
        fail("outer byte count changed")
    outer = _hex64(value.get("outer_sha256"), "outer_sha256")
    members = value.get("members")
    if not isinstance(members, list):
        fail("explicit member manifest has no members")
    orders = []
    cleaned = []
    for index, raw in enumerate(members):
        if not isinstance(raw, dict):
            fail("members[%d] is not an object" % index)
        extra = set(raw) - {"file_order", "member_sha256", "stage", "table_index"}
        if extra:
            fail("members[%d] has non-identity fields %s" % (index, ", ".join(sorted(extra))))
        file_order = raw.get("file_order")
        if not isinstance(file_order, int) or file_order < 0:
            fail("members[%d] file_order is not a non-negative integer" % index)
        if file_order == 2:
            fail("member 2 is listed")
        stage = raw.get("stage")
        if stage not in ("kernel", "vertex"):
            fail("members[%d] stage is not kernel or vertex" % index)
        if raw.get("table_index") != BINDING_INDEX:
            fail("members[%d] table_index is not the binding index" % index)
        cleaned.append({
            "file_order": file_order,
            "member_sha256": _hex64(raw.get("member_sha256"), "members[%d].member_sha256" % index),
            "stage": stage,
            "table_index": BINDING_INDEX,
        })
        orders.append(file_order)
    if tuple(orders) != EXPECTED_ORDERS or len(set(orders)) != 8:
        fail("explicit member manifest is not the eight file orders %s" % (EXPECTED_ORDERS,))
    return {
        "outer_sha256": outer,
        "outer_bytes": 768416,
        "library_path": LIBRARY_PATH,
        "binding_index": BINDING_INDEX,
        "members": cleaned,
    }


def member_argv(file_order: int, outer_sha256: str, member_sha256: str) -> list[str]:
    return [
        "--member-file-order",
        str(file_order),
        "--member-outer-sha256",
        outer_sha256,
        "--member-sha256",
        member_sha256,
    ]


def producer_command(
    producer: Path,
    host_turnip: Path,
    directory: Path,
    prime_list: Path,
    os_mount: Path,
    cryptex_mount: Path,
    target_turnip: Path,
    gpu: str,
    argv: Sequence[str],
) -> list[str]:
    if gpu != "a830":
        fail("explicit member producer refuses a gpu other than a830")
    if list(argv)[:1] != ["--member-file-order"] or len(argv) != 6:
        fail("explicit member argv is not the three required flags")
    return [
        str(producer),
        str(host_turnip),
        str(directory),
        str(prime_list),
        str(os_mount),
        str(cryptex_mount),
        str(target_turnip),
        gpu,
        "0",
        "1",
        *argv,
    ]


def write_single_list(path: Path, library_path: str = LIBRARY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(library_path + "\n")


def parse_bindings(stdout: str) -> list[dict[str, object]]:
    found = []
    for line in stdout.splitlines():
        if not line.startswith("MEMBER_BINDING: "):
            continue
        try:
            value = json.loads(line[len("MEMBER_BINDING: "):])
        except json.JSONDecodeError:
            fail("MEMBER_BINDING line is not JSON")
        if not isinstance(value, dict):
            fail("MEMBER_BINDING line is not an object")
        extra = set(value) - BINDING_KEYS
        if extra:
            fail("MEMBER_BINDING has unexpected fields")
        if "name" in value or any(not isinstance(key, str) for key in value):
            fail("MEMBER_BINDING carries a name")
        found.append(value)
    return found


def _digest64(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _binding_signature(row: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(row.get(key) for key in (
        "file_order", "index", "name_sha256", "disposition", "stage",
        "object_returned", "entered_request", "layer", "driver_pipeline_unproven",
    ))


def assess_member(item: Mapping[str, object], result: Mapping[str, object]) -> dict[str, object]:
    """Judge one producer result. A priming object is not a driver pipeline.

    Semantic binding errors fail this member and are all recorded. They do not
    abort the other seven members. Input identity stays the manifest SHA.
    """
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    if "first-refused=" in stdout or "first-refused=" in stderr:
        fail("producer output includes a function name")
    bindings = parse_bindings(stdout)
    order = item["file_order"]
    index_rows = [row for row in bindings if row.get("index") == BINDING_INDEX]
    own = [row for row in index_rows if row.get("file_order") == order]
    foreign = [row for row in index_rows if row.get("file_order") != order]
    signatures = {_binding_signature(row) for row in index_rows}
    checks = {name: False for name in ASSESS_ITEMS}
    reasons: list[str] = []
    checks["file_order"] = bool(index_rows) and not foreign
    if not checks["file_order"]:
        reasons.append("binding index 1 file_order is not this member")
    checks["index_unique"] = len(index_rows) == 1
    if len(index_rows) > 1 and len(signatures) == 1:
        reasons.append("binding index 1 is duplicated")
    elif not index_rows:
        reasons.append("binding index 1 has no structured record")
    checks["index_consistent"] = not (len(index_rows) > 1 and len(signatures) > 1)
    if not checks["index_consistent"]:
        reasons.append("binding index 1 conflicts")
    binding = own[0] if checks["file_order"] and checks["index_unique"] and checks["index_consistent"] else None
    checks["layer"] = bool(binding and binding.get("layer") == BINDING_LAYER)
    if not checks["layer"]:
        reasons.append("binding layer is not priming-request")
    checks["name_sha256"] = bool(binding and _digest64(binding.get("name_sha256")))
    if not checks["name_sha256"]:
        reasons.append("binding name_sha256 is not 64 lowercase hex")
    stage = item["stage"]
    checks["stage"] = bool(binding and binding.get("stage") == stage)
    if not checks["stage"]:
        reasons.append("binding index 1 stage does not match the manifest")
    entered = bool(
        binding and binding.get("entered_request") is True and binding.get("disposition") == "requested"
    )
    checks["entered_request"] = entered
    resumed = (
        result.get("resumed") is True
        or any(row.get("disposition") == "resumed" for row in index_rows)
    )
    if not entered:
        if resumed:
            reasons.append("binding index 1 was resumed or the run was not fresh")
        else:
            reasons.append("binding index 1 did not enter the priming request")
    checks["object_returned"] = bool(
        binding
        and binding.get("object_returned") is True
        and binding.get("driver_pipeline_unproven") is True
    )
    if not checks["object_returned"]:
        reasons.append("binding index 1 priming request did not return an object")
    if tuple(checks) != ASSESS_ITEMS or len(checks) != 8:
        fail("explicit member assess did not collect eight checks")
    object_returned = checks["object_returned"]
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    returncode = result.get("returncode")
    primed = summary.get("primed") == 1 and summary.get("requested") == 1 and summary.get("refused") == 0 and summary.get("absent") == 0
    binding_ok = all(checks.values()) and not resumed
    ok = (
        result.get("producer_invoked") is True
        and returncode == 0
        and primed
        and binding_ok
        and result.get("status") == "primed"
    )
    reason = "; ".join(reasons)
    if not ok and not reason:
        if result.get("producer_invoked") is not True:
            reason = "producer process was not invoked"
        elif returncode != 0:
            reason = "producer exit %s" % returncode
        else:
            reason = "member did not satisfy the explicit binding contract"
    return {
        "file_order": item["file_order"],
        "member_sha256": item["member_sha256"],
        "stage": stage,
        "table_index": BINDING_INDEX,
        "requested": 1,
        "primed": 1 if ok else 0,
        "refused": 0 if ok else 1,
        "absent": 1 if result.get("status") == "absent" else 0,
        "returncode": returncode,
        "producer_invoked": result.get("producer_invoked") is True,
        "resumed": resumed,
        "binding_ok": binding_ok,
        "entered_request": entered,
        "object_returned": object_returned,
        "layer": BINDING_LAYER,
        "driver_pipeline_unproven": True,
        "driver_claimed": False,
        "status": "observed" if ok else "failed",
        "reason": reason,
        "checks": checks,
        "stdout": stdout,
        "stderr": stderr,
    }


def collect_members(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    orders = [record.get("file_order") for record in records]
    if tuple(orders) != EXPECTED_ORDERS:
        fail("member collection is not the eight file orders")
    failed = [record for record in records if record.get("status") != "observed"]
    return {
        "members_expected": 8,
        "members_collected": len(records),
        "observed": 8 - len(failed),
        "failed": len(failed),
        "passed": not failed and len(records) == 8,
        "pack_written": False,
        "pack_attempted": False,
        "driver_claimed": False,
        "driver_pipeline_unproven": True,
        "cache_queue_drain_proven": False,
        "stillness_is_not_completion": True,
    }


def unverified_report(reason: str) -> dict[str, object]:
    return {
        "status": "unverified",
        "execution_source": "unverified",
        "ci_executed": False,
        "mac_producer_verified": False,
        "pack_verified": False,
        "pack_written": False,
        "pack_attempted": False,
        "driver_claimed": False,
        "driver_pipeline_unproven": True,
        "cache_queue_drain_proven": False,
        "members_expected": 8,
        "members_collected": 0,
        "reason": reason,
        "validation_boundary": (
            "This host did not execute the macOS producer. The result is unverified, "
            "not a Mac success and not a pack."
        ),
    }


def write_report(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def library_identity(path: Path, expected_sha: str, expected_bytes: int) -> tuple[bool, str]:
    if not path.is_file():
        return False, "library absent from the mounted OS"
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != expected_bytes or digest != expected_sha:
        return False, "mounted library identity does not match the manifest"
    return True, ""


def run_explicit_members(
    manifest: Mapping[str, object],
    *,
    os_mount: Path,
    cryptex_mount: Path,
    work: Path,
    library_file: Path,
    producer: Path,
    host_turnip: Path,
    target_turnip: Path,
    peer: Path,
    gpu: str,
    verify_peer: Callable[[], None],
    compile_producer: Callable[[], None],
    prime_one: Callable[[Path, list[str], Mapping[str, str], Path], Mapping[str, object]],
    environment: Callable[[], Mapping[str, str]],
    pack_writer: Callable[..., object],
    claim_host_execution: bool,
    execution_source: str = EXECUTION_FIXTURE,
) -> dict[str, object]:
    """Run or simulate eight fresh member invocations. Never calls pack_writer.

    execution_source is an explicit caller declaration. It defaults fail-closed
    to fixture. Platform and callback success are not authentication: a fixture
    stays unverified even when claim_host_execution is true on Darwin. real and
    observed sources still require the caller claim and the collected results,
    and a non-Darwin host cannot record a Mac result.
    """
    del pack_writer  # present so a caller cannot forget the boundary; this function must not call it
    if not isinstance(execution_source, str) or execution_source not in ALLOWED_EXECUTION_SOURCES:
        fail("execution_source must be fixture, real, or observed")
    if gpu != "a830":
        fail("refusing to treat a preset device name as a built peer")
    members = manifest["members"]
    assert isinstance(members, list)
    outer = str(manifest["outer_sha256"])
    identity_ok, identity_reason = library_identity(
        library_file, outer, int(manifest["outer_bytes"])
    )
    peer_called = False
    compiled = False
    records = []
    if identity_ok:
        verify_peer()
        peer_called = True
        compile_producer()
        compiled = True
        if not producer.is_file():
            fail("compile_producer returned without a producer binary")
    for item in members:
        assert isinstance(item, dict)
        file_order = int(item["file_order"])
        member_work = work / ("member-%02d" % file_order)
        if member_work.exists():
            fail("refusing to resume an existing member directory: " + str(member_work))
        member_work.mkdir(parents=True)
        prime_list = member_work / "prime-list.txt"
        write_single_list(prime_list)
        argv = member_argv(file_order, outer, str(item["member_sha256"]))
        run_dir = member_work / "runs" / "000"
        command = producer_command(
            producer, host_turnip, run_dir, prime_list,
            os_mount, cryptex_mount, target_turnip, gpu, argv,
        )
        (member_work / "command.json").write_text(json.dumps(command, indent=2) + "\n")
        if not identity_ok:
            result = {
                "producer_invoked": False,
                "returncode": None,
                "status": "failed",
                "resumed": False,
                "stdout": "",
                "stderr": identity_reason + "\n",
                "reason": identity_reason,
                "summary": {"requested": 1, "primed": 0, "refused": 1, "absent": 0},
            }
        else:
            env = dict(environment())
            for key in ("HOSTSIM_GPU", "HOSTSIM_PAGE_SIZE", "TU_DEBUG", "DYLD_INSERT_LIBRARIES"):
                if key not in env or env[key] == "":
                    fail("member environment lost " + key)
            if env["HOSTSIM_GPU"] != gpu or env["HOSTSIM_PAGE_SIZE"] != "4096" or env["TU_DEBUG"] != "sysmem,nocb":
                fail("member environment does not keep the host/target page and debug contract")
            if env["DYLD_INSERT_LIBRARIES"] != str(peer):
                fail("member environment is not using the built peer")
            result = prime_one(run_dir, command, env, prime_list)
        assessed = assess_member(item, result)
        assessed["command"] = command
        (member_work / "stdout.log").write_text(str(assessed["stdout"]))
        (member_work / "stderr.log").write_text(str(assessed["stderr"]))
        records.append(assessed)
    summary = collect_members(records)
    attestable = execution_source in ATTESTABLE_SOURCES
    # Platform only gates a Mac record. It does not prove this process ran the producer.
    host_can_record_mac = sys.platform == "darwin"
    member_results_hold = bool(
        peer_called
        and compiled
        and summary["passed"]
        and all(record["producer_invoked"] and not record["resumed"] for record in records)
    )
    mac_verified = bool(
        attestable
        and claim_host_execution
        and host_can_record_mac
        and member_results_hold
    )
    if execution_source == EXECUTION_FIXTURE:
        status = "fixture" if summary["passed"] else "fixture-failed"
    else:
        status = "explicit-members-observed" if summary["passed"] else "explicit-members-failed"
    report = {
        "status": status,
        "execution_source": execution_source,
        "ci_executed": bool(
            attestable and claim_host_execution and host_can_record_mac and compiled
        ),
        "mac_producer_verified": mac_verified,
        "pack_verified": False,
        "pack_written": False,
        "pack_attempted": False,
        "driver_claimed": False,
        "driver_pipeline_unproven": True,
        "cache_queue_drain_proven": False,
        "stillness_is_not_completion": True,
        "peer_built": peer_called,
        "producer_compiled": compiled,
        "outer_sha256": outer,
        "outer_bytes": manifest["outer_bytes"],
        "library_path": LIBRARY_PATH,
        "binding_index": BINDING_INDEX,
        "members": [
            {key: record[key] for key in (
                "file_order", "member_sha256", "stage", "table_index", "requested",
                "primed", "refused", "absent", "returncode", "producer_invoked",
                "resumed", "binding_ok", "entered_request", "object_returned",
                "layer", "driver_pipeline_unproven", "driver_claimed", "status", "reason",
                "checks",
            )}
            for record in records
        ],
        "summary": summary,
        "validation_boundary": (
            "Fixture report only. "
            if execution_source == EXECUTION_FIXTURE
            else "Explicit " + execution_source + " source. "
        ) + (
            "Binding index 1 entered_request is a priming-object request, not a driver "
            "VkPipeline, cache hit, or drain. A fixture cannot set mac_producer_verified."
        ),
    }
    if not summary["passed"]:
        report["reason"] = "one or more explicit members failed; no pack was written"
    return report


def assert_no_pack(paths: Sequence[Path]) -> None:
    for path in paths:
        if path.exists():
            fail("explicit member verification produced a pack artifact: " + str(path))
