#!/usr/bin/env python3
"""Qualify the real QuartzCore graphics recipes through Turnip's cache controls.

The linked host bridge and Turnip compiler use the mandatory software peer.
This compiles Adreno shaders but executes none. The target library must be the
exact 21H461 QuartzCore metallib supplied by --library or
NMB_TEST_QUARTZCORE_LIBRARY; there is deliberately no host macOS fallback.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from host_peer import hostsim_environment
from quartzcore_turnip_cache import (EvidenceError, parse_case, validate_cold,
                                     validate_consume, validate_produce,
                                     validate_refusal)

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / 'drivers/AdrenoMetal'
OUT = ROOT / 'build/quartzcore-turnip-cache'
EXPECTED_BYTES = 9670832
EXPECTED_SHA256 = '606f913adf520420f9b4bb666efb782a68c830f082f1e559e17b20d0d13adbc9'
INSTRUMENT_PATCH = ROOT / 'scripts/data/quartzcore-cache-host-instrument.patch'
SOURCE_SHA256 = {
    'drivers/AdrenoMetal/bridge/quartzcore_cache.mm':
        'eba452cfe53cf40692d00559aa610444fe2f324d8d54fbb5e0ddfe326c958e12',
    'drivers/AdrenoMetal/bridge/graphics_cache_loader.cpp':
        'e67a30a638a8abfab023036c32e953ffa799d4b79ba001f9ab20445362b621a7',
}
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--library', type=Path,
                    help='exact 21H461 QuartzCore default.metallib; defaults to NMB_TEST_QUARTZCORE_LIBRARY')
args = parser.parse_args()


def require(value, reason):
    if not value:
        raise SystemExit('FAIL: ' + reason)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fail(message, raw=None):
    print('FAIL: ' + message, flush=True)
    if raw is not None:
        print('RAW LOG:\n' + raw, flush=True)
    raise SystemExit(1)


selected = args.library or (Path(os.environ['NMB_TEST_QUARTZCORE_LIBRARY'])
                            if os.environ.get('NMB_TEST_QUARTZCORE_LIBRARY') else None)
require(selected, 'no target QuartzCore library; pass --library or set NMB_TEST_QUARTZCORE_LIBRARY')
library = selected.resolve()
require(library.is_file(), 'target QuartzCore library is missing: ' + str(library))
require(library.stat().st_size == EXPECTED_BYTES,
        'target QuartzCore library has %d bytes, expected %d: %s' %
        (library.stat().st_size, EXPECTED_BYTES, library))
require(sha(library) == EXPECTED_SHA256,
        'target QuartzCore library SHA256 is not the preserved 21H461 identity: ' + str(library))

work = args.output.resolve()
require(not work.exists(), 'preserve existing controls; choose a fresh output directory')
bridge = ROOT / 'build/adreno-metal-bridge/host/libadreno_metal_bridge.a'
driver = ROOT / 'build/mesa-tu/src/freedreno/vulkan/libvulkan_freedreno.dylib'
peer = ROOT / 'build/turnip-darwin-host/libhostiokit.dylib'
for path, reason in (
    (bridge, 'build the host Metal bridge first'),
    (driver, 'build the host Turnip first'),
    (peer, 'run check-turnip-darwin-host.py through its peer build first'),
):
    require(path.is_file(), reason + ': missing ' + str(path))

OUT.mkdir(parents=True, exist_ok=True)
work.mkdir(parents=True)
require(INSTRUMENT_PATCH.is_file(), 'host instrument patch is missing: ' + str(INSTRUMENT_PATCH))
instrument = work / 'host-instrument'
instrument.mkdir()
for relative, expected in SOURCE_SHA256.items():
    source = ROOT / relative
    actual = sha(source)
    require(actual == expected,
            'host instrument source identity changed: %s is %s, expected %s' %
            (relative, actual, expected))
    shutil.copy2(source, instrument / source.name)
subprocess.run([
    'patch', '--silent', '--batch', '--forward', '-p0', '-d', instrument,
    '-i', INSTRUMENT_PATCH,
], check=True, timeout=30)
loader = OUT / 'libOfflineGraphicsCacheControl.dylib'
program = OUT / 'quartzcore_cache_host'
flags = ['xcrun', 'clang++', '-O1', '-std=c++17', '-Wall', '-Wextra', '-Werror']
subprocess.run([
    *flags, '-dynamiclib', '-I', ROOT / '.cache/mesa-work/include',
    instrument / 'graphics_cache_loader.cpp', '-o', loader,
], check=True, timeout=60)
subprocess.run([
    *flags, instrument / 'quartzcore_cache.mm', '-I', KIT / 'bridge',
    bridge, '-Wl,-dead_strip', '-Wl,-S',
    '-framework', 'Foundation', '-framework', 'Metal', '-o', program,
], check=True, timeout=120)

env = hostsim_environment({
    key: value for key, value in os.environ.items()
    if not key.startswith(('IR3_', 'FD_MESA_', 'NIR_', 'TU_', 'HOSTSIM_', 'MESA_', 'NMB_TEST_'))
},
    DYLD_INSERT_LIBRARIES=str(peer),
    NMB_TEST_GRAPHICS_CACHE_LOADER=str(loader),
    NMB_TEST_QUARTZCORE_LIBRARY=str(library),
)
cases = {}
cache = work / 'control.cache'
archive = work / 'control.archive'


def case(label, mode, collect_failure=False):
    log = OUT / (label + '.log')
    command = [str(program), str(driver), str(work / 'control'), mode]
    try:
        run = subprocess.run(command, env=env, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired as error:
        detail = ((error.stdout or '') + (error.stderr or ''))
        if isinstance(detail, bytes):
            detail = detail.decode(errors='replace')
        log.write_text(detail)
        message = 'QuartzCore %s timed out' % label
        if collect_failure:
            return {
                'exit_code': None,
                'log': str(log.relative_to(ROOT)),
                'log_sha256': sha(log),
                'error': message,
            }
        fail(message, detail)
    raw = run.stdout + run.stderr
    log.write_text(raw)
    parse_error = None
    try:
        parsed = parse_case(raw) if run.returncode == 0 else None
    except EvidenceError as error:
        parsed = None
        parse_error = str(error)
    if run.returncode != 0 or parsed is None or parsed['summary']['mode'] != mode:
        detail = ('; evidence parser: ' + parse_error) if parse_error else ''
        message = 'QuartzCore %s exited %d%s' % (label, run.returncode, detail)
        if collect_failure:
            return {
                'exit_code': run.returncode,
                'log': str(log.relative_to(ROOT)),
                'log_sha256': sha(log),
                'error': message,
            }
        fail(message, raw)
    data = parsed
    data.update(
        exit_code=run.returncode,
        log=str(log.relative_to(ROOT)),
        log_sha256=sha(log),
    )
    cases[label] = data
    summary = data['summary']
    status = 'CAPTURED' if collect_failure else 'PASS'
    print('%s: QuartzCore %s process recipes=%d/%d vulkan=%d/%d required=%d '
          'app_cache=%d/%d/%d batches=%d/%d strict_suppressed=%d' %
          (status, mode, summary['recipe_ok'], summary['recipes'],
           summary['vulkan_success'], summary['vulkan_pipelines'],
           summary['vulkan_required_batches'], summary['app_cache_hits'],
           summary['app_cache_valid'], summary['app_cache_pipelines'],
           summary['vulkan_driver_batches'], summary['vulkan_batches'],
           summary['strict_suppressed']), flush=True)
    if mode == 'produce':
        cold_calls = [call for call in data['calls'] if call['phase'] == 'cold']
        print('PASS: QuartzCore cold batches=%d required=%d success_handles=%d '
              'strict_calls=%d/%d metal_object=%d' %
              (summary['cold_driver_batches'], summary['cold_driver_required'],
               summary['cold_driver_success'],
               sum(all(call['strict']) for call in cold_calls), len(cold_calls),
               summary['cold_object']), flush=True)
    return data


produced = case('produce', 'produce')
try:
    validate_produce(produced)
    cold = validate_cold(produced)
except EvidenceError as error:
    fail('producer evidence: ' + str(error),
         (ROOT / produced['log']).read_text())
print('PASS: QuartzCore cold layer batches=%d required=%d shaderless=%d '
      'cached_shader=%d metal_object=%d; first draw not exercised' %
      (cold['batches'], cold['compile_required_batches'],
       cold['shaderless_successes'], cold['cached_shader_successes'],
       cold['metal_object_returned']), flush=True)
require(archive.is_file() and archive.stat().st_size > 0,
        'producer did not write its real archive and cache records')

consumed = case('consume', 'consume')
try:
    consumer = validate_consume(consumed)
except EvidenceError as error:
    fail('consumer evidence: ' + str(error),
         (ROOT / consumed['log']).read_text())
print('PASS: QuartzCore consume evidence recipes=%d/%d pipeline_entries=%d '
      'application_cache_hits=%d/%d shaderless_strict_links=%d '
      'shader_bearing_entries=%d batches=%d/%d' %
      (consumer['recipe_calls'], consumed['summary']['recipes'],
       consumer['pipeline_entries'], consumer['app_cache_hits'],
       consumer['app_cache_entries'], consumer['shaderless_links'],
       consumer['shader_bearing_entries'], consumer['batches'],
       consumed['summary']['vulkan_batches']), flush=True)

def artifact_identity():
    identities = {str(archive.relative_to(work)): sha(archive)}
    for path in sorted(cache.rglob('*')):
        if path.is_file():
            identities[str(path.relative_to(work))] = sha(path)
    return identities


preserved = artifact_identity()
negative_runs = {
    'uuid-miss': case('uuid-miss', 'uuid-miss', collect_failure=True),
    'empty-driver': case('empty-driver', 'empty-driver', collect_failure=True),
}
negative_errors = {}
if artifact_identity() != preserved:
    negative_errors['artifacts'] = (
        'negative controls changed the producer archive or cache records')
negative_reports = {}
for label, uuid_mutated in (('uuid-miss', True), ('empty-driver', False)):
    run = negative_runs[label]
    if run.get('error'):
        negative_errors[label] = run['error']
        continue
    try:
        negative_reports[label] = validate_refusal(run, uuid_mutated)
    except EvidenceError as error:
        negative_errors[label] = 'negative evidence: ' + str(error)
if negative_errors:
    raw_logs = []
    for label, run in negative_runs.items():
        path = ROOT / run['log']
        if path.is_file():
            raw_logs.append('--- %s: %s ---\n%s' %
                            (label, run.get('error', 'evidence validation failed'),
                             path.read_text()))
    fail('\n'.join('%s: %s' % item for item in negative_errors.items()),
         '\n'.join(raw_logs))
for label, report in negative_reports.items():
    print('PASS: QuartzCore %s driver_compile_required=%d shader_bearing=%d '
          'shaderless_links=%d metal_object_returned=%d '
          'object_drawability_verified=0 init_calls_ignored=%d' %
          (label, report['driver_compile_required_entries'],
           report['shader_bearing_compile_required_entries'],
           report['shaderless_library_links'],
           int(report['metal_object_returned']),
           report['init_calls_ignored']), flush=True)
require(len({item['summary']['pid'] for item in cases.values()}) == len(cases),
        'QuartzCore controls did not run in independent processes')

sources = [
    KIT / 'bridge/NativeMetalObjects.h',
    KIT / 'bridge/NativeMetalBridge.rs',
]
peer_report = peer.parent / 'peer-build.json'
report = {
    'evidence': 'actual QuartzCore recipes through the host Metal bridge and Turnip compiler over the software peer; no shader execution',
    'checker_sha256': sha(Path(__file__)),
    'sources_sha256': {str(path.relative_to(ROOT)): sha(path) for path in sources},
    'host_instrument': {
        'source_sha256': {
            relative: sha(ROOT / relative) for relative in SOURCE_SHA256
        },
        'patch': str(INSTRUMENT_PATCH.relative_to(ROOT)),
        'patch_sha256': sha(INSTRUMENT_PATCH),
        'patched_sha256': {
            str((instrument / (ROOT / relative).name).relative_to(work)):
                sha(instrument / (ROOT / relative).name)
            for relative in SOURCE_SHA256
        },
        'target_sources_modified': False,
    },
    'library': {
        'path': str(library),
        'bytes': library.stat().st_size,
        'sha256': sha(library),
        'identity': '21H461 QuartzCore default.metallib',
    },
    'bridge_sha256': sha(bridge),
    'driver_sha256': sha(driver),
    'peer_sha256': sha(peer),
    'peer_report_sha256': sha(peer_report) if peer_report.is_file() else None,
    'process_sha256': sha(program),
    'loader_sha256': sha(loader),
    'archive': {
        'path': str(archive.relative_to(work)),
        'bytes': archive.stat().st_size,
        'sha256': sha(archive),
    },
    'cache_files': {
        name: digest for name, digest in preserved.items()
        if name != str(archive.relative_to(work))
    },
    'cases': cases,
    'recipes': 24,
    'counting_boundary': (
        'Recipe completion is one Metal bridge result. Vulkan pipeline entries '
        'are summed across possibly multiple vkCreateGraphicsPipelines batches; '
        'application-cache feedback is counted only for entries passed with a '
        'non-null pipeline cache. Strict shaderless links are counted separately '
        'from cached entries and are not cache hits.'),
    'consumer_control': {
        'pipeline_entries': consumer['pipeline_entries'],
        'successful_entries': consumer['successful_entries'],
        'valid_entries': consumer['valid_entries'],
        'application_cache_entries': consumer['app_cache_entries'],
        'application_cache_hits': consumer['app_cache_hits'],
        'shader_bearing_entries': consumer['shader_bearing_entries'],
        'shaderless_strict_links': consumer['shaderless_links'],
    },
    'cold_control': {
        'scope': (
            'Every cold Vulkan batch reached the real driver with effective '
            'FAIL_ON_PIPELINE_COMPILE_REQUIRED. Driver results and Metal object '
            'creation are reported separately.'),
        'batches': cold['batches'],
        'compile_required_batches': cold['compile_required_batches'],
        'driver_success_handles': cold['driver_success_handles'],
        'shaderless_successes': cold['shaderless_successes'],
        'cached_shader_successes': cold['cached_shader_successes'],
        'metal_object_returned': cold['metal_object_returned'],
        'first_draw_verified': False,
        'object_drawability_verified': False,
        'hardware_ready': False,
    },
    'negative_controls': {
        label: {
            'mode': negative_runs[label]['summary']['mode'],
            'recipes': negative_runs[label]['summary']['recipes'],
            'recipe_ok': negative_runs[label]['summary']['recipe_ok'],
            'uuid_mutations':
                negative_runs[label]['summary']['uuid_mutations'],
            'recipe_calls': negative_report['recipe_calls'],
            'driver_batches': negative_report['driver_batches'],
            'driver_compile_required_entries':
                negative_report['driver_compile_required_entries'],
            'shader_bearing_compile_required_entries':
                negative_report['shader_bearing_compile_required_entries'],
            'shaderless_successes': negative_report['shaderless_successes'],
            'shaderless_library_links':
                negative_report['shaderless_library_links'],
            'cache_hits': 0,
            'init_calls_ignored': negative_report['init_calls_ignored'],
            'metal_object_returned': negative_report['metal_object_returned'],
            'object_drawability_verified': False,
            'driver_compile_required_verified':
                negative_report['driver_compile_required_verified'],
        }
        for label, negative_report in negative_reports.items()
    },
    'compile_forbidden_hits_verified': True,
    'uuid_and_empty_refusals_verified': True,
    'negative_controls_preserved_artifacts': True,
    'host_macos_library_used': False,
    'shader_execution': False,
    'physical_device_tested': False,
    'hardware_ready': False,
}
serialized = json.dumps(report, indent=2) + '\n'
(work / 'checkpoint.json').write_text(serialized)
(OUT / 'checkpoint.json').write_text(serialized)
print('PASS: QuartzCore recipes=24/24 completed; every cache-bearing consumer entry '
      'hit its application cache with compilation forbidden; %d strict shaderless '
      'links are counted separately; cold driver batches, Metal object creation, '
      'and negative-control driver compile-required results are recorded separately; '
      'Metal object drawability and first draw were not verified'
      % consumer['shaderless_links'])
