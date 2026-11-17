// SPDX-License-Identifier: MIT
// Host-side driver for the render server's current shader-priming code. It
// compiles one OS metallib through the native Metal bridge and host Turnip
// over the software NativeAdreno peer, then leaves the resulting cache files
// for make-pipeline-cache.py to pack. No GPU command is executed here.
//
// argv: loader cache library-list os-root cryptex-root target-turnip gpu first
//       count [--validate-loads]
//       [--member-file-order N --member-outer-sha256 HEX --member-sha256 HEX]
// The member suffix is explicit selection. Absent flags keep newLibraryWithURL.
#include "PrimeShaders.h"
#include "NativeBridgeMetalFrontend.h"
#include "bridge/NativeMetalObjects.h"
#include "producer-member-selection.h"
#include "pipeline-cache-format.h"
#include <CommonCrypto/CommonDigest.h>
#include <fstream>
#include <iterator>

static void onMissing(const char *, const char *) {}

// The thin ARM64/iOS dylib's LC_UUID and LC_BUILD_VERSION. Unknown or
// mismatched identities are fatal: the device keys prime-done by this UUID.
static bool fileIdentity(const char *path, std::string &uuid, uint32_t &platform) {
    std::ifstream in(path, std::ios::binary);
    std::vector<char> bytes((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    if (bytes.size() < sizeof(mach_header_64)) return false;
    const auto *header = reinterpret_cast<const mach_header_64 *>(bytes.data());
    if (header->magic != MH_MAGIC_64 || header->cputype != CPU_TYPE_ARM64 ||
        header->filetype != MH_DYLIB || header->sizeofcmds > bytes.size() - sizeof(mach_header_64))
        return false;
    platform = 0;
    uuid.clear();
    const size_t end = sizeof(mach_header_64) + header->sizeofcmds;
    size_t at = sizeof(mach_header_64);
    for (uint32_t i = 0; i < header->ncmds && at + sizeof(load_command) <= end; ++i) {
        const auto *command = reinterpret_cast<const load_command *>(bytes.data() + at);
        if (command->cmdsize < sizeof(load_command) || at + command->cmdsize > end) return false;
        if (command->cmd == LC_UUID && command->cmdsize >= sizeof(uuid_command)) {
            const uint8_t *value = reinterpret_cast<const uuid_command *>(command)->uuid;
            char text[33];
            for (int k = 0; k < 16; ++k) snprintf(text + 2 * k, 3, "%02x", value[k]);
            uuid = text;
        } else if (command->cmd == LC_BUILD_VERSION && command->cmdsize >= sizeof(build_version_command)) {
            platform = reinterpret_cast<const build_version_command *>(command)->platform;
        }
        at += command->cmdsize;
    }
    return at == end && !uuid.empty() && platform != 0;
}

static std::string jsonString(const std::string &value) {
    std::string escaped;
    escaped.reserve(value.size() + 16);
    static const char hex[] = "0123456789abcdef";
    for (const unsigned char byte : value) {
        switch (byte) {
        case '"': escaped += "\\\""; break;
        case '\\': escaped += "\\\\"; break;
        case '\b': escaped += "\\b"; break;
        case '\f': escaped += "\\f"; break;
        case '\n': escaped += "\\n"; break;
        case '\r': escaped += "\\r"; break;
        case '\t': escaped += "\\t"; break;
        default:
            if (byte < 0x20) {
                escaped += "\\u00";
                escaped += hex[byte >> 4];
                escaped += hex[byte & 0xf];
            } else {
                escaped += static_cast<char>(byte);
            }
        }
    }
    return escaped;
}

static std::string header64Hex(const std::string &path, size_t &bytesRead) {
    std::ifstream in(path, std::ios::binary);
    unsigned char header[64] = {};
    in.read(reinterpret_cast<char *>(header), sizeof(header));
    bytesRead = in.gcount() > 0 ? static_cast<size_t>(in.gcount()) : 0;
    static const char hex[] = "0123456789abcdef";
    std::string text;
    text.reserve(bytesRead * 2);
    for (size_t i = 0; i < bytesRead; ++i) {
        text += hex[header[i] >> 4];
        text += hex[header[i] & 0xf];
    }
    return text;
}

static void printLoadResult(size_t index, const std::string &path, const char *status,
                            const std::string &header, size_t headerBytes, NSError *error) {
    const char *domain = error ? error.domain.UTF8String : nullptr;
    const char *description = error ? error.description.UTF8String : nullptr;
    const std::string domainJson = domain ? "\"" + jsonString(domain) + "\"" : "null";
    const std::string codeJson =
        error ? std::to_string(static_cast<long long>(error.code)) : "null";
    const std::string descriptionJson =
        description ? "\"" + jsonString(description) + "\"" : "null";
    std::printf(
        "LOAD_LIBRARY: {\"index\":%zu,\"path\":\"%s\",\"status\":\"%s\","
        "\"header64_hex\":\"%s\",\"header_bytes\":%zu,"
        "\"error_domain\":%s,\"error_code\":%s,\"error_description\":%s,"
        "\"pipelines_compiled\":false,\"shader_pack_produced\":false}\n",
        index, jsonString(path).c_str(), status, header.c_str(), headerBytes,
        domainJson.c_str(), codeJson.c_str(), descriptionJson.c_str());
}

static void printLoadResultText(size_t index, const std::string &path, const char *status,
                                const std::string &header, size_t headerBytes, const char *description) {
    const std::string descriptionJson =
        description ? "\"" + jsonString(description) + "\"" : "null";
    std::printf(
        "LOAD_LIBRARY: {\"index\":%zu,\"path\":\"%s\",\"status\":\"%s\","
        "\"header64_hex\":\"%s\",\"header_bytes\":%zu,"
        "\"error_domain\":null,\"error_code\":null,\"error_description\":%s,"
        "\"pipelines_compiled\":false,\"shader_pack_produced\":false}\n",
        index, jsonString(path).c_str(), status, header.c_str(), headerBytes, descriptionJson.c_str());
}

static bool validateLoads(id<MTLDevice> device, const std::string &osRoot,
                          const std::string &cryptexRoot,
                          const std::vector<std::string> &paths,
                          size_t first, size_t count) {
    const std::string cryptex = "/System/Cryptexes/OS";
    size_t loaded = 0, refused = 0, absent = 0;
    std::printf(
        "LOAD_PREFLIGHT: mode=validate-only pipelines_compiled=0 shader_pack_produced=0\n");
    for (size_t i = first; i < first + count; ++i) {
        const std::string &path = paths[i];
        const std::string local =
            path.compare(0, cryptex.size(), cryptex) == 0
                ? cryptexRoot + path.substr(cryptex.size())
                : osRoot + path;
        size_t headerBytes = 0;
        const std::string header = header64Hex(local, headerBytes);
        struct stat st = {};
        if (stat(local.c_str(), &st) != 0) {
            ++absent;
            printLoadResult(i, path, "absent", header, headerBytes, nil);
            continue;
        }
        @autoreleasepool {
            NSError *error = nil;
            NSURL *url = [NSURL fileURLWithPath:[NSString stringWithUTF8String:local.c_str()]];
            id<MTLLibrary> library = [device newLibraryWithURL:url error:&error];
            if (!library) {
                ++refused;
                printLoadResult(i, path, "load_refused", header, headerBytes, error);
            } else {
                ++loaded;
                printLoadResult(i, path, "loaded", header, headerBytes, nil);
            }
        }
    }
    std::printf(
        "LOAD_RESULT: requested=%zu loaded=%zu refused=%zu absent=%zu "
        "pipelines_compiled=0 shader_pack_produced=0\n",
        count, loaded, refused, absent);
    return loaded == count && !refused && !absent;
}

static bool loadExplicitMember(const std::string &osRoot, const std::string &cryptexRoot,
                               const std::vector<std::string> &paths, size_t first, size_t count,
                               const ProducerMemberSelection *member) {
    NmbDevice *nmb = native_bridge_metal_device();
    if (!nmb || !member) {
        std::fprintf(stderr, "pipeline-cache-host: explicit member selection has no bridge device\n");
        return false;
    }
    const std::string cryptex = "/System/Cryptexes/OS";
    size_t loaded = 0, refused = 0, absent = 0;
    std::printf("LOAD_PREFLIGHT: mode=explicit-member pipelines_compiled=0 shader_pack_produced=0\n");
    for (size_t i = first; i < first + count; ++i) {
        const std::string &path = paths[i];
        const std::string local =
            path.compare(0, cryptex.size(), cryptex) == 0
                ? cryptexRoot + path.substr(cryptex.size())
                : osRoot + path;
        size_t headerBytes = 0;
        const std::string header = header64Hex(local, headerBytes);
        struct stat st = {};
        if (stat(local.c_str(), &st) != 0) {
            ++absent;
            printLoadResultText(i, path, "absent", header, headerBytes, nullptr);
            continue;
        }
        std::ifstream in(local, std::ios::binary);
        std::vector<unsigned char> bytes((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
        if (!in && bytes.empty()) {
            ++refused;
            printLoadResultText(i, path, "load_refused", header, headerBytes,
                                "explicit member file could not be read");
            continue;
        }
        const void *data = bytes.empty() ? nullptr : static_cast<const void *>(bytes.data());
        NmbLibrary *library = nmb_library_new_member(
            nmb, data, bytes.size(), member->file_order, member->outer_sha256, member->member_sha256);
        if (!library) {
            ++refused;
            const char *reason = nmb_last_error();
            printLoadResultText(i, path, "load_refused", header, headerBytes,
                                reason ? reason : "explicit member load refused");
            continue;
        }
        nmb_library_release(library);
        ++loaded;
        printLoadResultText(i, path, "loaded", header, headerBytes, nullptr);
    }
    std::printf(
        "LOAD_RESULT: requested=%zu loaded=%zu refused=%zu absent=%zu "
        "pipelines_compiled=0 shader_pack_produced=0\n",
        count, loaded, refused, absent);
    return loaded == count && !refused && !absent;
}

// Safe binding line: index and name digest only. No function name leaves this process.
// object_returned is the priming constructor's object, not a driver pipeline.
static void printMemberBindings(uint32_t fileOrder, id<MTLLibrary> library, const prime::PrimePass &pass) {
    NSArray<NSString *> *names = library ? library.functionNames : nil;
    for (const prime::PrimeBindingNote &note : pass.bindings) {
        char nameSha[65] = "none";
        if (names && note.index < names.count) {
            const char *text = names[note.index].UTF8String;
            unsigned char digest[CC_SHA256_DIGEST_LENGTH];
            CC_SHA256(text ? text : "", text ? (CC_LONG)strlen(text) : 0, digest);
            static const char digits[] = "0123456789abcdef";
            for (int i = 0; i < CC_SHA256_DIGEST_LENGTH; ++i) {
                nameSha[2 * i] = digits[digest[i] >> 4];
                nameSha[2 * i + 1] = digits[digest[i] & 0xf];
            }
            nameSha[64] = 0;
        }
        print_member_binding(
            fileOrder, note.index, nameSha, note.disposition, note.stage,
            note.object_returned, note.entered_request);
    }
}

// One explicit member, already selected. Creates the wrapped library once and enters the shared
// core. Does not open the default URL loader and does not treat a priming-request object as a
// driver pipeline. Failure does not append prime-done. Binding index 1 is a CI assess check,
// not a generic success gate: a same-identity all-resumed library stays a policy success.
static int primeExplicitMember(id<MTLDevice> device, const std::string &root, const std::string &osRoot,
                               const std::string &cryptexRoot, const std::vector<std::string> &paths, size_t first,
                               const ProducerMemberSelection &member, const std::string &build, const std::string &deviceName) {
    const std::string &path = paths[first];
    const std::string cryptex = "/System/Cryptexes/OS";
    const std::string local =
        path.compare(0, cryptex.size(), cryptex) == 0 ? cryptexRoot + path.substr(cryptex.size()) : osRoot + path;
    unsigned primed = 0, refused = 0, absent = 0;
    const char *outcome = "refused";
    auto finish = [&](int code) {
        print_explicit_result(
            primed, refused, absent, deviceName.c_str(), (unsigned)native_prime_made, outcome, build.c_str());
        if (code == 0 && primed == 1 && !refused && !absent) {
            std::printf("PASS: requested=1 primed=%u refused=%u absent=%u\n", primed, refused, absent);
            return 0;
        }
        std::printf("FAIL: requested=1 primed=%u refused=%u absent=%u\n", primed, refused, absent);
        return code == 0 ? 1 : code;
    };
    struct stat st = {};
    if (stat(local.c_str(), &st) != 0) {
        ++absent;
        outcome = "absent";
        const std::string result = path + " absent from the image mount";
        prime::appendLine(root + "/prime.log", result);
        std::fprintf(stderr, "%s\n", result.c_str());
        return finish(1);
    }
    std::ifstream in(local, std::ios::binary);
    std::vector<unsigned char> bytes((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    if (!in && bytes.empty()) {
        ++refused;
        outcome = "refused";
        std::fprintf(stderr, "pipeline-cache-host: explicit member file could not be read\n");
        return finish(1);
    }
    char suffix[160];
    if (prime_member_identity_suffix(member.file_order, member.outer_sha256, member.member_sha256, suffix, sizeof suffix) != 0) {
        ++refused;
        outcome = "refused";
        std::fprintf(stderr, "pipeline-cache-host: explicit member identity suffix refused\n");
        return finish(1);
    }
    const std::string identity = prime_member_library_identity(path, suffix);
    const std::string current = root + "/prime-current";
    const std::string badPath = root + "/prime-bad";
    const std::string functionsPath = root + "/prime-fn";
    const std::string donePath = root + "/prime-done";
    const std::string prefix = identity + "\t";
    for (const std::string &line : prime::readLines(current)) {
        if (line.compare(0, prefix.size(), prefix) != 0) {
            ++refused;
            outcome = "refused";
            std::fprintf(stderr, "pipeline-cache-host: unknown prime-current entry is not this member; not swallowed\n");
            return finish(1);
        }
    }
    for (const std::string &line : prime::readLines(current)) prime::appendLine(badPath, line);
    if (!prime::readLines(current).empty()) unlink(current.c_str());
    const void *data = bytes.empty() ? nullptr : static_cast<const void *>(bytes.data());
    id<MTLLibrary> selected = native_bridge_metal_library_with_member(
        data, bytes.size(), member.file_order, member.outer_sha256, member.member_sha256);
    if (!selected) {
        ++refused;
        outcome = "refused";
        const char *reason = nmb_last_error();
        std::fprintf(stderr, "pipeline-cache-host: explicit member refused: %s\n", reason ? reason : "no error");
        return finish(1);
    }
    prime::PrimePass pass;
    @try {
        pass = prime::loadedLibrary(device, selected, identity, current, prime::readLines(badPath), functionsPath,
                                    prime::readLines(functionsPath), build, true);
    } @catch (NSException *) {
        pass.loaded = true;
        if (!pass.exceptions) ++pass.exceptions;
        outcome = "exception";
    }
    printMemberBindings(member.file_order, selected, pass);
    PrimeMemberCounts counts = {};
    counts.functions = pass.functions;
    counts.eligible = pass.eligible;
    counts.nonpipeline = pass.nonpipeline;
    counts.attempted = pass.attempted;
    counts.made = pass.made;
    counts.refused = pass.refused;
    counts.refused_before = pass.refused_before;
    counts.skipped = pass.skipped;
    counts.resumed = pass.resumed;
    counts.exceptions = pass.exceptions;
    counts.loaded = pass.loaded ? 1 : 0;
    const PrimeMemberDecision decision = prime_member_explicit_decision(counts);
    outcome = decision.outcome ? decision.outcome : "refused";
    std::fprintf(stderr,
                 "pipeline-cache-host: explicit outcome=%s functions=%u eligible=%u nonpipeline=%u attempted=%u made=%u refused=%u refused_before=%u skipped=%u resumed=%u exceptions=%u\n",
                 outcome, pass.functions, pass.eligible, pass.nonpipeline, pass.attempted, pass.made, pass.refused,
                 pass.refused_before, pass.skipped, pass.resumed, pass.exceptions);
    if (!decision.write_library_done || !decision.primed) {
        ++refused;
        return finish(1);
    }
    NmbShaderCacheStats stats = {};
    uint64_t last = UINT64_MAX;
    for (unsigned still = 0; still < 20; usleep(500000)) {
        if (!native_bridge_metal_shader_cache_stats(&stats)) {
            std::fprintf(stderr, "FAIL: shader-cache statistics unavailable\n");
            return 1;
        }
        const uint64_t now = stats.writes + stats.driver_saves;
        still = now == last ? still + 1 : 0;
        last = now;
    }
    print_translation_counters(
        (unsigned long long)stats.writes, (unsigned long long)stats.driver_saves,
        (unsigned long long)stats.hits, (unsigned long long)stats.misses,
        (unsigned long long)stats.driver_hits, (unsigned long long)stats.driver_misses);
    prime::appendLine(donePath, prime_member_library_done_key(identity, (long long)st.st_size, (long long)st.st_mtimespec.tv_sec, build));
    primed = 1;
    return finish(0);
}

int main(int argc, char **argv) {
    @autoreleasepool {
        ProducerMemberSelection member = {};
        char memberError[192];
        const int memberStatus = producer_member_selection_parse(argc, argv, &member, memberError, sizeof memberError);
        if (memberStatus < 0) {
            std::fprintf(stderr, "pipeline-cache-host: %s\n", memberError);
            return 2;
        }
        const bool explicitMember = memberStatus == PRODUCER_MEMBER_OK;
        const bool validateOnly =
            argc == 11 && strcmp(argv[10], "--validate-loads") == 0;
        if (!explicitMember && argc != 10 && !validateOnly) {
            std::fprintf(stderr, "pipeline-cache-host: invalid argument count\n");
            return 2;
        }
        const char *expectedGpu = argv[7];
        const char *simulatedGpu = getenv("HOSTSIM_GPU");
        if (!simulatedGpu || strcmp(simulatedGpu, expectedGpu)) {
            std::fprintf(stderr, "FAIL: HOSTSIM_GPU must be %s\n", expectedGpu);
            return 1;
        }
        std::string deviceUuid;
        uint32_t platform = 0;
        if (!fileIdentity(argv[6], deviceUuid, platform) || platform != PLATFORM_IOS) {
            std::fprintf(stderr, "FAIL: target Turnip is not a thin ARM64 iOS dylib with LC_UUID\n");
            return 1;
        }
        id<MTLDevice> device = native_bridge_metal_open(argv[1], argv[2], 0x6e6d70, onMissing);
        if (!device) {
            std::fprintf(stderr, "FAIL: device: %s\n", nmb_last_error());
            return 1;
        }
        NSString *metalName = [(id<MTLDevice>)device name];
        const std::string deviceName = metalName ? metalName.UTF8String : "unknown";
        const std::string wanted = expectedGpu[0] == 'a' ? expectedGpu + 1 : expectedGpu;
        if (deviceName.find(wanted) == std::string::npos ||
            (strcmp(expectedGpu, "a830") == 0 && deviceName.find("740") != std::string::npos)) {
            std::fprintf(stderr, "FAIL: requested %s, host peer reported %s\n", expectedGpu, deviceName.c_str());
            return 1;
        }
        native_bridge_metal_quiet_problems(true);
        const std::string root = argv[2], osRoot = argv[4], cryptexRoot = argv[5];
        const std::string build = std::string(nmb_runtime_build()) + "-" + deviceUuid;
        std::vector<std::string> paths;
        {
            std::ifstream list(argv[3]);
            for (std::string line; std::getline(list, line);)
                if (!line.empty() && line[0] == '/') paths.push_back(line);
        }
        const size_t first = strtoul(argv[8], nullptr, 10);
        const size_t count = strtoul(argv[9], nullptr, 10);
        if (first >= paths.size() || count > paths.size() - first) {
            std::fprintf(stderr, "FAIL: library range is outside the list\n");
            return 1;
        }
        if (explicitMember) {
            char rangeError[160];
            if (prime_member_range_ok(first, count, paths.size(), rangeError, sizeof rangeError) != 0) {
                std::fprintf(stderr, "pipeline-cache-host: %s\n", rangeError);
                std::printf("FAIL: explicit member range refused\n");
                return 1;
            }
            if (member.validate_only) {
                if (!loadExplicitMember(osRoot, cryptexRoot, paths, first, count, &member)) {
                    std::printf("FAIL: explicit member load rejected one or more libraries\n");
                    return 1;
                }
                std::printf("PASS: explicit member load accepted every library; no shader pack was produced\n");
                return 0;
            }
            return primeExplicitMember(device, root, osRoot, cryptexRoot, paths, first, member, build, deviceName);
        }
        if (validateOnly) {
            if (!validateLoads(device, osRoot, cryptexRoot, paths, first, count)) {
                std::printf("FAIL: load preflight rejected one or more libraries\n");
                return 1;
            }
            std::printf("PASS: load preflight accepted every library; no shader pack was produced\n");
            return 0;
        }
        const std::set<std::string> bad = prime::readLines(root + "/prime-bad");
        const std::string current = root + "/prime-current", log = root + "/prime.log",
                          done = root + "/prime-done";
        unsigned primed = 0, refused = 0, absent = 0;
        for (size_t i = first; i < first + count; ++i) {
            const std::string &path = paths[i];
            const std::string cryptex = "/System/Cryptexes/OS";
            const std::string local =
                path.compare(0, cryptex.size(), cryptex) == 0 ? cryptexRoot + path.substr(cryptex.size()) : osRoot + path;
            struct stat st = {};
            if (stat(local.c_str(), &st) != 0) {
                ++absent;
                const std::string result = path + " absent from the image mount";
                prime::appendLine(log, result);
                std::fprintf(stderr, "%s\n", result.c_str());
                continue;
            }
            const std::string result = prime::library(device, local, current, bad, root + "/prime-fn",
                                                      prime::readLines(root + "/prime-fn"), build);
            prime::appendLine(log, result);
            if (result.find(" library refused: ") != std::string::npos) {
                ++refused;
                std::fprintf(stderr, "%s\n", result.c_str());
                continue;
            }
            prime::appendLine(done, path + "\t" + std::to_string((long long)st.st_size) + "\t" +
                                        std::to_string((long long)st.st_mtimespec.tv_sec) + "\t" + build);
            ++primed;
        }
        // The runtime writes cache files after Metal calls return. Keep the
        // process alive until its write counters have stood still for 10 s.
        NmbShaderCacheStats stats = {};
        uint64_t last = UINT64_MAX;
        for (unsigned still = 0; still < 20; usleep(500000)) {
            if (!native_bridge_metal_shader_cache_stats(&stats)) {
                std::fprintf(stderr, "FAIL: shader-cache statistics unavailable\n");
                return 1;
            }
            const uint64_t now = stats.writes + stats.driver_saves;
            still = now == last ? still + 1 : 0;
            last = now;
        }
        std::printf(
            "RESULT: requested=%zu primed=%u refused=%u absent=%u device=%s made=%u writes=%llu driver_saves=%llu "
            "hits=%llu misses=%llu driver_hits=%llu driver_misses=%llu build=%s\n",
            count, primed, refused, absent, deviceName.c_str(), (unsigned)native_prime_made,
            (unsigned long long)stats.writes, (unsigned long long)stats.driver_saves,
            (unsigned long long)stats.hits, (unsigned long long)stats.misses,
            (unsigned long long)stats.driver_hits, (unsigned long long)stats.driver_misses, build.c_str());
        if (primed == count && !refused && !absent) {
            std::printf(
                "PASS: requested=%zu primed=%u refused=%u absent=%u\n",
                count, primed, refused, absent);
            return 0;
        }
        std::printf(
            "FAIL: requested=%zu primed=%u refused=%u absent=%u\n",
            count, primed, refused, absent);
        return 1;
    }
}
