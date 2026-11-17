/* SPDX-License-Identifier: MIT */
/* Host-only wrapper around the shipping Turnip smoke watchdog. It records real
 * poll deadlines without adding logging to the target plugin path. */
#include "NativeTurnipSmoke.h"

#include <chrono>

int main(int argc, char **argv) {
    if (argc < 2) return 2;
    unsigned marks[32], count = 0, samples = 0, watchdogPolls = 0;
    auto marker = [&](uint32_t value) { if (count < 32) marks[count++] = value; };
    const auto processStarted = std::chrono::steady_clock::now();
    const turnip::Result r = turnip::runWatchedDetailed(
        marker, [&](const char *text) { printf("watchdog: %s\n", text); ++samples; }, argv[1],
        [&](int64_t elapsedMs, unsigned polls) {
            watchdogPolls = polls;
            fprintf(stderr, "watchdog poll: elapsed_ms=%lld polls=%u\n", (long long)elapsedMs, polls);
        });
    const auto processWall = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - processStarted);
    fprintf(stderr, "watchdog process: wall_ms=%lld polls=%u\n", (long long)processWall.count(), watchdogPolls);
    printf("watchdog samples: %u\n", samples);
    printf("markers:");
    for (unsigned i = 0; i < count; ++i) printf(" %x", marks[i]);
    printf("\nstage %d result %d fill %d compute %d (step %d) render %d/%d (step %d) ok %d msg '%s'\n",
           r.stage, r.vkResult, r.verified, r.computeWords, r.computeStage, r.renderTriangle, r.renderOther,
           r.renderStage, r.ok, turnip::gFirstError);
    return r.stage == 0 && r.verified == 512 && r.computeWords == 0 && r.computeStage == 0 && r.renderStage == 0 &&
           r.renderTriangle == 0 && r.renderOther == 4096 ? 0 : 1;
}
