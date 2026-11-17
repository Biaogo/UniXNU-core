#!/usr/bin/env python3
"""Linux native regression for the QuartzCore graphics-cache loader wrapper.

The original source hashes are pinned before the instrument patch is replayed
with zero fuzz. Only the patched graphics_cache_loader.cpp is compiled. The
Metal host program is never built, and every Vulkan driver declaration below is
an explicitly marked FAKE_DRIVER used only to exercise the wrapper.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap

from quartzcore_turnip_cache import (
    CALL,
    EvidenceError,
    LIBRARY_BIT,
    STRICT_BIT,
    VK_PIPELINE_COMPILE_REQUIRED_RESULT,
    VK_SUCCESS_RESULT,
    validate_cold,
)

ROOT = Path(__file__).resolve().parents[1]
GXX = Path('/etc/profiles/per-user/biaogo/bin/g++')
VULKAN_INCLUDE = Path(
    '/nix/store/wj32fr9qda2anncv77ah46vxkhdz22qv-vulkan-headers-1.4.363.0/include')
INSTRUMENT_PATCH = ROOT / 'scripts/data/quartzcore-cache-host-instrument.patch'
SOURCE_SHA256 = {
    'drivers/AdrenoMetal/bridge/quartzcore_cache.mm':
        'eba452cfe53cf40692d00559aa610444fe2f324d8d54fbb5e0ddfe326c958e12',
    'drivers/AdrenoMetal/bridge/graphics_cache_loader.cpp':
        'e67a30a638a8abfab023036c32e953ffa799d4b79ba001f9ab20445362b621a7',
}


def require(condition, reason):
    if not condition:
        raise SystemExit('FAIL: ' + reason)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command):
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                            timeout=120)
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise SystemExit('FAIL: command exited %d: %s\n%s' %
                         (result.returncode, ' '.join(map(str, command)), detail))
    return result


FAKE_DRIVER_SOURCE = r'''
#define FAKE_DRIVER 1
#if !FAKE_DRIVER
#error "This Vulkan driver is test-only and must not be used as a real build"
#endif
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan.h>
#include <cstdint>
#include <cstring>

namespace {
struct FakeState {
    VkResult result = VK_SUCCESS;
    uintptr_t pipelineBase = 0;
    VkPipelineCreationFeedbackFlags feedbackFlags = 0;
    uint32_t calls = 0;
    uint32_t count = 0;
    VkPipelineCreateFlags flags[2] = {};
    uint64_t flags2[2] = {};
    uint8_t flags2Present[2] = {};
    uint8_t feedbackPresent[2] = {};
};

FakeState state;

VKAPI_ATTR VkResult VKAPI_CALL createGraphics(
    VkDevice, VkPipelineCache, uint32_t count,
    const VkGraphicsPipelineCreateInfo *infos,
    const VkAllocationCallbacks *, VkPipeline *pipelines) {
    state.calls++;
    state.count = count;
    for (uint32_t i = 0; i < count; ++i) {
        state.flags[i] = infos[i].flags;
        pipelines[i] = state.result == VK_SUCCESS ?
            reinterpret_cast<VkPipeline>(state.pipelineBase + i) : VK_NULL_HANDLE;
        for (auto *p = static_cast<const VkBaseInStructure *>(infos[i].pNext);
             p; p = p->pNext) {
            if (p->sType == VK_STRUCTURE_TYPE_PIPELINE_CREATE_FLAGS_2_CREATE_INFO) {
                const auto *flags2 =
                    reinterpret_cast<const VkPipelineCreateFlags2CreateInfo *>(p);
                state.flags2[i] = flags2->flags;
                state.flags2Present[i] = 1;
            }
            if (p->sType == VK_STRUCTURE_TYPE_PIPELINE_CREATION_FEEDBACK_CREATE_INFO) {
                const auto *feedback =
                    reinterpret_cast<const VkPipelineCreationFeedbackCreateInfo *>(p);
                state.feedbackPresent[i] =
                    feedback->pPipelineCreationFeedback ? 1 : 0;
                if (feedback->pPipelineCreationFeedback) {
                    feedback->pPipelineCreationFeedback->flags =
                        state.result == VK_SUCCESS ? state.feedbackFlags : 0;
                    feedback->pPipelineCreationFeedback->duration = 7;
                }
            }
        }
    }
    return state.result;
}

template <typename T, typename U>
PFN_vkVoidFunction vulkanFunction(U function) {
    static_assert(sizeof(T) == sizeof(function),
                  "Vulkan function pointer sizes differ");
    T wrapped{};
    std::memcpy(&wrapped, &function, sizeof(wrapped));
    return reinterpret_cast<PFN_vkVoidFunction>(wrapped);
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL getDeviceProcAddr(
    VkDevice, const char *name) {
    if (!std::strcmp(name, "vkCreateGraphicsPipelines"))
        return vulkanFunction<PFN_vkCreateGraphicsPipelines>(createGraphics);
    return nullptr;
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL getInstanceProcAddr(
    VkInstance, const char *name) {
    if (!std::strcmp(name, "vkGetDeviceProcAddr"))
        return vulkanFunction<PFN_vkGetDeviceProcAddr>(getDeviceProcAddr);
    if (!std::strcmp(name, "vkCreateGraphicsPipelines"))
        return vulkanFunction<PFN_vkCreateGraphicsPipelines>(createGraphics);
    return nullptr;
}
}  // namespace

extern "C" {

void fake_configure(VkResult result, uintptr_t pipelineBase,
                    uint32_t feedbackFlags) {
    state = FakeState{};
    state.result = result;
    state.pipelineBase = pipelineBase;
    state.feedbackFlags =
        static_cast<VkPipelineCreationFeedbackFlags>(feedbackFlags);
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(
    VkDevice device, const char *name) {
    return getDeviceProcAddr(device, name);
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(
    VkInstance instance, const char *name) {
    return getInstanceProcAddr(instance, name);
}

}  // extern "C"
'''


WRAPPER_TEST_SOURCE = r'''
#define FAKE_DRIVER 1
#if !FAKE_DRIVER
#error "This test-only translation unit must never replace a real driver build"
#endif
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <string>
#include "graphics_cache_loader.cpp"

namespace {
using Configure = void (*)(VkResult, uintptr_t, uint32_t);

bool check(bool condition, const char *label) {
    if (!condition) std::fprintf(stderr, "FAIL: native wrapper: %s\n", label);
    return condition;
}

template <typename T>
T symbol(void *handle, const char *name) {
    void *raw = dlsym(handle, name);
    if (!raw) {
        std::fprintf(stderr, "FAIL: missing fake symbol %s: %s\n",
                     name, dlerror());
        std::exit(1);
    }
    static_assert(sizeof(T) == sizeof(raw), "symbol pointer sizes differ");
    T function{};
    std::memcpy(&function, &raw, sizeof(function));
    return function;
}

template <typename T>
T vulkanFunction(PFN_vkVoidFunction raw) {
    if (!raw) return nullptr;
    static_assert(sizeof(T) == sizeof(raw), "Vulkan function pointer sizes differ");
    T function{};
    std::memcpy(&function, &raw, sizeof(function));
    return function;
}

bool call(PFN_vkCreateGraphicsPipelines create, Configure configure,
          const char *phase, size_t recipe, bool strictControl,
          VkPipelineCreateFlags legacyFlags, bool hasFlags2,
          VkPipelineCreateFlags2 flags2Flags, VkResult driverResult,
          uintptr_t driverPipeline, VkResult expectedResult,
          uintptr_t expectedPipeline) {
    VkPipelineCreateFlags2CreateInfo flags2 = {};
    flags2.sType = VK_STRUCTURE_TYPE_PIPELINE_CREATE_FLAGS_2_CREATE_INFO;
    flags2.flags = flags2Flags;

    VkGraphicsPipelineCreateInfo info = {};
    info.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;
    info.flags = legacyFlags;
    info.pNext = hasFlags2 ? &flags2 : nullptr;
    info.stageCount = 2;

    const VkPipelineCreateFlags savedFlags = info.flags;
    const void *savedNext = info.pNext;
    const VkPipelineCreateFlags2 savedFlags2 = flags2.flags;
    uint64_t before[counterCount] = {};
    uint64_t after[counterCount] = {};
    nmb_test_graphics_cache_counts(before);
    configure(driverResult, driverPipeline,
              driverResult == VK_SUCCESS ?
              (VK_PIPELINE_CREATION_FEEDBACK_VALID_BIT |
               VK_PIPELINE_CREATION_FEEDBACK_APPLICATION_PIPELINE_CACHE_HIT_BIT) : 0);
    nmb_test_graphics_cache_control(strictControl, false);
    nmb_test_graphics_cache_phase(phase, recipe);

    VkPipeline pipeline = VK_NULL_HANDLE;
    const VkDevice device =
        reinterpret_cast<VkDevice>(static_cast<uintptr_t>(0x11));
    const VkPipelineCache cache =
        reinterpret_cast<VkPipelineCache>(static_cast<uintptr_t>(0x22));
    const VkResult result =
        create(device, cache, 1, &info, nullptr, &pipeline);
    nmb_test_graphics_cache_counts(after);

    const uint64_t success = after[1] - before[1];
    const uint64_t required = after[2] - before[2];
    const uint64_t valid = after[3] - before[3];
    const uint64_t hits = after[4] - before[4];
    const uint64_t wrapperBatches = after[10] - before[10];
    const uint64_t suppressed = after[11] - before[11];
    const uint64_t driverBatches = after[12] - before[12];
    const bool expectedSuccess = driverResult == VK_SUCCESS;
    const uintptr_t expectedHandle =
        expectedSuccess ? expectedPipeline : static_cast<uintptr_t>(0);

    return
        check(info.flags == savedFlags, "caller legacy flags were modified") &&
        check(info.pNext == savedNext, "caller pNext chain was modified") &&
        check(flags2.flags == savedFlags2, "caller flags2 structure was modified") &&
        check(result == expectedResult, "wrapper changed the driver result") &&
        check(reinterpret_cast<uintptr_t>(pipeline) == expectedHandle,
              "wrapper changed the driver pipeline handle") &&
        check(wrapperBatches == 1 && driverBatches == 1,
              "wrapper did not forward exactly one batch") &&
        check(suppressed == 0, "wrapper suppressed a driver batch") &&
        check(success == (expectedSuccess ? 1u : 0u),
              "success handle count disagrees with the driver") &&
        check(required ==
              (driverResult == VK_PIPELINE_COMPILE_REQUIRED ? 1u : 0u),
              "compile-required count disagrees with the driver") &&
        check(valid == (expectedSuccess ? 1u : 0u),
              "explicit fake feedback was not observed") &&
        check(hits == (expectedSuccess ? 1u : 0u),
              "explicit fake cache-hit feedback was not observed");
}
}  // namespace

int main(int argc, char **argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: %s FAKE_DRIVER.so\n", argv[0]);
        return 2;
    }
    if (setenv("NMB_TEST_REAL_VULKAN_LOADER", argv[1], 1)) {
        std::fprintf(stderr, "FAIL: setenv\n");
        return 1;
    }
    void *fake = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!fake) {
        std::fprintf(stderr, "FAIL: load fake driver: %s\n", dlerror());
        return 1;
    }
    const Configure configure = symbol<Configure>(fake, "fake_configure");
    const VkInstance instance =
        reinterpret_cast<VkInstance>(static_cast<uintptr_t>(0x33));
    const VkDevice device =
        reinterpret_cast<VkDevice>(static_cast<uintptr_t>(0x44));
    const auto getDevice = vulkanFunction<PFN_vkGetDeviceProcAddr>(
        vkGetInstanceProcAddr(instance, "vkGetDeviceProcAddr"));
    if (!getDevice) {
        std::fprintf(stderr, "FAIL: wrapper did not expose vkGetDeviceProcAddr\n");
        return 1;
    }
    const auto create = vulkanFunction<PFN_vkCreateGraphicsPipelines>(
        getDevice(device, "vkCreateGraphicsPipelines"));
    if (!create) {
        std::fprintf(stderr, "FAIL: wrapper did not expose vkCreateGraphicsPipelines\n");
        return 1;
    }

    bool ok = true;
    ok &= call(create, configure, "strict-success", 10, true, 0, false, 0,
               VK_SUCCESS, 0x1234, VK_SUCCESS, 0x1234);
    ok &= call(create, configure, "required-passthrough", 11, true, 0, false, 0,
               VK_PIPELINE_COMPILE_REQUIRED, 0,
               VK_PIPELINE_COMPILE_REQUIRED, 0);
    ok &= call(create, configure, "legacy-overridden", 12, true,
               VK_PIPELINE_CREATE_FAIL_ON_PIPELINE_COMPILE_REQUIRED_BIT |
               VK_PIPELINE_CREATE_LIBRARY_BIT_KHR, true, 0,
               VK_SUCCESS, 0x5678, VK_SUCCESS, 0x5678);
    ok &= call(create, configure, "flags2-effective", 13, false, 0, true,
               VK_PIPELINE_CREATE_FAIL_ON_PIPELINE_COMPILE_REQUIRED_BIT |
               VK_PIPELINE_CREATE_LIBRARY_BIT_KHR,
               VK_SUCCESS, 0x9abc, VK_SUCCESS, 0x9abc);
    if (!ok) return 1;
    std::printf("PASS: FAKE_DRIVER GIPA/GDPA forwarding, explicit handles/feedback, "
                "and caller-owned structures\n");
    return 0;
}
'''


def split_flags(value):
    return [int(item, 0) for item in value.split('|')]


def split_bools(value):
    return [item == '1' for item in value.split('|')]


def parse_calls(text):
    calls = {}
    for match in CALL.finditer(text):
        data = match.groupdict()
        data['recipe'] = int(data['recipe'])
        data['cache'] = int(data['cache'])
        data['result_raw'] = int(data['result_raw'])
        data['nonnull'] = int(data['nonnull'])
        data['flags'] = split_flags(data['flags'])
        data['flags2_present'] = split_bools(data['flags2_present'])
        data['flags2'] = split_flags(data['flags2'])
        data['strict'] = split_bools(data['strict'])
        data['stages'] = [int(item) for item in data['stages'].split('|')]
        data['libraries'] = split_bools(data['libraries'])
        data['pipelines'] = split_bools(data['pipelines'])
        data['feedback'] = split_flags(data['feedback'])
        require(data['phase'] not in calls,
                'duplicate native wrapper phase ' + data['phase'])
        calls[data['phase']] = data
    return calls


def main():
    require(GXX.is_file(), 'g++ is missing: ' + str(GXX))
    require((VULKAN_INCLUDE / 'vulkan/vulkan.h').is_file(),
            'Vulkan headers are missing: ' + str(VULKAN_INCLUDE))
    require(INSTRUMENT_PATCH.is_file(),
            'instrument patch is missing: ' + str(INSTRUMENT_PATCH))

    temporary_root = Path(os.environ.get('TMPDIR', tempfile.gettempdir()))
    require(temporary_root.is_dir(), 'TMPDIR is not a directory: ' +
            str(temporary_root))
    with tempfile.TemporaryDirectory(
            prefix='quartzcore-cache-loader-', dir=temporary_root) as temporary:
        scratch = Path(temporary)
        for relative, expected in SOURCE_SHA256.items():
            source = ROOT / relative
            require(source.is_file(), 'original source is missing: ' + relative)
            actual = sha(source)
            require(actual == expected,
                    'original source SHA256 changed: %s is %s, expected %s' %
                    (relative, actual, expected))
            shutil.copy2(source, scratch / source.name)

        run(['patch', '--silent', '--batch', '--forward', '--fuzz=0',
             '--no-backup-if-mismatch', '-p0', '-d', scratch,
             '-i', INSTRUMENT_PATCH])
        host = (scratch / 'quartzcore_cache.mm').read_text()
        phase_position = host.index('phase("recipe",index);')
        archive_position = host.index(
            'archived=nmb_binary_archive_add_render')
        require(phase_position < archive_position,
                'recipe phase is still recorded after archive production')
        loader = (scratch / 'graphics_cache_loader.cpp').read_text()
        require('flags2Present[i]?flags2[i]:copies[i].flags' in loader,
                'loader does not use flags2 as the override')
        require('(copies[i].flags|flags2[i])' not in loader,
                'loader still ORs legacy flags with flags2')
        require('(effectiveFlags&VK_PIPELINE_CREATE_LIBRARY_BIT_KHR)' in loader,
                'loader does not judge library from effective flags')

        fake_source = scratch / 'fake_driver.cpp'
        wrapper_source = scratch / 'loader_wrapper_test.cpp'
        fake_source.write_text(textwrap.dedent(FAKE_DRIVER_SOURCE))
        wrapper_source.write_text(textwrap.dedent(WRAPPER_TEST_SOURCE))
        fake_driver = scratch / 'libfake_driver.so'
        wrapper_test = scratch / 'test-graphics-cache-loader'
        common = [str(GXX), '-O1', '-std=c++17', '-Wall', '-Wextra',
                  '-Werror', '-I', str(VULKAN_INCLUDE)]
        run([*common, '-fPIC', '-shared', fake_source, '-o', fake_driver])
        run([*common, wrapper_source, '-ldl', '-pthread', '-o', wrapper_test])

        result = run([wrapper_test, fake_driver])
        require('PASS: FAKE_DRIVER' in result.stdout,
                'native wrapper did not report success')
        calls = parse_calls(result.stdout + result.stderr)
        require(set(calls) == {
            'strict-success', 'required-passthrough',
            'legacy-overridden', 'flags2-effective',
        }, 'native wrapper did not record all Vulkan phases')

        strict = calls['strict-success']
        require(strict['flags'] == [STRICT_BIT] and
                strict['flags2_present'] == [False] and
                strict['strict'] == [True] and
                strict['libraries'] == [False] and
                strict['result_raw'] == VK_SUCCESS_RESULT and
                strict['pipelines'] == [True] and
                strict['feedback'] == [3],
                'legacy strict, handle, and feedback forwarding changed')

        required = calls['required-passthrough']
        require(required['strict'] == [True] and
                required['result'] == 'compile_required' and
                required['result_raw'] == VK_PIPELINE_COMPILE_REQUIRED_RESULT and
                required['pipelines'] == [False] and
                required['nonnull'] == 0,
                'real compile-required result was not passed through')

        overridden = calls['legacy-overridden']
        require(overridden['flags'] == [STRICT_BIT | LIBRARY_BIT] and
                overridden['flags2_present'] == [True] and
                overridden['flags2'] == [0] and
                overridden['strict'] == [False] and
                overridden['libraries'] == [False],
                'legacy injection escaped flags2 override or was mislabeled strict')
        try:
            validate_cold({
                'summary': {
                    'cold_driver_batches': 1,
                    'cold_strict_suppressed': 0,
                    'cold_driver_required': 0,
                    'cold_driver_success': 1,
                    'cold_object': 1,
                },
                'calls': [overridden],
            })
        except EvidenceError:
            pass
        else:
            raise SystemExit(
                'FAIL: legacy strict with present flags2=0 was accepted')

        effective = calls['flags2-effective']
        require(effective['flags'] == [0] and
                effective['flags2_present'] == [True] and
                effective['flags2'] == [STRICT_BIT | LIBRARY_BIT] and
                effective['strict'] == [True] and
                effective['libraries'] == [True] and
                effective['pipelines'] == [True],
                'flags2 strict/library bits were not used as effective flags')

    print('PASS: QuartzCore loader patch replay and Linux FAKE_DRIVER wrapper regression; '
          'flags2 overrides legacy flags, real results pass through, library uses '
          'effective flags, and the legacy-strict/flags2=0 counterexample is rejected')
    print('NOT COVERED: the FAKE_DRIVER is not a real Vulkan driver, and the Metal '
          'host .mm was not compiled or run')


if __name__ == '__main__':
    main()
