#!/usr/bin/env python3
"""Drive the Turnip darwin backend through a real Vulkan instance against the host copy of the NativeAdreno kernel stack.

The kernel half is the real GPU context bring-up, VM, ring, submit and user-client code over software SMMU/CP peers
(tests/hostsim/host_gpu_service.cpp); an IOKit shim answers the backend's IOKit calls. Nothing here runs a shader or touches
hardware: the software CP retires every submission at once. Needs the host Turnip build (build-turnip-darwin.py --target host)."""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from host_peer import CONTRACTS, HOST_CLOCK_SHIM, host_core_cflags, host_mmio_source, hostsim_environment, prepare, write_build_manifest, write_host_includes
from watchdog_source import prepare_watchdog_include

ROOT = Path(__file__).resolve().parents[1]
# The host peer and kernel core stay on the pinned Adreno740 source. The peer
# patch selects A740 (unset/a740) or A830 (a830) without changing the submodule.
kit = ROOT / 'drivers/Adreno740'
sim = kit / 'tests/hostsim'
out = ROOT / 'build/turnip-darwin-host'
out.mkdir(parents=True, exist_ok=True)
watchdog_include = prepare_watchdog_include(out / 'watchdog-source')
peer = prepare(out)
smmu = ROOT / 'kits/ArmSmmu4K'
library = ROOT / 'build/mesa-tu/src/freedreno/vulkan/libvulkan_freedreno.dylib'
# The pinned Mesa's Vulkan headers (the ones Turnip is built with) after Homebrew's, so the host programs need no vulkan-headers package.
VULKAN_INCLUDE = ROOT / '.cache/mesa-work/include'
if not library.exists():
    sys.exit('Build the host Turnip first: python3 scripts/build-turnip-darwin.py --target host')

includes = write_host_includes(out)
dump = subprocess.run(['xcrun', 'clang', '-dumpmachine'], check=True, capture_output=True, text=True)
host_cpu = dump.stdout.strip()
core_flags = host_core_cflags(host_cpu)
mmio_source = host_mmio_source(out, host_cpu)

objects = []
for stem in ['ArmSmmu4KContext', 'ArmSmmu4KProgram', 'ArmSmmu4KLive', 'ArmSmmu4KStreams', 'ArmSmmu4KMmio']:
    obj = out / (stem + '.o')
    source = mmio_source if stem == 'ArmSmmu4KMmio' else smmu / (stem + '.c')
    subprocess.run(['xcrun', 'clang', '-std=c11', '-O1', '-fPIC', *core_flags, '-c', source, '-o', obj], check=True)
    objects.append(obj)
obj = out / 'AdrenoPM4.o'
subprocess.run(['xcrun', 'clang', '-std=c11', '-O1', '-fPIC', *core_flags, '-c', ROOT / 'kits/AdrenoPM4/AdrenoPM4.c', '-o', obj], check=True)
objects.append(obj)

service = out / 'host_gpu_service.o'
subprocess.run(['xcrun', 'clang++', '-std=c++23', '-O1', '-fPIC', *core_flags, '-I', includes, '-I', sim.parent, '-I', kit, '-I', smmu,
                '-I', ROOT / 'kits/AdrenoPM4', '-c', peer.source, '-o', service], check=True)
core = []
for name in ['NativeAdrenoMemory', 'NativeAdrenoSmmuMemory', 'NativeAdrenoVm', 'NativeAdrenoRing', 'NativeAdrenoPreempt',
             'NativeAdrenoSubmit', 'NativeAdrenoUserClient', 'NativeAdrenoTimer', 'NativeAdrenoSmmuContext',
             'NativeAdrenoGpuContext']:
    obj = out / (name + '.o')
    subprocess.run(['xcrun', 'clang++', '-std=c++23', '-O1', '-fPIC', *core_flags, '-I', includes, '-I', sim.parent, '-I', kit, '-I', smmu,
                    '-I', ROOT / 'kits/AdrenoPM4', '-c', kit / (name + '.cpp'), '-o', obj], check=True)
    core.append(obj)
shim_obj = out / 'host_iokit_shim.o'
subprocess.run(['xcrun', 'clang++', '-std=c++17', '-O1', '-fPIC', '-c', peer.shim_source, '-o', shim_obj], check=True)
clock_obj = out / 'host-clock-shim.o'
subprocess.run(['xcrun', 'clang++', '-std=c++17', '-O1', '-fPIC', '-c', HOST_CLOCK_SHIM, '-o', clock_obj], check=True)
# Kernel KPIs the user-client code calls that the peer does not define (proc_selfpid, for the import bookkeeping); weak, so a peer that
# defines one wins.
kpi = out / 'host_kpi.c'
kpi.write_text('#include <unistd.h>\n__attribute__((weak)) int proc_selfpid(void) { return getpid(); }\n')
kpi_obj = out / 'host_kpi.o'
subprocess.run(['xcrun', 'clang', '-std=c11', '-O1', '-fPIC', '-c', kpi, '-o', kpi_obj], check=True)
shim = out / 'libhostiokit.dylib'
subprocess.run(['xcrun', 'clang++', '-dynamiclib', '-o', shim, service, shim_obj, kpi_obj, clock_obj, *core, *objects,
                '-framework', 'IOKit', '-framework', 'CoreFoundation'], check=True)
write_build_manifest(peer, shim)
# Device names do not prove the host contract. Inspect the actual ABI records
# and the context buffer layout built by bringUp() for both shipped choices.
abi_probe = out / 'host_peer_probe'
subprocess.run(['xcrun', 'clang++', '-std=c++17', '-O1', '-I', kit, '-I', ROOT / 'kits/AdrenoPM4',
                ROOT / 'scripts/host-peer-probe.cpp', '-o', abi_probe], check=True)
for name in ('a740', 'a830'):
    expected = CONTRACTS[name]
    probe_env = hostsim_environment(HOSTSIM_GPU=name)
    probe_env.pop('DYLD_INSERT_LIBRARIES', None)
    result = subprocess.run([str(abi_probe), 'info', str(shim), *expected.probe_arguments()],
                            env=probe_env, capture_output=True, text=True, timeout=120)
    (out / ('abi-' + name + '.log')).write_text(result.stdout + result.stderr)
    print(result.stdout, end='')
    if result.returncode:
        sys.exit('Host peer %s ABI check failed (exit %d); see %s' %
                 (name, result.returncode, out / ('abi-' + name + '.log')))
invalid_env = hostsim_environment(HOSTSIM_GPU='a830x')
invalid_env.pop('DYLD_INSERT_LIBRARIES', None)
invalid = subprocess.run([str(abi_probe), 'info', str(shim), *CONTRACTS['a830'].probe_arguments()],
                         env=invalid_env, capture_output=True, text=True, timeout=120)
if invalid.returncode == 0 or 'invalid HOSTSIM_GPU=a830x' not in invalid.stderr:
    sys.exit('The host peer did not reject an invalid HOSTSIM_GPU value')
smoke = out / 'vulkan_smoke'
subprocess.run(['xcrun', 'clang', '-std=c11', '-O1', '-I', '/opt/homebrew/include', '-I', VULKAN_INCLUDE, sim / 'vulkan_smoke.c', '-o', smoke,
                '-Wl,-rpath,' + str(out)], check=True)

plugin_smoke = out / 'plugin_smoke'
subprocess.run([sys.executable, ROOT / 'scripts/gen-turnip-smoke-shaders.py'], check=True)
subprocess.run(['xcrun', 'clang++', '-std=c++17', '-O1', '-I', watchdog_include, '-I', ROOT / 'drivers/AdrenoMetal',
                '-I', ROOT / '.cache/mesa-work/include',
                '-I', ROOT / 'build/turnip-smoke',
                ROOT / 'scripts/check-turnip-watchdog-host.cpp', '-o', plugin_smoke], check=True)
env = hostsim_environment(DYLD_INSERT_LIBRARIES=str(shim), MESA_LOG='')
env.pop('TU_DARWIN_SERVICE', None)   # the default is the production transport: accelerator service, open type 0x4147, tunnel through one admitted selector
env.pop('HOSTSIM_GPU', None)   # unset is the shipped A740 default
for gpu in (None, 'a830'):
    smoke_env = hostsim_environment(env, HOSTSIM_TRACE='1')
    label = 'unset' if gpu is None else gpu
    if gpu:
        smoke_env['HOSTSIM_GPU'] = gpu
    log = out / ('smoke.log' if gpu is None else 'smoke-' + gpu + '.log')
    result = subprocess.run([str(smoke), str(library)], env=smoke_env, capture_output=True, text=True, timeout=300)
    log.write_text(result.stdout + result.stderr)
    print(result.stdout, end='')
    if result.stderr.strip():
        print(result.stderr[-3000:], end='')
    if result.returncode or 'PASS' not in result.stdout:
        sys.exit('Turnip darwin host smoke failed for %s (exit %d); see %s' %
                 (label, result.returncode, log))
# Keep a software ring fence pending while freeing an unrelated mapped allocation.
# The actual KMD must defer native handle/view/IOVA retirement, bound pressure waits,
# and preserve ownership on failures. This exercises real transport code, not pixels.
deferred = out / 'deferred_bo'
subprocess.run(['xcrun', 'clang++', '-std=c++23', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                '-I', '/opt/homebrew/include', '-I', VULKAN_INCLUDE, '-I', kit, sim / 'deferred_bo.cpp', '-o', deferred], check=True)
deferred_env = dict(env, TU_DARWIN_RETIRE_TRACE='1')
deferred_env.pop('HOSTSIM_FAIL_USER_UNMAP', None)
for mode in ['normal', 'busy', 'fence-error', 'free-error', 'lease-error', 'view-error', 'map-cleanup-error', 'pressure']:
    # The release paths need every free to reach the kernel: only the normal mode runs with the BO cache on.
    mode_env = dict(deferred_env, TU_DARWIN_BO_CACHE='1' if mode == 'normal' else '0')
    test = subprocess.run([str(deferred), str(library), mode], env=mode_env, capture_output=True, text=True, timeout=60)
    (out / ('deferred-' + mode + '.log')).write_text(test.stdout + test.stderr)
    verdict = [line for line in test.stdout.splitlines() if line.startswith('PASS:')]
    if test.returncode or not verdict:
        sys.exit('Deferred BO %s failed (exit %d); see %s' % (mode, test.returncode, out / ('deferred-' + mode + '.log')))
    if mode == 'pressure' and 'pressure=0' not in test.stderr:
        sys.exit('Deferred BO pressure control waited for the GPU instead of growing its queue')
    print(verdict[0])
# Driver cache controls compile Adreno code but submit/execute no shader.
subprocess.run([sys.executable, ROOT / 'scripts/check-turnip-pipeline-cache-host.py'], check=True)
# Original UI recipes additionally cover graphics-library caches, strict driver
# feedback and genuine UUID/empty-cache refusals. The software peer runs no shader.
with tempfile.TemporaryDirectory(prefix='quartzcore-driver-cache-', dir=out) as temporary:
    subprocess.run([sys.executable, ROOT / 'scripts/check-quartzcore-turnip-cache-host.py',
                    '--output', Path(temporary) / 'control'], check=True, timeout=660)

with tempfile.TemporaryDirectory(prefix='priming-driver-cache-', dir=out) as temporary:
    subprocess.run([sys.executable, ROOT / 'scripts/check-priming-cache-host.py',
                    '--output', Path(temporary) / 'control'], check=True, timeout=360)

env_plugin = dict(env)
plugin = subprocess.run([str(plugin_smoke), str(library)], env=env_plugin, capture_output=True, text=True, timeout=300)
(out / 'plugin_smoke.log').write_text(plugin.stdout + plugin.stderr)
print(plugin.stdout, end='')
if plugin.returncode:
    sys.exit('The plugin Turnip sequence did not behave as expected on the host (exit %d); see %s' % (plugin.returncode, out / 'plugin_smoke.log'))
# Watchdog: block the first BoAlloc for 17 s; the 15 s sample must name the last marker (6004), show the worker waiting (not running) and
# carry at least one return address inside the Turnip image ('v' frames).
stalled = subprocess.run([str(plugin_smoke), str(library)], env=dict(env_plugin, HOSTSIM_STALL='17'), capture_output=True, text=True, timeout=300)
(out / 'plugin_watchdog.log').write_text(stalled.stdout + stalled.stderr)
(out / 'plugin_watchdog.stdout.log').write_text(stalled.stdout)
(out / 'plugin_watchdog.stderr.log').write_text(stalled.stderr)
lines = [l for l in stalled.stdout.splitlines() if l.startswith('watchdog: W ')]
polls = re.findall(r'^watchdog poll: elapsed_ms=(\d+) polls=(\d+)$', stalled.stderr, re.M)
process_wall = re.findall(r'^watchdog process: wall_ms=(\d+) polls=(\d+)$', stalled.stderr, re.M)
print('\n'.join(lines))
stall_begin = re.findall(
    r'^shim: stall method=2 route=(tunnel|direct) requested_ms=17000 result=begin$',
    stalled.stderr, re.M,
)
stall_done = []
if len(stall_begin) == 1:
    stall_done = re.findall(
        r'^shim: stall method=2 route=' + stall_begin[0]
        + r' requested_ms=17000 interrupts=(\d+) result=complete elapsed_ms=(\d+)$',
        stalled.stderr, re.M,
    )
if (stalled.returncode or len(lines) != 1 or not lines[0].startswith('watchdog: W 6004 ') or ' v' not in lines[0] or 'kern' in lines[0]
        or len(stall_begin) != 1 or len(stall_done) != 1 or int(stall_done[0][1]) < 17000
        or not polls or [
            int(elapsed) for elapsed, _poll in polls
        ] != sorted(int(elapsed) for elapsed, _poll in polls)
        or max(int(elapsed) for elapsed, _poll in polls) < 15000
        or int(polls[-1][1]) != len(polls) or len(process_wall) != 1
        or int(process_wall[0][0]) < 17000):
    sys.exit('The plugin watchdog did not sample the stalled worker as expected; see %s, %s, and %s' %
             (out / 'plugin_watchdog.log', out / 'plugin_watchdog.stdout.log', out / 'plugin_watchdog.stderr.log'))
print('PASS: host shim injected one 17000 ms %s stall (%s interrupted attempts), measured elapsed %s ms' %
      (stall_begin[0], stall_done[0][0], stall_done[0][1]))
print('PASS: the plugin watchdog sampled a worker blocked in vkCreateDevice (one line, marker 6004, frames inside Turnip)')
print('PASS: the watchdog recorded %s real polls through %s ms and a %s ms process wall clock' %
      (process_wall[0][1], polls[-1][0], process_wall[0][0]))
print('PASS: the plugin Turnip sequence (fill, compute pipeline, render pass) built, submitted and retired every command buffer over the host kernel stack (the software CP executes nothing, so no result is verified by design)')
print('PASS: Turnip darwin backend drove a Vulkan instance, device, memory mapping and a submission over the host kernel stack; no GPU')
