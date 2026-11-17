/* In-repo parser fixture. Not a Metal device, not a library, not a pipeline.
 * Compiled by scripts/test-pipeline-cache-member-priming.py against
 * scripts/producer-member-selection.c. */
#include "producer-member-selection.h"

#include <stdio.h>
#include <string.h>

static int checks;
static int fails;

static void expect(int condition, const char *message) {
    if (!condition) {
        fprintf(stderr, "FAIL %s\n", message);
        fails++;
        return;
    }
    checks++;
}

static void expect_absent(const char *name, int argc, char **argv) {
    ProducerMemberSelection out;
    ProducerMemberSelection zero;
    char error[128];
    memset(&out, 0x5a, sizeof out);
    memset(&zero, 0, sizeof zero);
    memset(error, 'Z', sizeof error);
    int status = producer_member_selection_parse(argc, argv, &out, error, sizeof error);
    expect(status == PRODUCER_MEMBER_ABSENT && memcmp(&out, &zero, sizeof out) == 0 && error[0] == '\0', name);
}

static void expect_reject(const char *name, int argc, char **argv, const char *snippet) {
    ProducerMemberSelection out;
    ProducerMemberSelection zero;
    char error[160];
    memset(&out, 0x5a, sizeof out);
    memset(&zero, 0, sizeof zero);
    int status = producer_member_selection_parse(argc, argv, &out, error, sizeof error);
    expect(status == PRODUCER_MEMBER_REJECT && memcmp(&out, &zero, sizeof zero) == 0 && strstr(error, snippet) != NULL,
           name);
}

static int hex_ok(const uint8_t raw[32], const char *hex) {
    for (int i = 0; i < 32; i++) {
        unsigned value;
        if (sscanf(hex + 2 * i, "%2x", &value) != 1 || raw[i] != (uint8_t)value) return 0;
    }
    return 1;
}

int main(void) {
    char *plain[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1"};
    expect_absent("no flags", 10, plain);

    char *validate[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                        "--validate-loads"};
    expect_absent("validate without member flags", 11, validate);

    const char *outer = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff";
    const char *member = "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100";
    char *only_order[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                          "--member-file-order", "0"};
    expect_reject("only file order", 12, only_order,
                  "member selection requires --member-file-order, --member-outer-sha256, and --member-sha256");

    char *only_outer[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                          "--member-outer-sha256", (char *)outer};
    expect_reject("only outer", 12, only_outer,
                  "member selection requires --member-file-order, --member-outer-sha256, and --member-sha256");

    char *only_member[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                           "--member-sha256", (char *)member};
    expect_reject("only member", 12, only_member,
                  "member selection requires --member-file-order, --member-outer-sha256, and --member-sha256");

    char *two[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                   "--member-file-order", "1", "--member-outer-sha256", (char *)outer};
    expect_reject("two of three", 14, two,
                  "member selection requires --member-file-order, --member-outer-sha256, and --member-sha256");

    char *short_hex[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                         "--member-file-order", "0", "--member-outer-sha256", "abcd", "--member-sha256", (char *)member};
    expect_reject("short outer hex", 16, short_hex, "member outer sha256 is not 64 hex digits");

    char *odd_hex[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                       "--member-file-order", "0", "--member-outer-sha256", (char *)outer, "--member-sha256", "abc"};
    expect_reject("odd member hex", 16, odd_hex, "member sha256 is not 64 hex digits");

    char *prefix_hex[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                          "--member-file-order", "0", "--member-outer-sha256",
                          "0x00112233445566778899aabbccddeeff00112233445566778899aabbccddee",
                          "--member-sha256", (char *)member};
    expect_reject("0x outer hex", 16, prefix_hex, "member outer sha256 is not 64 hex digits");

    char *bad_digit[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                         "--member-file-order", "0", "--member-outer-sha256", (char *)outer,
                         "--member-sha256", "gg00112233445566778899aabbccddeeff00112233445566778899aabbccddee"};
    expect_reject("non-hex member", 16, bad_digit, "member sha256 is not 64 hex digits");

    char *negative[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                        "--member-file-order", "-1", "--member-outer-sha256", (char *)outer, "--member-sha256", (char *)member};
    expect_reject("negative order", 16, negative, "member file order is not a uint32");

    char *junk[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                    "--member-file-order", "1a", "--member-outer-sha256", (char *)outer, "--member-sha256", (char *)member};
    expect_reject("trailing junk order", 16, junk, "member file order is not a uint32");

    char *plus[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                    "--member-file-order", "+1", "--member-outer-sha256", (char *)outer, "--member-sha256", (char *)member};
    expect_reject("plus order", 16, plus, "member file order is not a uint32");

    char *overflow[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                        "--member-file-order", "4294967296", "--member-outer-sha256", (char *)outer,
                        "--member-sha256", (char *)member};
    expect_reject("overflow order", 16, overflow, "member file order is not a uint32");

    char *empty_order[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                           "--member-file-order", "", "--member-outer-sha256", (char *)outer, "--member-sha256", (char *)member};
    expect_reject("empty order", 16, empty_order, "member file order is not a uint32");

    char *missing_value[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                             "--member-file-order"};
    expect_reject("missing order value", 11, missing_value,
                  "member selection requires --member-file-order, --member-outer-sha256, and --member-sha256");

    char *duplicate[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                         "--member-file-order", "0", "--member-file-order", "1", "--member-outer-sha256", (char *)outer,
                         "--member-sha256", (char *)member};
    expect_reject("duplicate order", 18, duplicate, "duplicate member selection flag");

    char *middle[] = {"pipeline-cache-host", "--member-file-order", "0", "cache", "list", "os", "cryptex", "turnip", "a830",
                      "0", "1", "--member-outer-sha256", (char *)outer, "--member-sha256", (char *)member};
    expect_reject("flags not a suffix", 15, middle, "member selection flags must be a suffix after the existing arguments");

    char *wrong_order[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                           "--member-sha256", (char *)member, "--member-file-order", "0", "--member-outer-sha256", (char *)outer};
    expect_reject("flag order", 16, wrong_order, "member selection flags must be a suffix after the existing arguments");

    char *extra[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                     "--member-file-order", "0", "--member-outer-sha256", (char *)outer, "--member-sha256", (char *)member,
                     "--extra"};
    expect_reject("extra after suffix", 17, extra, "member selection flags must be a suffix after the existing arguments");

    char *value_is_flag[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                             "--member-file-order", "--member-outer-sha256", (char *)outer, "--member-sha256", (char *)member};
    expect_reject("order value is a flag", 15, value_is_flag,
                  "member selection flags must be a suffix after the existing arguments");

    char order_token[] = "0";
    char *ok_argv[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                       "--member-file-order", order_token, "--member-outer-sha256", (char *)outer, "--member-sha256",
                       (char *)member};
    ProducerMemberSelection ok;
    char ok_error[64];
    memset(&ok, 0x5a, sizeof ok);
    memset(ok_error, 'Z', sizeof ok_error);
    int ok_status = producer_member_selection_parse(16, ok_argv, &ok, ok_error, sizeof ok_error);
    expect(ok_status == PRODUCER_MEMBER_OK && ok.validate_only == 0 && ok.file_order == 0 && ok_error[0] == '\0' &&
               hex_ok(ok.outer_sha256, outer) && hex_ok(ok.member_sha256, member),
           "explicit zero is an index not absent");

    char upper_outer[] = "00112233445566778899AABBCCDDEEFF00112233445566778899AABBCCDDEEFF";
    char *upper_argv[] = {"pipeline-cache-host", "loader", "cache", "list", "os", "cryptex", "turnip", "a830", "0", "1",
                          "--validate-loads", "--member-file-order", "4294967295", "--member-outer-sha256", upper_outer,
                          "--member-sha256", (char *)member};
    ProducerMemberSelection upper;
    char upper_error[64] = "stale";
    int upper_status = producer_member_selection_parse(17, upper_argv, &upper, upper_error, sizeof upper_error);
    expect(upper_status == PRODUCER_MEMBER_OK && upper.validate_only == 1 && upper.file_order == 4294967295u &&
               upper_error[0] == '\0' && hex_ok(upper.outer_sha256, upper_outer) && hex_ok(upper.member_sha256, member),
           "validate suffix uppercase");

    if (fails) {
        fprintf(stderr, "%d parser checks failed\n", fails);
        return 1;
    }
    printf("PASS parser checks=%d\n", checks);
    return 0;
}
