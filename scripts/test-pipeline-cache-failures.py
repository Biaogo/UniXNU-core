#!/usr/bin/env python3
"""Control-flow regression for pipeline-cache producer failures.

The producer is replaced with deterministic subprocess fixtures. These cases do
not compile Metal, open an Apple metallib, or claim target GPU execution.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
REFUSED = {10, 11, 12, 13, 23, 31, 34, 36, 38, 40, 115, 153}


def load_generator():
    spec = importlib.util.spec_from_file_location(
        "pipeline_cache_failure_generator", SCRIPTS / "make-pipeline-cache.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def require(condition: object, message: str) -> None:
    if not condition:
        raise SystemExit("FAIL: " + message)


generator = load_generator()


with tempfile.TemporaryDirectory(prefix="pipeline-cache-stamp-") as temporary:
    root = Path(temporary)
    producer = root / "producer"
    producer.write_bytes(b"producer one")
    one = root / "one"
    one.write_bytes(b"one")
    stamp_inputs = SimpleNamespace(
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
        "DYLD_INSERT_LIBRARIES": "one",
        "HOSTSIM_GPU": "a830",
        "HOSTSIM_PAGE_SIZE": "4096",
        "MESA_LOG": "",
        "MESA_SHADER_CACHE_DISABLE": "true",
        "TU_DEBUG": "sysmem,nocb",
    }
    before = generator.stamp(producer, stamp_inputs, environment, "21H461")
    producer.write_bytes(b"producer two")
    after = generator.stamp(producer, stamp_inputs, environment, "21H461")
    require(before != after, "producer content did not invalidate the pipeline-cache stamp")
    stamped = {path.resolve() for path in generator.stamp_inputs(stamp_inputs)}
for semantic_source in (
    SCRIPTS / "make-pipeline-cache.py",
    SCRIPTS / "pipeline-cache-host.mm",
    ROOT / "drivers/AdrenoMetal/PrimeShaders.h",
    ROOT / "drivers/AdrenoMetal/NativeBridgeMetalFrontend.mm",
    ROOT / "drivers/AdrenoMetal/bridge/NativeMetalObjects.rs",
):
    require(
        semantic_source.resolve() in stamped,
        "semantic source is missing from the pipeline-cache stamp: " + str(semantic_source),
    )


def fixture_paths(count: int) -> list[str]:
    return ["/System/Library/Fixture%03d.metallib" % index for index in range(count)]


def make_inputs(root: Path, count: int) -> tuple[object, SimpleNamespace, dict]:
    work = root / "work"
    work.mkdir()
    producer = work / "pipeline-cache-host"
    producer.write_bytes(b"fixture producer\n")
    for name in ("host.dylib", "target.dylib", "peer.dylib", "bridge.a"):
        (root / name).write_bytes(name.encode())
    prime_list = root / "prime-list.txt"
    paths = fixture_paths(count)
    prime_list.write_text("".join(path + "\n" for path in paths))
    os_mount = root / "os"
    cryptex_mount = root / "cryptex"
    os_mount.mkdir()
    cryptex_mount.mkdir()
    inputs = SimpleNamespace(
        host_turnip=root / "host.dylib",
        target_turnip=root / "target.dylib",
        host_profile=root / "host.json",
        target_profile=root / "target.json",
        peer=root / "peer.dylib",
        bridge=root / "bridge.a",
        prime_list=prime_list,
        work=work,
        producer=producer,
    )
    args = SimpleNamespace(
        target="ios",
        gpu="a830",
        jobs=4,
        pack=r"/QuartzCore\.framework/",
        output=root / "dodge.m2vpack",
    )
    identity = {
        "os_build": "21H461",
        "target_uuid": "00112233445566778899aabbccddeeff",
        "turnip_build_id": "fixture-turnip-build",
        "paths": paths,
        "selected": [path for path in paths if "Fixture000" in path],
        "os_mount": os_mount,
        "cryptex_mount": cryptex_mount,
    }
    return args, inputs, identity


def run_main(
    *,
    load_refused: set[int] | None = None,
    load_timeout: bool = False,
    refused: set[int] | None = None,
    exit_failure: set[int] | None = None,
    timeout: int | None = None,
    absent: int | None = None,
    resume_after: bool = False,
) -> tuple[int, str, str, dict, dict]:
    load_refused = load_refused or set()
    refused = refused or set()
    exit_failure = exit_failure or set()
    temporary = tempfile.TemporaryDirectory(prefix="pipeline-cache-flow-")
    root = Path(temporary.name)
    args, inputs, identity = make_inputs(root, 166)
    calls: list[int] = []
    load_calls: list[list[str]] = []
    prime_one_calls: list[int] = []
    merge_calls = 0
    pack_calls = 0

    def load_record(index: int, status: str, reason: str = "") -> str:
        path = identity["paths"][index]
        value = {
            "index": index,
            "path": path,
            "status": status,
            "header64_hex": "4d544c42" + ("00" * 60),
            "header_bytes": 64,
            "error_domain": None,
            "error_code": None,
            "error_description": None,
            "pipelines_compiled": False,
            "shader_pack_produced": False,
        }
        if status == "load_refused":
            value.update(
                error_domain="MTLLibraryErrorDomain",
                error_code=index,
                error_description=reason,
            )
        return "LOAD_LIBRARY: " + json.dumps(value, separators=(",", ":"))

    def fake_run(command, **_kwargs):
        if command[-1] == "--validate-loads":
            load_calls.append(command)
            if load_timeout:
                partial = "\n".join(
                    load_record(index, "loaded") for index in range(7)
                ) + "\n"
                raise subprocess.TimeoutExpired(
                    command,
                    generator.LOAD_PREFLIGHT_TIMEOUT_SECONDS,
                    output=partial.encode(),
                    stderr=b"load preflight fixture timed out\n",
                )
            lines = []
            for index in range(166):
                if index in load_refused:
                    reason = "cannot load fixture %d" % index
                    lines.append(load_record(index, "load_refused", reason))
                else:
                    lines.append(load_record(index, "loaded"))
            loaded = 166 - len(load_refused)
            lines.append(
                "LOAD_RESULT: requested=166 loaded=%d refused=%d absent=0 "
                "pipelines_compiled=0 shader_pack_produced=0"
                % (loaded, len(load_refused))
            )
            stdout = "\n".join(lines) + "\n"
            return subprocess.CompletedProcess(
                command, 0 if not load_refused else 1, stdout, ""
            )
        index = int(command[8])
        calls.append(index)
        directory = Path(command[2])
        path = identity["paths"][index]
        if index == timeout:
            raise subprocess.TimeoutExpired(
                command,
                generator.PRIME_TIMEOUT_SECONDS,
                output=b"partial stdout\n",
                stderr=b"partial stderr\n",
            )
        if index == absent:
            reason = path + " absent from the image mount\n"
            (directory / "prime.log").write_text(reason)
            stdout = (
                "RESULT: requested=1 primed=0 refused=0 absent=1 device=fixture\n"
                "FAIL: requested=1 primed=0 refused=0 absent=1\n"
            )
            return subprocess.CompletedProcess(command, 1, stdout, reason)
        if index in exit_failure:
            reason = path + " producer fixture exited before RESULT\n"
            return subprocess.CompletedProcess(command, 1, "", reason)
        if index in refused:
            reason = path + " library refused: synthetic NSError %d\n" % index
            (directory / "prime.log").write_text(reason)
            stdout = (
                "RESULT: requested=1 primed=0 refused=1 absent=0 device=fixture\n"
                "FAIL: requested=1 primed=0 refused=1 absent=0\n"
            )
            return subprocess.CompletedProcess(command, 1, stdout, reason)
        (directory / "prime.log").write_text(
            path + " kernels=1 renders=0 made=1 refused=0\n"
        )
        (directory / "prime-done").write_text(
            path + "\t1\t1\tfixture-build\n"
        )
        stdout = (
            "RESULT: requested=1 primed=1 refused=0 absent=0 device=fixture made=1\n"
            "PASS: requested=1 primed=1 refused=0 absent=0\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    def fake_preflight(_args, _inputs):
        return identity

    def fake_merge(_args, _inputs, _identity):
        nonlocal merge_calls
        merge_calls += 1
        return [(inputs.work / "cache/v1/fixture", "v1/fixture")]

    def fake_pack(output, _cache_root, _entries):
        nonlocal pack_calls
        pack_calls += 1
        output.write_bytes(b"SHEXP001 fixture\n")
        return SimpleNamespace(
            stream_bytes=16,
            packed_bytes=8,
            files=["v1/fixture", "prime-done"],
            root="/fixture/m2v",
        )

    original = {
        "parser": generator.parser,
        "resolve_inputs": generator.resolve_inputs,
        "preflight": generator.preflight,
        "compile_producer": generator.compile_producer,
        "runtime_environment": generator.runtime_environment,
        "prime_one": generator.prime_one,
        "stamp": generator.stamp,
        "merge_cache": generator.merge_cache,
        "write_pipeline_pack": generator.write_pipeline_pack,
        "subprocess": generator.subprocess,
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    def counting_prime_one(index, *args, **kwargs):
        prime_one_calls.append(index)
        return original["prime_one"](index, *args, **kwargs)

    try:
        generator.parser = lambda: SimpleNamespace(parse_args=lambda: args)
        generator.resolve_inputs = lambda _args: inputs
        generator.preflight = fake_preflight
        generator.compile_producer = lambda _args, _inputs: inputs.producer
        generator.runtime_environment = lambda _args, _inputs: {"HOSTSIM_GPU": "a830"}
        generator.prime_one = counting_prime_one
        generator.stamp = lambda _producer, _inputs, _env, _build: "fixture-stamp\n"
        generator.merge_cache = fake_merge
        generator.write_pipeline_pack = fake_pack
        generator.subprocess = SimpleNamespace(
            run=fake_run, TimeoutExpired=subprocess.TimeoutExpired
        )
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = generator.main()
        resumed_records = None
        if resume_after:
            (inputs.work / "runs/005/result.json").unlink()
            resumed_records = generator.run_libraries(args, inputs, identity)
    finally:
        for name, value in original.items():
            setattr(generator, name, value)
    report_path = args.output.with_name(args.output.name + ".json")
    report = json.loads(report_path.read_text())
    result = {
        "args": args,
        "inputs": inputs,
        "identity": identity,
        "calls": calls,
        "load_calls": load_calls,
        "prime_one_calls": prime_one_calls,
        "merge_calls": merge_calls,
        "pack_calls": pack_calls,
        "report_path": report_path,
        "resumed_records": resumed_records,
        "temporary": temporary,
    }
    return status, stdout.getvalue(), stderr.getvalue(), report, result


status, stdout, stderr, report, failure = run_main(load_refused=REFUSED)
require(status == 1, "12 load-refused libraries did not fail generation")
require("PASS:" not in stdout, "failed generator printed PASS")
require("no shader pack was produced" in stderr,
        "failure summary does not say no shader pack was produced")
require(failure["merge_calls"] == 0 and failure["pack_calls"] == 0, "failed generation merged or packed a partial cache")
require(not failure["args"].output.exists(), "failed generation wrote a pipeline pack")
require(len(failure["load_calls"]) == 1 and failure["prime_one_calls"] == [],
        "load refusal did not stop before prime_one")
require(len(failure["calls"]) == 0,
        "load refusal started a real producer priming subprocess")
require(report["requested"] == report["primed"] + report["not_run"] == 166
        and report["primed"] == 0 and report["not_run"] == 166
        and report["refused"] == 0 and report["load_refused"] == 12,
        "load refusal was mislabeled as an executed prime refusal")
require(report["failed"] == 166 and not report["pack_produced"] and report["pack"] is None,
        "failure report counts or pack state are misleading")
require(report["producer_execution_attempted"]
        and report["producer_execution"]["load_preflight_attempted"]
        and not report["host_pipeline_compiler_subprocess"]["attempted"]
        and report["physical_gpu_execution"] is False
        and report["native_apple_gpu_execution"] is False
        and report["target_device_cache_hit"] is False,
        "failure report does not distinguish load execution from pipeline/GPU execution")
require(len(report["libraries"]) == 166, "failure report did not persist all 166 library records")
require({record["index"] for record in report["libraries"]} == set(range(166)),
        "failure report lost a library index")
require(all(record["status"] == "not_run" for record in report["libraries"]),
        "unprimed libraries were not labeled not_run")
preflight = report["load_preflight"]
require(preflight["status"] == "failed" and preflight["loaded"] == 154
        and preflight["load_refused"] == 12 and preflight["not_run"] == 0,
        "load preflight summary did not preserve all results")
preflight_json = failure["inputs"].work / "load-preflight.json"
preflight_log = failure["inputs"].work / "load-preflight.log"
require(preflight_json.is_file() and preflight_log.is_file(),
        "load preflight JSON or log was not retained")
preflight_detail = json.loads(preflight_json.read_text())
require(len(preflight_detail["libraries"]) == 166,
        "load preflight JSON did not persist every selected library")
for index in sorted(REFUSED):
    record = preflight_detail["libraries"][index]
    require(record["status"] == "load_refused"
            and record["error_domain"] == "MTLLibraryErrorDomain"
            and record["error_code"] == index
            and record["error_description"] == "cannot load fixture %d" % index,
            "load-refused library %d lost its raw NSError fields" % index)
    require("NSError domain=MTLLibraryErrorDomain code=%d" % index in record["reason"],
            "load-refused library %d lost its refusal reason" % index)
require(all(
    (failure["inputs"].work / "runs" / ("%03d" % index) / "result.json").is_file()
    for index in range(166)
), "load refusal did not persist all 166 not_run prime records")
failure["temporary"].cleanup()

status, stdout, _, report, success = run_main(resume_after=True)
require(status == 0, "all-success fixture failed")
require("PASS: requested=166 primed=166 refused=0 absent=0" in stdout,
        "all-success summary is not exact")
require(success["merge_calls"] == 1 and success["pack_calls"] == 1,
        "all-success fixture did not merge and pack once")
require(
    len(success["load_calls"]) >= 1
    and len(success["prime_one_calls"]) >= 166
    and set(success["prime_one_calls"][:166]) == set(range(166)),
    "all-success load preflight did not continue into every prime_one",
)
require(report["status"] == "host-cache-pack-produced"
        and report["requested"] == report["primed"] == 166
        and report["failed"] == 0 and report["pack_produced"]
        and report["load_preflight"]["status"] == "passed"
        and report["load_preflight"]["loaded"] == 166
        and report["host_pipeline_compiler_subprocess"]["invocations"] == 166
        and report["host_pipeline_compiler_subprocess"]["returncode_zero"] == 166
        and report["physical_gpu_execution"] is False
        and report["native_apple_gpu_execution"] is False
        and report["target_device_cache_hit"] is False,
        "all-success report is inconsistent")
require(
    len(success["calls"]) >= 166
    and set(success["calls"][:166]) == set(range(166)),
    "all-success did not execute all 166 producers",
)

resumed = success["resumed_records"]
require(len(resumed) == 166 and sum(record.get("resumed", False) for record in resumed) == 165,
        "structured resume did not validate 165 runs and rerun only the missing proof")
require(not resumed[5].get("resumed", False),
        "a result without structured metadata was accepted as a resumed PASS")
require((success["inputs"].work / "runs/005/result.json").is_file(),
        "rerun did not restore structured per-library metadata")
require((success["inputs"].work / "inputs.sha256").read_text() == "fixture-stamp\n",
        "resume fixture stamp changed unexpectedly")
require(len(success["calls"]) == 167 and success["calls"].count(5) == 2,
        "resume invoked producers other than the one missing structured proof")
require(len(success["load_calls"]) == 2 and len(success["prime_one_calls"]) >= 167,
        "resume skipped the load preflight or did not invoke the missing proof")
success["temporary"].cleanup()

status, stdout, stderr, report, timeout = run_main(
    refused={7}, exit_failure={55}, timeout=33, absent=99
)
require(status == 1 and "PASS:" not in stdout, "timeout path printed PASS")
require(timeout["merge_calls"] == 0 and timeout["pack_calls"] == 0,
        "timeout path merged or packed other successful futures")
require(len(timeout["load_calls"]) == 1 and len(timeout["prime_one_calls"]) == 166,
        "prime failure fixture did not pass the load preflight first")
require(
    report["timeout"] == report["refused"] == report["absent"] == report["exit_failure"] == 1,
    "timeout/refused/absent/exit-failure classifications are not distinct",
)
require(report["primed"] == 162 and len(report["libraries"]) == 166,
        "timeout lost successful or failed future records")
record = report["libraries"][33]
require(record["status"] == "timeout"
        and record["stdout"] == "partial stdout\n"
        and record["stderr"] == "partial stderr\n"
        and record["timeout_seconds"] == generator.PRIME_TIMEOUT_SECONDS,
        "timeout record did not preserve bytes output with text capture")
require(report["libraries"][55]["status"] == "exit_failure"
        and "exited before RESULT" in report["libraries"][55]["reason"],
        "ordinary producer exit failure was not classified")
require(all(
    (timeout["inputs"].work / "runs" / ("%03d" % index) / "result.json").is_file()
    for index in (0, 7, 33, 55, 99, 165)
), "timeout path did not preserve surrounding future metadata")
require("no pack produced" in stderr, "timeout failure summary omits the pack boundary")
timeout["temporary"].cleanup()

status, stdout, stderr, report, load_timeout = run_main(load_timeout=True)
require(status == 1 and "PASS:" not in stdout,
        "load preflight timeout printed PASS")
require(len(load_timeout["load_calls"]) == 1
        and load_timeout["prime_one_calls"] == []
        and load_timeout["calls"] == []
        and load_timeout["merge_calls"] == 0
        and load_timeout["pack_calls"] == 0,
        "load preflight timeout did not stop before real priming or pack")
preflight = report["load_preflight"]
require(preflight["status"] == "timeout"
        and preflight["timeout_seconds"] == generator.LOAD_PREFLIGHT_TIMEOUT_SECONDS
        and preflight["loaded"] == 7
        and preflight["not_run"] == 159,
        "load preflight timeout did not preserve partial structured results")
preflight_log = (load_timeout["inputs"].work / "load-preflight.log").read_text()
require("LOAD_LIBRARY:" in preflight_log
        and "load preflight fixture timed out" in preflight_log,
        "load preflight timeout discarded partial stdout/stderr")
require((load_timeout["inputs"].work / "load-preflight.json").is_file()
        and report["primed"] == 0 and report["not_run"] == 166,
        "load preflight timeout did not retain JSON or not_run prime state")
load_timeout["temporary"].cleanup()

print(
    "PASS: 166-library control flow validates loads before priming, collects 12 raw NSError "
    "refusals without calling prime_one, persists all records, refuses to pack, resumes only "
    "structured unchanged results, continues after all-success, and retains timeout logs"
)
print(
    "NOT COVERED: this fixture does not run Metal, load a real 21H461 metallib, prove the 12 "
    "production NSError causes, execute on a native Mac, import the cache on target, or hit A830 hardware; "
    "fixture labels are not generator results"
)
