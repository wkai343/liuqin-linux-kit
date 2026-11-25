#!/usr/bin/env python3
"""Prepare isolated ICP transport test modules; never load modules or reboot.

Run `snapshot --output out/live.json` on the tablet first. Build only against
that running kernel's source/config/Module.symvers. The generated DT overlay
uses live numeric phandles because ABL may redirect the __symbols__ entries.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess

RESOURCES = {
    'CAMCC': ('soc@0/clock-controller@ade0000', 'qcom,sm8475-camcc'),
    'GCC': ('soc@0/clock-controller@100000', 'qcom,sm8475-gcc'),
    'GIC': ('soc@0/interrupt-controller@17100000', 'arm,gic-v3'),
    'SMMU': ('soc@0/iommu@15000000', 'qcom,sm8450-smmu-500'),
    'MMSS': ('soc@0/interconnect@1740000', 'qcom,sm8450-mmss-noc'),
    'MC': ('interconnect-1', 'qcom,sm8450-mc-virt'),
    'GEM': ('soc@0/interconnect@19100000', 'qcom,sm8450-gem-noc'),
    'CONFIG': ('soc@0/interconnect@1500000', 'qcom,sm8450-config-noc'),
}
FIRMWARE_SHA256 = '1dfc57b560935614742fe4dc90e03bc510b9b7bf67f9a01e13740bd71d4a4edc'
HDR_STILL_FIXTURE = {
    'fusion_two_off_iq.bin': (6180, '09f487d4cf037755f710b59dbbc4978e42c3dc4974446bbb032e5c0e1692e598'),
    'fusion_off_frame.bin': (39208, 'a61b7e41b057c1a3c3157328babfe8b56dff88ce186782f90cadf7da7c8d38d3'),
    'fusion_off_iq.bin': (6180, '8b11ac4f7a9b0a4afb5b898622a54c550d0633e450ad701324d4349a01f99582'),
    'fusion_off_stripes.bin': (69220, '96c1e807b12ec9486c0a7e2995990404eeca4af5e926e94a5a32f74ee3732faa'),
    'post_frame.bin': (39208, '53d93c4a2868054f2edb14d784d16d15900ea92d19f93f06d097319e993faa71'),
    'post_iq.bin': (6180, '86e88aac9f9cdbf53300ef23e2739860e28375c2bb20d8e69203a9179fbb7f6a'),
    'post_stripes.bin': (69220, '18a4434fb28378d33024fc6fc9af5b7a9e8ac73f0f92d15b2ce58ecf88ef53b8'),
    'pre_dmi.bin': (48, 'ba264acdd1a716b0de822fe576a0de36c6e70bf577bf08071528458387c44dce'),
    'pre_frame.bin': (39208, '492484aae8a23380794e4e658eb500a11502737c50428dd0cd3b18f57c7f40e2'),
    'pre_iq.bin': (6180, '0cbe553af31ba857dcc6ece46cfb6c09e59176dff8049583d9470ce28a3b0fd6'),
    'pre_lut.bin': (2304, '96093c56de6fdb276f4ae2d37b2123191786c920d99707f1c9581c958a09812e'),
    'pre_registers.bin': (1152, '626c9d20e31e1bf2bdec59ebce2655cf6cac6b4add99cfb28d481c966be5d4d4'),
    'pre_stripes.bin': (64872, '69595898bcb1875ad525b50ffbbaa03c48412eb0a05eb19c3db11db7497ac49b'),
}
# Fixed full + DS4 layout, three exposures. Only the sensor register payload
# changes with the quantized exposure ratios used for these diagnostics.
HDR_MAC_FIXTURE = {
    'mac_frame.bin': (39208, 'a42332b5c58a421e02acd8623c313cf75444f1d72d73b3557cf2abb6028cee37'),
    'p010_iq.bin': (6180, '1bd27b331742102099be7f8c7b9d55aabfc8d7e6fec0da5628133fc99257c325'),
    'p010_stripes.bin': (69220, '3e05cdecff7fe2366d7a869a2ba06bc6819bab884100d66274925bcfd5615de7'),
    'mac_iq.bin': (6180, 'e473c7782a50ec058fd7098ec8c4fc61917618b9d37e4263e4a99c6fc6565a02'),
    'mac_stripes.bin': (69220, 'c21bb79301a81567de334b0d4558e530b5fe26885c867489b7fd86ad4153a365'),
}
MAC_REGISTER_SHA = {
    'front': '6aac66018d5916a023adb6e7894c5f6cc1a6d77bba86216cb1c5acb9afbe8753',
    'rear': '2774f0fcfa565cf75f8f65ab96999779f28fe2a7671edc5ecd985986fc1b34c7',
}

FRAME_FIXTURE = {
    'ipe_frame.bin': (39208, '3791832508dfebe07f0af83226ee80ea73e02e1251fbef6da1ced5c0670f1373'),
    'ipe_iq.bin': (6180, '86e88aac9f9cdbf53300ef23e2739860e28375c2bb20d8e69203a9179fbb7f6a'),
    'ipe_stripes.bin': (64872, '70d63dc16aab8f52a21739e8579fef129f72137a4407ecd44179e0b059d1e84c'),
}

VIDEO_FIXTURE = {'video_frame.bin': [39208, 'efc5d3cac70d36c2b57208594a1866f7d856b1dfe4f22473cb3c60f770651e5c'], 'video_iq_640_480.bin': [6180, '86e88aac9f9cdbf53300ef23e2739860e28375c2bb20d8e69203a9179fbb7f6a'], 'video_stripes_640_480.bin': [64872, '70d63dc16aab8f52a21739e8579fef129f72137a4407ecd44179e0b059d1e84c'], 'video_iq_2592_1472.bin': [6180, '9b73ac156cb554c98edb373d5cc5b2181e4c104468699166fbdc32b62bda3c07'], 'video_stripes_2592_1472.bin': [104004, 'b47cad915a9502528ce9b8e7245de4e80674bf3ee6f81fe0970845007a45aa3e'], 'video_iq_2592_1952.bin': [6180, '9d372c529a4b2df03383edcd3a6e4383d01ada42dea8178ed52aa2cd318ef409'], 'video_stripes_2592_1952.bin': [104004, '5c6bb4477b20f3bd0c47997e6d65fb5863aa786606aa84c1d78965aa132162ca'], 'video_iq_4080_3060.bin': [6180, '165634727b95b7db385351105789c00439b6b0511e1eb166f4bf6df8833c06eb'], 'video_stripes_4080_3060.bin': [138788, 'c892ef920b0c8f84b08dfc952b5a1268377635e29ec2c14fd56da27a01464d63']}
VIDEO_HEADER_SHA256 = 'e7417bdc2328f5de6c80ddc0157b2201c8be967f4a45632041e40c895565748c'

# First/reference-frame TF21 programs from the pinned shipping front/rear tuning.
TEMPORAL_FIXTURE = {'p640_480_iq_0.bin': [6180, '71aa76671a12f844b7426d1427a845e31be66c5c91b809183a3bb16278a52c6b'],
 'p640_480_stripes_0.bin': [64872, 'bdf569900d2629b83a827176914d67a8a5a17043ff35b97a4e0f929abccf483b'],
 'p640_480_cdm_0.bin': [1680, '869f8a3140dd0438ae3199cb1b15ac9e1ec6abd0545d054d6ee69bebcc26027a'],
 'p640_480_iq_1.bin': [6180, '23cc225b442a53e04023f8a8c8d58ab2c4723ddd80f4ec9c25e9f0ee611feffc'],
 'p640_480_stripes_1.bin': [64872, 'bdf569900d2629b83a827176914d67a8a5a17043ff35b97a4e0f929abccf483b'],
 'p640_480_cdm_1.bin': [1680, '8819b17267d571a44472621325ff8724fe626df44438a788b03b010d0f3e3bf8'],
 'p2592_1472_iq_0.bin': [6180, '5d5e11d64951d38f42b11bd1077d1969b9152937b5acee11281de30b82dcd2e0'],
 'p2592_1472_stripes_0.bin': [104004, 'afd22a05ac55c15093f7c7314deada7ee5626ca9c6ec8d430cb23782203cbb0a'],
 'p2592_1472_cdm_0.bin': [1680, '869f8a3140dd0438ae3199cb1b15ac9e1ec6abd0545d054d6ee69bebcc26027a'],
 'p2592_1472_iq_1.bin': [6180, '9b442bca42650ea80366ccdb68db5ad145fa34a6c54703947b90166ce81d7421'],
 'p2592_1472_stripes_1.bin': [104004, 'afd22a05ac55c15093f7c7314deada7ee5626ca9c6ec8d430cb23782203cbb0a'],
 'p2592_1472_cdm_1.bin': [1680, '8819b17267d571a44472621325ff8724fe626df44438a788b03b010d0f3e3bf8'],
 'p2592_1952_iq_0.bin': [6180, '7cc44f4ea70e660dd5289dfb1881f2e50823dffac5b89f6438b49637f8663910'],
 'p2592_1952_stripes_0.bin': [104004, '7585366643621c1199781f53d472aaec4d7b834a2ab82fb455050715c3a6aefa'],
 'p2592_1952_cdm_0.bin': [1680, '869f8a3140dd0438ae3199cb1b15ac9e1ec6abd0545d054d6ee69bebcc26027a'],
 'p2592_1952_iq_1.bin': [6180, '8c855b6a158fd41773ad0b637bbb172b3a3b9f831e1302d3ce018b241a3906e9'],
 'p2592_1952_stripes_1.bin': [104004, '7585366643621c1199781f53d472aaec4d7b834a2ab82fb455050715c3a6aefa'],
 'p2592_1952_cdm_1.bin': [1680, '8819b17267d571a44472621325ff8724fe626df44438a788b03b010d0f3e3bf8'],
 'p4080_3060_iq_0.bin': [6180, '6e5840fd2d38191d838028f1d5b347aa2f61c9ed8ae51eca7b7ffd123d36b1b3'],
 'p4080_3060_stripes_0.bin': [138788, '2b5a5c8b520edb6df2a59eae9e84c280e03218a17211f7c5c0ea658e40a920a3'],
 'p4080_3060_cdm_0.bin': [1680, '869f8a3140dd0438ae3199cb1b15ac9e1ec6abd0545d054d6ee69bebcc26027a'],
 'p4080_3060_iq_1.bin': [6180, '3fc4215d768517a269b0397e3a102752f9657b12b9963cdd2097bd31f8ece0d4'],
 'p4080_3060_stripes_1.bin': [138788, '2b5a5c8b520edb6df2a59eae9e84c280e03218a17211f7c5c0ea658e40a920a3'],
 'p4080_3060_cdm_1.bin': [1680, '8819b17267d571a44472621325ff8724fe626df44438a788b03b010d0f3e3bf8'],
 'tf_frame_0.bin': [39208, '01d2e7b0000d06ef3795e036398990f89d56e978d250005f84db6de2cc39104e'],
 'tf_frame_1.bin': [39208, '01d2e7b0000d06ef3795e036398990f89d56e978d250005f84db6de2cc39104e'],
 'video-fixture.h': [1988946, 'ab875a3179376c978765b116c695496908f5f377db904d28d9239aac0aa376f9']}


WIDE_TEMPORAL_FIXTURE = {**TEMPORAL_FIXTURE,
    'p3840_2160_iq_0.bin': [6180, 'f48ea229af29a86d456cc6f717f1535b7620e9399454bd5068fb116f2db0342e'],
    'p3840_2160_stripes_0.bin': [134440, '7f069d71f8a22dbdad6a5c86db3bd6af72e1b453d1edb43b0760c9ef59f45930'],
    'p3840_2160_cdm_0.bin': [1680, '869f8a3140dd0438ae3199cb1b15ac9e1ec6abd0545d054d6ee69bebcc26027a'],
    'p3840_2160_iq_1.bin': [6180, 'bc7546e534d0d97b0034c778433c85909b12bd508282f95724d08d0729cd49e8'],
    'p3840_2160_stripes_1.bin': [134440, '7f069d71f8a22dbdad6a5c86db3bd6af72e1b453d1edb43b0760c9ef59f45930'],
    'p3840_2160_cdm_1.bin': [1680, '8819b17267d571a44472621325ff8724fe626df44438a788b03b010d0f3e3bf8'],
    'p4080_2296_iq_0.bin': [6180, 'bd746cd4774985f027f58043a146069c3d4513d31958b84b4150fccdb08c653f'],
    'p4080_2296_stripes_0.bin': [138788, 'fb6e5bc86f28d237dc888239f233dd23d337f7d4b00cd5af5537a5a66ed69397'],
    'p4080_2296_cdm_0.bin': [1680, '869f8a3140dd0438ae3199cb1b15ac9e1ec6abd0545d054d6ee69bebcc26027a'],
    'p4080_2296_iq_1.bin': [6180, 'bb3801ab0d31c39678e6b578b7bc898ebaa7fb7e25fadeef116372c1f4ca3a07'],
    'p4080_2296_stripes_1.bin': [138788, 'fb6e5bc86f28d237dc888239f233dd23d337f7d4b00cd5af5537a5a66ed69397'],
    'p4080_2296_cdm_1.bin': [1680, '8819b17267d571a44472621325ff8724fe626df44438a788b03b010d0f3e3bf8'],
    'video-fixture.h': [3183012, 'db356d6f85473f37ec0b0da6209c93ddc1b1436280d50063a9cef890302700a2'],
}


def fixture_header(directory, files=FRAME_FIXTURE):
    # Only the reviewed fixed-address, 640x480 fixture is accepted. A manifest
    # supplied alongside an arbitrary command blob is not sufficient authority.
    header = '/* Private fixed synthetic IPE fixture. */\n'
    for filename, (size, sha) in files.items():
        path = directory / filename
        data = path.read_bytes()
        if len(data) != size or digest(path) != sha:
            raise ValueError('Unsupported frame fixture: ' + filename)
        header += 'static const unsigned char ' + path.stem + '[] = {\n'
        header += '\n'.join(','.join('0x%02x' % v for v in data[i:i+24]) + ','
                            for i in range(0, len(data), 24)) + '\n};\n'
    return header


NATIVE_SHA = {'front': '438600f85d898b2f98502187f8e0e0bccaab72a5954768551ac5a51b20482465',
              'rear': 'dc2c3b4d8cdf59e6a71c3deb8e76bd0ecec3f8c3d631abe1839c58d3fb284e19'}


WIDE_PROFILES = {
    'rear_wide': ((4080, 2296), '1ef34f5259e7b82d2c5b8cc143f4cb9dd775fb03e090ede20996356117177189', '2774f0fcfa565cf75f8f65ab96999779f28fe2a7671edc5ecd985986fc1b34c7'),
    'rear_uhd': ((3840, 2160), '949e8ba916b5bbb46626558ed0ea31d92fc74af817d19e619ea5a73667caf035', '8d7abb29a39b4865e300daaac5bb931f633612f78eea32ecc51292ac80fbcb53'),
}


def native_header(directory, sensor, vga):
    # Frames, DMI headers, register commands and LUTs have identical bytes at
    # both sizes. Only the descriptors, IQ and relocated striping differ.
    for key in ('pre_frame', 'mac_frame', 'post_frame', 'pre_lut', 'pre_dmi', 'pre_registers'):
        name = key + '.bin'
        if (directory / name).read_bytes() != (vga / name).read_bytes():
            raise ValueError('Unexpected native command: ' + name)
    fields, arrays = [], []
    checksum = hashlib.sha256()
    for stage in ('pre', 'mac', 'p010'):
        for part in ('cfg', 'iq', 'stripes'):
            key = stage + '_' + part
            data = (directory / (key + '.bin')).read_bytes()
            checksum.update(struct.pack('<I', len(data)))
            checksum.update(data)
            name = sensor + '_' + key
            body = (','.join('0x%02x' % v for v in data[i:i+24]) + ',' for i in range(0, len(data), 24))
            arrays.append('static const u8 ' + name + '[] = {\n' + '\n'.join(body) + '\n};')
            fields.append('.' + key + '=' + name)
            if part == 'stripes':
                fields.append('.' + stage + '_length=sizeof(' + name + ')')
    if sensor in WIDE_PROFILES:
        (width, height), expected, register_sha = WIDE_PROFILES[sensor]
        symbol = sensor + '_registers'
        arrays.append(fixture_header(directory, {'mac_registers.bin': (256, register_sha)}).replace(
            ' mac_registers[]', ' ' + symbol + '[]'))
        fields.append('.mac_registers=' + symbol)
    else:
        width, height = (2592, 1952) if sensor == 'front' else (4080, 3060)
        expected = NATIVE_SHA[sensor]
        if (directory / 'mac_registers.bin').read_bytes() != (vga / 'mac_registers.bin').read_bytes():
            raise ValueError('Unexpected native MAC registers: ' + sensor)
    if checksum.hexdigest() != expected:
        raise ValueError('Unexpected native HDR profile: ' + sensor)
    fields.extend([f'.width={width}', f'.height={height}'])
    arrays.append('static const struct snapshot_native ' + sensor + '_native = {' + ', '.join(fields) + '};')
    return '\n'.join(arrays) + '\n'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(output):
    root = Path('/sys/firmware/devicetree/base')
    handles = {}
    for key, (node, compatible) in RESOURCES.items():
        path = root / node
        if compatible.encode() not in (path / 'compatible').read_bytes().split(b'\0'):
            raise ValueError('Unexpected resource: ' + node)
        handles[key], = struct.unpack('>I', (path / 'phandle').read_bytes())
    region = root / 'reserved-memory/memory@9f500000'
    if (region / 'reg').read_bytes() != struct.pack('>4I', 0, 0x9f500000, 0, 0x800000):
        raise ValueError('Unexpected firmware carveout')
    if not (region / 'no-map').exists():
        raise ValueError('Firmware carveout must be reserved no-map')
    if (root / 'soc@0/icp@ac01000').exists():
        raise ValueError('An ICP device already exists; do not overlay it')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dict(schema=1, kernel=os.uname().release,
        target=Path('/etc/liuqin-boot-target').read_text(), handles=handles), indent=2) + '\n')


def build(args):
    if args.hdr_wide_fixtures and not args.hdr_native_fixtures:
        raise ValueError('wide HDR requires the resident native profiles')
    if args.hdr_native_fixtures and not args.hdr_mac_fixtures:
        raise ValueError('native HDR requires the verified MAC programs')
    if args.hdr_mac_fixtures and not args.hdr_still_fixture:
        raise ValueError('the MAC frame requires the matching still-image fixtures')
    frame = fixture_header(args.frame_fixture) if args.frame_fixture else None
    video = None
    if args.video_fixture:
        for filename, (size, sha) in VIDEO_FIXTURE.items():
            path = args.video_fixture / filename
            if path.stat().st_size != size or digest(path) != sha:
                raise ValueError('Unsupported video fixture: ' + filename)
        path = args.video_fixture / 'video-fixture.h'
        if digest(path) != VIDEO_HEADER_SHA256:
            raise ValueError('Unsupported video fixture header')
        video = path.read_bytes()
    temporal_files = TEMPORAL_FIXTURE
    if args.temporal_fixture:
        if digest(args.temporal_fixture / 'video-fixture.h') == WIDE_TEMPORAL_FIXTURE['video-fixture.h'][1]:
            temporal_files = WIDE_TEMPORAL_FIXTURE
        if args.video_fixture:
            raise ValueError('Select identity or temporal video, not both')
        for filename, (size, sha) in temporal_files.items():
            path = args.temporal_fixture / filename
            if path.stat().st_size != size or digest(path) != sha:
                raise ValueError('Unsupported temporal fixture: ' + filename)
        video = (args.temporal_fixture / 'video-fixture.h').read_bytes()
    live = json.loads(args.snapshot.read_text())
    if live['schema'] != 1 or set(live['handles']) != set(RESOURCES):
        raise ValueError('Invalid resource snapshot')
    if digest(args.firmware) != FIRMWARE_SHA256:
        raise ValueError('Only the verified shipping CAMERA_ICP.mbn is supported')
    source, kernel = args.kernel_source.resolve(), args.kernel_build.resolve()
    if (kernel / 'include/config/kernel.release').read_text().strip() != live['kernel']:
        raise ValueError('Kernel build does not match the running kernel snapshot')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    here = Path(__file__).resolve().parent
    for name in ('qcom-icp-test.c', 'icp-overlay.c', 'Makefile'):
        shutil.copyfile(here / name, out / name)
    if args.hdr_config_probe or args.hdr_still_fixture:
        for name in ('hdr-config.h', 'hdr-dma.h', 'frame-layout.h'):
            shutil.copyfile(here / name, out / name)
        with (out / 'Makefile').open('a') as f:
            f.write('\nccflags-y += -DICP_HDR_CONFIG_PROBE\n')
    if args.hdr_still_fixture:
        (out / 'snapshot-fixture.h').write_text(fixture_header(args.hdr_still_fixture, HDR_STILL_FIXTURE))
        shutil.copyfile(here / 'hdr-snapshot-frame.h', out / 'hdr-snapshot-frame.h')
        with (out / 'Makefile').open('a') as f:
            f.write('\nccflags-y += -DICP_HDR_STILL_FRAME\n')
    if args.hdr_mac_fixtures:
        front, rear = args.hdr_mac_fixtures
        header = fixture_header(front, HDR_MAC_FIXTURE)
        if fixture_header(rear, HDR_MAC_FIXTURE) != header:
            raise ValueError('front and rear must share the verified frame layout')
        for sensor, directory in (('front', front), ('rear', rear)):
            registers = fixture_header(directory, {'mac_registers.bin': (256, MAC_REGISTER_SHA[sensor])})
            header += registers.replace(' mac_registers[]', ' mac_' + sensor + '_registers[]')
        (out / 'mac-fixture.h').write_text(header)
        with (out / 'Makefile').open('a') as f:
            f.write('\nccflags-y += -DICP_HDR_MAC_FRAME\n')
        live['hdr_mac_fixture'] = HDR_MAC_FIXTURE
        live['hdr_mac_registers'] = MAC_REGISTER_SHA
    if args.hdr_native_fixtures:
        header = ''.join(native_header(directory, sensor, vga) for directory, sensor, vga in
                         zip(args.hdr_native_fixtures, ('front', 'rear'), args.hdr_mac_fixtures))
        if args.hdr_wide_fixtures:
            for directory, name in zip(args.hdr_wide_fixtures, WIDE_PROFILES, strict=True):
                header += native_header(directory, name, args.hdr_mac_fixtures[1])
            with (out / 'Makefile').open('a') as f:
                f.write('\nccflags-y += -DICP_HDR_WIDE\n')
            live['hdr_wide_profiles'] = WIDE_PROFILES
        (out / 'native-fixture.h').write_text(header)
        with (out / 'Makefile').open('a') as f:
            f.write('\nccflags-y += -DICP_HDR_NATIVE\n')
        live['hdr_native_sha256'] = NATIVE_SHA
    if frame is not None:
        (out / 'frame-fixture.h').write_text(frame)
        with (out / 'Makefile').open('a') as f:
            f.write('\nccflags-y += -DICP_FRAME_FIXTURE\n')
    if video is not None:
        shutil.copyfile(here / 'ipe-video.h', out / 'ipe-video.h')
        shutil.copyfile(here / 'frame-layout.h', out / 'frame-layout.h')
        (out / 'video-fixture.h').write_bytes(video)
        with (out / 'Makefile').open('a') as f:
            f.write('\nccflags-y += -DICP_VIDEO_FIXTURE\n')
            if args.temporal_fixture:
                f.write('ccflags-y += -DICP_TEMPORAL_FIXTURE\n')
    dts = (here / 'icp-test.dts.in').read_text()
    for key, value in live['handles'].items():
        if not isinstance(value, int) or not 0 < value < 0xffffffff:
            raise ValueError('Invalid phandle')
        dts = dts.replace('@' + key + '@', str(value))
    (out / 'icp-test.dts').write_text(dts)
    cpp = subprocess.check_output(['cpp', '-nostdinc', '-undef', '-x', 'assembler-with-cpp',
        '-I' + str(source / 'include'), str(out / 'icp-test.dts')])
    (out / 'icp-test.pp.dts').write_bytes(cpp)
    subprocess.run([str(kernel / 'scripts/dtc/dtc'), '-@', '-I', 'dts', '-O', 'dtb',
        '-o', str(out / 'icp-test.dtbo'), str(out / 'icp-test.pp.dts')], check=True)
    blob = (out / 'icp-test.dtbo').read_bytes()
    (out / 'overlay-blob.h').write_text('static unsigned char overlay_blob[] = {' +
        ','.join(map(str, blob)) + '};\n')
    subprocess.run(['make', '-C', str(source), 'O=' + str(kernel), 'M=' + str(out),
        'ARCH=arm64', 'CROSS_COMPILE=' + args.cross_compile, '-j' + str(args.jobs), 'modules'], check=True)
    live['firmware_sha256'] = FIRMWARE_SHA256
    live['frame_fixture'] = FRAME_FIXTURE if frame is not None else None
    live['video_fixture'] = (temporal_files if args.temporal_fixture else VIDEO_FIXTURE) if video is not None else None
    if args.hdr_still_fixture:
        live['hdr_still_fixture'] = HDR_STILL_FIXTURE
    live['modules'] = {p.name: digest(p) for p in out.glob('*.ko')}
    (out / 'manifest.json').write_text(json.dumps(live, indent=2) + '\n')
    print('Prepared test modules only. No installation, clock replacement, or boot changes.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    snap = sub.add_parser('snapshot')
    snap.add_argument('--output', type=Path, required=True)
    compile_ = sub.add_parser('build')
    for name in ('snapshot', 'firmware', 'kernel-source', 'kernel-build', 'output'):
        compile_.add_argument('--' + name, type=Path, required=True)
    compile_.add_argument('--hdr-mac-fixtures', metavar=('FRONT', 'REAR'), type=Path, nargs=2,
        help='checked per-sensor programs for three-exposure stills and P010 output')
    compile_.add_argument('--hdr-still-fixture', type=Path, help='pinned private 640x480 programs for the still-image selftests 14–20')
    compile_.add_argument('--hdr-wide-fixtures', nargs=2, type=Path, metavar=('WIDE', 'UHD'),
                          help='pinned rear 4080x2296 and 3840x2160 HDR profiles')
    compile_.add_argument('--hdr-native-fixtures', nargs=2, type=Path, metavar=('FRONT', 'REAR'),
                          help='pinned full-resolution three-exposure snapshot profiles')
    compile_.add_argument('--hdr-config-probe', action='store_true', help='build config-only snapshot selftests 9–13')
    compile_.add_argument('--cross-compile', default='aarch64-linux-gnu-')
    compile_.add_argument('--frame-fixture', type=Path,
        help='Private output of make-frame-fixture.py; enables fixed frame tests')
    compile_.add_argument('--video-fixture', type=Path, help='Pinned private V4L2 video profiles')
    compile_.add_argument('--temporal-fixture', type=Path, help='Pinned private TF21 first/reference frame profiles')
    compile_.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    if args.command == 'snapshot':
        snapshot(args.output)
    else:
        if not 1 <= args.jobs <= 128:
            parser.error('jobs must be 1..128')
        build(args)


if __name__ == '__main__':
    main()
