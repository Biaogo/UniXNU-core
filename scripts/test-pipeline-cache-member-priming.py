#!/usr/bin/env python3
"""Explicit-member priming regression.

Fixture bytes are not metallibs, not a Mac producer, and not a device result.
Objective-C sources are wiring guards only; this host does not compile them.
The success and identity cases compile and run scripts/prime-member-policy.h.
Parser positives and negatives compile and run scripts/producer-member-selection.c
against the in-repo fixture. Build output is a TemporaryDirectory under TMPDIR.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
KIT = ROOT / "drivers/AdrenoMetal"
POLICY = SCRIPTS / "prime-member-policy.h"
PARSER_FIXTURE = SCRIPTS / "fixtures" / "producer-member-parser.c"

# Fixture digests. Not a measured outer or member.
FIXTURE_OUTER = "11" * 32
FIXTURE_OUTER_OTHER = "22" * 32
FIXTURE_MEMBER = "33" * 32
FIXTURE_MEMBER_OTHER = "44" * 32

checks = 0


def load_generator():
    spec = importlib.util.spec_from_file_location(
        "pipeline_cache_member_priming_generator", SCRIPTS / "make-pipeline-cache.py"
    )
    if spec is None or spec.loader is None:
        raise SystemExit("FAIL: could not load make-pipeline-cache.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def check(condition: object, message: str) -> None:
    global checks
    if not condition:
        raise SystemExit("FAIL: " + message)
    checks += 1


def assert_under_tmpdir(path: Path) -> None:
    tmpdir = os.environ.get("TMPDIR")
    if not tmpdir:
        return
    root = Path(tmpdir).resolve()
    resolved = path.resolve()
    check(resolved == root or root in resolved.parents, "build output is not under TMPDIR: " + str(path))


def test_self_contained() -> None:
    text = Path(__file__).read_text()
    home = "/" + "home" + "/" + "biaogo"
    check(home not in text, "formal test hardcodes a home path")
    check("dodge-" + "producer-member-entry" not in text, "formal test still points at the old scratch parser")
    check("dodge-" + "member-priming" not in text and "HARNESS_" + "DIR" not in text,
          "formal test still uses the old scratch harness directory")
    check(PARSER_FIXTURE.is_file(), "in-repo parser fixture is missing")
    check(POLICY.is_file(), "shared policy header is missing")
    fixture = PARSER_FIXTURE.read_text()
    check(home not in fixture, "parser fixture hardcodes a home path")
    check("producer_member_selection_parse" in fixture, "parser fixture does not call the production parser")
    check("SKIP " + "parser" not in text, "formal parser test can still skip")


def function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    for index, char in enumerate(source[brace:], brace):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise SystemExit("FAIL: unclosed " + signature)


def method_body(source: str, signature: str) -> str:
    start = source.index(signature)
    nxt = source.find("\n- (", start + len(signature))
    if nxt < 0:
        nxt = source.find("\n// ", start + len(signature))
    return source[start:nxt if nxt > 0 else start + 500]


generator: Any = load_generator()


def test_parser_argv_stamp() -> None:
    with tempfile.TemporaryDirectory(prefix="fixture-member-priming-") as temporary:
        root = Path(temporary)
        one = root / "fixture-one"
        one.write_bytes(b"fixture-not-a-metallib\n")
        os_mount = root / "os"
        cryptex = root / "cryptex"
        (os_mount / "System/Library").mkdir(parents=True)
        (cryptex / "System/Library").mkdir(parents=True)
        library = os_mount / "System/Library/FixtureArchive.metallib"
        library.write_bytes(b"fixture-not-a-metallib\n")
        prime_list = root / "prime-list.txt"
        listed = "/System/Library/FixtureArchive.metallib"
        prime_list.write_text(listed + "\n")
        output = root / "fixture-out.m2vpack"
        args = generator.parser().parse_args([
            "--os-mount", str(os_mount),
            "--cryptex-mount", str(cryptex),
            "--output", str(output),
            "--prime-list", str(prime_list),
            "--os-build", "fixture-os",
            "--member-file-order", "0",
            "--member-outer-sha256", FIXTURE_OUTER,
            "--member-sha256", FIXTURE_MEMBER,
        ])
        argv = generator.member_argv(args)
        check(argv[:2] == ["--member-file-order", "0"], "parser did not keep file order")
        check(FIXTURE_OUTER in argv and FIXTURE_MEMBER in argv, "parser did not keep both fixture digests")
        check(generator.member_argv(SimpleNamespace(
            member_file_order=None, member_outer_sha256=None, member_sha256=None
        )) == [], "absent flags invented a member")
        try:
            generator.member_argv(SimpleNamespace(
                member_file_order="0", member_outer_sha256=None, member_sha256=FIXTURE_MEMBER
            ))
        except SystemExit as error:
            check("requires --member-file-order" in str(error), "partial flags were not refused")
        else:
            raise SystemExit("FAIL: partial member flags were accepted")
        check(True, "partial flag rejection counted")

        producer = root / "fixture-producer"
        producer.write_bytes(b"fixture producer\n")
        inputs = SimpleNamespace(
            producer=producer, host_turnip=one, target_turnip=one, peer=one, bridge=one,
            prime_list=prime_list, host_profile=one, target_profile=one, peer_original=one,
            peer_source=one, peer_patch=one, peer_helper=one, peer_manifest=one,
        )
        environment = {
            "DYLD_INSERT_LIBRARIES": "fixture", "HOSTSIM_GPU": "a830", "HOSTSIM_PAGE_SIZE": "4096",
            "MESA_LOG": "", "MESA_SHADER_CACHE_DISABLE": "true", "TU_DEBUG": "sysmem,nocb",
        }
        absent = generator.stamp(producer, inputs, environment, "fixture-os")
        inputs.member_argv = []
        check(generator.stamp(producer, inputs, environment, "fixture-os") == absent,
              "empty member argv changed the no-selection stamp")
        check("member_argv=" not in absent and absent.endswith("os_build=fixture-os\n"),
              "no-selection stamp no longer ends at os_build")
        inputs.member_argv = argv
        explicit = generator.stamp(producer, inputs, environment, "fixture-os")
        check(explicit != absent and explicit.endswith("member_argv=" + "\t".join(argv) + "\n"),
              "explicit stamp did not append the member argv after os_build")
        inputs.member_argv = ["--member-file-order", "1", "--member-outer-sha256", FIXTURE_OUTER,
                              "--member-sha256", FIXTURE_MEMBER_OTHER]
        other = generator.stamp(producer, inputs, environment, "fixture-os")
        check(other != explicit, "a different member reused the same stamp")

        paths = generator.library_paths(
            prime_list, os_mount, cryptex, "ios", "21H461", argv
        )
        check(paths == [listed], "explicit single list was filtered or rewritten")
        try:
            generator.library_paths(prime_list, os_mount, cryptex, "ios", "21H461", ())
        except SystemExit as error:
            check("expected 166" in str(error), "default 21H461 one-entry list no longer requires 166")
        else:
            raise SystemExit("FAIL: default one-entry 21H461 list was accepted")
        check(True, "166 regression counted")
        many = root / "many.txt"
        many.write_text(listed + "\n/System/Library/OtherFixture.metallib\n")
        try:
            generator.library_paths(many, os_mount, cryptex, "ios", "21H461", argv)
        except SystemExit as error:
            text = str(error)
            check("exactly one library, got 2" in text and "no pack" in text,
                  "multi-library explicit selection was not refused")
            check("expected 166" not in text, "explicit multi-library refusal rewrote the 166 check")
        else:
            raise SystemExit("FAIL: explicit selection accepted two libraries")
        check(True, "multi-library refusal counted")


def test_main_control_flow() -> None:
    with tempfile.TemporaryDirectory(prefix="fixture-member-priming-main-") as temporary:
        root = Path(temporary)
        work = root / "work"
        work.mkdir()
        producer = work / "pipeline-cache-host"
        producer.write_bytes(b"fixture producer\n")
        prime_list = root / "prime-list.txt"
        paths = ["/System/Library/Fixture%03d.metallib" % index for index in range(166)]
        prime_list.write_text("".join(path + "\n" for path in paths))
        output = root / "fixture.m2vpack"
        args = SimpleNamespace(
            target="ios", gpu="a830", jobs=1, pack=r"/Fixture000", output=output, os_build="fixture-os",
        )
        member_argv = [
            "--member-file-order", "0",
            "--member-outer-sha256", FIXTURE_OUTER,
            "--member-sha256", FIXTURE_MEMBER,
        ]
        inputs = SimpleNamespace(
            host_turnip=producer, target_turnip=producer, host_profile=producer, target_profile=producer,
            peer=producer, bridge=producer, prime_list=prime_list, work=work, producer=producer,
            member_argv=member_argv, peer_original=producer, peer_source=producer, peer_patch=producer,
            peer_helper=producer, peer_manifest=producer,
        )
        identity = {
            "os_build": "fixture-os",
            "target_uuid": "00112233445566778899aabbccddeeff",
            "turnip_build_id": "fixture",
            "paths": paths,
            "selected": paths[:1],
            "os_mount": root / "os",
            "cryptex_mount": root / "cryptex",
        }
        pack_calls = 0
        library_calls = 0
        original = {
            "parser": generator.parser,
            "resolve_inputs": generator.resolve_inputs,
            "preflight": generator.preflight,
            "run_libraries": generator.run_libraries,
            "merge_cache": generator.merge_cache,
            "write_pipeline_pack": generator.write_pipeline_pack,
        }

        def refuse_pack(*_args, **_kwargs):
            nonlocal pack_calls
            pack_calls += 1
            raise SystemExit("pack should not run")

        def fake_libraries(*_args, **_kwargs):
            nonlocal library_calls
            library_calls += 1
            raise SystemExit("priming should not start for a multi-library explicit list")

        try:
            generator.parser = lambda: SimpleNamespace(parse_args=lambda: args)
            generator.resolve_inputs = lambda _args: inputs
            generator.preflight = lambda _args, _inputs: identity
            generator.run_libraries = fake_libraries
            generator.merge_cache = lambda *_a, **_k: []
            generator.write_pipeline_pack = refuse_pack
            try:
                generator.main()
            except SystemExit as error:
                check("exactly one library, got 166" in str(error) and "no pack" in str(error),
                      "main did not refuse a 166-entry explicit list")
            else:
                raise SystemExit("FAIL: main accepted explicit selection for 166 libraries")
            check(pack_calls == 0 and library_calls == 0 and not output.exists(),
                  "refused explicit list started priming or packing")
        finally:
            for name, value in original.items():
                setattr(generator, name, value)

    with tempfile.TemporaryDirectory(prefix="fixture-member-priming-fail-") as temporary:
        root = Path(temporary)
        work = root / "work"
        work.mkdir()
        producer = work / "pipeline-cache-host"
        producer.write_bytes(b"fixture producer\n")
        one = root / "fixture-one"
        one.write_bytes(b"fixture-not-a-metallib\n")
        prime_list = root / "prime-list.txt"
        listed = "/System/Library/FixtureArchive.metallib"
        prime_list.write_text(listed + "\n")
        output = root / "fixture.m2vpack"
        args = SimpleNamespace(
            target="ios", gpu="a830", jobs=1, pack=r"FixtureArchive", output=output, os_build="fixture-os",
        )
        member_argv = [
            "--member-file-order", "0",
            "--member-outer-sha256", FIXTURE_OUTER,
            "--member-sha256", FIXTURE_MEMBER,
        ]
        inputs = SimpleNamespace(
            host_turnip=one, target_turnip=one, host_profile=one, target_profile=one,
            peer=one, bridge=one, prime_list=prime_list, work=work, producer=producer,
            member_argv=member_argv, peer_original=one, peer_source=one, peer_patch=one,
            peer_helper=one, peer_manifest=one,
        )
        identity = {
            "os_build": "fixture-os",
            "target_uuid": "00112233445566778899aabbccddeeff",
            "turnip_build_id": "fixture",
            "paths": [listed],
            "selected": [listed],
            "os_mount": root / "os",
            "cryptex_mount": root / "cryptex",
        }
        seen = []
        pack_calls = 0

        def fake_run(command, **_kwargs):
            seen.append(list(command))
            if "--validate-loads" in command:
                stdout = (
                    "LOAD_LIBRARY: " + json.dumps({
                        "index": 0, "path": listed, "status": "loaded",
                        "header64_hex": "00", "header_bytes": 1,
                        "error_domain": None, "error_code": None, "error_description": None,
                        "pipelines_compiled": False, "shader_pack_produced": False,
                    }) + "\n"
                    "LOAD_RESULT: requested=1 loaded=1 refused=0 absent=0 "
                    "pipelines_compiled=0 shader_pack_produced=0\n"
                )
                return subprocess.CompletedProcess(command, 0, stdout, "")
            check(command[8:10] == ["0", "1"], "explicit prime invocation was not one library")
            check(command[-6:] == member_argv, "explicit prime invocation did not forward the member argv")
            stdout = (
                "RESULT: requested=1 primed=0 refused=1 absent=0 device=fixture outcome=refused "
                "layer=priming-request driver_pipeline_unproven=1\n"
                "FAIL: requested=1 primed=0 refused=1 absent=0\n"
            )
            return subprocess.CompletedProcess(command, 1, stdout, listed + " explicit member refused: fixture\n")

        original = {
            "parser": generator.parser,
            "resolve_inputs": generator.resolve_inputs,
            "preflight": generator.preflight,
            "compile_producer": generator.compile_producer,
            "runtime_environment": generator.runtime_environment,
            "subprocess": generator.subprocess,
            "write_pipeline_pack": generator.write_pipeline_pack,
            "merge_cache": generator.merge_cache,
        }

        def fake_pack(*_args, **_kwargs):
            nonlocal pack_calls
            pack_calls += 1
            return SimpleNamespace(stream_bytes=1, packed_bytes=1, files=["fixture"], root="fixture")

        try:
            generator.parser = lambda: SimpleNamespace(parse_args=lambda: args)
            generator.resolve_inputs = lambda _args: inputs
            generator.preflight = lambda _args, _inputs: identity
            generator.compile_producer = lambda _args, _inputs: producer
            generator.runtime_environment = lambda _args, _inputs: {"HOSTSIM_GPU": "a830"}
            generator.subprocess = SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired)
            generator.write_pipeline_pack = fake_pack
            generator.merge_cache = lambda *_a, **_k: (_ for _ in ()).throw(SystemExit("merge on failure"))
            status = generator.main()
        finally:
            for name, value in original.items():
                setattr(generator, name, value)
        check(status == 1 and pack_calls == 0 and not output.exists(),
              "explicit prime failure packed or returned success")
        report = json.loads(output.with_name(output.name + ".json").read_text())
        check(report["primed"] == 0 and report["pack_produced"] is False and report["failed"] == 1,
              "failure collection marked an explicit refusal as primed or packed")
        check(any("--validate-loads" in command for command in seen), "load preflight was skipped")
        check(any(command[8:10] == ["0", "1"] for command in seen if "--validate-loads" not in command),
              "failure collection did not invoke the one-library producer")


def test_policy_binary() -> None:
    with tempfile.TemporaryDirectory(prefix="member-priming-policy-") as temporary:
        _run_policy_binary(Path(temporary))


def _run_policy_binary(root: Path) -> None:
    assert_under_tmpdir(root)
    harness = root / "policy-harness.cpp"
    binary = root / "policy-harness"
    harness.write_text(r"""
#include "prime-member-policy.h"
#include <stdio.h>
#include <string.h>
static int checks, fails;
static void expect(int condition, const char *message) {
    if (!condition) { fprintf(stderr, "FAIL %s\n", message); fails++; return; }
    checks++;
}
static void fill(uint8_t out[32], uint8_t value) { for (int i = 0; i < 32; i++) out[i] = value; }
int main() {
    uint8_t outer[32], member[32], other_outer[32], other_member[32];
    fill(outer, 0x11); fill(member, 0x33); fill(other_outer, 0x22); fill(other_member, 0x44);
    char a[160], b[160], c[160], d[160];
    expect(prime_member_identity_suffix(0, outer, member, a, sizeof a) == 0, "suffix a");
    expect(prime_member_identity_suffix(0, outer, member, b, sizeof b) == 0, "suffix b");
    expect(strcmp(a, b) == 0, "same request suffix");
    expect(prime_member_identity_suffix(1, outer, other_member, c, sizeof c) == 0, "suffix c");
    expect(prime_member_identity_suffix(0, other_outer, member, d, sizeof d) == 0, "suffix d");
    expect(strcmp(a, c) != 0 && strcmp(a, d) != 0, "member identity not isolated");
    expect(strstr(a, "function") == NULL && strchr(a, '\t') == NULL, "suffix contains a function or tab");
    std::string id0 = prime_member_library_identity("/fixture/archive.metallib", a);
    std::string id1 = prime_member_library_identity("/fixture/archive.metallib", c);
    std::string key0 = prime_member_function_key(id0, "same_public_name");
    std::string key1 = prime_member_function_key(id1, "same_public_name");
    expect(key0 != key1, "same public name collided across members");
    expect(prime_member_function_done_key(key0, "build") != prime_member_function_done_key(key1, "build"),
           "done keys collided");
    expect(prime_member_library_done_key(id0, 1, 2, "build") != prime_member_library_done_key(id1, 1, 2, "build"),
           "library done keys collided");
    expect(prime_member_function_key("/fixture/archive.metallib", "same_public_name") ==
               std::string("/fixture/archive.metallib\tsame_public_name"),
           "default function key changed");
    expect(prime_member_record_function_done(0, 0) == 1, "default path stopped recording a failed attempt");
    expect(prime_member_record_function_done(1, 0) == 0, "explicit failure entered success functions");
    expect(prime_member_record_function_done(1, 1) == 1, "explicit success was not recordable");
    char error[128];
    expect(prime_member_range_ok(0, 1, 1, error, sizeof error) == 0, "one library rejected");
    expect(prime_member_range_ok(0, 0, 1, error, sizeof error) != 0, "count 0 accepted");
    expect(prime_member_range_ok(0, 2, 2, error, sizeof error) != 0, "count 2 accepted");
    expect(prime_member_range_ok(1, 1, 1, error, sizeof error) != 0, "overflow index accepted");
    expect(prime_member_range_ok(0, 1, 0, error, sizeof error) != 0, "empty list accepted");
    PrimeMemberCounts counts = {};
    counts.loaded = 1; counts.functions = 2; counts.eligible = 2; counts.attempted = 2; counts.made = 2;
    PrimeMemberDecision made = prime_member_explicit_decision(counts);
    expect(made.write_library_done == 1 && made.primed == 1 && strcmp(made.outcome, "made") == 0,
           "complete kernel/vertex success");
    counts = {};
    counts.loaded = 1; counts.functions = 2; counts.eligible = 1; counts.nonpipeline = 1;
    counts.attempted = 1; counts.made = 1;
    PrimeMemberDecision paired = prime_member_explicit_decision(counts);
    expect(paired.write_library_done == 1 && paired.primed == 1 && strcmp(paired.outcome, "made") == 0,
           "legal vertex+fragment pair");
    counts = {};
    counts.loaded = 1; counts.functions = 4; counts.eligible = 2; counts.nonpipeline = 2; counts.resumed = 2;
    PrimeMemberDecision resumed = prime_member_explicit_decision(counts);
    expect(resumed.write_library_done == 1 && resumed.primed == 1 && strcmp(resumed.outcome, "resumed") == 0,
           "all eligible resumed beside fragments");
    counts = {};
    counts.loaded = 1; counts.functions = 3; counts.eligible = 3; counts.resumed = 3;
    expect(prime_member_explicit_decision(counts).write_library_done == 1 &&
               prime_member_explicit_decision(counts).primed == 1 &&
               strcmp(prime_member_explicit_decision(counts).outcome, "resumed") == 0,
           "all resumed");
    counts = {};
    counts.loaded = 1; counts.functions = 3; counts.eligible = 3; counts.resumed = 1;
    counts.attempted = 2; counts.made = 2;
    expect(strcmp(prime_member_explicit_decision(counts).outcome, "made") == 0 &&
               prime_member_explicit_decision(counts).write_library_done == 1,
           "resumed plus new made");
    counts = {};
    counts.loaded = 1; counts.functions = 3; counts.nonpipeline = 3;
    PrimeMemberDecision fragments = prime_member_explicit_decision(counts);
    expect(fragments.write_library_done == 0 && fragments.primed == 0 && strcmp(fragments.outcome, "empty") == 0,
           "pure fragment/dependency counted as primed");
    counts = {}; counts.loaded = 1;
    expect(strcmp(prime_member_explicit_decision(counts).outcome, "empty") == 0 &&
               prime_member_explicit_decision(counts).write_library_done == 0, "empty wrote done");
    counts = {};
    counts.loaded = 1; counts.eligible = 1; counts.attempted = 1; counts.made = 1;
    PrimeMemberDecision invented = prime_member_explicit_decision(counts);
    expect(invented.write_library_done == 0 && invented.primed == 0 && strcmp(invented.outcome, "made") != 0,
           "functions 0 but made 1");
    counts = {};
    counts.loaded = 1; counts.attempted = 1; counts.made = 1;
    expect(prime_member_explicit_decision(counts).write_library_done == 0 &&
               strcmp(prime_member_explicit_decision(counts).outcome, "made") != 0,
           "loaded/functions0/attempted1/made1 wrote done");
    counts = {};
    counts.loaded = 1; counts.functions = 2; counts.eligible = 3; counts.resumed = 3;
    PrimeMemberDecision too_many = prime_member_explicit_decision(counts);
    expect(too_many.write_library_done == 0 && too_many.primed == 0 && strcmp(too_many.outcome, "resumed") != 0,
           "resumed greater than functions");
    counts = {};
    counts.loaded = 1; counts.functions = 2; counts.nonpipeline = 1; counts.attempted = 1; counts.made = 1;
    PrimeMemberDecision missed = prime_member_explicit_decision(counts);
    expect(missed.write_library_done == 0 && missed.primed == 0 && strcmp(missed.outcome, "made") != 0,
           "missed eligible");
    counts = {};
    counts.loaded = 1; counts.functions = 10; counts.eligible = 1; counts.resumed = 1;
    expect(prime_member_explicit_decision(counts).write_library_done == 0 &&
               strcmp(prime_member_explicit_decision(counts).outcome, "resumed") != 0,
           "unaccounted functions");
    counts = {};
    counts.loaded = 1; counts.functions = 10; counts.resumed = 1;
    expect(prime_member_explicit_decision(counts).write_library_done == 0 &&
               strcmp(prime_member_explicit_decision(counts).outcome, "resumed") != 0,
           "functions10/resumed1/attempted0 wrote done");
    counts = {};
    counts.loaded = 1; counts.functions = 2; counts.eligible = 2; counts.attempted = 2; counts.made = 2;
    counts.nonpipeline = 1;
    expect(prime_member_explicit_decision(counts).write_library_done == 0 &&
               strcmp(prime_member_explicit_decision(counts).outcome, "made") != 0,
           "illegal overcount");
    counts = {};
    counts.loaded = 1; counts.functions = 2; counts.eligible = 2; counts.attempted = 2; counts.made = 1;
    counts.refused = 1;
    expect(strcmp(prime_member_explicit_decision(counts).outcome, "partial") == 0 &&
               prime_member_explicit_decision(counts).write_library_done == 0,
           "partial failure wrote done");
    counts = {};
    counts.loaded = 1; counts.functions = 1; counts.eligible = 1; counts.attempted = 1; counts.refused = 1;
    expect(strcmp(prime_member_explicit_decision(counts).outcome, "refused") == 0 &&
               prime_member_explicit_decision(counts).write_library_done == 0,
           "pipeline refusal double-counted or accepted");
    counts = {};
    counts.loaded = 1; counts.functions = 2; counts.eligible = 1; counts.refused_before = 1; counts.refused = 1;
    counts.attempted = 1; counts.made = 1;
    expect(strcmp(prime_member_explicit_decision(counts).outcome, "partial") == 0 &&
               prime_member_explicit_decision(counts).write_library_done == 0,
           "refused-before-attempt was dropped or double-counted");
    counts = {};
    counts.loaded = 1; counts.functions = 1; counts.eligible = 1; counts.attempted = 1; counts.refused = 1;
    counts.exceptions = 1;
    expect(strcmp(prime_member_explicit_decision(counts).outcome, "exception") == 0 &&
               prime_member_explicit_decision(counts).write_library_done == 0, "exception wrote done");
    counts = {};
    counts.loaded = 1; counts.functions = 2; counts.eligible = 2; counts.skipped = 1; counts.attempted = 1;
    counts.made = 1;
    expect(strcmp(prime_member_explicit_decision(counts).outcome, "skipped") == 0 &&
               prime_member_explicit_decision(counts).write_library_done == 0, "skipped wrote done");
    counts = {}; counts.loaded = 1; counts.functions = 4;
    expect(prime_member_explicit_decision(counts).write_library_done == 0 &&
               strcmp(prime_member_explicit_decision(counts).outcome, "empty") != 0,
           "unaccounted functions were called empty");
    counts = {};
    expect(prime_member_explicit_decision(counts).write_library_done == 0, "nil library wrote done");
    counts = {}; counts.loaded = 1; counts.made = 4; counts.attempted = 1;
    expect(prime_member_explicit_decision(counts).write_library_done == 0 &&
               strcmp(prime_member_explicit_decision(counts).outcome, "made") != 0,
           "inconsistent made was swallowed");
    if (fails) return 1;
    printf("PASS policy checks=%d\n", checks);
    return 0;
}
""")
    compile_command = [
        "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(SCRIPTS),
        str(harness), "-o", str(binary),
    ]
    compiled = subprocess.run(compile_command, capture_output=True, text=True)
    check(compiled.returncode == 0, "policy helper failed to compile: " + compiled.stderr[-500:])
    ran = subprocess.run([str(binary)], capture_output=True, text=True)
    check(ran.returncode == 0, "policy helper cases failed: " + ran.stderr)
    check("PASS policy checks=" in ran.stdout, "policy helper did not report its count")
    print(ran.stdout.strip())


def test_source_guards() -> None:
    print("NON-MAC source guard: not an Objective-C compile and not a producer process")
    frontend_h = (KIT / "NativeBridgeMetalFrontend.h").read_text()
    frontend = (KIT / "NativeBridgeMetalFrontend.mm").read_text()
    prime = (KIT / "PrimeShaders.h").read_text()
    host = (SCRIPTS / "pipeline-cache-host.mm").read_text()
    declaration = function_body  # silence unused if refactoring fails
    del declaration
    check(
        "id native_bridge_metal_library_with_member(const void *bytes, size_t length, uint32_t file_order,"
        in frontend_h and "Not automatic selection" in frontend_h,
        "explicit helper declaration is incomplete or does not say it is not automatic selection",
    )
    helper = function_body(frontend, "id native_bridge_metal_library_with_member(")
    check(helper.count("nmb_library_new_member(") == 1, "explicit helper does not call the member FFI once")
    check("nmb_library_release(" not in helper, "explicit helper releases the handle itself")
    check("if(!handle)return nil;" in helper, "nil path does not return before release")
    check(frontend.count("nmb_library_new_member(") == 1, "member FFI escaped the explicit helper")
    for signature in (
        "- (id<MTLLibrary>)newLibraryWithData:",
        "- (id<MTLLibrary>)newLibraryWithFile:",
        "- (id<MTLLibrary>)newLibraryWithURL:",
    ):
        body = method_body(frontend, signature)
        check("nmb_library_new_member(" not in body, signature + " now calls member selection")
    air = function_body(frontend, "id native_bridge_metal_library_with_air(")
    check("nmb_library_new(" in air and "nmb_library_new_member(" not in air, "bare AIR helper changed")
    loaded = function_body(prime, "static PrimePass loadedLibrary(")
    library = function_body(prime, "static std::string library(")
    check("newLibraryWithURL:" not in loaded, "shared core reopens a URL")
    check("newLibraryWithURL:" in library and "loadedLibrary(" in library and ", false);" in library,
          "default path no longer loads the original URL and calls the core without success-only")
    check("appendLine(functionsPath, doneKey);" not in prime or "prime_member_record_function_done" in loaded,
          "function-done recording no longer goes through the shared policy")
    check("prime_member_function_key(identity," in loaded, "shared core dropped the identity key")
    explicit = function_body(host, "static int primeExplicitMember(")
    check("native_bridge_metal_library_with_member(" in explicit, "explicit producer does not wrap once")
    check("prime::loadedLibrary(" in explicit and ", true);" in explicit, "explicit producer misses the shared core")
    check("newLibraryWithURL:" not in explicit and "prime::library(" not in explicit,
          "explicit producer reopens the default library")
    check("nmb_library_new_member(" not in explicit and "nmb_library_release(" not in explicit,
          "explicit producer load/releases instead of handing the wrapper one handle")
    check(".find(" not in explicit and "library refused:" not in explicit,
          "explicit success is parsed from a log string")
    check("decision.write_library_done" in explicit and "prime_member_library_done_key" in explicit,
          "explicit done key ignores the structured decision")
    validate = host.split("if (member.validate_only)", 1)[1].split("return primeExplicitMember", 1)[0]
    check("loadExplicitMember(" in validate and "prime::loadedLibrary(" not in validate
          and "prime-done" not in validate, "validate-only now primes or writes done")
    check(host.index("if (validateOnly)") < host.index("prime::library("),
          "default validation is no longer ahead of the URL priming call")
    check('unsigned primed = 0, refused = 0, absent = 0;' in host, "default failure counters were removed")
    check("++pass.nonpipeline" in loaded, "shared core does not count nonpipeline entries")
    check("++pass.refused_before" in loaded, "shared core folds pre-attempt refusal into attempted")
    check(loaded.count("++pass.eligible") == 3, "eligible is not exactly skip, resume, and attempt")
    check(
        loaded.index("++pass.skipped") < loaded.index("++pass.resumed") < loaded.index("++pass.refused_before")
        < loaded.index("++pass.nonpipeline") < loaded.index("++pass.attempted"),
        "accounting left the loadedLibrary branch order",
    )
    check(
        " kernels=%u renders=%u made=%u half=%u zeroed=%u linked=%u refused=%u skipped=%u ms=%llu waited=%llu"
        in prime,
        "default library log line changed",
    )
    check("counts.functions = pass.functions;" in explicit, "explicit path dropped functions")
    check("counts.eligible = pass.eligible;" in explicit, "explicit path does not pass eligible")
    check("counts.nonpipeline = pass.nonpipeline;" in explicit, "explicit path does not pass nonpipeline")
    check("counts.refused_before = pass.refused_before;" in explicit, "explicit path does not pass refused_before")
    check("print_explicit_result(" in explicit and "driver_pipeline_unproven=1" in (SCRIPTS / "pipeline-cache-format.h").read_text(),
          "explicit result no longer marks driver/cache unproven")
    check("drain" not in explicit, "explicit path claims a drain")
    check("still < 20" in explicit and "usleep(500000)" in explicit,
          "explicit counter-stillness wait was retuned")
    policy = POLICY.read_text()
    decision = function_body(policy, "inline PrimeMemberDecision prime_member_explicit_decision(")
    consistent = function_body(policy, "inline int prime_member_counts_consistent(")
    check("prime_member_counts_consistent" in decision, "decision does not check the partition")
    check("counts.eligible == 0" in decision, "zero eligible can still succeed")
    check(
        "counts.functions" in consistent and "counts.eligible" in consistent
        and "counts.nonpipeline" in consistent and "counts.refused_before" in consistent,
        "partition helper dropped a count class",
    )
    check("counts.refused - counts.refused_before" in consistent,
          "pipeline refusals are not kept inside attempted")
    print(
        "NOTE: an older scratch guard required Frontend to contain no member FFI call. "
        "That guard expired when this explicit helper was added. It is not a product bug. "
        "The current guard requires one member FFI call, only in the explicit helper."
    )
    print("NOTE: explicit 10s counter stillness is not drain completion; driver and cache remain unproven")


def test_parser_binary() -> None:
    check(PARSER_FIXTURE.is_file(), "in-repo parser fixture is missing")
    with tempfile.TemporaryDirectory(prefix="member-priming-parser-") as temporary:
        root = Path(temporary)
        assert_under_tmpdir(root)
        binary = root / "parser-harness"
        compiled = subprocess.run(
            ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(SCRIPTS),
             str(PARSER_FIXTURE), str(SCRIPTS / "producer-member-selection.c"), "-o", str(binary)],
            capture_output=True, text=True,
        )
        check(compiled.returncode == 0, "production parser test failed to compile: " + compiled.stderr[-500:])
        ran = subprocess.run([str(binary)], capture_output=True, text=True)
        check(ran.returncode == 0, "production parser test failed: " + (ran.stdout + ran.stderr)[-500:])
        check("PASS parser checks=" in ran.stdout, "parser fixture did not report its count")
        print(ran.stdout.strip())


def main() -> int:
    test_self_contained()
    test_parser_argv_stamp()
    test_main_control_flow()
    test_policy_binary()
    test_source_guards()
    test_parser_binary()
    print("PASS fixture member priming checks=%d" % checks)
    print("Mac producer compile and process remain unverified on this host")
    print("Explicit 10s counter stillness is not drain completion; driver and cache remain unproven")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
