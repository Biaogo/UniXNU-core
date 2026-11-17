/* SPDX-License-Identifier: MIT */
/* Shared producer lines. A trailing newline is a real newline. The two-character
 * sequence backslash-n is a JSON escape inside jsonString, not a line ending. */
#ifndef PIPELINE_CACHE_FORMAT_H
#define PIPELINE_CACHE_FORMAT_H

#include <cstdint>
#include <cstdio>

inline void print_member_binding(std::uint32_t file_order, unsigned index, const char *name_sha256,
                                 const char *disposition, const char *stage, bool object_returned,
                                 bool entered_request) {
    std::printf(
        "MEMBER_BINDING: {\"file_order\":%u,\"index\":%u,\"name_sha256\":\"%s\","
        "\"disposition\":\"%s\",\"stage\":\"%s\",\"object_returned\":%s,"
        "\"entered_request\":%s,\"layer\":\"priming-request\",\"driver_pipeline_unproven\":true}\n",
        file_order, index, name_sha256 ? name_sha256 : "none",
        disposition ? disposition : "unknown", stage ? stage : "other",
        object_returned ? "true" : "false", entered_request ? "true" : "false");
}

inline void print_translation_counters(unsigned long long writes, unsigned long long driver_saves,
                                       unsigned long long hits, unsigned long long misses,
                                       unsigned long long driver_hits, unsigned long long driver_misses) {
    std::printf(
        "TRANSLATION_COUNTERS: writes=%llu driver_saves=%llu hits=%llu misses=%llu "
        "driver_hits=%llu driver_misses=%llu stillness_is_not_completion=1 "
        "layer=priming-request driver_pipeline_unproven=1\n",
        writes, driver_saves, hits, misses, driver_hits, driver_misses);
}

inline void print_explicit_result(unsigned primed, unsigned refused, unsigned absent, const char *device,
                                  unsigned made, const char *outcome, const char *build) {
    std::printf(
        "RESULT: requested=1 primed=%u refused=%u absent=%u device=%s made=%u outcome=%s "
        "layer=priming-request driver_pipeline_unproven=1 build=%s\n",
        primed, refused, absent, device ? device : "", made, outcome ? outcome : "refused",
        build ? build : "");
}

#endif
