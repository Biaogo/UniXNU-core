/* SPDX-License-Identifier: MIT */
/* Shared explicit-member identity and success policy.
 * Used by the producer and by PrimeShaders. Not a Metal runtime, not a
 * driver-pipeline proof, and not a test double of the compile loop.
 * "made" counts priming requests that returned an object. It does not mean a
 * driver pipeline was created, drained, or served from cache.
 *
 * loadedLibrary's terminal branches are mutually exclusive. Every public
 * function is exactly one of:
 *   attempted + resumed + skipped + refused_before + nonpipeline
 * eligible is the independently primeable subset:
 *   attempted + resumed + skipped
 * so functions = eligible + refused_before + nonpipeline.
 * A fragment or other dependency is nonpipeline, not eligible. A vertex paired
 * with a fragment therefore does not need functions == attempted + resumed.
 * Pipeline-failure refused is already inside attempted
 * (attempted = made + (refused - refused_before)). Adding refused into the
 * function partition double-counts. Zero eligible cannot succeed. The only
 * zero-attempt success is a same-identity resume of every eligible object. */
#ifndef PRIME_MEMBER_POLICY_H
#define PRIME_MEMBER_POLICY_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string>

struct PrimeMemberCounts {
    unsigned functions;
    unsigned eligible;
    unsigned nonpipeline;
    unsigned attempted;
    unsigned made;
    unsigned refused;
    unsigned refused_before;
    unsigned skipped;
    unsigned resumed;
    unsigned exceptions;
    int loaded;
};

struct PrimeMemberDecision {
    const char *outcome;
    int write_library_done;
    int primed;
};

inline void prime_member_set_error(char *error, size_t error_cap, const char *text) {
    if (!error || error_cap == 0) return;
    snprintf(error, error_cap, "%s", text ? text : "");
}

/* Suffix is file-order plus two SHA-256 digests. It contains no function name. */
inline int prime_member_identity_suffix(uint32_t file_order, const uint8_t outer_sha256[32],
                                        const uint8_t member_sha256[32], char *out, size_t out_cap) {
    if (!out || !outer_sha256 || !member_sha256 || out_cap < 144) return -1;
    char outer_hex[65];
    char member_hex[65];
    static const char digits[] = "0123456789abcdef";
    for (int i = 0; i < 32; ++i) {
        outer_hex[2 * i] = digits[outer_sha256[i] >> 4];
        outer_hex[2 * i + 1] = digits[outer_sha256[i] & 0xf];
        member_hex[2 * i] = digits[member_sha256[i] >> 4];
        member_hex[2 * i + 1] = digits[member_sha256[i] & 0xf];
    }
    outer_hex[64] = 0;
    member_hex[64] = 0;
    const int wrote = snprintf(out, out_cap, "m:%u:%s:%s", file_order, outer_hex, member_hex);
    if (wrote < 0 || static_cast<size_t>(wrote) >= out_cap) return -1;
    return 0;
}

inline std::string prime_member_library_identity(const std::string &path, const std::string &suffix) {
    if (suffix.empty()) return path;
    return path + "\t" + suffix;
}

inline std::string prime_member_function_key(const std::string &library_identity, const std::string &function_name) {
    return library_identity + "\t" + function_name;
}

inline std::string prime_member_function_done_key(const std::string &function_key, const std::string &build) {
    return function_key + "\t" + build;
}

inline std::string prime_member_library_done_key(const std::string &library_identity, long long size, long long mtime,
                                                 const std::string &build) {
    return library_identity + "\t" + std::to_string(size) + "\t" + std::to_string(mtime) + "\t" + build;
}

/* Explicit selection is one library. Zero, any other count, and an out-of-range index are refused.
 * This does not choose a member and does not rewrite the caller's path list. */
inline int prime_member_range_ok(size_t first, size_t count, size_t paths, char *error, size_t error_cap) {
    if (count != 1) {
        prime_member_set_error(error, error_cap, "explicit member selection allows exactly one library");
        return -1;
    }
    if (paths == 0 || first >= paths || count > paths - first) {
        prime_member_set_error(error, error_cap, "explicit member library range is outside the list");
        return -1;
    }
    prime_member_set_error(error, error_cap, "");
    return 0;
}

/* success_only is the explicit path. The default path still records a failed attempt so its
 * historical prime-fn resume behavior stays. */
inline int prime_member_record_function_done(int success_only, int ok) {
    if (!success_only) return 1;
    return ok ? 1 : 0;
}

inline int prime_member_uadd(unsigned left, unsigned right, unsigned *sum) {
    if (!sum || right > ~0u - left) return -1;
    *sum = left + right;
    return 0;
}

/* Fail closed on a gap, an overlap, or an overflow. Pipeline refusals are not a
 * sixth function class: they already sit inside attempted. */
inline int prime_member_counts_consistent(const PrimeMemberCounts &counts) {
    unsigned pipeline = 0;
    unsigned accounted = 0;
    unsigned attempt_closed = 0;
    if (prime_member_uadd(counts.attempted, counts.resumed, &pipeline) != 0) return 0;
    if (prime_member_uadd(pipeline, counts.skipped, &pipeline) != 0) return 0;
    if (pipeline != counts.eligible) return 0;
    if (prime_member_uadd(pipeline, counts.refused_before, &accounted) != 0) return 0;
    if (prime_member_uadd(accounted, counts.nonpipeline, &accounted) != 0) return 0;
    if (accounted != counts.functions) return 0;
    if (counts.refused < counts.refused_before) return 0;
    if (prime_member_uadd(counts.made, counts.refused - counts.refused_before, &attempt_closed) != 0) return 0;
    if (attempt_closed != counts.attempted) return 0;
    if (counts.exceptions > counts.refused - counts.refused_before) return 0;
    return 1;
}

/* Fail closed. Unknown or inconsistent counts do not write done. A same-identity
 * resume with no new failure is the only zero-attempt success. */
inline PrimeMemberDecision prime_member_explicit_decision(const PrimeMemberCounts &counts) {
    PrimeMemberDecision decision = {"refused", 0, 0};
    if (!counts.loaded) return decision;
    if (counts.exceptions) {
        decision.outcome = "exception";
        return decision;
    }
    if (!prime_member_counts_consistent(counts)) return decision;
    if (counts.skipped) {
        decision.outcome = "skipped";
        return decision;
    }
    if (counts.refused || counts.made != counts.attempted) {
        decision.outcome = (counts.made || counts.resumed) ? "partial" : "refused";
        return decision;
    }
    /* Nothing independently primeable: empty library, or only fragments and
     * other dependencies. That is not a primed library. */
    if (counts.eligible == 0) {
        decision.outcome = "empty";
        return decision;
    }
    if (counts.attempted == 0 && counts.resumed > 0) {
        decision.outcome = "resumed";
        decision.write_library_done = 1;
        decision.primed = 1;
        return decision;
    }
    if (counts.made > 0) {
        decision.outcome = "made";
        decision.write_library_done = 1;
        decision.primed = 1;
        return decision;
    }
    return decision;
}

#endif
