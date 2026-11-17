#!/usr/bin/env python3
"""Compile the generic platform expert against the actual source-kernel ABI."""
import argparse
import json
import tomllib
from pathlib import Path
import subprocess
import sys

from kernel_target import ARCH, TRIPLE, check_object

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
catalog = {entry['id']: entry for entry in tomllib.loads((ROOT / 'components.toml').read_text())['component']
           if entry['kind'] == 'driver' and any(name.endswith('.cpp') for name in entry.get('source_files', []))}
aliases = {entry.get('legacy_name', identifier): identifier for identifier, entry in catalog.items()}
auxiliary = ['NativeLegacyDisplay', 'NativeGOPPresenter', 'NativeOfflineGPU', 'NativeOfflineAccelerator', 'NativeIOGPUProviderProbe']
parser.add_argument('component', choices=sorted(set(catalog) | set(aliases) | set(auxiliary)))
parser.add_argument('--negative-aes-kat', action='store_true', help='Isolated own-source wrong-answer refusal control; never changes original inputs')
parser.add_argument('--verified-root', action='store_true', help='Authenticate all actual disk reads against an independently signed immutable policy')
parser.add_argument('--verified-root-bundle', action='store_true', help='Select exactly one capacity-matching policy per device, then independently authenticate it')
parser.add_argument('--negative-verified-block', action='store_true', help='Negative own-buffer control: corrupt only the completed DMA snapshot')
args = parser.parse_args()
identifier = aliases.get(args.component, args.component)
if identifier == 'ShengUfs' and ARCH == 'arm64e':
    if any((args.negative_aes_kat, args.verified_root, args.verified_root_bundle, args.negative_verified_block)):
        parser.error('The source UFS runtime always uses the authenticated policy bundle of the OS profile')
    raise SystemExit(subprocess.call([sys.executable, ROOT / 'scripts/build-sheng-source-storage.py', '--tree', ROOT]))
metadata = catalog.get(identifier, {})
runtime = metadata.get('legacy_name', identifier)
args.component = runtime
if args.verified_root_bundle and not args.verified_root:
    parser.error('A policy bundle requires verified block mode')
if args.verified_root_bundle and args.negative_verified_block:
    parser.error('Select either bundle policy qualification or the isolated DMA snapshot fault')
if (args.verified_root or args.negative_verified_block) and args.component != 'NativeVirtioBlock':
    parser.error('Verified block mode requires the concrete read-only block transport')
if args.negative_verified_block and not args.verified_root:
    parser.error('The isolated input fault requires verified block mode')
component = args.component
if identifier == 'Adreno740' and (ROOT / 'kits/oneplus13t/PaganiGpu.cpp').is_file():
    subprocess.run([sys.executable, ROOT / 'scripts/build-pagani-gpu.py', '--tree', ROOT,
                    '--kernelcache', ROOT / '../../../../ipsw/21H461/kernelcache.decompressed'], check=True)
    raise SystemExit(0)
recipe = ROOT / 'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/iokit/DEVELOPMENT/IOPlatformExpert.cpo.json'
record = json.loads(recipe.read_text().strip().rstrip(','))
directory = 'GopFramebuffer' if runtime in ('NativeLegacyDisplay', 'NativeGOPPresenter') else 'AdrenoMetal' if runtime in ('NativeOfflineGPU', 'NativeOfflineAccelerator', 'NativeIOGPUProviderProbe') else identifier
filename = next((name for name in metadata.get('source_files', []) if name.endswith('.cpp')), runtime + '.cpp')
source = ROOT / 'drivers' / directory / filename
outputs = metadata.get('outputs', [])
archive_output = next((path for path in outputs if path.endswith('.a')), None)
aux_dirs = {'NativeLegacyDisplay':'native-legacy-display', 'NativeGOPPresenter':'native-gop-presenter',
            'NativeOfflineGPU':'native-offline-gpu', 'NativeOfflineAccelerator':'native-offline-accelerator',
            'NativeIOGPUProviderProbe':'native-original-provider'}
out = ROOT / Path(archive_output or outputs[0]).parent if outputs else ROOT / 'build' / aux_dirs[runtime]
if args.verified_root:
    out = ROOT / ('build/virtio-mmio-block-verified-fault' if args.negative_verified_block else 'build/virtio-mmio-block-verified')
    if args.verified_root_bundle:
        out = ROOT / 'build/virtio-mmio-block-verified-bundle'
out.mkdir(parents=True, exist_ok=True)
if args.negative_aes_kat:
    if component != 'NativeAES':
        parser.error('The own-source KAT control requires NativeAES')
    out = ROOT / 'build/software-aes-kat-negative'
    out.mkdir(parents=True, exist_ok=True)
    text = source.read_text()
    marker = '0x76,0x49,0xab,0xac'
    if text.count(marker) != 1:
        raise SystemExit('The exact own-source NIST answer was not uniquely identified')
    (out / 'OriginalAESABI.h').write_bytes(source.with_name('OriginalAESABI.h').read_bytes())
    source = out / 'NativeAES.cpp'
    source.write_text(text.replace(marker, '0x77,0x49,0xab,0xac'))
destination = out / (component + '.o')
arguments = record['arguments']
command = []
index = 0
while index < len(arguments):
    value = arguments[index]
    if value == record['file']:
        command.append(str(source))
    elif value == '-o':
        command.extend(['-o', str(destination)])
        index += 1
    elif value == '-Xclang' and arguments[index + 1].startswith('-W'):
        index += 1
    elif not value.startswith('-W'):
        command.append(value)
    index += 1
command.extend(['-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                '-Wno-c99-designator', '-Wno-reorder-init-list', '-Wno-deprecated-enum-enum-conversion',
                '-Wno-unnecessary-virtual-specifier', '-I', str(ROOT / '.cache/xnu-work/osfmk')])
if component in ('NativeVirtioBlock', 'NativeRamBlock', 'NativeUfs'):
    command += ['-I', str(ROOT / 'build/storage-abi/include')]
    if args.verified_root:
        command += ['-I', str(ROOT / 'kits/VerifiedRoot/include'), '-DNATIVE_VERIFIED_BLOCK=1']
    if args.negative_verified_block:
        command += ['-DNATIVE_VERIFIED_BLOCK_INJECT_FAULT=1']
    if args.verified_root_bundle:
        command += ['-DNATIVE_VERIFIED_BLOCK_MULTI_POLICY=1']
if component == 'NativeUfs':
    command += ['-I', str(ROOT / 'kits/VerifiedRoot/include'), '-I', str(ROOT / 'drivers/VirtioMmioBlock'),
                '-DNATIVE_VERIFIED_BLOCK=1', '-DNATIVE_VERIFIED_BLOCK_MULTI_POLICY=1', '-mgeneral-regs-only']
if component=='NativeOfflineGPU':
    command += ['-I',str(ROOT/'drivers/VirtioMmioConsole')]
if component in ('NativeLegacyDisplay','NativeHidInput'):
    command += ['-I', str(out)]
if component == 'NativeAdreno':
    command += ['-I', str(ROOT / 'kits/ArmSmmu4K'), '-I', str(ROOT / 'kits/AdrenoPM4'),
                '-I', str(ROOT / 'kits/sheng'), '-I', str(ROOT / 'kits/Sm8550Platform'), '-I', str(ROOT / 'kits/AdrenoFirmware'),
                '-I', str(ROOT / 'build/sheng-gmu'), '-I', str(ROOT / 'build/adreno740/include')]
if identifier in ('Adreno830', 'AdrenoNext'):
    kit_candidates = [ROOT / 'kits/Sm8750Platform',
                      ROOT / 'out/feeds/UniXNU-sm8750-common-9c94b128/kits/Sm8750Platform']
    compat_candidates = [ROOT / 'kits/ShengCompat',
                         ROOT.parent / 'UniXNU-Dodge_SM8750/modules/Pagani/kits/ShengCompat']
    kit_candidates += [parent / 'out/feeds/UniXNU-sm8750-common-9c94b128/kits/Sm8750Platform'
                       for parent in ROOT.parents]
    compat_candidates += [parent / 'UniXNU-Dodge_SM8750/modules/Pagani/kits/ShengCompat'
                          for parent in ROOT.parents]
    sm8750_kit = next((path for path in kit_candidates if path.is_dir()), None)
    sheng_compat = next((path for path in compat_candidates if path.is_dir()), None)
    if sm8750_kit is None or sheng_compat is None:
        raise SystemExit(f'{identifier}: Sm8750Platform kit or ShengCompat headers were not found')
    if identifier == 'AdrenoNext':
        arm_smmu_kit = ROOT / 'kits/ArmSmmu4K'
        if not arm_smmu_kit.is_dir():
            raise SystemExit('AdrenoNext: the ArmSmmu4K kit is missing')
        adreno_metal_candidates = [ROOT / 'drivers/AdrenoMetal']
        adreno_metal_candidates += [parent / 'drivers/AdrenoMetal' for parent in ROOT.parents]
        adreno_metal = next((path for path in adreno_metal_candidates if path.is_dir()), None)
        if adreno_metal is None:
            raise SystemExit('AdrenoNext: AdrenoMetal headers were not found')
        # The driver carries its matching SM8750 platform headers. Build the
        # vendored driver against the tree's current ArmSmmu4K kit.
        command += ['-I', str(ROOT / 'drivers' / directory),
                    '-I', str(ROOT / 'drivers' / directory / 'platform/sm8750'),
                    '-I', str(adreno_metal),
                    '-I', str(arm_smmu_kit),
                    # clang-24 asserts inside the XNU signature-type builtin
                    # while parsing IODeleteData. This compile-time-only shim
                    # keeps the runtime operation and warning policy unchanged.
                    '-D__builtin_xnu_types_compatible(a,b)=1']
    command += ['-I', str(ROOT / 'kits/AdrenoFirmware'),
                '-I', str(sm8750_kit), '-I', str(sheng_compat),
                '-I', str(ROOT / 'kits/AdrenoPM4'), '-I', str(ROOT / 'kits/ArmSmmu4K'),
                '-DNATIVE_ADRENO_HAVE_GMU_RULES=1',
                '-DNATIVE_ADRENO_HAVE_COMMAND_DB=1',
                '-DNATIVE_ADRENO_HAVE_GMU_VOTES=1',
                '-Wno-missing-designated-field-initializers']
archive = out / ('lib' + identifier + '.a')
if identifier == 'AdrenoNext':
    driver_dir = ROOT / 'drivers' / directory
    # The upstream driver classifies every source. NativeAdrenoClient and NativeAdrenoBoard are host-only
    # ABI/FDT test support.
    excluded = {
        'NativeAdrenoClient.cpp': 'host-only user-space ABI test client in the upstream source',
        'NativeAdrenoBoard.cpp': 'host-only FDT parser used by the upstream ABI tests',
    }
    # NativeAdrenoAccelerator.cpp is archived; the staging block below binds its three IOAcceleratorES imports
    # (metaclass, constructor and destructor) to absolute addresses from scripts/data/ioaccelerator-base-abi.json.
    # It also removes the __mod_init_func entry so the class initializer runs once kMetalPrepare loads the kext.
    sources = [driver_dir / name for name in (
        'NativeAdrenoAccelerator.cpp', 'NativeAdrenoPublish.cpp',
        'NativeAdrenoUserClient.cpp', 'NativeAdrenoSubmit.cpp',
        'NativeAdrenoRing.cpp', 'NativeAdrenoVm.cpp', 'NativeAdrenoMemory.cpp',
        'NativeAdrenoCpu.cpp', 'NativeAdrenoSmmuMemory.cpp', 'NativeAdrenoSmmuContext.cpp',
        'NativeAdrenoGmuContext.cpp', 'NativeAdrenoGpuContext.cpp', 'NativeAdrenoGmuMemory.cpp',
        'NativeAdrenoTimer.cpp', 'NativeAdrenoPwrup.cpp', 'NativeAdrenoFirmware.cpp',
        'NativeAdrenoFirmwareDefaults.cpp', 'NativeAdrenoSha256.cpp', 'NativeAdrenoKernelTime.cpp',
        'Adreno830Status.cpp', 'Adreno830Start.cpp', 'A830Smmu.cpp', 'A830GmuFirmware.cpp',
        'A830Gmu.cpp', 'A830Votes.cpp', 'A830Hfi.cpp', 'A830Cp.cpp', 'A830Zap.cpp',
        'A830Irq.cpp', 'A830Fence.cpp', 'A830SelfTest.cpp', 'A830PowerState.cpp',
        'A830Recovery.cpp', 'A830Dcvs.cpp', 'PaganiGpuBoard.cpp',
        'AdrenoNextFirmware.cpp',
    )]
    platform_sources = [driver_dir / 'platform/sm8750' / name for name in (
        # AdrenoNext owns the complete SM8750 power/SCM kit, including APIs used only by S2/S3.
        'Sm8750GpuPower.c', 'Sm8750Scm.c',
        'Sm8750Rpmh.c', 'Sm8750SocInfo.c',
        'Sm8750AossQmp.c', 'Sm8750Llcc.c', 'Sm8750Tsens.c',
    )]
    # The Adreno830 archive already contains the kit implementations. Both
    # archives are force_loaded into the kernel, so compiling ROOT/kits/*.c
    # here would define the same global symbols twice and fail the link.
    sources += platform_sources
    missing = [str(path.relative_to(ROOT)) for path in sources if not path.is_file()]
    if missing:
        raise SystemExit('AdrenoNext: missing sources: ' + ', '.join(missing))
    object_dir = out / 'obj'
    object_dir.mkdir(parents=True, exist_ok=True)
    (out / 'compile-errors.log').unlink(missing_ok=True)
    archive.unlink(missing_ok=True)

    def failure_summary(text):
        errors = [line.strip() for line in text.splitlines() if 'error:' in line]
        if errors:
            return errors[0]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[-1] if lines else 'compiler produced no diagnostic'

    def object_name(candidate):
        if candidate.is_relative_to(driver_dir):
            name = candidate.relative_to(driver_dir).as_posix().replace('/', '__')
        elif candidate.is_relative_to(ROOT / 'kits'):
            name = 'kit__' + candidate.relative_to(ROOT / 'kits').as_posix().replace('/', '__')
        else:
            raise SystemExit(f'AdrenoNext: source outside the driver and kit roots: {candidate}')
        return candidate.with_name(Path(name).stem).with_suffix('.o').name

    failures = []
    objects = []
    audit_log = out / 'compile.log'
    (out / 'compile-command.json').write_text(json.dumps(command, indent=2) + '\n')
    audit = subprocess.run(command, cwd=record['directory'], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True)
    audit_log.write_text(audit.stdout)
    if audit.returncode:
        failures.append(('AdrenoNext.o (audit)', audit_log, audit.stdout))
    else:
        try:
            check_object(destination.read_bytes(), component)
        except SystemExit as error:
            failures.append(('AdrenoNext.o (audit)', audit_log, str(error)))

    for candidate in sources:
        candidate_destination = object_dir / object_name(candidate)
        candidate_command = [
            str(candidate_destination) if value == str(destination)
            else str(candidate) if value == str(source)
            else '-xc' if candidate.suffix == '.c' and value == '-xc++'
            else '-std=gnu11' if candidate.suffix == '.c' and value == '-std=gnu++2b'
            else value
            for value in command
        ]
        command_log = object_dir / (candidate_destination.stem + '.compile-command.json')
        compile_log = object_dir / (candidate_destination.stem + '.compile.log')
        command_log.write_text(json.dumps(candidate_command, indent=2) + '\n')
        result = subprocess.run(candidate_command, cwd=record['directory'],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        compile_log.write_text(result.stdout)
        if result.returncode:
            failures.append((candidate.name, compile_log, result.stdout))
            continue
        try:
            check_object(candidate_destination.read_bytes(), candidate.name)
        except SystemExit as error:
            failures.append((candidate.name, compile_log, str(error)))
            continue
        objects.append(candidate_destination)
        print(f'PASS: AdrenoNext {candidate.name}: {TRIPLE} object audit passed')

    if failures:
        report = []
        for name, compile_log, output in failures:
            report.append(f'{name}: {failure_summary(output)} [log: {compile_log}]')
        text = '\n'.join(report)
        (out / 'compile-errors.log').write_text(text + '\n')
        raise SystemExit(text)

    if identifier == 'AdrenoNext':
        # The staged AGXAccelerator derives from the original IOAcceleratorES, whose metaclass, constructor and
        # destructor live in com.apple.iokit.IOGPUFamily and are not in the kernel link. Bind those three imports
        # to the audited original addresses (the recipe of scripts/build-adreno740-accelerator.py) and keep the
        # class initializer out of __mod_init_func: kMetalPrepare runs it once the kext is loaded.
        plan_path = ROOT / 'build/gpu-analysis/ioaccelerator-base-abi.json'
        if not plan_path.exists():
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_bytes((ROOT / 'scripts/data/ioaccelerator-base-abi.json').read_bytes())
        plan = json.loads(plan_path.read_text())
        if plan['operational_overrides'] != []:
            raise SystemExit('AdrenoNext: operational_overrides is %r; expected []' % plan['operational_overrides'])
        if plan['instance_bytes'] != 136:
            raise SystemExit('AdrenoNext: instance_bytes is %r; expected 136' % plan['instance_bytes'])
        if plan['virtual_entries'] != 168:
            raise SystemExit('AdrenoNext: virtual_entries is %r; expected 168' % plan['virtual_entries'])
        bindings = {
            '__ZN15IOAcceleratorESC2EPK11OSMetaClass': int(plan['protected_compatible_constructor_aliases'][0], 16),
            '__ZN15IOAcceleratorESD2Ev': int(plan['nondeleting_destructor'], 16),
            '__ZN15IOAcceleratorES10gMetaClassE': int(plan['metaclass'], 16),
        }
        accelerator = object_dir / 'NativeAdrenoAccelerator.o'
        undefined = set()
        for line in subprocess.check_output(['xcrun', 'nm', '-u', str(accelerator)], text=True).splitlines():
            fields = line.split()
            if fields and 'IOAcceleratorES' in fields[-1]:
                undefined.add(fields[-1])
        extra = undefined - set(bindings)
        if extra or not undefined:
            raise SystemExit('unexpected original-base imports in NativeAdrenoAccelerator.o: %s' % sorted(extra))
        assembly = object_dir / 'NativeAdrenoAccelerator.bindings.S'
        assembly.write_text(''.join('.globl ' + name + '\n.set ' + name + ', ' + hex(addr) + '\n'
                                    for name, addr in sorted(bindings.items()) if name in undefined))
        binding_object = object_dir / 'NativeAdrenoAccelerator.bindings.o'
        subprocess.run(['xcrun', 'clang', '-target', TRIPLE, '-c', str(assembly), '-o', str(binding_object)], check=True)
        late = object_dir / 'NativeAdrenoAccelerator.late.o'
        subprocess.run(['xcrun', 'llvm-objcopy', '--remove-section=__DATA,__mod_init_func',
                        '--globalize-symbol=__GLOBAL__sub_I_NativeAdrenoAccelerator.cpp', str(accelerator), str(late)],
                       check=True)
        objects[objects.index(accelerator)] = late
        objects.append(binding_object)

    archive.unlink(missing_ok=True)
    subprocess.run(['xcrun', 'ar', 'rcs', archive, *objects], check=True)
    (out / 'excluded.json').write_text(json.dumps(excluded, indent=2) + '\n')
    print(f'{identifier}: -Werror build and {TRIPLE} object audit passed '
          f'({len(objects)} objects archived)')
elif identifier == 'Adreno830':
    driver_dir = ROOT / 'drivers' / directory
    # NativeAdrenoAccelerator.cpp 依赖 dodge 没有且无人引用的 AdrenoMetal ABI；
    # NativeAdrenoA830Probe.cpp 读取 a830-probe=1，会与 Stage7Probe 的同名探针重复执行
    excluded = {'NativeAdrenoAccelerator.cpp', 'NativeAdrenoA830Probe.cpp'}
    sources = sorted(path for path in driver_dir.glob('*.cpp')
                     if 'Test' not in path.name and 'tests' not in path.parts and path.name not in excluded)
    sources += [
        driver_dir / 'ShengGmuSource.c',
        driver_dir / 'ShengGmuVotesSource.c',
        ROOT / 'kits/Sm8750Platform/ArmSmmu4KLive.c',
        ROOT / 'kits/Sm8750Platform/ArmSmmu4KMmio.c',
        ROOT / 'kits/AdrenoPM4/AdrenoPM4.c',
    ]
    if not sources:
        raise SystemExit('Adreno830: no kernel .cpp sources found')
    object_dir = out / 'obj'
    object_dir.mkdir(parents=True, exist_ok=True)
    (out / 'compile-errors.log').unlink(missing_ok=True)
    archive.unlink(missing_ok=True)

    def failure_summary(text):
        errors = [line.strip() for line in text.splitlines() if 'error:' in line]
        if errors:
            return errors[0]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[-1] if lines else 'compiler produced no diagnostic'

    failures = []
    objects = []
    audit_log = out / 'compile.log'
    (out / 'compile-command.json').write_text(json.dumps(command, indent=2) + '\n')
    audit = subprocess.run(command, cwd=record['directory'], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True)
    audit_log.write_text(audit.stdout)
    if audit.returncode:
        failures.append(('Adreno830.o (audit)', audit_log, audit.stdout))
    else:
        try:
            check_object(destination.read_bytes(), component)
        except SystemExit as error:
            failures.append(('Adreno830.o (audit)', audit_log, str(error)))

    for candidate in sources:
        candidate_destination = object_dir / (candidate.stem + '.o')
        candidate_command = [
            str(candidate_destination) if value == str(destination)
            else str(candidate) if value == str(source)
            else '-xc' if candidate.suffix == '.c' and value == '-xc++'
            else '-std=gnu11' if candidate.suffix == '.c' and value == '-std=gnu++2b'
            else value
            for value in command
        ]
        command_log = object_dir / (candidate.stem + '.compile-command.json')
        compile_log = object_dir / (candidate.stem + '.compile.log')
        command_log.write_text(json.dumps(candidate_command, indent=2) + '\n')
        result = subprocess.run(candidate_command, cwd=record['directory'],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        compile_log.write_text(result.stdout)
        if result.returncode:
            failures.append((candidate.name, compile_log, result.stdout))
            continue
        try:
            check_object(candidate_destination.read_bytes(), candidate.name)
        except SystemExit as error:
            failures.append((candidate.name, compile_log, str(error)))
            continue
        objects.append(candidate_destination)
        print(f'PASS: Adreno830 {candidate.name}: {TRIPLE} object audit passed')

    if failures:
        report = []
        for name, compile_log, output in failures:
            report.append(f'{name}: {failure_summary(output)} [log: {compile_log}]')
        text = '\n'.join(report)
        (out / 'compile-errors.log').write_text(text + '\n')
        raise SystemExit(text)

    archive.unlink(missing_ok=True)
    subprocess.run(['xcrun', 'ar', 'rcs', archive, *objects], check=True)
    print(f'{identifier}: -Werror build and {TRIPLE} object audit passed '
          f'({len(objects)} objects archived)')
else:
    (out / 'compile-command.json').write_text(json.dumps(command, indent=2) + '\n')
    result = subprocess.run(command, cwd=record['directory'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (out / 'compile.log').write_text(result.stdout)
    if result.returncode:
        raise SystemExit(result.stdout[-3500:])
    check_object(destination.read_bytes(), component)
    archive.unlink(missing_ok=True)
    subprocess.run(['xcrun', 'ar', 'rcs', archive, destination], check=True)
    print(f'{identifier}: -Werror build and {TRIPLE} object audit passed')
