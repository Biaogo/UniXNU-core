#!/usr/bin/env python3
"""Pure parser and acceptance tests for QuartzCore cache evidence."""
import textwrap

from quartzcore_turnip_cache import (EvidenceError, parse_case, validate_cold,
                                     validate_consume, validate_produce,
                                     validate_refusal)


# FIXTURE: producer rows copy the per-recipe counts from CI 43158661284.
# The QC vk cold trace is a synthetic representation of that run's two cold
# batches (one shaderless success and one actual strict rejection); it is not a
# new hardware or CI observation.
FIXTURE_PRODUCER = textwrap.dedent("""\
QC recipe produce index=0 vertex=v0 fragment=f0 made=1 archived=1 pipelines=5 success=5 required=0 valid=5 hits=0 app_calls=2 app_valid=2 app_hits=0 batches=5 driver_batches=5 suppressed=0
QC recipe produce index=1 vertex=v1 fragment=f1 made=1 archived=1 pipelines=3 success=3 required=0 valid=3 hits=0 app_calls=2 app_valid=2 app_hits=0 batches=3 driver_batches=3 suppressed=0
QC recipe produce index=2 vertex=v2 fragment=f2 made=1 archived=1 pipelines=4 success=4 required=0 valid=4 hits=0 app_calls=2 app_valid=2 app_hits=0 batches=4 driver_batches=4 suppressed=0
QC recipe produce index=3 vertex=v3 fragment=f3 made=1 archived=1 pipelines=5 success=5 required=0 valid=5 hits=0 app_calls=2 app_valid=2 app_hits=0 batches=5 driver_batches=5 suppressed=0
QC recipe produce index=4 vertex=v4 fragment=f4 made=1 archived=1 pipelines=3 success=3 required=0 valid=3 hits=0 app_calls=2 app_valid=2 app_hits=0 batches=3 driver_batches=3 suppressed=0
QC recipe produce index=5 vertex=v5 fragment=f5 made=1 archived=1 pipelines=3 success=3 required=0 valid=3 hits=0 app_calls=2 app_valid=2 app_hits=0 batches=3 driver_batches=3 suppressed=0
QC recipe produce index=6 vertex=v6 fragment=f6 made=1 archived=1 pipelines=3 success=3 required=0 valid=3 hits=0 app_calls=2 app_valid=2 app_hits=0 batches=3 driver_batches=3 suppressed=0
QC recipe produce index=7 vertex=v7 fragment=f7 made=1 archived=1 pipelines=4 success=4 required=0 valid=4 hits=0 app_calls=2 app_valid=2 app_hits=0 batches=4 driver_batches=4 suppressed=0
QC recipe produce index=8 vertex=v8 fragment=f8 made=1 archived=1 pipelines=3 success=3 required=0 valid=3 hits=0 app_calls=2 app_valid=2 app_hits=0 batches=3 driver_batches=3 suppressed=0
QC recipe produce index=9 vertex=v9 fragment=f9 made=1 archived=1 pipelines=3 success=3 required=0 valid=3 hits=0 app_calls=2 app_valid=2 app_hits=0 batches=3 driver_batches=3 suppressed=0
QC recipe produce index=10 vertex=v10 fragment=f10 made=1 archived=1 pipelines=3 success=3 required=0 valid=3 hits=0 app_calls=2 app_valid=2 app_hits=0 batches=3 driver_batches=3 suppressed=0
QC recipe produce index=11 vertex=v11 fragment=f11 made=1 archived=1 pipelines=2 success=2 required=0 valid=2 hits=0 app_calls=1 app_valid=1 app_hits=0 batches=2 driver_batches=2 suppressed=0
QC recipe produce index=12 vertex=v12 fragment=f12 made=1 archived=1 pipelines=2 success=2 required=0 valid=2 hits=0 app_calls=1 app_valid=1 app_hits=0 batches=2 driver_batches=2 suppressed=0
QC recipe produce index=13 vertex=v13 fragment=f13 made=1 archived=1 pipelines=2 success=2 required=0 valid=2 hits=0 app_calls=1 app_valid=1 app_hits=0 batches=2 driver_batches=2 suppressed=0
QC recipe produce index=14 vertex=v14 fragment=f14 made=1 archived=1 pipelines=1 success=1 required=0 valid=1 hits=0 app_calls=0 app_valid=0 app_hits=0 batches=1 driver_batches=1 suppressed=0
QC recipe produce index=15 vertex=v15 fragment=f15 made=1 archived=1 pipelines=1 success=1 required=0 valid=1 hits=0 app_calls=0 app_valid=0 app_hits=0 batches=1 driver_batches=1 suppressed=0
QC recipe produce index=16 vertex=v16 fragment=f16 made=1 archived=1 pipelines=1 success=1 required=0 valid=1 hits=0 app_calls=0 app_valid=0 app_hits=0 batches=1 driver_batches=1 suppressed=0
QC recipe produce index=17 vertex=v17 fragment=f17 made=1 archived=1 pipelines=1 success=1 required=0 valid=1 hits=0 app_calls=0 app_valid=0 app_hits=0 batches=1 driver_batches=1 suppressed=0
QC recipe produce index=18 vertex=v18 fragment=f18 made=1 archived=1 pipelines=2 success=2 required=0 valid=2 hits=0 app_calls=1 app_valid=1 app_hits=0 batches=2 driver_batches=2 suppressed=0
QC recipe produce index=19 vertex=v19 fragment=f19 made=1 archived=1 pipelines=2 success=2 required=0 valid=2 hits=0 app_calls=1 app_valid=1 app_hits=0 batches=2 driver_batches=2 suppressed=0
QC recipe produce index=20 vertex=v20 fragment=f20 made=1 archived=1 pipelines=3 success=3 required=0 valid=3 hits=0 app_calls=2 app_valid=2 app_hits=0 batches=3 driver_batches=3 suppressed=0
QC recipe produce index=21 vertex=v21 fragment=f21 made=1 archived=1 pipelines=2 success=2 required=0 valid=2 hits=0 app_calls=1 app_valid=1 app_hits=0 batches=2 driver_batches=2 suppressed=0
QC recipe produce index=22 vertex=v22 fragment=f22 made=1 archived=1 pipelines=5 success=5 required=0 valid=5 hits=0 app_calls=2 app_valid=2 app_hits=0 batches=5 driver_batches=5 suppressed=0
QC recipe produce index=23 vertex=v23 fragment=f23 made=1 archived=1 pipelines=3 success=3 required=0 valid=3 hits=0 app_calls=2 app_valid=2 app_hits=0 batches=3 driver_batches=3 suppressed=0
QC vk phase=cold recipe=23 flags=0x100|0x100 flags2_present=0|0 flags2=0x0|0x0 strict=1|1 stages=2|2 libraries=0|0 cache=0 result=compile_required result_raw=1000297000 pipelines=0|0 feedback=0x0|0x0 nonnull=0
QC vk phase=cold recipe=23 flags=0x900 flags2_present=0 flags2=0x0 strict=1 stages=0 libraries=1 cache=1 result=success result_raw=0 pipelines=1 feedback=0x0 nonnull=1
PASS: QuartzCore compiler produce pid=42 recipes=24 recipe_ok=24 vulkan_pipelines=66 vulkan_success=66 vulkan_required_batches=0 vulkan_valid=66 vulkan_hits=0 vulkan_batches=66 vulkan_driver_batches=66 strict_suppressed=0 app_cache_pipelines=34 app_cache_valid=34 app_cache_hits=0 uuid_mutations=0 devices=1 shader_cache=0/72/48 driver=0/0/0/34 device_graphics=1 cold_driver_required=1 cold_driver_batches=2 cold_driver_success=1 cold_strict_suppressed=0 cold_object=1 init_submissions=1; no shader execution
""")


def replace(text, old, new):
    if old not in text:
        raise AssertionError("fixture does not contain " + old)
    return text.replace(old, new, 1)


def expect_failure(function, *arguments, reason=None):
    try:
        function(*arguments)
    except EvidenceError as error:
        if reason is not None:
            assert reason in str(error), "failure reason %r does not contain %r" % (
                str(error), reason)
        return
    raise AssertionError("invalid cache evidence was accepted")


producer = parse_case(FIXTURE_PRODUCER)
validate_produce(producer)
cold = validate_cold(producer)

assert producer["summary"]["recipes"] == 24
assert producer["summary"]["vulkan_pipelines"] == 66
assert cold == {
    "batches": 2,
    "compile_required_batches": 1,
    "driver_success_handles": 1,
    "shaderless_successes": 1,
    "cached_shader_successes": 0,
    "metal_object_returned": True,
}

required_call = (
    "QC vk phase=cold recipe=23 flags=0x100|0x100 flags2_present=0|0 "
    "flags2=0x0|0x0 strict=1|1 stages=2|2 libraries=0|0 cache=0 "
    "result=compile_required result_raw=1000297000 pipelines=0|0 "
    "feedback=0x0|0x0 nonnull=0"
)
missing_strict = replace(
    FIXTURE_PRODUCER,
    "flags=0x100|0x100 flags2_present=0|0 flags2=0x0|0x0 strict=1|1",
    "flags=0x0|0x0 flags2_present=0|0 flags2=0x0|0x0 strict=0|0",
)
expect_failure(validate_cold, parse_case(missing_strict))

suppressed_cold = replace(
    FIXTURE_PRODUCER,
    "cold_strict_suppressed=0",
    "cold_strict_suppressed=1",
)
expect_failure(validate_cold, parse_case(suppressed_cold))

flags2_strict = replace(
    FIXTURE_PRODUCER,
    required_call,
    required_call.replace(
        "flags=0x100|0x100 flags2_present=0|0 flags2=0x0|0x0 "
        "strict=1|1 stages=2|2 libraries=0|0",
        "flags=0x900|0x900 flags2_present=1|1 flags2=0x100|0x100 "
        "strict=1|1 stages=2|2 libraries=0|0"),
)
validate_cold(parse_case(flags2_strict))

flags2_library = replace(
    FIXTURE_PRODUCER,
    required_call,
    required_call.replace(
        "flags=0x100|0x100 flags2_present=0|0 flags2=0x0|0x0 "
        "strict=1|1 stages=2|2 libraries=0|0",
        "flags=0x0|0x0 flags2_present=1|1 flags2=0x900|0x900 "
        "strict=1|1 stages=2|2 libraries=1|1"),
)
validate_cold(parse_case(flags2_library))

legacy_strict_overridden = replace(
    FIXTURE_PRODUCER,
    required_call,
    required_call.replace(
        "flags=0x100|0x100 flags2_present=0|0 flags2=0x0|0x0 "
        "strict=1|1 stages=2|2 libraries=0|0",
        "flags=0x900|0x900 flags2_present=1|1 flags2=0x0|0x0 "
        "strict=0|0 stages=2|2 libraries=0|0"),
)
expect_failure(validate_cold, parse_case(legacy_strict_overridden))

no_required = replace(
    replace(
        FIXTURE_PRODUCER,
        required_call,
        "QC vk phase=cold recipe=23 flags=0x900|0x900 flags2_present=0|0 "
        "flags2=0x0|0x0 strict=1|1 stages=0|0 libraries=1|1 cache=1 "
        "result=success result_raw=0 pipelines=1|1 feedback=0x0|0x0 nonnull=2",
    ),
    "cold_driver_required=1 cold_driver_batches=2 cold_driver_success=1",
    "cold_driver_required=0 cold_driver_batches=2 cold_driver_success=2",
)
expect_failure(validate_cold, parse_case(no_required))

nonnull_required = replace(
    replace(
        FIXTURE_PRODUCER,
        required_call,
        required_call.replace("pipelines=0|0", "pipelines=1|1").replace(
            "nonnull=0", "nonnull=2"),
    ),
    "cold_driver_success=1",
    "cold_driver_success=3",
)
expect_failure(validate_cold, parse_case(nonnull_required))

shader_success_without_cache = replace(
    FIXTURE_PRODUCER,
    "strict=1 stages=0 libraries=1 cache=1 result=success result_raw=0 "
    "pipelines=1 feedback=0x0 nonnull=1",
    "strict=1 stages=2 libraries=1 cache=1 result=success result_raw=0 "
    "pipelines=1 feedback=0x0 nonnull=1",
)
expect_failure(validate_cold, parse_case(shader_success_without_cache))

cached_shader_success = replace(
    shader_success_without_cache,
    "pipelines=1 feedback=0x0 nonnull=1",
    "pipelines=1 feedback=0x3 nonnull=1",
)
assert validate_cold(parse_case(cached_shader_success))[
    "cached_shader_successes"] == 1

def consumer_fixture():
    """Build an explicit 24-recipe consume trace from synthetic entries."""
    recipe_batches = {}
    for index in range(24):
        if 14 <= index <= 17:
            batches = [(
                [{"stages": 0, "flags": 0x100, "feedback": 0x1}], False
            )]
        else:
            cached = [
                {"stages": 1, "flags": 0x900, "feedback": 0x3},
                {"stages": 0, "flags": 0x900, "feedback": 0x3},
            ]
            link = [{"stages": 0, "flags": 0x100, "feedback": 0x1}]
            if index == 19:
                for entry in cached + link:
                    entry["flags2_present"] = True
                    entry["flags2"] = 0x100
                cached[1]["libraries"] = False
                link[0]["libraries"] = False
            batches = [(cached, True), (link, False)]
        recipe_batches[index] = batches

    calls = []
    rows = []
    for index, batches in recipe_batches.items():
        for entries, cache in batches:
            lists = {name: [] for name in (
                "flags", "flags2_present", "flags2", "strict", "stages",
                "libraries", "pipelines", "feedback")}
            for entry in entries:
                flags2_present = entry.get("flags2_present", False)
                effective = entry.get("flags2", entry["flags"]) if flags2_present \
                    else entry["flags"]
                lists["flags"].append("0x%x" % entry["flags"])
                lists["flags2_present"].append("1" if flags2_present else "0")
                lists["flags2"].append("0x%x" % entry.get("flags2", 0))
                lists["strict"].append("1" if effective & 0x100 else "0")
                lists["stages"].append(str(entry["stages"]))
                lists["libraries"].append(
                    "1" if entry.get("libraries", bool(effective & 0x800)) else "0")
                lists["pipelines"].append("1")
                lists["feedback"].append("0x%x" % entry["feedback"])
            calls.append(
                "QC vk phase=recipe recipe=%d flags=%s flags2_present=%s "
                "flags2=%s strict=%s stages=%s libraries=%s cache=%d "
                "result=success result_raw=0 pipelines=%s feedback=%s nonnull=%d"
                % (index, "|".join(lists["flags"]),
                   "|".join(lists["flags2_present"]),
                   "|".join(lists["flags2"]), "|".join(lists["strict"]),
                   "|".join(lists["stages"]), "|".join(lists["libraries"]),
                   int(cache), "|".join(lists["pipelines"]),
                   "|".join(lists["feedback"]), len(entries))
            )
        entries = [entry for batch, _cache in batches for entry in batch]
        cached = [entry for batch, cache in batches if cache for entry in batch]
        rows.append(
            "QC recipe consume index=%d vertex=v%d fragment=f%d made=1 "
            "archived=2 pipelines=%d success=%d required=0 valid=%d hits=%d "
            "app_calls=%d app_valid=%d app_hits=%d batches=%d "
            "driver_batches=%d suppressed=0"
            % (index, index, index, len(entries), len(entries), len(entries),
               len(cached), len(cached), len(cached), len(cached),
               len(batches), len(batches)))

    pipelines = sum(len([entry for batch, _cache in batches for entry in batch])
                    for batches in recipe_batches.values())
    batches = sum(len(value) for value in recipe_batches.values())
    cached = sum(len([entry for batch, cache in value if cache for entry in batch])
                 for value in recipe_batches.values())
    return parse_case(
        "PASS: QuartzCore compiler consume pid=43 recipes=24 recipe_ok=24 "
        "vulkan_pipelines=%d vulkan_success=%d vulkan_required_batches=0 "
        "vulkan_valid=%d vulkan_hits=%d vulkan_batches=%d "
        "vulkan_driver_batches=%d strict_suppressed=0 app_cache_pipelines=%d "
        "app_cache_valid=%d app_cache_hits=%d uuid_mutations=0 devices=1 "
        "shader_cache=0/0/0 driver=1/0/0/0 device_graphics=1 "
        "cold_driver_required=0 cold_driver_batches=0 cold_driver_success=0 "
        "cold_strict_suppressed=0 cold_object=0 init_submissions=1; "
        "no shader execution\n%s\n%s\n"
        % (pipelines, pipelines, pipelines, cached, batches, batches, cached,
           cached, cached, "\n".join(rows), "\n".join(calls)))


consumer = consumer_fixture()
consumer_report = validate_consume(consumer)
assert consumer_report["recipe_calls"] == 24
assert consumer_report["pipeline_entries"] == sum(
    row["pipelines"] for row in consumer["recipes"])
assert consumer_report["app_cache_entries"] == sum(
    row["app_calls"] for row in consumer["recipes"])
assert consumer_report["app_cache_hits"] == consumer_report["app_cache_entries"]
assert consumer_report["shaderless_links"] == 24

missing_trace = consumer_fixture()
missing_trace["calls"] = [
    call for call in missing_trace["calls"] if call["recipe"] != 14
]
expect_failure(validate_consume, missing_trace, reason="no consumer CALL trace")

wrong_row_count = consumer_fixture()
wrong_row_count["recipes"][0]["batches"] += 1
wrong_row_count["summary"]["vulkan_batches"] += 1
expect_failure(validate_consume, wrong_row_count,
               reason="batches does not match its CALL trace")

missing_strict = consumer_fixture()
strict_call = next(call for call in missing_strict["calls"]
                   if call["recipe"] == 14)
strict_call["flags2_present"] = [True]
strict_call["flags2"] = [0]
strict_call["strict"] = [False]
expect_failure(validate_consume, missing_strict,
               reason="did not pass effective strict flags")

invalid_feedback = consumer_fixture()
feedback_call = next(call for call in invalid_feedback["calls"]
                     if call["recipe"] == 0 and call["cache"])
feedback_call["feedback"][0] = 0
expect_failure(validate_consume, invalid_feedback,
               reason="invalid feedback")

cache_miss = consumer_fixture()
feedback_call = next(call for call in cache_miss["calls"]
                     if call["recipe"] == 0 and call["cache"])
feedback_call["feedback"][0] = 0x1
expect_failure(validate_consume, cache_miss,
               reason="missed its application cache")

stage_zero_cache_miss = consumer_fixture()
stage_zero_call = next(call for call in stage_zero_cache_miss["calls"]
                       if call["recipe"] == 18 and call["cache"])
stage_zero_call["feedback"][1] = 0x1
expect_failure(validate_consume, stage_zero_cache_miss,
               reason="missed its application cache")

shader_without_cache = consumer_fixture()
shader_call = next(call for call in shader_without_cache["calls"]
                   if call["recipe"] == 0 and call["cache"])
shader_call["cache"] = 0
shader_call["feedback"] = [0x1, 0x1]
expect_failure(validate_consume, shader_without_cache,
               reason="shader-bearing entry has no pipeline cache")

null_success = consumer_fixture()
null_call = next(call for call in null_success["calls"]
                 if call["recipe"] == 0 and call["cache"])
null_call["pipelines"][0] = False
null_call["nonnull"] = 1
expect_failure(validate_consume, null_success,
               reason="successful CALL returned a null pipeline")

error_result = consumer_fixture()
error_call = next(call for call in error_result["calls"]
                  if call["recipe"] == 14)
error_call["result"] = "other"
error_call["result_raw"] = -3
expect_failure(validate_consume, error_result,
               reason="returned required or error result")

# FIXTURE: unmodified QuartzCore uuid-miss evidence from CI 43164596688. The
# non-strict init success must not count as recipe evidence.
FIXTURE_UUID_MISS = textwrap.dedent("""\
PASS: QuartzCore compiler uuid-miss pid=24198 recipes=1 recipe_ok=1 vulkan_pipelines=2 vulkan_success=1 vulkan_required_batches=1 vulkan_valid=1 vulkan_hits=0 vulkan_batches=2 vulkan_driver_batches=2 strict_suppressed=0 app_cache_pipelines=1 app_cache_valid=0 app_cache_hits=0 uuid_mutations=1 devices=1 shader_cache=0/0/0 driver=1/0/0/0 device_graphics=1 cold_driver_required=0 cold_driver_batches=0 cold_driver_success=0 cold_strict_suppressed=0 cold_object=0 init_submissions=1; no shader execution
QC vk phase=init recipe=0 flags=0x0 flags2_present=0 flags2=0x0 strict=0 stages=1 libraries=0 cache=1 result=success result_raw=0 pipelines=1 feedback=0x3 nonnull=1
QC vk phase=recipe recipe=0 flags=0x900 flags2_present=0 flags2=0x0 strict=1 stages=0 libraries=1 cache=0 result=success result_raw=0 pipelines=1 feedback=0x1 nonnull=1
QC vk phase=recipe recipe=0 flags=0x900 flags2_present=0 flags2=0x0 strict=1 stages=1 libraries=1 cache=1 result=compile_required result_raw=1000297000 pipelines=0 feedback=0x0 nonnull=0
QC recipe uuid-miss index=0 vertex=upsample_vert_lpf fragment=upsample_frag_lpf made=1 archived=2 pipelines=2 success=1 required=1 valid=1 hits=0 app_calls=1 app_valid=0 app_hits=0 batches=2 driver_batches=2 suppressed=0
graphics results uuid-miss: pipelines=2 success=1 required=1 valid=1 hits=0 app_cache=1/0/0 batches=2/0/2
NativeAdreno BO cache: 19 allocations, 0 reused (0.0%), 19 cached now (8 MiB), 0 evicted, 0 flushed, 1 free GPU ranges
""")


def refusal_fixture(mode, uuid_mutations):
    return FIXTURE_UUID_MISS.replace("uuid-miss", mode).replace(
        "uuid_mutations=1", "uuid_mutations=%d" % uuid_mutations)


UUID_MISS_REFUSAL = FIXTURE_UUID_MISS
EMPTY_DRIVER_REFUSAL = refusal_fixture("empty-driver", 0)
uuid_refusal = parse_case(UUID_MISS_REFUSAL)
empty_driver_refusal = parse_case(EMPTY_DRIVER_REFUSAL)
uuid_report = validate_refusal(uuid_refusal, uuid_mutated=True)
validate_refusal(empty_driver_refusal, uuid_mutated=False)
assert uuid_report == {
    "recipe_calls": 2,
    "driver_batches": 2,
    "driver_compile_required_entries": 1,
    "shader_bearing_compile_required_entries": 1,
    "shaderless_successes": 1,
    "shaderless_library_links": 1,
    "metal_object_returned": True,
    "object_drawability_verified": False,
    "init_calls_ignored": 1,
    "driver_compile_required_verified": True,
}

zero_driver = parse_case(FIXTURE_UUID_MISS)
zero_driver["recipes"][0]["driver_batches"] = 0
zero_driver["summary"]["vulkan_driver_batches"] = 0
expect_failure(validate_refusal, zero_driver, True,
               reason="did not enter the real Vulkan driver")

zero_required = parse_case(FIXTURE_UUID_MISS)
zero_required["recipes"][0]["required"] = 0
zero_required["summary"]["vulkan_required_batches"] = 0
expect_failure(validate_refusal, zero_required, True,
               reason="required does not match its CALL trace")

missing_trace = parse_case(FIXTURE_UUID_MISS)
missing_trace["calls"] = [
    call for call in missing_trace["calls"] if call["phase"] != "recipe"
]
expect_failure(validate_refusal, missing_trace, True,
               reason="no recipe CALL trace")

missing_strict = parse_case(replace(
    FIXTURE_UUID_MISS,
    "flags=0x900 flags2_present=0 flags2=0x0 strict=1 stages=0",
    "flags=0x800 flags2_present=0 flags2=0x0 strict=0 stages=0",
))
expect_failure(validate_refusal, missing_strict, True,
               reason="did not pass effective strict flags")

flags2_override = parse_case(replace(
    FIXTURE_UUID_MISS,
    "flags=0x900 flags2_present=0 flags2=0x0 strict=1 stages=0",
    "flags=0x800 flags2_present=1 flags2=0x900 strict=1 stages=0",
))
validate_refusal(flags2_override, True)

empty_metal_object = parse_case(FIXTURE_UUID_MISS)
empty_metal_object["recipes"][0]["made"] = 0
empty_metal_object["summary"]["recipe_ok"] = 0
empty_metal_report = validate_refusal(empty_metal_object, True)
assert empty_metal_report["metal_object_returned"] is False
assert empty_metal_report["driver_compile_required_verified"] is True

metal_summary_mismatch = parse_case(FIXTURE_UUID_MISS)
metal_summary_mismatch["recipes"][0]["made"] = 0
expect_failure(validate_refusal, metal_summary_mismatch, True,
               reason="recipe_ok does not match the per-recipe results")

shader_success = parse_case(FIXTURE_UUID_MISS)
shader_success_call = next(
    call for call in shader_success["calls"]
    if call["phase"] == "recipe" and call["result"] == "compile_required"
)
shader_success_call["result"] = "success"
shader_success_call["result_raw"] = 0
shader_success_call["pipelines"] = [True]
shader_success_call["feedback"] = [0x1]
shader_success_call["nonnull"] = 1
shader_success["recipes"][0]["success"] = 2
shader_success["recipes"][0]["required"] = 0
shader_success["recipes"][0]["valid"] = 2
shader_success["summary"]["vulkan_success"] = 2
shader_success["summary"]["vulkan_required_batches"] = 0
shader_success["summary"]["vulkan_valid"] = 2
expect_failure(validate_refusal, shader_success, True,
               reason="shader-bearing success")

ordinary_error = parse_case(FIXTURE_UUID_MISS)
error_call = next(
    call for call in ordinary_error["calls"]
    if call["phase"] == "recipe" and call["result"] == "compile_required"
)
error_call["result"] = "error"
error_call["result_raw"] = -3
expect_failure(validate_refusal, ordinary_error, True,
               reason="unexpected Vulkan result")

fake_result_label = parse_case(FIXTURE_UUID_MISS)
fake_call = next(
    call for call in fake_result_label["calls"]
    if call["phase"] == "recipe" and call["result"] == "compile_required"
)
fake_call["result_raw"] = 0
expect_failure(validate_refusal, fake_result_label, True,
               reason="label disagrees with its raw result")

negative_stage = parse_case(FIXTURE_UUID_MISS)
negative_stage_call = next(
    call for call in negative_stage["calls"]
    if call["phase"] == "recipe" and call["result"] == "compile_required"
)
negative_stage_call["stages"] = [-1]
expect_failure(validate_refusal, negative_stage, True,
               reason="negative shader stage count")

nonnull_required = parse_case(FIXTURE_UUID_MISS)
nonnull_required_call = next(
    call for call in nonnull_required["calls"]
    if call["phase"] == "recipe" and call["result"] == "compile_required"
)
nonnull_required_call["pipelines"] = [True]
nonnull_required_call["nonnull"] = 1
expect_failure(validate_refusal, nonnull_required, True,
               reason="compile-required CALL returned a pipeline")

no_required = parse_case(FIXTURE_UUID_MISS)
no_required_call = next(
    call for call in no_required["calls"]
    if call["phase"] == "recipe" and call["result"] == "compile_required"
)
no_required_call.update(
    stages=[0], cache=0, result="success", result_raw=0,
    pipelines=[True], feedback=[0x1], nonnull=1,
)
expect_failure(validate_refusal, no_required, True,
               reason="no shader-bearing compile-required")

cached_shaderless_success = parse_case(FIXTURE_UUID_MISS)
cached_shaderless_call = next(
    call for call in cached_shaderless_success["calls"]
    if call["phase"] == "recipe" and call["result"] == "success"
)
cached_shaderless_call["cache"] = 1
expect_failure(validate_refusal, cached_shaderless_success, True,
               reason="shaderless success used a pipeline cache")

invalid_shaderless_feedback = parse_case(FIXTURE_UUID_MISS)
invalid_feedback_call = next(
    call for call in invalid_shaderless_feedback["calls"]
    if call["phase"] == "recipe" and call["result"] == "success"
)
invalid_feedback_call["feedback"] = [0]
expect_failure(validate_refusal, invalid_shaderless_feedback, True,
               reason="invalid feedback")

cache_hit = parse_case(FIXTURE_UUID_MISS)
cache_hit_call = next(
    call for call in cache_hit["calls"]
    if call["phase"] == "recipe" and call["result"] == "success"
)
cache_hit_call["feedback"] = [0x3]
expect_failure(validate_refusal, cache_hit, True,
               reason="reported a cache hit")

summary_mismatch = parse_case(FIXTURE_UUID_MISS)
summary_mismatch["summary"]["vulkan_required_batches"] += 1
expect_failure(validate_refusal, summary_mismatch, True,
               reason="does not match the per-recipe results")

row_mismatch = parse_case(FIXTURE_UUID_MISS)
row_mismatch["recipes"][0]["batches"] += 1
row_mismatch["summary"]["vulkan_batches"] += 1
expect_failure(validate_refusal, row_mismatch, True,
               reason="batches does not match its CALL trace")

expect_failure(validate_refusal, empty_driver_refusal, True,
               reason="mode does not match")
expect_failure(validate_refusal, uuid_refusal, False,
               reason="mode does not match")
wrong_mutation = parse_case(replace(
    FIXTURE_UUID_MISS, "uuid_mutations=1", "uuid_mutations=0",
))
expect_failure(validate_refusal, wrong_mutation, True,
               reason="UUID mutation contract")

print("PASS: QuartzCore fixture completes 24 recipes, validates every consumer "
      "CALL entry against the row counts, requires all cache-bearing entries to "
      "hit, accepts strict stage-0/cache-0 links separately, validates strict "
      "per-batch cold evidence, and accepts the actual UUID-miss CALL boundary: "
      "one shaderless strict success plus one strict shader-bearing "
      "compile-required call with no pipeline handle")
print("NOT COVERED: the synthetic cold trace and Linux parsing do not prove "
      "macOS compilation, the mounted 21H461 input, Metal object drawability, "
      "a first draw, or physical GPU execution")
