/* SPDX-License-Identifier: MIT */
/* Query the host NativeAdreno ABI.  Direct builds may also exercise allocation,
 * mapping and submission through the real software kernel peer.  This program
 * checks host wiring only; it executes no GPU work. */
#if defined(HOST_PEER_DIRECT)
#include "AdrenoPM4.h"
#endif
#include "NativeAdrenoABI.h"
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
#if defined(HOST_PEER_DIRECT)
extern "C" {
uint64_t native_adreno_now_us(void);
int host_gpu_open();
int host_gpu_close();
int host_gpu_call(uint32_t, const void *, size_t, void *, size_t);
int host_gpu_map(uint32_t, void **, uint64_t *);
unsigned host_gpu_submissions();
unsigned host_gpu_context_buffer_count();
unsigned host_gpu_context_input_bits();
unsigned host_gpu_context_output_bits();
}
#else
#include <dlfcn.h>
#endif

namespace {

struct Expected {
    NativeAdrenoAbiDeviceInfo info{};
    unsigned context_buffers = 0;
    unsigned input_bits = 0;
    unsigned output_bits = 0;
};

struct Api {
    int (*open)() = nullptr;
    int (*close)() = nullptr;
    int (*call)(uint32_t, const void *, size_t, void *, size_t) = nullptr;
    int (*map)(uint32_t, void **, uint64_t *) = nullptr;
    unsigned (*submissions)() = nullptr;
    unsigned (*context_buffers)() = nullptr;
    unsigned (*context_input_bits)() = nullptr;
    unsigned (*context_output_bits)() = nullptr;
    uint64_t (*now_us)() = nullptr;
};

uint64_t number(const char *text) {
    char *end = nullptr;
    const uint64_t value = std::strtoull(text, &end, 0);
    if (!end || *end) {
        std::fprintf(stderr, "probe: invalid number %s\n", text);
        std::exit(2);
    }
    return value;
}

bool load(const char *path, Api &api) {
#if defined(HOST_PEER_DIRECT)
    (void)path;
    api = {host_gpu_open, host_gpu_close, host_gpu_call, host_gpu_map, host_gpu_submissions,
           host_gpu_context_buffer_count, host_gpu_context_input_bits, host_gpu_context_output_bits,
           native_adreno_now_us};
    return true;
#else
    void *library = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!library) {
        std::fprintf(stderr, "probe: dlopen: %s\n", dlerror());
        return false;
    }
#define FIND(field, name) api.field = reinterpret_cast<decltype(api.field)>(dlsym(library, name)); \
    if (!api.field) { std::fprintf(stderr, "probe: missing %s\n", name); return false; }
    FIND(open, "host_gpu_open")
    FIND(close, "host_gpu_close")
    FIND(call, "host_gpu_call")
    FIND(map, "host_gpu_map")
    FIND(submissions, "host_gpu_submissions")
    FIND(context_buffers, "host_gpu_context_buffer_count")
    FIND(context_input_bits, "host_gpu_context_input_bits")
    FIND(context_output_bits, "host_gpu_context_output_bits")
    FIND(now_us, "native_adreno_now_us")
#undef FIND
    return true;
#endif
}

bool equal(const NativeAdrenoAbiDeviceInfo &got, const Expected &want) {
    return sizeof(NativeAdrenoAbiDeviceInfo) == 64 &&
        got.chip_id == want.info.chip_id &&
        got.gmem_bytes == want.info.gmem_bytes &&
        got.gmem_base == want.info.gmem_base &&
        got.uche_trap_base == want.info.uche_trap_base &&
        got.va_start == want.info.va_start &&
        got.va_size == want.info.va_size &&
        got.highest_bank_bit == want.info.highest_bank_bit &&
        got.ubwc_swizzle == want.info.ubwc_swizzle &&
        got.macrotile_mode == want.info.macrotile_mode &&
        got.ubwc_version == want.info.ubwc_version &&
        got.flags == want.info.flags &&
        got.reserved == 0;
}

void print_info(const NativeAdrenoAbiDeviceInfo &info, const Api &api) {
    std::printf(
        "chip=0x%08x gmem=%u base=0x%llx trap=0x%llx va=0x%llx+0x%llx "
        "hbb=%u swizzle=%u macrotile=%u ubwc=%u flags=%u context_buffers=%u "
        "context_input_bits=%u context_output_bits=%u\n",
        info.chip_id, info.gmem_bytes, (unsigned long long)info.gmem_base,
        (unsigned long long)info.uche_trap_base, (unsigned long long)info.va_start,
        (unsigned long long)info.va_size, info.highest_bank_bit, info.ubwc_swizzle,
        info.macrotile_mode, info.ubwc_version, info.flags, api.context_buffers(),
        api.context_input_bits(), api.context_output_bits());
}

} // namespace

int main(int argc, char **argv) {
    if (argc != 17 || (std::strcmp(argv[1], "info") && std::strcmp(argv[1], "exercise"))) {
#if defined(HOST_PEER_DIRECT)
        const char *modes = "info|exercise";
#else
        const char *modes = "info";
#endif
        std::fprintf(stderr,
            "usage: %s %s loader chip gmem_bytes gmem_base uche_trap va_start va_size "
            "hbb swizzle macrotile ubwc flags context_buffers input_bits output_bits\n",
            argv[0], modes);
        return 2;
    }
#if !defined(HOST_PEER_DIRECT)
    if (!std::strcmp(argv[1], "exercise")) {
        std::fprintf(stderr, "probe: exercise requires HOST_PEER_DIRECT\n");
        return 2;
    }
#endif
    Expected want;
    want.info.chip_id = uint32_t(number(argv[3]));
    want.info.gmem_bytes = uint32_t(number(argv[4]));
    want.info.gmem_base = number(argv[5]);
    want.info.uche_trap_base = number(argv[6]);
    want.info.va_start = number(argv[7]);
    want.info.va_size = number(argv[8]);
    want.info.highest_bank_bit = uint32_t(number(argv[9]));
    want.info.ubwc_swizzle = uint32_t(number(argv[10]));
    want.info.macrotile_mode = uint32_t(number(argv[11]));
    want.info.ubwc_version = uint32_t(number(argv[12]));
    want.info.flags = uint32_t(number(argv[13]));
    want.context_buffers = unsigned(number(argv[14]));
    want.input_bits = unsigned(number(argv[15]));
    want.output_bits = unsigned(number(argv[16]));

    Api api;
    if (!load(argv[2], api)) return 1;
    const uint64_t clock_begin = api.now_us();
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
    const uint64_t clock_end = api.now_us();
    if (!clock_begin || clock_end <= clock_begin) {
        std::fprintf(stderr, "probe: native_adreno_now_us is not a monotonic host clock: %llu -> %llu\n",
                     (unsigned long long)clock_begin, (unsigned long long)clock_end);
        return 1;
    }
    if (api.open() != 0) {
        std::fprintf(stderr, "probe: host_gpu_open failed\n");
        return 1;
    }
    NativeAdrenoAbiDeviceInfo got{};
    int status = api.call(NativeAdrenoMethodGetDeviceInfo, nullptr, 0, &got, sizeof(got));
    if (status || !equal(got, want)) {
        std::fprintf(stderr, "probe: GetDeviceInfo mismatch, status=%d\n", status);
        print_info(got, api);
        api.close();
        return 1;
    }
    if (api.context_buffers() != want.context_buffers ||
        api.context_input_bits() != want.input_bits ||
        api.context_output_bits() != want.output_bits) {
        std::fprintf(stderr, "probe: context did not take the selected chip contract\n");
        print_info(got, api);
        api.close();
        return 1;
    }
    print_info(got, api);
    std::printf("clock: native_adreno_now_us=%llu..%llu us monotonic\n",
                (unsigned long long)clock_begin, (unsigned long long)clock_end);

#if defined(HOST_PEER_DIRECT)
    if (!std::strcmp(argv[1], "exercise")) {
        /* tu_CreateDevice's first BO: sizeof(tu6_global) = 17664 plus
         * TU_BORDER_COLOR_COUNT * sizeof(bcolor_entry) = 4096 * 128 is
         * 0x84400. darwin_size_class() rounds that to a quarter of the next
         * power of two: 0xa0000. Keep the exact KMD request so this probe
         * covers the allocation that failed at tu_device.cc:3041. */
        constexpr uint64_t kCreateDeviceGlobalBytes = 0xa0000;
        NativeAdrenoAbiBoAlloc alloc{kCreateDeviceGlobalBytes, 12, 0};
        NativeAdrenoAbiBoAllocReply allocated{};
        status = api.call(NativeAdrenoMethodBoAlloc, &alloc, sizeof(alloc), &allocated, sizeof(allocated));
        if (status || !allocated.handle) {
            std::fprintf(stderr, "probe: BoAlloc failed, status=%d\n", status);
            api.close();
            return 1;
        }
        uint64_t map_iova = got.va_start;
        NativeAdrenoAbiBoMap map{allocated.handle, map_iova, 3, 0, 2, 0};
        NativeAdrenoAbiBoMapReply mapped{};
        std::printf("exercise: global BoAlloc bytes=%llu align_log2=%u contiguous=%u",
                    (unsigned long long)alloc.bytes, alloc.align_log2, alloc.contiguous);
        status = api.call(NativeAdrenoMethodBoMap, &map, sizeof(map), &mapped, sizeof(mapped));
        if (status || mapped.iova != map_iova) {
            std::fprintf(stderr, "probe: BoMap failed, status=%d iova=0x%llx\n", status,
                         (unsigned long long)map_iova);
            api.close();
            return 1;
        }
        NativeAdrenoAbiBoCpuMap cpu_map{allocated.handle};
        NativeAdrenoAbiBoCpuMapReply cpu{};
        status = api.call(NativeAdrenoMethodBoCpuMap, &cpu_map, sizeof(cpu_map), &cpu, sizeof(cpu));
        void *address = nullptr;
        uint64_t bytes = 0;
        if (status || !cpu.token || api.map(cpu.token, &address, &bytes) ||
            !address || bytes != kCreateDeviceGlobalBytes) {
            std::fprintf(stderr, "probe: BoCpuMap failed, status=%d\n", status);
            api.close();
            return 1;
        }
        uint32_t nop = 0;
        if (AdrenoPM4Type7(0x10, 0, &nop) != AdrenoPM4Success) {
            std::fprintf(stderr, "probe: CP_NOP framing failed\n");
            api.close();
            return 1;
        }
        *static_cast<volatile uint32_t *>(address) = nop;
        NativeAdrenoAbiSubmit submit{};
        submit.buffer_count = 1;
        submit.buffers[0] = {allocated.handle, mapped.iova, 4};
        NativeAdrenoAbiSubmitReply fence{};
        status = api.call(NativeAdrenoMethodSubmit, &submit, sizeof(submit), &fence, sizeof(fence));
        if (status || !fence.fence || api.submissions() == 0) {
            std::fprintf(stderr, "probe: Submit failed, status=%d fence=%llu\n",
                         status, (unsigned long long)fence.fence);
            api.close();
            return 1;
        }
        std::printf(" iova=0x%llx cpu_bytes=%llu; allocate/map/cpu-map/submit/close succeeded, submissions=%u\n",
                    (unsigned long long)mapped.iova, (unsigned long long)bytes,
                    api.submissions());
    }
#endif
    status = api.close();
    if (status) {
        std::fprintf(stderr, "probe: host_gpu_close failed, status=%d\n", status);
        return 1;
    }
    std::printf("%s\n", std::strcmp(argv[1], "exercise") ? "PASS: ABI info" : "PASS: ABI exercise");
    return 0;
}
