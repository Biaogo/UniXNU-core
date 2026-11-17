// SPDX-License-Identifier: MIT
// Exercises the same deadline policy included by the shipping Turnip smoke test.
#include "NativeTurnipWatchdog.h"

#include <chrono>
#include <cstdio>
#include <vector>

using turnip::watchdog::Clock;
using turnip::watchdog::Schedule;

static unsigned gFailures;

static void check(bool condition, const char *message) {
    if (!condition) {
        std::fprintf(stderr, "FAIL: %s\n", message);
        ++gFailures;
    }
}

static Clock::time_point atMs(long long milliseconds) {
    return Clock::time_point(std::chrono::milliseconds(milliseconds));
}

int main() {
    const auto started = Clock::time_point{};
    bool done = false;

    Schedule thresholds;
    std::vector<unsigned> events;
    const auto sample = [&](unsigned checkpoint) { events.push_back(checkpoint); };
    check(thresholds.poll(started, atMs(14999), [&] { return done; }, sample) == false,
          "15 s checkpoint fired early");
    check(thresholds.poll(started, atMs(15000), [&] { return done; }, sample) == true,
          "15 s checkpoint did not fire at its deadline");
    check(thresholds.poll(started, atMs(15000), [&] { return done; }, sample) == false,
          "15 s checkpoint repeated at the same poll time");
    check(thresholds.poll(started, atMs(44999), [&] { return done; }, sample) == false,
          "45 s checkpoint fired early");
    check(thresholds.poll(started, atMs(45000), [&] { return done; }, sample) == true,
          "45 s checkpoint did not fire at its deadline");
    check(thresholds.poll(started, atMs(45000), [&] { return done; }, sample) == false,
          "45 s checkpoint repeated at the same poll time");
    check(thresholds.poll(started, atMs(89999), [&] { return done; }, sample) == false,
          "90 s checkpoint fired early");
    check(thresholds.poll(started, atMs(90000), [&] { return done; }, sample) == true,
          "90 s checkpoint did not fire at its deadline");
    check(thresholds.poll(started, atMs(90000), [&] { return done; }, sample) == false,
          "90 s checkpoint repeated at the same poll time");
    check(events == std::vector<unsigned>({15, 45, 90}), "checkpoint order or identity changed");
    check(thresholds.complete(), "schedule did not complete after all checkpoints");

    Schedule completedWorker;
    done = true;
    events.clear();
    check(completedWorker.poll(started, atMs(90000), [&] { return done; }, sample) == false
              && events.empty(),
          "completed worker received a synthetic sample");

    // A delayed poll consumes one overdue checkpoint, then the next poll consumes
    // the next one after the 300 ms sample. One poll can never emit a same-time
    // burst of all three checkpoints.
    Schedule delayed;
    done = false;
    events.clear();
    check(delayed.poll(started, atMs(91000), [&] { return done; }, sample), "late poll missed 15 s");
    check(events.size() == 1, "one late poll emitted a same-time burst");
    check(delayed.poll(started, atMs(91300), [&] { return done; }, sample), "late poll missed 45 s");
    check(delayed.poll(started, atMs(91600), [&] { return done; }, sample), "late poll missed 90 s");
    check(delayed.poll(started, atMs(91900), [&] { return done; }, sample) == false,
          "late poll duplicated a completed checkpoint");
    check(events == std::vector<unsigned>({15, 45, 90}), "late poll order changed");

    // Model a requested 100 ms poll that actually takes 120 ms. The old
    // 150-iteration sleep counter would not inspect its first deadline until
    // 18 s, after a worker that takes 17 s has already completed.
    const auto oldFirstInspection = std::chrono::milliseconds(150 * 120);
    check(oldFirstInspection == std::chrono::milliseconds(18000) &&
              oldFirstInspection > std::chrono::milliseconds(17000),
          "old sleep-count regression model changed");
    bool oldDone = false, oldSample = false;
    auto oldNow = Clock::time_point{};
    for (unsigned poll = 1; poll <= 150; ++poll) {
        oldNow += std::chrono::milliseconds(120);
        if (oldNow >= Clock::time_point(std::chrono::milliseconds(17000))) {
            oldDone = true;
            break;
        }
        if (poll == 150) oldSample = true;
    }
    check(oldDone && !oldSample, "old sleep counter unexpectedly sampled before completion");

    Schedule fixed;
    done = false;
    events.clear();
    auto now = Clock::time_point{};
    for (unsigned poll = 1; poll <= 150; ++poll) {
        now += std::chrono::milliseconds(120);
        if (now >= Clock::time_point(std::chrono::milliseconds(17000))) done = true;
        fixed.poll(started, now, [&] { return done; }, sample);
        if (done) break;
    }
    check(now >= Clock::time_point(std::chrono::milliseconds(17000)), "fake worker did not complete at 17 s");
    check(events == std::vector<unsigned>({15}), "fixed deadline did not sample 15 s exactly once before completion");

    if (gFailures) return 1;
    std::puts("PASS: watchdog uses monotonic elapsed deadlines, samples each 15/45/90 checkpoint once, and never samples a completed worker");
    return 0;
}
