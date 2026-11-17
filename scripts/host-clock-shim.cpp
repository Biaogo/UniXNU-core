/* SPDX-License-Identifier: MIT */
/* Host clock for NativeAdreno's optional diagnostic logging. */
#include <chrono>
#include <cstdint>

extern "C" uint64_t native_adreno_now_us(void)
{
    using namespace std::chrono;
    return static_cast<uint64_t>(
        duration_cast<microseconds>(steady_clock::now().time_since_epoch()).count());
}
