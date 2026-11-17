/* Compile with the prepared host_iokit_shim.cpp preincluded and HOSTSIM_IOKIT_STALL_TEST defined. */
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <unistd.h>

static volatile sig_atomic_t received;

static void interrupted(int) {
    ++received;
}

static uint64_t nowNs() {
    timespec value = {};
    if (clock_gettime(CLOCK_MONOTONIC, &value)) {
        std::perror("clock_gettime");
        std::exit(90);
    }
    return static_cast<uint64_t>(value.tv_sec) * 1000000000ull + static_cast<uint64_t>(value.tv_nsec);
}

int main() {
    unsetenv("HOSTSIM_STALL");
    if (hostsimStall(2, "test")) {
        std::fprintf(stderr, "FAIL: stall was injected without HOSTSIM_STALL\n");
        return 1;
    }
    setenv("HOSTSIM_STALL", "invalid", 1);
    if (hostsimStall(2, "test")) {
        std::fprintf(stderr, "FAIL: malformed HOSTSIM_STALL was not rejected\n");
        return 2;
    }
    setenv("HOSTSIM_STALL", "1", 1);
    if (hostsimStall(1, "test")) {
        std::fprintf(stderr, "FAIL: a non-BoAlloc method was stalled\n");
        return 3;
    }
    if (std::signal(SIGALRM, interrupted) == SIG_ERR) {
        std::perror("signal");
        return 4;
    }
    ualarm(100000, 0);
    const uint64_t start = nowNs();
    if (!hostsimStall(2, "test")) {
        std::fprintf(stderr, "FAIL: first BoAlloc was not stalled\n");
        return 5;
    }
    const uint64_t elapsedMs = (nowNs() - start) / 1000000ull;
    if (elapsedMs < 1000) {
        std::fprintf(stderr, "FAIL: stall returned before its 1 s deadline: %llu ms\n",
                     static_cast<unsigned long long>(elapsedMs));
        return 6;
    }
    if (received != 1) {
        std::fprintf(stderr, "FAIL: expected one signal interruption, got %d\n", static_cast<int>(received));
        return 7;
    }
    if (hostsimStall(2, "test")) {
        std::fprintf(stderr, "FAIL: a later BoAlloc repeated the one-shot stall\n");
        return 8;
    }
    std::printf("PASS: signal interrupted the first stall; monotonic deadline completed it; later BoAlloc did not block\n");
    return 0;
}
