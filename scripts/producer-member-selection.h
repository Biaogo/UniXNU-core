/* SPDX-License-Identifier: MIT */
/* Explicit producer member selection. No flags means absent, not member 0.
 * This parser does not open a library and does not choose a default member. */
#ifndef PRODUCER_MEMBER_SELECTION_H
#define PRODUCER_MEMBER_SELECTION_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum {
    PRODUCER_MEMBER_ABSENT = 0,
    PRODUCER_MEMBER_OK = 1,
    PRODUCER_MEMBER_REJECT = -1
};

typedef struct ProducerMemberSelection {
    int validate_only;
    uint32_t file_order;
    uint8_t outer_sha256[32];
    uint8_t member_sha256[32];
} ProducerMemberSelection;

/* 0 absent, 1 explicit and valid, -1 rejected. On absent or reject, *out is zero.
 * error is always NUL-terminated when error_cap > 0. Digests are raw bytes, not hex. */
int producer_member_selection_parse(int argc, char **argv, ProducerMemberSelection *out, char *error, size_t error_cap);

#ifdef __cplusplus
}
#endif

#endif
