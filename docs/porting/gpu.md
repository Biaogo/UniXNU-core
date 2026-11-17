# GPU and Metal adaptation

Complete [platform DMA](/porting/platform) and basic [display presentation](/porting/display)
first. Read the [Adreno740 runtime path](/drivers/adreno740) and
[AdrenoMetal interface](/drivers/adreno-metal).

Use [driver build integration](/porting/drivers#build-integration) to connect
initialization, archive recipes and the OS ABI, then run this page's hardware checks.

## Sources and interfaces {#source-map}

These links identify reviewed source revisions. Match interfaces to the selected
SoC, component and OS versions. Files in `UniXNU-sheng` provide board integration
examples.

| Implementation | Source | What to read and implement |
| --- | --- | --- |
| Power and firmware | [NativeAdrenoGmuPlatform.h](https://github.com/UniXNU/Adreno740/blob/4e0314df8ad29d52f6fa3f9383a540adcd618b87/NativeAdrenoGmuPlatform.h#L15), [build-adreno740-kits.py](https://github.com/UniXNU/UniXNU-sheng/blob/516a780473a2ac59c8b438e132bb60204dda9e22/scripts/build-adreno740-kits.py) | `NativeAdrenoGmuPlatform::bind/services`, mapped windows, timers and firmware inputs |
| Submission and memory | [NativeAdrenoClient.h](https://github.com/UniXNU/Adreno740/blob/4e0314df8ad29d52f6fa3f9383a540adcd618b87/NativeAdrenoClient.h#L14), [NativeAdrenoVm.h](https://github.com/UniXNU/Adreno740/blob/4e0314df8ad29d52f6fa3f9383a540adcd618b87/NativeAdrenoVm.h) | `NativeAdrenoTransport`, BO lifetime, GPU mappings and completion |
| OS/frontend interface | [NativeOfflineAccelerator.cpp](https://github.com/UniXNU/AdrenoMetal/blob/454ff2ce83766ce7b3e0f355e83f1d72907708b3/NativeOfflineAccelerator.cpp), [OfflineMetalWire.h](https://github.com/UniXNU/AdrenoMetal/blob/454ff2ce83766ce7b3e0f355e83f1d72907708b3/OfflineMetalWire.h) | Accelerator publication and request structures used by the matching frontend |

## 1. Match the GPU generation and OS ABI

Adreno740 implements the A740 kernel interface. AdrenoMetal uses that interface,
Turnip, metal2vulkan and the selected OS GPU/IOSurface ABI. Identify the target
GPU revision and compare register, firmware, packet and memory interfaces.
For another GPU generation, implement the matching kernel backend and compiler
support before selecting its frontend.

For another A740 board, collect GPU/GMU registers, interrupts, SMMU context and
stream IDs, firmware, power domains, clock/voltage levels, interconnect votes,
RAM reservations and scanout layout. Check them against the selected firmware.

## 2. Adapt power and firmware inputs

Read the [SM8550 platform sources](https://github.com/UniXNU/UniXNU-sm8550-common/tree/main/kits)
and the [sheng package](https://github.com/UniXNU/UniXNU-sheng/tree/main/package).
`Sm8550Platform` supplies shared GMU operations and command-database handling.
`ShengGmuVotes.c` and `ShengGmuVotesSource.h` supply the board's OPP and bandwidth
policy. Port those inputs to the target's rails and command database.

The sheng recipe `scripts/build-adreno740-kits.py` embeds these local inputs:

| Firmware | Purpose |
| --- | --- |
| `qcom/gmu_gen70200.bin` | GMU image |
| `qcom/a740_sqe.fw` | Command processor firmware |
| `qcom/sm8550/sheng/a740_zap.mbn` | Board's signed ZAP image |

The recipe checks pins in `scripts/data/adreno-inputs.json` and reads
`.cache/firmware/sheng-gpu/`. Adapt the extraction paths and pins to the target's
verified firmware; record provenance in `proprietary-files.txt`.

**Expected result:** board power inputs and firmware match the target GPU.
Confirm GMU startup and HFI response before submitting commands.

## 3. Establish memory and command submission

1. Review ArmSmmu4K context ownership, stream selection, address width and cache
   maintenance. Use the target GPU and firmware stream IDs from its hardware description.
2. Adapt reserved memory and GPU virtual-address windows. Keep scanout, firmware,
   ring and client allocations within their owned ranges.
3. Confirm CP initialization, ring progress and fence completion with bounded work.
4. Render a known image and inspect the result before using UI workloads.
5. Verify client teardown, mapping lifetime and fence failures.

Use `AdrenoFirmware` for firmware/HFI operations and `AdrenoPM4` for supported
packet encoding. The sheng recipe links these kits, ArmSmmu4K and board votes
into `libAdreno740.a`, then builds the accelerator through
`build-adreno740-accelerator.py`. Preserve that order when creating the target recipe.

**Expected result:** commands complete, fences advance and the known image is
correct. Record GPU results independently from desktop presentation.

### Keep mapping and submission cheap {#memory-path}

A desktop frame allocates, maps and frees many buffers, so per-call cost decides
smoothness. Adreno740 and ArmSmmu4K use these rules; keep them when porting:

| Operation | Rule |
| --- | --- |
| Object lookup | A client's objects are limited by memory and found through a handle index. |
| Allocation | BOs come from system memory: a pageable `IOBufferMemoryDescriptor` with no task and the write-combined cache mode, wired by `prepare()`. The VM sets the page attribute in one batch and restores it on free; client views map that descriptor. Only a caller that asks for contiguous memory gets a contiguous kernel allocation. |
| Map of owned memory | `ArmSmmu4KLiveMapContextBatchExclusive`: no alias check against the existing ranges. |
| Map of imported memory | The same call when every mapped import shares its cache class and none of its pages is in a BO of this driver (a page bitmap over DRAM); otherwise the fully checked batch map. |
| Unmap | Up to 512 pages: one TLBIVAL per page. Larger: invalidate the ASID. |
| Submission | No TLB invalidation. A map fills only invalid leaves, an unmap invalidates its own pages, and the upper tables never change. A stalled fence still invalidates. |

The boot log's `[vm map]`, `[vm unmap]`, `[vm kinds]` and `[bo memory]` rows give
the count and time of each operation. Compare them before and after a change.

With `NativeAdrenoClient`, implement one bounded submission in this order:
query ABI/capabilities, allocate a BO, map it to an allowed IOVA, populate commands,
submit and wait for the returned fence. Bind `NativeAdrenoTransport.call` to the
actual kernel dispatcher, preserving each request's input/output sizes. Synchronize
caches before CPU readback. On fence timeout, stop the queue and confirm DMA has stopped
before releasing the BO and mapping. Report the failed request, handle and fence
to distinguish allocation, mapping and execution failures.

### Render mode with an 8-bit BGRA framebuffer {#render-mode}

With `sheng-fb-bgra=1` the plugin sets `TU_DEBUG=sysmem,nocb`: every pass renders in system memory. Tile memory holds a colour
in the byte order its render target writes, so a GMEM load or store must move the bytes with the identity swap on the memory
side; the recorded Mesa patch `turnip-gmem-mutable-swap.patch` does that for resolve, 2D and 3D stores and 3D loads. With it
the lock screen matched the RGhA picture to 99.5 % in tile memory, but the GPU time per command buffer was no better than in
system memory (17.8 ms mean, 56.1 ms at p95, against 17.1 and 52.1) and one home screen frame still came out with red and blue
exchanged, so system memory stays the default. `sheng-tu-debug=` overrides it for experiments.

### Find where a slow frame goes {#frame-path}

Split a slow frame into the time each layer holds it before changing anything. Every segment below is
measured on the host uptime clock; the GPU's always-on counter is mapped to it by a fitted line.

| Segment | Where it is measured |
| --- | --- |
| Commit to scheduler | Runtime stage records (commit, prepared, handed, taken). |
| Scheduler to submit | Runtime stage records, with the reason a batch was held. |
| Submit to GPU start | Driver job timeline: the jobs ahead in the ring, by process. |
| GPU start to end | CP start and end stamps per job; Turnip stamps per render pass. |
| GPU end to handler | Driver retire time, the completion waiter, the completed handler. |

Measured on sheng with iPadOS 27 (widget page, Settings, Safari):

- Driver lock waits stay under 7 ms. A render pass takes 0.5 ms of GPU time at the median, a full-screen pass 1.1 ms.
- Most slow frames wait in the ring behind other processes' jobs: a poster extension submitted jobs of about 77 ms each,
  and the ring runs jobs in order with no priority.
- The runtime threads that bring a command buffer to its handlers ran at the default QoS: the class was set on macOS only.
- Blocks of 94 to 112 MiB took 281 to 340 ms to wire when Settings and Safari opened.

## 4. Build and stage Metal

Run these commands from the resolved target tree after preparing the matching
SDK, compiler dependencies and ABI inputs:

```sh
python3 scripts/build-turnip-darwin.py --target ios
python3 scripts/build-adreno-metal-plugin.py
python3 scripts/add-gpu-plugin-data.py --publish /path/to/base.dbr /path/to/gpu.dbr
```

The staging command uses macOS image tools and writes a copy of the Data seed.
Use that output as `--data-ramdisk` when building the payload, then rebuild the
boot image. For selected app processes, the staging command also accepts
`--clients NAME,...`. The runtime plugin name is `NativeAdrenoMetal`.

Check the selected OS profile's sandbox, user-client and trust-cache integration.
The Metal bridge never returns a nil encoder: a pass it cannot encode gets an
encoder whose command buffer is refused, because RenderBox caches the encoder's
methods. The process's first problem appears on the driver's per-process text
line (`[gpu client text]` in the boot log).
AirNir is the offline compiler kit; the runtime frontend uses the
AIR → SPIR-V → NIR → IR3 chain described in the component page.

**Expected result:** Metal device discovery, client connection, pipeline creation,
rendering and presentation each succeed. Record firmware, compiler and plugin
revisions with the frame result. Cold pipeline compilation and cache reuse need
separate timing observations.

### Warm the shader caches {#shader-warmup}

A pipeline compiles from Apple's AIR the first time something asks for it, which stalls frames. Warm the caches ahead so the desktop finds them. Two inputs do this, and both use the same mechanism for every OS profile (iOS/iPadOS 17 `--target ios`, 27 `--target ios27`); only the library list differs, because it names the libraries of that OS image.

1. **Priming list.** Write the absolute path of every `.metallib` of the OS image, one per line, as the render server sees it (`/System/Library/...`). Keep it in `build/prime-list.txt`; `build-adreno-metal-plugin.py` embeds it in the plugin as the `__DATA,__primelist` section, and `make-gpu-addon.py --prime-list FILE` ships it as `addons/NativeAdrenoMetal.primelist`. With a list the render server does not scan directories: its sandbox hides most of them.
2. **Pipeline pack.** Boot an image built with `make-gpu-addon.py --cache-export`; the plugin writes its cache directory to the board log. Read the run with `scripts/read-sheng-log.py`, merge one or more exports with `scripts/make-m2vpack.py OUT EXPORT_DIR...` (later directories win) and ship the result with `make-gpu-addon.py --pipeline-pack OUT`. The plugin imports the pack into the cache directory before the runtime opens it.

#### Host-generated pipeline pack

`scripts/make-pipeline-cache.py` drives the current `PrimeShaders.h` through the native Metal bridge and a host Turnip build over the software NativeAdreno peer. It is a macOS/Xcode flow and requires the target iOS Turnip, a host Turnip built from the same compiler-source identity, the host Metal bridge and peer, `build/prime-list.txt`, and real mounts of the matching OS and `System/Cryptexes/OS` volumes. The dodge 21H461 defaults are target `ios`, GPU `a830`, `TU_DEBUG=sysmem,nocb`, and `HOSTSIM_GPU=a830`.

The A830 peer contract is a top-level host-only patch, `patches/adreno740/hostsim-a830-peer.patch`, applied by `scripts/host_peer.py` to a copy of the pinned Adreno740 peer under `build/`. The submodule and its pin stay unchanged. An unset or `a740` value keeps the original A740 device record; `a830` selects chip `0x44050001`, 12 MiB GMEM, UBWC 5, HBB 16, swizzle 6, macrotile 1, and the base context's 4 GiB slot-0 window at `0x20000000`. This is not the device driver's 16 GiB configuration. `check-turnip-darwin-host.py` checks the actual `GetDeviceInfo` record and the context buffer layout for both choices, and rejects any other GPU value.

Build the existing host pieces first, then provide the two read-only OS mounts:

```sh
mkdir -p build
cp scripts/data/prime-list.txt build/prime-list.txt
python3 scripts/build-turnip-darwin.py --target host
python3 scripts/build-turnip-darwin.py --target ios
python3 scripts/build-adreno-metal-bridge.py --target host
NMB_TEST_QUARTZCORE_LIBRARY=/path/to/21H461/os/System/Library/Frameworks/QuartzCore.framework/default.metallib \
  python3 scripts/check-turnip-darwin-host.py
python3 scripts/make-pipeline-cache.py \
  --target ios --gpu a830 \
  --os-mount /path/to/21H461/os \
  --cryptex-mount /path/to/21H461/cryptex \
  --output build/dodge.m2vpack
```

`check-turnip-darwin-host.py` includes
`check-quartzcore-turnip-cache-host.py`, which compiles the actual
`bridge/quartzcore_cache.mm` instrument with `bridge/graphics_cache_loader.cpp`.
It requires the mounted 21H461 `default.metallib` (9,670,832 bytes, SHA256
`606f913adf520420f9b4bb666efb782a68c830f082f1e559e17b20d0d13adbc9`), reports
the actual 24 recipes, and checks produce, compilation-forbidden consume, and
two driver negative controls. For UUID miss and empty-driver, the checker
captures both independent-process cases and their logs before validating each
recipe CALL. A driver compile-required result must be strict, shader-bearing,
and return no pipeline handle. Metal object creation is reported separately and
does not establish drawability. The checker writes case logs and a JSON identity
report under `build/quartzcore-turnip-cache/`; there is no fallback to the host
macOS QuartzCore library.

The generator fails before compiling the shader producer if the mounts are not 21H461, the list is not the expected 166 libraries, host and target compiler-source identities differ, the target dylib is not a thin ARM64/iOS Mach-O, or the prepared host peer product is stale. It also reads the actual ABI device record and checks the A830 chip, GMEM, base, trap, VA, HBB, swizzle, macrotile, and UBWC fields. The output is a real `SHEXP001`/`M2VCACH1` pack plus a JSON identity report.

Host shader compilation and the mandatory software transport do not execute GPU commands or prove A830 hardware behavior. The pack must still be checked on the target with a cold import followed by a warm boot/cache-hit observation. A Linux source regression can build and run the prepared peer with the real NativeAdreno kernel core:

```sh
TMPDIR=/path/to/scratch CXX=/path/to/g++ python3 scripts/test-dodge-host-peer.py
```

This checks unset, `a740`, and `a830` in separate processes, including allocate, map, submit, and close over the software fixture. It does not build the Metal producer. The macOS compile of this flow and portability of a host-produced cache to the iOS runtime are not yet verified.

### Addon CI

The Adreno addon workflow runs on an arm64 `macos-26` runner for up to 240
minutes. It builds the host Turnip and bridge, prepares the OS inputs, then
enters one read-only OS/cryptex mount lifecycle. Inside that lifecycle the
workflow first compiles the host peer and runs the complete host smoke with
`NMB_TEST_QUARTZCORE_LIBRARY` pointing at the mounted 21H461 metallib, and then
runs `make-pipeline-cache.py`. Both stages share one timeout and log; no
standalone host-smoke step runs against the runner's macOS framework. After the
generator finishes, both mounts are detached and the iOS plugin and mandatory
packed addon are built. The removed external-pack input cannot bypass this
generation path.

The default source is the fixed official iPadOS 17.7.11 image
`iPad_Pro_HFR_17.7.11_21H461_Restore.ipsw`:

```text
https://updates.cdn-apple.com/2025SpringFCS/fullrestores/122-77166/DCA6CFE8-90E9-4B92-880D-3DE171798E40/iPad_Pro_HFR_17.7.11_21H461_Restore.ipsw
```

Its pin is 6,835,124,840 bytes with SHA256
`c84ad24b6981a859eec499b1802be9ac593acfd3dc0a185a76d06e4d767a1451`.
`scripts/ci-dodge-pipeline-cache.py` streams the download while enforcing that
length and digest, then parses `BuildManifest.plist`. It requires build
`21H461`, the pinned iPad7,1-iPad7,4 product set, the `j120ap`/`Erase`
identity, OS entry
`044-76040-038.dmg` and cryptex entry `044-76034-038.dmg`, and extracts only
those two images. The IPSW may be cached on the runner, but no IPSW, DMG,
unmodified metallib, host tree or secret URL is uploaded as an artifact.

The OS image is attached with
`hdiutil attach -readonly -nobrowse -owners off -mountpoint ... -plist`; the
raw APFS cryptex additionally uses
`-imagekey diskimage-class=CRawDiskImage`. Both mounts are detached in a
`finally` path using only device identities returned for those two attachments.
The cache generator uses `--target ios --gpu a830 --jobs 2`, and the packed
addon is unpacked and checked to carry `addons/NativeAdrenoMetal.m2vpack` byte
for byte. The workflow uploads the pack, generator and OS identity reports,
addon, host-smoke/generator log, and compiled logs.

Budget roughly 16 GiB of transient disk for the IPSW plus both extracted images
on a cache miss. The Linux regression exercises the download identity,
manifest selection, ZIP traversal rejection, workflow shell syntax, mounted
QuartzCore override, smoke-before-generator order, and mount cleanup control
flow with fixtures only. It does not prove a macOS compile, A830 execution or
target-side cache reuse.

How priming runs:

- It starts 90 s after the render server opens the device, on a background thread with an 8 MiB stack.
- Each kernel function becomes a compute pipeline. Each vertex function becomes a render pipeline with the fragment function of the same stem (`_vert_` to `_frag_`) and one BGRA8 attachment. Pipelines made later with other constants or attachments still compile on demand.
- It waits while the desktop animates: more than 8 command buffers in 250 ms is busy, and the idle desktop at 120 Hz submits about 6. After an animation it waits 2 s of quiet, and it never waits longer than 10 s, so one pipeline still compiles on a desktop that never settles.
- Progress lives in the cache directory: `prime-done` (finished libraries), `prime-fn` (finished functions of libraries not finished yet), `prime-current` (the function being compiled), `prime-bad` (functions that ended the process; a start that finds `prime-current` moves it there) and `prime.log`. A restart resumes inside a library with thousands of functions.
- Each finished entry carries the build id: the runtime's translator build plus the Turnip library's `LC_UUID`. A runtime or Turnip rebuild primes every library again; a plugin rebuild alone keeps the work.
- The PR row of the board log shows what the thread does (compiling, waiting, opening a library), libraries done of the list, pipelines made and the highest submit rate a wait saw.

**Expected result:** the PR row reaches all libraries of the list, and a second boot shows no new pipeline compiles for the same scenes. Record the list size, the build id and the cold and warm frame timings.

## Next step

Continue with [Wi-Fi](/porting/network), or return to the
[driver checklist](/porting/drivers).
