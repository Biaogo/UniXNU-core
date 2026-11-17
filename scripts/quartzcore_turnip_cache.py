"""Parse and validate QuartzCore host cache-instrument evidence.

The host instrument records recipe completion separately from Vulkan pipeline
creation.  A recipe can create several graphics pipelines before the runtime
returns one Metal state, so neither count implies the other.
"""
import re


class EvidenceError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise EvidenceError(reason)


SUMMARY = re.compile(
    r"^PASS: QuartzCore compiler (?P<mode>\S+) pid=(?P<pid>\d+) "
    r"recipes=(?P<recipes>\d+) recipe_ok=(?P<recipe_ok>\d+) "
    r"vulkan_pipelines=(?P<vulkan_pipelines>\d+) "
    r"vulkan_success=(?P<vulkan_success>\d+) "
    r"vulkan_required_batches=(?P<vulkan_required_batches>\d+) "
    r"vulkan_valid=(?P<vulkan_valid>\d+) vulkan_hits=(?P<vulkan_hits>\d+) "
    r"vulkan_batches=(?P<vulkan_batches>\d+) "
    r"vulkan_driver_batches=(?P<vulkan_driver_batches>\d+) "
    r"strict_suppressed=(?P<strict_suppressed>\d+) "
    r"app_cache_pipelines=(?P<app_cache_pipelines>\d+) "
    r"app_cache_valid=(?P<app_cache_valid>\d+) "
    r"app_cache_hits=(?P<app_cache_hits>\d+) "
    r"uuid_mutations=(?P<uuid_mutations>\d+) devices=(?P<devices>\d+) "
    r"shader_cache=(?P<shader_hits>\d+)/(?P<shader_misses>\d+)/(?P<shader_writes>\d+) "
    r"driver=(?P<driver_loads>\d+)/(?P<driver_hits>\d+)/"
    r"(?P<driver_misses>\d+)/(?P<driver_saves>\d+) "
    r"device_graphics=(?P<device_graphics>\d+) "
    r"cold_driver_required=(?P<cold_driver_required>\d+) "
    r"cold_driver_batches=(?P<cold_driver_batches>\d+) "
    r"cold_driver_success=(?P<cold_driver_success>\d+) "
    r"cold_strict_suppressed=(?P<cold_strict_suppressed>\d+) "
    r"cold_object=(?P<cold_object>[01]) "
    r"init_submissions=(?P<init_submissions>\d+); no shader execution$",
    re.MULTILINE,
)

RECIPE = re.compile(
    r"^QC recipe (?P<mode>\S+) index=(?P<index>\d+) "
    r"vertex=(?P<vertex>\S+) fragment=(?P<fragment>\S+) "
    r"made=(?P<made>[01]) archived=(?P<archived>[012]) "
    r"pipelines=(?P<pipelines>\d+) success=(?P<success>\d+) "
    r"required=(?P<required>\d+) valid=(?P<valid>\d+) hits=(?P<hits>\d+) "
    r"app_calls=(?P<app_calls>\d+) app_valid=(?P<app_valid>\d+) "
    r"app_hits=(?P<app_hits>\d+) batches=(?P<batches>\d+) "
    r"driver_batches=(?P<driver_batches>\d+) "
    r"suppressed=(?P<suppressed>\d+)$",
    re.MULTILINE,
)

CALL = re.compile(
    r"^QC vk phase=(?P<phase>\S+) recipe=(?P<recipe>\d+) "
    r"flags=(?P<flags>\S+) flags2_present=(?P<flags2_present>\S+) "
    r"flags2=(?P<flags2>\S+) strict=(?P<strict>\S+) "
    r"stages=(?P<stages>\S+) libraries=(?P<libraries>\S+) "
    r"cache=(?P<cache>[01]) result=(?P<result>\S+) "
    r"result_raw=(?P<result_raw>-?\d+) pipelines=(?P<pipelines>\S+) "
    r"feedback=(?P<feedback>\S+) nonnull=(?P<nonnull>\d+)$",
    re.MULTILINE,
)

RECIPE_AGGREGATES = {
    "vulkan_pipelines": "pipelines",
    "vulkan_success": "success",
    "vulkan_required_batches": "required",
    "vulkan_valid": "valid",
    "vulkan_hits": "hits",
    "app_cache_pipelines": "app_calls",
    "app_cache_valid": "app_valid",
    "app_cache_hits": "app_hits",
    "vulkan_batches": "batches",
    "vulkan_driver_batches": "driver_batches",
    "strict_suppressed": "suppressed",
}

STRICT_BIT = 0x00000100
LIBRARY_BIT = 0x00000800
FEEDBACK_VALID_BIT = 0x00000001
FEEDBACK_CACHE_HIT_BIT = 0x00000002
VK_SUCCESS_RESULT = 0
VK_PIPELINE_COMPILE_REQUIRED_RESULT = 1000297000


def _integers(value, base=10):
    return [int(item, base) for item in value.split("|")]


def _flags(value):
    return [int(item, 0) for item in value.split("|")]


def _booleans(value):
    return [item == "1" for item in value.split("|")]


def _effective_flags(call):
    return [
        call["flags2"][index] if call["flags2_present"][index]
        else call["flags"][index]
        for index in range(len(call["flags"]))
    ]


def parse_case(text):
    summaries = list(SUMMARY.finditer(text))
    require(len(summaries) == 1, "expected exactly one QuartzCore summary")
    match = summaries[0]
    summary = {name: (value if name == "mode" else int(value))
               for name, value in match.groupdict().items()}
    recipes = []
    for row in RECIPE.finditer(text):
        data = row.groupdict()
        data["index"] = int(data["index"])
        for key in ("made", "archived", "pipelines", "success", "required",
                    "valid", "hits", "app_calls", "app_valid", "app_hits",
                    "batches", "driver_batches", "suppressed"):
            data[key] = int(data[key])
        recipes.append(data)
    calls = []
    for row in CALL.finditer(text):
        data = row.groupdict()
        data["recipe"] = int(data["recipe"])
        data["cache"] = int(data["cache"])
        data["result_raw"] = int(data["result_raw"])
        data["nonnull"] = int(data["nonnull"])
        data["flags"] = _flags(data["flags"])
        data["flags2_present"] = _booleans(data["flags2_present"])
        data["flags2"] = _flags(data["flags2"])
        data["strict"] = _booleans(data["strict"])
        data["stages"] = _integers(data["stages"])
        data["libraries"] = _booleans(data["libraries"])
        data["pipelines"] = _booleans(data["pipelines"])
        data["feedback"] = _flags(data["feedback"])
        calls.append(data)
    return {"summary": summary, "recipes": recipes, "calls": calls}


def _check_recipes(case, expected):
    summary = case["summary"]
    recipes = case["recipes"]
    require(summary["recipes"] == expected,
            "attempted %d recipes, expected %d" % (summary["recipes"], expected))
    require(len(recipes) == expected,
            "recorded %d recipe results, expected %d" % (len(recipes), expected))
    require([row["index"] for row in recipes] == list(range(expected)),
            "recipe result indexes are not complete and ordered")
    require(all(row["mode"] == summary["mode"] for row in recipes),
            "recipe results contain a different mode")
    require(summary["recipe_ok"] == sum(row["made"] for row in recipes),
            "recipe_ok does not match the per-recipe results")
    for aggregate, per_recipe in RECIPE_AGGREGATES.items():
        require(summary[aggregate] == sum(row[per_recipe] for row in recipes),
                "%s does not match the per-recipe results" % aggregate)


def validate_produce(case):
    _check_recipes(case, 24)
    summary = case["summary"]
    require(summary["mode"] == "produce", "producer mode changed")
    require(summary["recipe_ok"] == 24, "not every QuartzCore recipe completed")
    require(summary["vulkan_required_batches"] == 0 and
            summary["strict_suppressed"] == 0,
            "producer unexpectedly forbade pipeline compilation")
    require(summary["vulkan_pipelines"] == summary["vulkan_success"] ==
            summary["vulkan_valid"],
            "producer did not create a valid pipeline for every Vulkan entry")
    require(summary["vulkan_pipelines"] >= summary["recipes"] and
            summary["vulkan_driver_batches"] > 0,
            "producer recorded no real Vulkan graphics create")
    require(summary["app_cache_pipelines"] > 0,
            "producer did not use the application pipeline cache")
    require(summary["app_cache_valid"] == summary["app_cache_pipelines"],
            "producer application-cache feedback was not valid")
    require(summary["app_cache_hits"] == 0 and summary["vulkan_hits"] == 0,
            "producer cold run reported a cache hit")
    require(summary["shader_misses"] > 0 and summary["shader_writes"] > 0,
            "producer did not miss and populate the shader cache")
    for row in case["recipes"]:
        require(row["made"] == 1 and row["archived"] == 1,
                "recipe %d was not made and archived" % row["index"])
        require(row["pipelines"] > 0 and row["success"] == row["valid"] == row["pipelines"],
                "recipe %d did not create valid Vulkan pipeline entries" % row["index"])
        require(row["required"] == 0 and row["suppressed"] == 0 and row["hits"] == 0,
                "recipe %d compiled under a forbidden policy or hit a cache" % row["index"])
        require(row["app_valid"] == row["app_calls"],
                "recipe %d application-cache feedback was invalid" % row["index"])


def validate_consume(case):
    _check_recipes(case, 24)
    summary = case["summary"]
    require(summary["mode"] == "consume", "consumer mode changed")
    require(summary["recipe_ok"] == 24, "not every QuartzCore recipe was consumed")
    require(summary["vulkan_required_batches"] == 0 and
            summary["strict_suppressed"] == 0,
            "consumer attempted a forbidden pipeline compilation")
    require(summary["vulkan_pipelines"] == summary["vulkan_success"] ==
            summary["vulkan_valid"],
            "consumer did not create a valid pipeline for every Vulkan entry")
    require(summary["app_cache_pipelines"] > 0 and
            summary["app_cache_valid"] == summary["app_cache_pipelines"] and
            summary["app_cache_hits"] == summary["app_cache_pipelines"],
            "consumer application-cache feedback was not a complete hit")
    require(summary["shader_misses"] == 0 and summary["shader_writes"] == 0,
            "consumer compiled or wrote a shader")
    require(summary["driver_loads"] > 0 and summary["driver_misses"] == 0,
            "consumer did not load the driver pipeline cache without a miss")

    calls_by_recipe = {}
    for call in case["calls"]:
        if call["phase"] != "recipe":
            continue
        require(call["recipe"] < summary["recipes"],
                "consumer trace names a recipe outside the recorded range")
        calls_by_recipe.setdefault(call["recipe"], []).append(call)

    totals = {
        "pipelines": 0,
        "success": 0,
        "required": 0,
        "valid": 0,
        "hits": 0,
        "app_calls": 0,
        "app_valid": 0,
        "app_hits": 0,
        "batches": 0,
        "driver_batches": 0,
        "suppressed": 0,
    }
    shaderless_links = 0
    shader_bearing_entries = 0
    for row in case["recipes"]:
        require(row["made"] == 1 and row["archived"] == 2,
                "recipe %d was not made from the loaded archive" % row["index"])
        require(row["required"] == 0 and row["suppressed"] == 0,
                "recipe %d required a forbidden compilation" % row["index"])
        calls = calls_by_recipe.get(row["index"])
        require(calls, "recipe %d has no consumer CALL trace" % row["index"])
        actual = {
            "pipelines": 0,
            "success": 0,
            "required": 0,
            "valid": 0,
            "hits": 0,
            "app_calls": 0,
            "app_valid": 0,
            "app_hits": 0,
            "batches": len(calls),
            "driver_batches": len(calls),
            "suppressed": 0,
        }
        for call in calls:
            fields = ("flags", "flags2_present", "flags2", "strict", "stages",
                      "libraries", "pipelines", "feedback")
            require(len({len(call[field]) for field in fields}) == 1,
                    "recipe %d has inconsistent per-entry CALL evidence" %
                    row["index"])
            require(all(stage >= 0 for stage in call["stages"]),
                    "recipe %d CALL has a negative shader stage count" %
                    row["index"])
            effective_flags = _effective_flags(call)
            effective_strict = [bool(flags & STRICT_BIT)
                                for flags in effective_flags]
            require(call["strict"] == effective_strict,
                    "recipe %d CALL strict report disagrees with effective flags" %
                    row["index"])
            require(all(effective_strict),
                    "recipe %d CALL did not pass effective strict flags" %
                    row["index"])
            require(call["libraries"] ==
                    [bool(flags & LIBRARY_BIT) for flags in effective_flags],
                    "recipe %d CALL library report disagrees with effective flags" %
                    row["index"])
            require(call["result"] == "success" and
                    call["result_raw"] == VK_SUCCESS_RESULT,
                    "recipe %d CALL returned required or error result %s/%d" %
                    (row["index"], call["result"], call["result_raw"]))
            require(call["nonnull"] == sum(call["pipelines"]),
                    "recipe %d CALL non-null count disagrees with its pipelines" %
                    row["index"])
            require(all(call["pipelines"]),
                    "recipe %d successful CALL returned a null pipeline" %
                    row["index"])
            actual["pipelines"] += len(call["pipelines"])
            actual["success"] += sum(call["pipelines"])
            for index, pipeline in enumerate(call["pipelines"]):
                feedback = call["feedback"][index]
                require(feedback & FEEDBACK_VALID_BIT,
                        "recipe %d non-null pipeline returned invalid feedback" %
                        row["index"])
                actual["valid"] += 1
                if call["cache"]:
                    actual["app_calls"] += 1
                    actual["app_valid"] += 1
                    require(feedback & FEEDBACK_CACHE_HIT_BIT,
                            "recipe %d cache-bearing entry missed its application cache" %
                            row["index"])
                    actual["hits"] += 1
                    actual["app_hits"] += 1
                else:
                    require(not feedback & FEEDBACK_CACHE_HIT_BIT,
                            "recipe %d cacheless entry reported an application-cache hit" %
                            row["index"])
                    require(call["stages"][index] == 0,
                            "recipe %d shader-bearing entry has no pipeline cache" %
                            row["index"])
                    shaderless_links += 1
                if call["stages"][index] > 0:
                    shader_bearing_entries += 1
        for field, value in actual.items():
            require(row[field] == value,
                    "recipe %d %s does not match its CALL trace" %
                    (row["index"], field))
        for field, value in actual.items():
            totals[field] += value

    require(summary["vulkan_hits"] == summary["app_cache_hits"],
            "consumer reported a cache hit without a cache-bearing entry")
    return {
        "recipe_calls": len(calls_by_recipe),
        "batches": totals["batches"],
        "pipeline_entries": totals["pipelines"],
        "successful_entries": totals["success"],
        "valid_entries": totals["valid"],
        "app_cache_entries": totals["app_calls"],
        "app_cache_hits": totals["app_hits"],
        "shader_bearing_entries": shader_bearing_entries,
        "shaderless_links": shaderless_links,
    }


def validate_refusal(case, uuid_mutated):
    _check_recipes(case, 1)
    summary = case["summary"]
    expected_mode = "uuid-miss" if uuid_mutated else "empty-driver"
    require(summary["mode"] == expected_mode,
            "negative control mode does not match its UUID mutation contract")
    require(summary["uuid_mutations"] == int(uuid_mutated),
            "negative control changed its UUID mutation contract")
    require(summary["recipe_ok"] == case["recipes"][0]["made"],
            "negative control recipe completion disagrees with its Metal object")
    require(summary["vulkan_driver_batches"] > 0,
            "negative control did not enter the real Vulkan driver")
    require(summary["shader_hits"] == 0 and summary["driver_hits"] == 0 and
            summary["app_cache_hits"] == 0 and summary["vulkan_hits"] == 0,
            "negative control reported a cache hit")
    require(summary["shader_misses"] == 0 and summary["shader_writes"] == 0,
            "negative control compiled or wrote a shader")

    recipe_calls = [call for call in case["calls"]
                    if call["phase"] == "recipe"]
    require(recipe_calls, "negative control has no recipe CALL trace")
    actual = {
        "pipelines": 0,
        "success": 0,
        "required": 0,
        "valid": 0,
        "hits": 0,
        "app_calls": 0,
        "app_valid": 0,
        "app_hits": 0,
        "batches": len(recipe_calls),
        "driver_batches": len(recipe_calls),
        "suppressed": 0,
    }
    shaderless_successes = 0
    shaderless_library_links = 0
    shader_bearing_required = 0
    for call in recipe_calls:
        require(call["recipe"] < summary["recipes"],
                "negative CALL names a recipe outside the recorded range")
        fields = ("flags", "flags2_present", "flags2", "strict", "stages",
                  "libraries", "pipelines", "feedback")
        require(len({len(call[field]) for field in fields}) == 1,
                "negative recipe CALL has inconsistent per-entry evidence")
        require(all(stage >= 0 for stage in call["stages"]),
                "negative recipe CALL has a negative shader stage count")
        effective_flags = _effective_flags(call)
        effective_strict = [bool(flags & STRICT_BIT)
                            for flags in effective_flags]
        require(call["strict"] == effective_strict,
                "negative recipe CALL strict report disagrees with effective flags")
        require(all(effective_strict),
                "negative recipe CALL did not pass effective strict flags")
        require(call["libraries"] ==
                [bool(flags & LIBRARY_BIT) for flags in effective_flags],
                "negative recipe CALL library report disagrees with effective flags")
        require(call["nonnull"] == sum(call["pipelines"]),
                "negative recipe CALL non-null count disagrees with its pipelines")
        require(call["result"] in ("success", "compile_required"),
                "negative recipe CALL returned an unexpected Vulkan result")
        expected_result = (VK_SUCCESS_RESULT if call["result"] == "success"
                           else VK_PIPELINE_COMPILE_REQUIRED_RESULT)
        require(call["result_raw"] == expected_result,
                "negative recipe result label disagrees with its raw result")
        require(not any(feedback & FEEDBACK_CACHE_HIT_BIT
                        for feedback in call["feedback"]),
                "negative recipe CALL reported a cache hit")

        if call["result"] == "success":
            require(all(stage == 0 for stage in call["stages"]),
                    "negative recipe CALL allowed a shader-bearing success")
            require(call["cache"] == 0,
                    "negative recipe shaderless success used a pipeline cache")
            require(all(call["libraries"]),
                    "negative recipe shaderless success lacked a library link")
            require(all(call["pipelines"]),
                    "negative recipe successful CALL returned a null pipeline")
            for feedback in call["feedback"]:
                require(feedback & FEEDBACK_VALID_BIT,
                        "negative recipe shaderless success returned invalid feedback")
                shaderless_successes += 1
                shaderless_library_links += 1
        else:
            require(all(stage > 0 for stage in call["stages"]),
                    "negative recipe compile-required CALL was not shader-bearing")
            require(not any(call["pipelines"]),
                    "negative recipe compile-required CALL returned a pipeline")
            shader_bearing_required += len(call["stages"])

        actual["pipelines"] += len(call["pipelines"])
        actual["success"] += sum(call["pipelines"])
        if call["result"] == "compile_required":
            actual["required"] += len(call["stages"])
        for index, pipeline in enumerate(call["pipelines"]):
            feedback = call["feedback"][index]
            if call["cache"]:
                actual["app_calls"] += 1
            if pipeline and feedback & FEEDBACK_VALID_BIT:
                actual["valid"] += 1
                if call["cache"]:
                    actual["app_valid"] += 1

    require(shader_bearing_required > 0,
            "negative control has no shader-bearing compile-required driver CALL")
    row = case["recipes"][0]
    for field, value in actual.items():
        require(row[field] == value,
                "negative recipe %s does not match its CALL trace" % field)
    return {
        "recipe_calls": len(recipe_calls),
        "driver_batches": len(recipe_calls),
        "driver_compile_required_entries": shader_bearing_required,
        "shader_bearing_compile_required_entries": shader_bearing_required,
        "shaderless_successes": shaderless_successes,
        "shaderless_library_links": shaderless_library_links,
        "metal_object_returned": bool(row["made"]),
        "object_drawability_verified": False,
        "init_calls_ignored": sum(call["phase"] == "init"
                                  for call in case["calls"]),
        "driver_compile_required_verified": True,
    }


def validate_cold(case):
    summary = case["summary"]
    calls = [call for call in case["calls"] if call["phase"] == "cold"]
    require(calls, "cold control recorded no per-batch Vulkan call")
    for call in calls:
        fields = ("flags", "flags2_present", "flags2", "strict", "stages",
                  "libraries", "pipelines", "feedback")
        require(len({len(call[field]) for field in fields}) == 1,
                "cold Vulkan call has inconsistent per-entry evidence")
        effective_flags = [
            (call["flags2"][index]
             if call["flags2_present"][index] else call["flags"][index])
            for index in range(len(call["flags"]))
        ]
        effective = [bool(flags & STRICT_BIT) for flags in effective_flags]
        require(call["strict"] == effective,
                "cold Vulkan call strict report disagrees with effective flags")
        require(call["libraries"] ==
                [bool(flags & LIBRARY_BIT) for flags in effective_flags],
                "cold Vulkan call library report disagrees with effective flags")
        require(call["nonnull"] == sum(call["pipelines"]),
                "cold Vulkan call non-null count disagrees with its pipelines")
        require(all(effective),
                "cold Vulkan call did not pass strict flags to the driver")
    require(summary["cold_driver_batches"] > 0,
            "cold control made no real Vulkan graphics create call")
    require(summary["cold_driver_batches"] == len(calls),
            "cold batch count does not match the per-batch Vulkan calls")
    require(summary["cold_strict_suppressed"] == 0,
            "cold control suppressed a real Vulkan call")
    require(summary["cold_driver_required"] > 0,
            "cold control did not receive a compile-required driver result")
    required = [call for call in calls
                if call["result"] == "compile_required"]
    require(summary["cold_driver_required"] == len(required),
            "cold compile-required count does not match the Vulkan calls")
    for call in required:
        require(not any(call["pipelines"]),
                "compile-required cold call returned a non-null pipeline")
    require(summary["cold_driver_success"] ==
            sum(sum(call["pipelines"]) for call in calls),
            "cold pipeline count does not match the Vulkan calls")
    shaderless = 0
    cached_shader_successes = 0
    for call in calls:
        require(call["result"] in ("success", "compile_required"),
                "cold call returned an unexpected Vulkan result")
        expected_result = (VK_SUCCESS_RESULT if call["result"] == "success"
                           else VK_PIPELINE_COMPILE_REQUIRED_RESULT)
        require(call["result_raw"] == expected_result,
                "cold Vulkan result label disagrees with its raw result")
        if call["result"] != "success":
            continue
        require(all(call["pipelines"]),
                "successful cold batch returned a null pipeline")
        for index, pipeline in enumerate(call["pipelines"]):
            if not pipeline:
                continue
            if call["stages"][index] == 0:
                require(call["libraries"][index],
                        "zero-stage cold success lacked pipeline-library evidence")
                shaderless += 1
                continue
            require(call["cache"] == 1 and
                    call["feedback"][index] & FEEDBACK_VALID_BIT and
                    call["feedback"][index] & FEEDBACK_CACHE_HIT_BIT,
                    "strict shader-bearing pipeline succeeded without cache-hit evidence")
            cached_shader_successes += 1
    return {
        "batches": len(calls),
        "compile_required_batches": len(required),
        "driver_success_handles": summary["cold_driver_success"],
        "shaderless_successes": shaderless,
        "cached_shader_successes": cached_shader_successes,
        "metal_object_returned": bool(summary["cold_object"]),
    }
