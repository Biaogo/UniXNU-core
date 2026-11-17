#!/usr/bin/env python3
"""Fixture for the explicit eight-member CI entry.

A passing fixture is not a Mac producer result and not a pack.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise SystemExit("FAIL: " + message)


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    if spec is None or spec.loader is None:
        raise SystemExit("FAIL: cannot load " + relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def binding_line(file_order: int, stage: str, *, entered: bool = True, disposition: str = "requested") -> str:
    return "MEMBER_BINDING: " + json.dumps({
        "file_order": file_order,
        "index": 1,
        "name_sha256": "ab" * 32,
        "disposition": disposition,
        "stage": stage,
        "object_returned": entered,
        "entered_request": entered,
        "layer": "priming-request",
        "driver_pipeline_unproven": True,
    }, sort_keys=True) + "\n"


def success_result(item: dict) -> dict:
    stdout = (
        binding_line(item["file_order"], item["stage"])
        + "RESULT: requested=1 primed=1 refused=0 absent=0 device=fixture "
        + "layer=priming-request driver_pipeline_unproven=1\n"
        + "TRANSLATION_COUNTERS: writes=1 driver_saves=0 hits=0 misses=1 "
        + "driver_hits=0 driver_misses=0 stillness_is_not_completion=1 "
        + "layer=priming-request driver_pipeline_unproven=1\n"
    )
    return {
        "producer_invoked": True,
        "returncode": 0,
        "status": "primed",
        "resumed": False,
        "stdout": stdout,
        "stderr": "pipeline-cache-host: explicit outcome=made\n",
        "reason": "",
        "summary": {"requested": 1, "primed": 1, "refused": 0, "absent": 0},
    }


def environment() -> dict[str, str]:
    return {
        "HOSTSIM_GPU": "a830",
        "HOSTSIM_PAGE_SIZE": "4096",
        "TU_DEBUG": "sysmem,nocb",
        "DYLD_INSERT_LIBRARIES": str(PEER),
    }


PEER = Path("/fixture/libhostiokit.dylib")
PRODUCER = Path("/fixture/pipeline-cache-host")
HOST = Path("/fixture/host-turnip.dylib")
TARGET = Path("/fixture/target-turnip.dylib")


def run_case(members, library: Path, work: Path, prime, *, claim: bool = False, producer: Path = PRODUCER, execution_source: str = "fixture", platform: str | None = None) -> dict:
    calls = {"peer": 0, "compile": 0, "prime": 0, "pack": 0}

    def verify_peer() -> None:
        calls["peer"] += 1

    def compile_producer() -> None:
        calls["compile"] += 1
        producer.parent.mkdir(parents=True, exist_ok=True)
        producer.write_bytes(b"fixture-producer")

    def counted_prime(*args, **kwargs):
        calls["prime"] += 1
        return prime(*args, **kwargs)

    def refuse_pack(*_args, **_kwargs):
        calls["pack"] += 1
        raise members.VerifyError("pack was called")

    def invoke():
        return members.run_explicit_members(
            members.load_member_manifest(),
            os_mount=library.parent,
            cryptex_mount=library.parent,
            work=work,
            library_file=library,
            producer=producer,
            host_turnip=HOST,
            target_turnip=TARGET,
            peer=PEER,
            gpu="a830",
            verify_peer=verify_peer,
            compile_producer=compile_producer,
            prime_one=counted_prime,
            environment=environment,
            pack_writer=refuse_pack,
            claim_host_execution=claim,
            execution_source=execution_source,
        )

    if platform is None:
        report = invoke()
    else:
        with patch.object(members.sys, "platform", platform):
            report = invoke()
    report["_calls"] = calls
    report["_harness"] = "fixture-or-simulated-predicate"
    return report


def production_binding_stdout(file_order: int, stage: str) -> str:
    return binding_line(file_order, stage)


def test_production_formatter() -> None:
    header = SCRIPTS / "pipeline-cache-format.h"
    text = header.read_text()
    check("}" + "\\" + "\\" + "n" not in text, "production formatter still escapes its newline")
    check("print_member_binding" in text and "print_translation_counters" in text and "print_explicit_result" in text,
          "production formatter is missing a line helper")
    digest = "ab" * 32
    with tempfile.TemporaryDirectory(prefix="member-format-") as temporary:
        root = Path(temporary)
        positive = root / "positive.cpp"
        positive.write_text(
            '#include "pipeline-cache-format.h"\n'
            "int main() {\n"
            '  const char *sha = "%s";\n'
            '  print_member_binding(0, 1, sha, "requested", "kernel", true, true);\n'
            '  print_member_binding(3, 4, sha, "skipped", "vertex", false, false);\n'
            "  print_translation_counters(2, 0, 1, 3, 0, 4);\n"
            '  print_explicit_result(1, 0, 0, "fixture", 1, "made", "test");\n'
            "  return 0;\n"
            "}\n" % digest
        )
        binary = root / "positive"
        compiled = subprocess.run(
            ["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(SCRIPTS), str(positive), "-o", str(binary)],
            capture_output=True, text=True,
        )
        check(compiled.returncode == 0, "production formatter failed to compile: " + compiled.stderr[-400:])
        ran = subprocess.run([str(binary)], capture_output=True, text=True)
        check(ran.returncode == 0, "production formatter failed to run")
        members = load("explicit_member_verify_format", "scripts/explicit-member-verify.py")
        generator = load("make_pipeline_cache_format", "scripts/make-pipeline-cache.py")
        bindings = members.parse_bindings(ran.stdout)
        summary = generator.producer_summary(ran.stdout)
        check(len(bindings) == 2, "production printf did not yield two binding lines")
        check(bindings[0]["file_order"] == 0 and bindings[1]["index"] == 4, "production binding fields were not parsed")
        check(bindings[0]["name_sha256"] == digest and bindings[0]["layer"] == "priming-request",
              "production binding digest or layer was not parsed")
        check(summary == {"requested": 1, "primed": 1, "refused": 0, "absent": 0},
              "production RESULT was not parsed on its own line")
        check("\\" + "n" not in ran.stdout.replace("\n", ""), "production stdout still contains a literal backslash-n")
        emitted = (
            'MEMBER_BINDING: {"file_order":0,"index":1,"name_sha256":"%s",'
            '"disposition":"requested","stage":"kernel","object_returned":true,'
            '"entered_request":true,"layer":"priming-request","driver_pipeline_unproven":true}'
            % digest
        ) + "\\n" + (
            "TRANSLATION_COUNTERS: writes=1 driver_saves=0 hits=0 misses=1 "
            "driver_hits=0 driver_misses=0 stillness_is_not_completion=1 "
            "layer=priming-request driver_pipeline_unproven=1"
        ) + "\\n" + (
            "RESULT: requested=1 primed=1 refused=0 absent=0 device=fixture made=1 "
            "outcome=made layer=priming-request driver_pipeline_unproven=1 build=test"
        ) + "\\n"
        negative = root / "negative.cpp"
        negative.write_text(
            "#include <cstdio>\n"
            "int main() {\n"
            "  std::printf(%s);\n"
            "  return 0;\n"
            "}\n" % json.dumps(emitted)
        )
        bad = root / "negative"
        compiled = subprocess.run(
            ["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", str(negative), "-o", str(bad)],
            capture_output=True, text=True,
        )
        check(compiled.returncode == 0, "escaped-newline control failed to compile: " + compiled.stderr[-400:])
        ran = subprocess.run([str(bad)], capture_output=True, text=True)
        check(ran.returncode == 0, "escaped-newline control failed to run")
        check("\\" + "n" in ran.stdout, "negative control did not print a literal backslash-n")
        check(generator.producer_summary(ran.stdout) is None, "parser accepted a RESULT that is not at line start")
        rejected = False
        try:
            members.parse_bindings(ran.stdout)
        except members.VerifyError:
            rejected = True
        check(rejected, "parser accepted a binding line ended by an escaped newline")


def test_assess_exceptions(members) -> None:
    manifest = members.load_member_manifest()
    orders = [item["file_order"] for item in manifest["members"]]
    by_order = {item["file_order"]: item for item in manifest["members"]}

    def base(item, stdout, **extra):
        value = success_result(item)
        value["stdout"] = stdout
        value.update(extra)
        return value

    def line(order, stage, **fields):
        payload = {
            "file_order": order,
            "index": 1,
            "name_sha256": "ab" * 32,
            "disposition": "requested",
            "stage": stage,
            "object_returned": True,
            "entered_request": True,
            "layer": "priming-request",
            "driver_pipeline_unproven": True,
        }
        payload.update(fields)
        return "MEMBER_BINDING: " + json.dumps(payload, sort_keys=True) + "\n"

    cases = {
        orders[0]: ("file_order", line(orders[1], by_order[orders[0]]["stage"])),
        orders[1]: ("duplicated", line(orders[1], by_order[orders[1]]["stage"]) + line(orders[1], by_order[orders[1]]["stage"])),
        orders[2]: ("conflicts", line(orders[2], by_order[orders[2]]["stage"]) + line(orders[2], by_order[orders[2]]["stage"], name_sha256="cd" * 32)),
        orders[3]: ("layer", line(orders[3], by_order[orders[3]]["stage"], layer="unknown-layer")),
        orders[4]: ("name_sha256", line(orders[4], by_order[orders[4]]["stage"], name_sha256="not-a-digest")),
        orders[5]: ("resumed", line(orders[5], by_order[orders[5]]["stage"], disposition="resumed", entered_request=False, object_returned=False)),
    }
    seen = []

    def prime(run_dir, command, env, prime_list):
        del run_dir, env, prime_list
        order = int(command[command.index("--member-file-order") + 1])
        item = by_order[order]
        seen.append(order)
        if order not in cases:
            return success_result(item)
        label, stdout = cases[order]
        extra = {"resumed": True} if label == "resumed" else {}
        return base(item, stdout, **extra)

    with tempfile.TemporaryDirectory(prefix="member-assess-") as temporary:
        root = Path(temporary)
        library = root / "real.metallib"
        library.write_bytes(b"x" * 768416)
        digest = __import__("hashlib").sha256(library.read_bytes()).hexdigest()
        saved = members.load_member_manifest

        def matching():
            value = saved()
            value["outer_sha256"] = digest
            return value

        members.load_member_manifest = matching
        try:
            report = run_case(members, library, root / "work", prime, producer=root / "pipeline-cache-host")
        finally:
            members.load_member_manifest = saved
    check(seen == list(orders), "an assess error stopped the remaining members")
    check(report["summary"]["members_collected"] == 8 and report["summary"]["passed"] is False,
          "assess errors were not collected for all eight members")
    rows = {row["file_order"]: row for row in report["members"]}
    check(all(tuple(row["checks"]) == members.ASSESS_ITEMS and len(row["checks"]) == 8 for row in report["members"]),
          "a member did not collect eight assess checks")
    check(rows[orders[0]]["status"] == "failed" and rows[orders[0]]["checks"]["file_order"] is False,
          "wrong file_order was accepted")
    check(rows[orders[1]]["status"] == "failed" and "duplicated" in rows[orders[1]]["reason"],
          "duplicate index 1 was accepted")
    check(rows[orders[2]]["status"] == "failed" and "conflicts" in rows[orders[2]]["reason"],
          "conflicting index 1 was accepted")
    check(rows[orders[3]]["checks"]["layer"] is False and rows[orders[3]]["status"] == "failed",
          "unknown layer was accepted")
    check(rows[orders[4]]["checks"]["name_sha256"] is False and rows[orders[4]]["status"] == "failed",
          "bad digest was accepted")
    check(rows[orders[5]]["resumed"] is True and rows[orders[5]]["status"] == "failed",
          "CI accepted a resumed binding")
    check(rows[orders[6]]["status"] == "observed" and rows[orders[7]]["status"] == "observed",
          "a different member was contaminated by another member's binding")
    check(rows[orders[6]]["member_sha256"] == by_order[orders[6]]["member_sha256"],
          "collected member SHA was not the exact manifest value")


def test_mounted_peer_order() -> None:
    ci = load("ci_dodge_pipeline_cache_order", "scripts/ci-dodge-pipeline-cache.py")
    members = load("explicit_member_verify_order", "scripts/explicit-member-verify.py")
    pins = json.loads((SCRIPTS / "data/adreno-compiler-inputs.json").read_text())
    commit = pins["mesa"]["commit"]
    profile = {
        "mesa_revision": commit,
        "source_diff_sha256": "ab" * 32,
        "extra_sources": [],
        "target": "ios",
        "options": ["-Dshader-cache=enabled"],
    }
    peer = ROOT / "build/turnip-darwin-host/libhostiokit.dylib"
    existed = peer.exists()
    previous = peer.read_bytes() if existed else None
    events = []

    class Completed:
        def __init__(self, command, returncode, stdout="", stderr=""):
            self.args = command
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def install_generator(module):
        def require_path(path, what):
            del what
            events.append(("require_path", str(path)))
            return path

        def macho_identity(path):
            del path
            events.append(("macho_identity",))
            return "ab" * 16, module.PLATFORM_IOS

        def profile_identity(path, expected_target):
            del path, expected_target
            events.append(("profile_identity",))
            return dict(profile)

        def verify_host_peer(args, inputs):
            del args
            events.append(("verify_host_peer", inputs.peer.is_file()))
            if not inputs.peer.is_file():
                raise SystemExit("peer missing at verify")

        def compile_producer(args, inputs):
            del args
            events.append(("compile_producer", inputs.peer.is_file()))
            inputs.producer.parent.mkdir(parents=True, exist_ok=True)
            inputs.producer.write_bytes(b"fixture-producer")
            return inputs.producer

        def stamp(*_args):
            return "fixture-stamp"

        module.require_path = require_path
        module.macho_identity = macho_identity
        module.profile_identity = profile_identity
        module.verify_host_peer = verify_host_peer
        module.compile_producer = compile_producer
        module.stamp = stamp
        return module

    hooks = {}
    real_load = ci._load_script

    def load_wrapped(name, relative):
        module = real_load(name, relative)
        if relative.endswith("make-pipeline-cache.py"):
            install_generator(module)
            if "prime_one" in hooks:
                module.prime_one = hooks["prime_one"]
        return module

    ci._load_script = load_wrapped
    saved_manifest = members.load_member_manifest
    try:
        with tempfile.TemporaryDirectory(prefix="member-mount-") as temporary:
            root = Path(temporary)
            os_mount = root / "os"
            cryptex = root / "cryptex"
            quartz = os_mount / "System/Library/Frameworks/QuartzCore.framework/default.metallib"
            quartz.parent.mkdir(parents=True)
            quartz.write_bytes(b"mounted-quartzcore")
            (cryptex / "System/Library").mkdir(parents=True)
            version = os_mount / "System/Library/CoreServices/SystemVersion.plist"
            version.parent.mkdir(parents=True, exist_ok=True)
            version.write_text("<plist><dict><key>ProductBuildVersion</key><string>21H461</string></dict></plist>")
            payload = b"y" * 768416
            digest = __import__("hashlib").sha256(payload).hexdigest()
            library = os_mount / members.LIBRARY_PATH.lstrip("/")
            library.parent.mkdir(parents=True, exist_ok=True)
            library.write_bytes(payload)

            def matching():
                value = saved_manifest()
                value["outer_sha256"] = digest
                return value

            members.load_member_manifest = matching
            by_order = {item["file_order"]: item for item in matching()["members"]}

            def prime_ok(index, cloned, identity, env, stamp_id):
                del index, identity, env, stamp_id
                events.append(("prime_one", Path(cloned.peer).is_file()))
                argv = list(cloned.member_argv)
                order = int(argv[argv.index("--member-file-order") + 1])
                item = by_order[order]
                result = success_result(item)
                result["producer_summary"] = result.pop("summary")
                return result

            def run_build(command, **kwargs):
                library_env = (kwargs.get("env") or {}).get("NMB_TEST_QUARTZCORE_LIBRARY")
                events.append(("peer_build", [str(item) for item in command], library_env))
                peer.parent.mkdir(parents=True, exist_ok=True)
                peer.write_bytes(b"peer-from-build")
                return Completed(command, 0, stdout="peer-built\n", stderr="")

            hooks["prime_one"] = prime_ok
            ci._RUN = run_build
            config = type("Config", (), {"product_build": "21H461"})()
            mounts = {
                "os": type("Mount", (), {"mountpoint": os_mount})(),
                "cryptex": type("Mount", (), {"mountpoint": cryptex})(),
            }
            with patch.object(members.sys, "platform", "linux"):
                report = ci._verify_members_mounted(config, mounts, members, {})
            names = [item[0] for item in events]
            check(names.index("peer_build") < names.index("verify_host_peer") < names.index("compile_producer") < names.index("prime_one"),
                  "peer build was not established before verify and compile")
            check(events[names.index("verify_host_peer")][1] is True, "verify ran before the peer library existed")
            check(events[names.index("compile_producer")][1] is True, "producer compile ran before the peer library existed")
            library_env = events[names.index("peer_build")][2]
            check(library_env == str(quartz.resolve()), "peer build did not receive the mounted QuartzCore library")
            check(str(quartz.resolve()).startswith(str(os_mount.resolve())), "QuartzCore path left the OS mount")
            check(not str(library_env).startswith("/System/Library/"), "peer build used a host library path")
            check("check-turnip-darwin-host.py" in events[names.index("peer_build")][1][-1],
                  "peer build did not call the real smoke entry")
            log = ROOT / "build/explicit-members/host-peer-build.log"
            check(log.is_file() and log.read_text().endswith("exit=0\n"), "peer build did not record exit")
            check(report["execution_source"] == "observed", "real mounted caller did not set observed source")
            check(report["ci_executed"] is False and report["mac_producer_verified"] is False,
                  "Linux mounted fixture claimed a Mac or CI result")
            check(report["pack_written"] is False, "mounted fixture packed")
            missing = []

            def run_without_product(command, **kwargs):
                missing.append(kwargs.get("env", {}).get("NMB_TEST_QUARTZCORE_LIBRARY"))
                if peer.exists():
                    peer.unlink()
                return Completed(command, 0, stdout="no-product\n", stderr="")

            ci._RUN = run_without_product
            events.clear()
            raised = False
            try:
                ci._verify_members_mounted(config, mounts, members, {})
            except ci.CiError as error:
                raised = "did not produce" in str(error)
            check(raised and "verify_host_peer" not in [item[0] for item in events],
                  "a missing peer library still reached verify")
            quartz.unlink()
            called = []
            ci._RUN = lambda *args, **kwargs: called.append(args) or Completed(args[0], 0)
            refused = False
            try:
                ci._verify_members_mounted(config, mounts, members, {})
            except ci.CiError as error:
                refused = "QuartzCore" in str(error)
            check(refused and not called, "missing mounted QuartzCore still invoked the peer script")
    finally:
        members.load_member_manifest = saved_manifest
        ci._load_script = real_load
        ci._RUN = subprocess.run
        sys.modules.pop("make_pipeline_cache", None)
        if existed and previous is not None:
            peer.parent.mkdir(parents=True, exist_ok=True)
            peer.write_bytes(previous)
        elif peer.exists():
            peer.unlink()


def test_execution_source_matrix(members) -> None:
    """Fixture and observed-source predicates. Not a Mac producer result.

    Platform patches stay inside each call, after the compiler fixture.
    Observed-source positives are in-process predicates only.
    """
    host_platform = sys.platform
    print("NOTE: execution-source checks are fixture or in-process predicates, not Mac producer evidence")
    payload = b"x" * 768416
    digest = __import__("hashlib").sha256(payload).hexdigest()
    saved = members.load_member_manifest
    real_report = members.REPORT
    before_report = real_report.read_bytes() if real_report.is_file() else None

    def matching():
        value = saved()
        value["outer_sha256"] = digest
        return value

    def item_for(command):
        order = int(command[command.index("--member-file-order") + 1])
        return next(row for row in matching()["members"] if row["file_order"] == order)

    def prime_ok(run_dir, command, env, prime_list):
        del run_dir, env, prime_list
        return success_result(item_for(command))

    def prime_failed(run_dir, command, env, prime_list):
        del run_dir, env, prime_list
        item = item_for(command)
        if item["file_order"] == 4:
            bad = success_result(item)
            bad["returncode"] = 3
            bad["status"] = "exit_failure"
            bad["summary"] = {"requested": 1, "primed": 0, "refused": 1, "absent": 0}
            return bad
        return success_result(item)

    def prime_incomplete(run_dir, command, env, prime_list):
        del run_dir, env, prime_list
        item = item_for(command)
        if item["file_order"] == 0:
            bad = success_result(item)
            bad["stdout"] = binding_line(0, item["stage"]).replace("priming-request", "unknown-layer")
            return bad
        return success_result(item)

    def prime_raises(run_dir, command, env, prime_list):
        del run_dir, command, env, prime_list
        raise RuntimeError("member prime failed")

    def boundaries(report, message: str) -> None:
        check(report["pack_written"] is False and report["pack_attempted"] is False and report["pack_verified"] is False,
              message + " packed")
        check(report["driver_claimed"] is False and report["cache_queue_drain_proven"] is False,
              message + " upgraded drain or driver proof")
        check(report["_harness"] == "fixture-or-simulated-predicate", message + " left the test harness")

    members.load_member_manifest = matching
    try:
        with tempfile.TemporaryDirectory(prefix="execution-source-") as temporary:
            root = Path(temporary)
            library = root / "real.metallib"
            library.write_bytes(payload)
            short = root / "short.metallib"
            short.write_bytes(b"nope")
            producer = root / "pipeline-cache-host"
            case_id = {"n": 0}

            def work(name: str) -> Path:
                case_id["n"] += 1
                return root / ("%02d-%s" % (case_id["n"], name))

            for platform in ("linux", "darwin"):
                passed = run_case(
                    members, library, work("fixture-pass"), prime_ok,
                    claim=True, producer=producer, execution_source="fixture", platform=platform,
                )
                check(passed["mac_producer_verified"] is False, "fixture claimed mac_producer_verified")
                check(passed["ci_executed"] is False, "fixture claimed ci_executed")
                check(passed["execution_source"] == "fixture" and passed["status"] == "fixture",
                      platform + " fixture success was mixed into a real report")
                check(str(passed["validation_boundary"]).startswith("Fixture report only."),
                      platform + " fixture report lost its fixture boundary")
                boundaries(passed, platform + " fixture success")

                failed = run_case(
                    members, library, work("fixture-fail"), prime_failed,
                    claim=True, producer=producer, execution_source="fixture", platform=platform,
                )
                check(failed["mac_producer_verified"] is False and failed["ci_executed"] is False,
                      platform + " fixture failure claimed a Mac or CI result")
                check(failed["status"] == "fixture-failed" and failed["summary"]["passed"] is False,
                      platform + " fixture failure was not collected as a fixture")
                check(failed["_calls"]["compile"] == 1 and failed["summary"]["members_collected"] == 8,
                      platform + " fixture failure skipped compile or dropped a member")
                boundaries(failed, platform + " fixture failure")

                uncompiled = run_case(
                    members, short, work("fixture-uncompiled"), prime_ok,
                    claim=True, producer=producer, execution_source="fixture", platform=platform,
                )
                check(uncompiled["_calls"]["compile"] == 0 and uncompiled["_calls"]["prime"] == 0,
                      platform + " uncompiled fixture invoked the producer")
                check(uncompiled["mac_producer_verified"] is False and uncompiled["ci_executed"] is False,
                      platform + " uncompiled fixture claimed a Mac or CI result")
                check(uncompiled["execution_source"] == "fixture" and uncompiled["status"] == "fixture-failed",
                      platform + " uncompiled fixture was mixed into a real report")
                boundaries(uncompiled, platform + " uncompiled fixture")

                incomplete = run_case(
                    members, library, work("fixture-incomplete"), prime_incomplete,
                    claim=True, producer=producer, execution_source="fixture", platform=platform,
                )
                check(incomplete["summary"]["members_collected"] == 8 and incomplete["summary"]["passed"] is False,
                      platform + " incomplete eight-check fixture was accepted")
                check(incomplete["members"][0]["checks"]["layer"] is False and len(incomplete["members"][0]["checks"]) == 8,
                      platform + " incomplete member did not keep eight assess checks")
                check(incomplete["mac_producer_verified"] is False and incomplete["ci_executed"] is False,
                      platform + " incomplete fixture claimed a Mac or CI result")
                boundaries(incomplete, platform + " incomplete fixture")

                raised = False
                try:
                    run_case(
                        members, library, work("fixture-raise"), prime_raises,
                        claim=True, producer=producer, execution_source="fixture", platform=platform,
                    )
                except RuntimeError as error:
                    raised = "member prime failed" in str(error)
                check(raised, platform + " fixture exception became a report")

                observed_fail = run_case(
                    members, library, work("observed-fail"), prime_failed,
                    claim=True, producer=producer, execution_source="observed", platform=platform,
                )
                check(observed_fail["mac_producer_verified"] is False, platform + " observed failure was verified")
                check(observed_fail["execution_source"] == "observed" and observed_fail["status"] == "explicit-members-failed",
                      platform + " observed failure lost its source")
                boundaries(observed_fail, platform + " observed failure")
                if platform == "darwin":
                    check(observed_fail["ci_executed"] is True and observed_fail["producer_compiled"] is True,
                          "observed Darwin predicate dropped compile evidence after a failed member")
                else:
                    check(observed_fail["ci_executed"] is False, "Linux observed failure claimed ci_executed")

                observed_short = run_case(
                    members, short, work("observed-uncompiled"), prime_ok,
                    claim=True, producer=producer, execution_source="observed", platform=platform,
                )
                check(observed_short["_calls"]["compile"] == 0, platform + " observed uncompiled path compiled")
                check(observed_short["mac_producer_verified"] is False and observed_short["ci_executed"] is False,
                      platform + " observed uncompiled path claimed a Mac or CI result")
                boundaries(observed_short, platform + " observed uncompiled")

                raised = False
                try:
                    run_case(
                        members, library, work("observed-raise"), prime_raises,
                        claim=True, producer=producer, execution_source="observed", platform=platform,
                    )
                except RuntimeError as error:
                    raised = "member prime failed" in str(error)
                check(raised, platform + " observed exception became a verified report")

                refused = False
                try:
                    run_case(
                        members, library, work("bad-source"), prime_ok,
                        claim=True, producer=producer, execution_source="ci-true", platform=platform,
                    )
                except members.VerifyError as error:
                    refused = "execution_source" in str(error)
                check(refused, platform + " unknown execution source was accepted")

            omitted = None
            with patch.object(members.sys, "platform", "darwin"):
                omitted = members.run_explicit_members(
                    matching(),
                    os_mount=library.parent,
                    cryptex_mount=library.parent,
                    work=work("omitted"),
                    library_file=library,
                    producer=producer,
                    host_turnip=HOST,
                    target_turnip=TARGET,
                    peer=PEER,
                    gpu="a830",
                    verify_peer=lambda: None,
                    compile_producer=lambda: producer.write_bytes(b"fixture-producer"),
                    prime_one=prime_ok,
                    environment=environment,
                    pack_writer=lambda *_args, **_kwargs: None,
                    claim_host_execution=True,
                )
            check(omitted["execution_source"] == "fixture", "omitted source did not fail closed to fixture")
            check(omitted["mac_producer_verified"] is False, "fixture claimed mac_producer_verified")
            check(omitted["ci_executed"] is False, "fixture claimed ci_executed")

            linux_real = run_case(
                members, library, work("linux-real"), prime_ok,
                claim=True, producer=producer, execution_source="real", platform="linux",
            )
            check(linux_real["execution_source"] == "real" and linux_real["summary"]["passed"] is True,
                  "Linux real-source predicate did not collect eight members")
            check(linux_real["mac_producer_verified"] is False and linux_real["ci_executed"] is False,
                  "Linux real mode claimed a Mac result")
            boundaries(linux_real, "Linux real mode")

            unclaimed = run_case(
                members, library, work("darwin-unclaimed"), prime_ok,
                claim=False, producer=producer, execution_source="observed", platform="darwin",
            )
            check(unclaimed["mac_producer_verified"] is False and unclaimed["ci_executed"] is False,
                  "observed Darwin predicate certified without the caller claim")
            boundaries(unclaimed, "unclaimed observed")

            # Predicate only: this process did not run a Mac producer.
            simulated = run_case(
                members, library, work("darwin-observed"), prime_ok,
                claim=True, producer=producer, execution_source="observed", platform="darwin",
            )
            check(simulated["execution_source"] == "observed" and simulated["status"] == "explicit-members-observed",
                  "observed Darwin predicate lost the explicit source")
            check(simulated["mac_producer_verified"] is True and simulated["ci_executed"] is True,
                  "observed Darwin predicate stayed permanently false")
            check(simulated["_harness"] == "fixture-or-simulated-predicate",
                  "observed Darwin predicate was emitted as Mac evidence")
            boundaries(simulated, "observed Darwin predicate")
            real_alias = run_case(
                members, library, work("darwin-real-alias"), prime_ok,
                claim=True, producer=producer, execution_source="real", platform="darwin",
            )
            check(real_alias["execution_source"] == "real" and real_alias["mac_producer_verified"] is True,
                  "real-source Darwin predicate stayed permanently false")
            check(real_alias["_harness"] == "fixture-or-simulated-predicate",
                  "real-source predicate was emitted as Mac evidence")
            boundaries(real_alias, "real-source Darwin predicate")

            short_collection = [{"file_order": order, "status": "observed"} for order in members.EXPECTED_ORDERS[:-1]]
            incomplete_collection = False
            try:
                members.collect_members(short_collection)
            except members.VerifyError as error:
                incomplete_collection = "eight file orders" in str(error)
            check(incomplete_collection, "seven members were accepted as a complete collection")
    finally:
        members.load_member_manifest = saved
    after_report = real_report.read_bytes() if real_report.is_file() else None
    check(before_report == after_report, "fixture or simulated predicate wrote the real member report")
    check(sys.platform == host_platform, "simulated platform patch leaked onto the host")


def main() -> int:
    members = load("explicit_member_verify", "scripts/explicit-member-verify.py")
    manifest = members.load_member_manifest()
    check(tuple(item["file_order"] for item in manifest["members"]) == members.EXPECTED_ORDERS,
          "manifest is not the eight file orders")
    check(2 not in [item["file_order"] for item in manifest["members"]], "member 2 was listed")
    raw = json.loads((SCRIPTS / "data/dodge-explicit-members.json").read_text())
    check("name" not in json.dumps(raw), "member manifest contains a name field")
    check(all(set(item) == {"file_order", "member_sha256", "stage", "table_index"} for item in raw["members"]),
          "member manifest is not limited to SHA, index, and stage")

    listed = json.loads((SCRIPTS / "data/dodge-addon-components.json").read_text())["required_main_sources"]
    for relative in (
        "scripts/prime-member-policy.h",
        "scripts/producer-member-selection.c",
        "scripts/producer-member-selection.h",
        "scripts/fixtures/producer-member-parser.c",
        "scripts/test-pipeline-cache-member-priming.py",
        "scripts/pipeline_library_format.py",
        "scripts/pipeline-cache-format.h",
        "scripts/host_peer.py",
        "scripts/check-turnip-darwin-host.py",
        "scripts/make-pipeline-cache.py",
        "scripts/test-host-bridge-triple.py",
        "patches/adreno-metal/0003-explicit-member-frontend.patch",
    ):
        check(relative in listed and (ROOT / relative).is_file(), "release list missing " + relative)

    test_production_formatter()
    test_execution_source_matrix(members)

    with tempfile.TemporaryDirectory(prefix="explicit-member-ci-") as temporary:
        root = Path(temporary)
        library = root / "archive.metallib"
        library.write_bytes(b"fixture-library")
        # identity mismatch: do not call the producer, still collect eight failures, no pack
        producer = root / "pipeline-cache-host"
        mismatch = run_case(
            members, library, root / "mismatch",
            lambda *_args: success_result(manifest["members"][0]),
            producer=producer,
        )
        check(mismatch["_calls"]["prime"] == 0 and mismatch["_calls"]["compile"] == 0, "identity mismatch invoked the producer")
        check(mismatch["_calls"]["pack"] == 0 and mismatch["pack_written"] is False, "identity mismatch packed")
        check(mismatch["summary"]["members_collected"] == 8 and mismatch["summary"]["passed"] is False,
              "identity mismatch did not collect eight failures")
        check(mismatch["mac_producer_verified"] is False and mismatch["ci_executed"] is False,
              "identity mismatch claimed a Mac result")
        check(mismatch["execution_source"] == "fixture" and mismatch["status"] == "fixture-failed",
              "identity mismatch fixture was mixed into a real report")

        payload = b"x" * 768416
        digest = __import__("hashlib").sha256(payload).hexdigest()
        real = root / "real.metallib"
        real.write_bytes(payload)
        saved = members.load_member_manifest
        def matching_manifest():
            value = saved()
            value["outer_sha256"] = digest
            return value
        members.load_member_manifest = matching_manifest
        seen = []

        def prime_ok(run_dir, command, env, prime_list):
            seen.append((run_dir, list(command), dict(env), prime_list))
            order = int(command[command.index("--member-file-order") + 1])
            item = next(row for row in matching_manifest()["members"] if row["file_order"] == order)
            check(command[8:10] == ["0", "1"], "producer invocation was not one fresh library")
            check(command[-6:] == members.member_argv(order, digest, item["member_sha256"]),
                  "producer argv lost the member identity")
            check(env["HOSTSIM_PAGE_SIZE"] == "4096" and env["TU_DEBUG"] == "sysmem,nocb", "host contract env missing")
            check(env["DYLD_INSERT_LIBRARIES"] == str(PEER), "peer was not the built peer")
            check("runs/000" in str(run_dir), "run directory is not a fresh prime_one slot")
            return success_result(item)

        try:
            passed = run_case(members, real, root / "pass", prime_ok, claim=True, producer=producer)
        finally:
            members.load_member_manifest = saved
        check(len(seen) == 8, "positive fixture did not invoke every member")
        check(passed["_calls"]["peer"] == 1 and passed["_calls"]["compile"] == 1, "peer or producer compile was skipped")
        check(passed["_calls"]["pack"] == 0 and passed["pack_attempted"] is False, "positive fixture packed")
        check(passed["summary"]["passed"] is True and passed["summary"]["observed"] == 8, "eight observations were not counted")
        check(passed["mac_producer_verified"] is False, "fixture claimed mac_producer_verified")
        check(passed["ci_executed"] is False, "fixture claimed ci_executed")
        check(passed["execution_source"] == "fixture" and passed["status"] == "fixture",
              "fixture report was mixed into a real observation report")
        check(passed["driver_claimed"] is False and passed["cache_queue_drain_proven"] is False,
              "fixture upgraded a priming object to driver or drain proof")
        check(all(row["entered_request"] and row["table_index"] == 1 and not row["resumed"] for row in passed["members"]),
              "binding index 1 was not recorded for every member")

        members.load_member_manifest = matching_manifest
        calls = {"n": 0}

        def prime_one_bad(run_dir, command, env, prime_list):
            del run_dir, env, prime_list
            calls["n"] += 1
            order = int(command[command.index("--member-file-order") + 1])
            item = next(row for row in matching_manifest()["members"] if row["file_order"] == order)
            if order == 1:
                bad = success_result(item)
                bad["stdout"] = binding_line(1, "kernel", entered=False, disposition="resumed")
                bad["resumed"] = True
                bad["status"] = "primed"
                bad["summary"] = {"requested": 1, "primed": 1, "refused": 0, "absent": 0}
                return bad
            if order == 4:
                bad = success_result(item)
                bad["returncode"] = 3
                bad["status"] = "exit_failure"
                bad["summary"] = {"requested": 1, "primed": 0, "refused": 1, "absent": 0}
                return bad
            return success_result(item)

        try:
            failed = run_case(members, real, root / "fail", prime_one_bad, producer=producer)
        finally:
            members.load_member_manifest = saved
        check(calls["n"] == 8, "a member failure stopped the remaining members")
        check(failed["summary"]["passed"] is False and failed["summary"]["members_collected"] == 8,
              "failure collection did not keep all eight")
        check(failed["pack_written"] is False and failed["_calls"]["pack"] == 0, "failure path packed")
        by_order = {row["file_order"]: row for row in failed["members"]}
        check(by_order[1]["status"] == "failed" and by_order[1]["resumed"] is True, "resumed binding index 1 was accepted")
        check(by_order[4]["returncode"] == 3 and by_order[4]["status"] == "failed", "non-zero producer exit was ignored")
        check(by_order[0]["status"] == "observed" and by_order[8]["status"] == "observed",
              "unrelated members were not still collected")

    test_assess_exceptions(members)
    test_mounted_peer_order()

    workflow = (ROOT / ".github/workflows/addon.yml").read_text()
    check("validation_mode:" in workflow and "default: full-pack" in workflow and "- members-only" in workflow,
          "workflow has no explicit validation mode")
    check(re.search(
        r"name: Generate the real pinned 21H461 pipeline pack\n"
        r"\s+run: \|\n"
        r"\s+set -euo pipefail\n"
        r"\s+python3 scripts/ci-dodge-pipeline-cache\.py --mode generate",
        workflow,
    ) is not None, "default generate invocation changed")
    check("python3 scripts/ci-dodge-pipeline-cache.py --mode verify-members" in workflow,
          "members-only does not call the real verify mode")
    check("python3 scripts/make-gpu-addon.py" in workflow, "default pack command was removed")
    for name in (
        "Generate the real pinned 21H461 pipeline pack",
        "Build the Metal plugin",
        "Pack the addon with the pipeline cache",
        "Verify the addon carries the exact pack and priming list",
        "Upload the pipeline pack and identity reports",
        "Upload the addon",
    ):
        check("if: inputs.validation_mode != 'members-only'" in workflow, name + " can still run during members-only")
    check(workflow.count("uses: actions/upload-artifact@v4") == 3, "artifact upload count changed")
    check("if: always()" in workflow and "build/explicit-member-verify.json" in workflow,
          "member report is not always uploaded")
    check(".ipsw" not in workflow.split("build/explicit-member-verify.json", 1)[1][:500],
          "member artifact block gained an IPSW path")
    scripts = []
    lines = workflow.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)run: \|\s*$", line)
        if match is None:
            continue
        indent = len(match.group(1))
        body = []
        for child in lines[index + 1:]:
            if child.strip() and len(child) - len(child.lstrip()) <= indent:
                break
            body.append(child)
        scripts.append(textwrap.dedent("\n".join(body)) + "\n")
    check(scripts, "workflow has no shell blocks")
    for index, script in enumerate(scripts):
        shell = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        check(shell.returncode == 0, "workflow shell %d failed: %s" % (index, shell.stderr))

    ci = load("ci_dodge_pipeline_cache", "scripts/ci-dodge-pipeline-cache.py")
    check(ci.parser().parse_args([]).mode == "all", "CI default mode changed")
    source = (SCRIPTS / "ci-dodge-pipeline-cache.py").read_text()
    command = source.split("def generator_command", 1)[1].split("\ndef ", 1)[0]
    check("make-pipeline-cache.py" in command and "member-file-order" not in command,
          "default generator_command gained member selection")
    check("def verify_explicit_members" in source and "generator.compile_producer" in source
          and "generator.prime_one" in source and "establish_host_peer" in source
          and "write_pipeline_pack" not in source.split("def verify_explicit_members", 1)[1],
          "verify-members does not call the real compiler/prime_one or still packs")
    mounted = source.split("def _verify_members_mounted", 1)[1].split("\ndef verify_explicit_members", 1)[0]
    check(0 <= mounted.find("establish_host_peer") < mounted.find("verify_host_peer") < mounted.find("compile_producer"),
          "members path does not build the peer before verify and compile")
    check('execution_source="observed"' in mounted, "real members caller did not set an observed source")
    check("claim_host_execution=True" in mounted, "real members caller dropped the host-execution claim")
    caller = mounted.split("return members.run_explicit_members", 1)[1].split("\n\n", 1)[0]
    check("os.environ" not in caller and "getenv" not in caller,
          "real members caller takes execution source from the environment")
    generator = load("make_pipeline_cache_commands", "scripts/make-pipeline-cache.py")
    c_command, cxx_command = generator.producer_compile_commands(type("Inputs", (), {
        "work": Path("/tmp/producer-work"),
        "bridge": Path("/tmp/bridge.a"),
        "producer": Path("/tmp/pipeline-cache-host"),
    })())
    check(c_command[1] == "clang" and "-std=c11" in c_command and any(str(item).endswith("producer-member-selection.c") for item in c_command),
          "C selection source is not compiled as C")
    check("clang++" in cxx_command and "-std=c++17" in cxx_command
          and not any(str(item).endswith(".c") for item in cxx_command),
          "clang++ producer command still receives a .c file")

    if sys.platform != "darwin":
        report_path = Path(tempfile.mkdtemp(prefix="member-unverified-")) / "report.json"
        ran = subprocess.run(
            [sys.executable, str(SCRIPTS / "ci-dodge-pipeline-cache.py"), "--mode", "verify-members", "--report", str(report_path)],
            capture_output=True, text=True,
        )
        check(ran.returncode != 0, "Linux verify-members returned success")
        check("unverified" in (ran.stdout + ran.stderr).lower(), "Linux verify-members did not say unverified")
        written = json.loads(report_path.read_text())
        check(written["status"] == "unverified" and written["execution_source"] == "unverified"
              and written["ci_executed"] is False and written["mac_producer_verified"] is False,
              "Linux report claimed a Mac or CI result")
        check(written["pack_written"] is False and not (ROOT / "build/dodge.m2vpack").exists(),
              "Linux verify-members wrote a pack")
        report_path.unlink()
        report_path.parent.rmdir()

    host = (SCRIPTS / "pipeline-cache-host.mm").read_text()
    explicit = host.split("static int primeExplicitMember(", 1)[1].split("\nint main(", 1)[0]
    check("drain" not in explicit, "explicit producer claims a drain")
    check("bindingAt(pass, 1)" not in explicit, "generic explicit path still hard-requires binding index 1")
    check("printMemberBindings(" in explicit and "print_member_binding(" in host,
          "structured binding output was removed")
    policy = (SCRIPTS / "prime-member-policy.h").read_text()
    check('decision.outcome = "resumed"' in policy and "decision.primed = 1" in policy,
          "shared policy no longer accepts an all-resumed library")
    check("still < 20" in explicit and "usleep(500000)" in explicit, "counter stillness wait was retuned")
    upload = workflow.split("name: Upload compiled and generator logs", 1)[1].split("path: |", 1)[1].split("if-no-files-found: ignore", 1)[0]
    check("stdout.log" not in upload and "stderr.log" not in upload and "host-peer-build.log" not in upload,
          "member or peer-build logs are uploaded")
    verify_source = (SCRIPTS / "explicit-member-verify.py").read_text()
    check('(member_work / "stdout.log")' in verify_source and '(member_work / "stderr.log")' in verify_source,
          "member logs are no longer written")
    prime = (ROOT / "drivers/AdrenoMetal/PrimeShaders.h").read_text()
    loaded = prime.split("static PrimePass loadedLibrary(", 1)[1].split("static std::string library(", 1)[0]
    check(loaded.count("++pass.eligible") == 3, "eligible accounting changed")

    print("PASS explicit member CI fixtures: %d checks" % CHECKS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
