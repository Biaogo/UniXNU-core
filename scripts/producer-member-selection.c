/* SPDX-License-Identifier: MIT */
#include "producer-member-selection.h"

#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void set_error(char *error, size_t error_cap, const char *text) {
    if (!error || error_cap == 0) return;
    snprintf(error, error_cap, "%s", text ? text : "");
}

static void clear_out(ProducerMemberSelection *out) {
    if (out) memset(out, 0, sizeof *out);
}

static int is_flag(const char *arg, const char *name) {
    return arg && name && strcmp(arg, name) == 0;
}

static int hex_value(char ch) {
    if (ch >= '0' && ch <= '9') return ch - '0';
    if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
    if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
    return -1;
}

static int parse_hex64(const char *text, uint8_t out[32]) {
    if (!text || strlen(text) != 64) return -1;
    for (int i = 0; i < 32; i++) {
        int hi = hex_value(text[2 * i]);
        int lo = hex_value(text[2 * i + 1]);
        if (hi < 0 || lo < 0) return -1;
        out[i] = (uint8_t)((hi << 4) | lo);
    }
    return 0;
}

static int parse_u32(const char *text, uint32_t *out) {
    if (!text || text[0] < '0' || text[0] > '9') return -1;
    errno = 0;
    char *end = NULL;
    unsigned long value = strtoul(text, &end, 10);
    if (!end || *end != '\0' || errno == ERANGE || value > UINT32_MAX) return -1;
    *out = (uint32_t)value;
    return 0;
}

int producer_member_selection_parse(int argc, char **argv, ProducerMemberSelection *out, char *error, size_t error_cap) {
    clear_out(out);
    set_error(error, error_cap, "");
    if (argc < 1 || !argv) {
        set_error(error, error_cap, "member selection flags must be a suffix after the existing arguments");
        return PRODUCER_MEMBER_REJECT;
    }

    const char *order_name = "--member-file-order";
    const char *outer_name = "--member-outer-sha256";
    const char *member_name = "--member-sha256";
    int order_count = 0, outer_count = 0, member_count = 0;
    for (int i = 1; i < argc; i++) {
        if (is_flag(argv[i], order_name)) order_count++;
        else if (is_flag(argv[i], outer_name)) outer_count++;
        else if (is_flag(argv[i], member_name)) member_count++;
    }
    if (order_count == 0 && outer_count == 0 && member_count == 0) return PRODUCER_MEMBER_ABSENT;
    if (order_count > 1 || outer_count > 1 || member_count > 1) {
        set_error(error, error_cap, "duplicate member selection flag");
        return PRODUCER_MEMBER_REJECT;
    }
    if (order_count != 1 || outer_count != 1 || member_count != 1) {
        set_error(error, error_cap,
                  "member selection requires --member-file-order, --member-outer-sha256, and --member-sha256");
        return PRODUCER_MEMBER_REJECT;
    }
    if (argc < 7 ||
        !is_flag(argv[argc - 6], order_name) || !is_flag(argv[argc - 4], outer_name) || !is_flag(argv[argc - 2], member_name)) {
        set_error(error, error_cap, "member selection flags must be a suffix after the existing arguments");
        return PRODUCER_MEMBER_REJECT;
    }

    const int prefix = argc - 6;
    int validate_only = 0;
    if (prefix == 11 && argv[10] && strcmp(argv[10], "--validate-loads") == 0) {
        validate_only = 1;
    } else if (prefix != 10) {
        set_error(error, error_cap, "member selection flags must be a suffix after the existing arguments");
        return PRODUCER_MEMBER_REJECT;
    }

    ProducerMemberSelection parsed;
    memset(&parsed, 0, sizeof parsed);
    parsed.validate_only = validate_only;
    if (parse_u32(argv[argc - 5], &parsed.file_order) != 0) {
        set_error(error, error_cap, "member file order is not a uint32");
        return PRODUCER_MEMBER_REJECT;
    }
    if (parse_hex64(argv[argc - 3], parsed.outer_sha256) != 0) {
        set_error(error, error_cap, "member outer sha256 is not 64 hex digits");
        return PRODUCER_MEMBER_REJECT;
    }
    if (parse_hex64(argv[argc - 1], parsed.member_sha256) != 0) {
        set_error(error, error_cap, "member sha256 is not 64 hex digits");
        return PRODUCER_MEMBER_REJECT;
    }
    if (out) *out = parsed;
    return PRODUCER_MEMBER_OK;
}
