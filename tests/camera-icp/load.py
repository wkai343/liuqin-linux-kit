#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only
"""Load a locally prepared, root-owned IPE module bundle for this exact boot.

No firmware or private tuning is distributed. Install a reviewed prepare.py
bundle under /usr/local/lib/liuqin-ipe and keep its manifest root-owned.
An incompatible kernel or changed boot target fails closed, without rebooting.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
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


def trusted(path):
    for p in (path, *path.parents):
        s = p.lstat()
        if stat.S_ISLNK(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
            raise ValueError('Expected root-owned, non-writable bundle path: ' + str(p))
    if not path.is_file():
        raise ValueError('Expected regular bundle file: ' + str(path))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(directory):
    manifest = directory / 'manifest.json'
    trusted(manifest)
    m = json.loads(manifest.read_text())
    if m['schema'] != 1 or m['kernel'] != os.uname().release:
        raise ValueError('Bundle does not match the running kernel')
    if m['target'] != Path('/etc/liuqin-boot-target').read_text():
        raise ValueError('Boot target changed; prepare a fresh bundle manifest')
    firmware = Path('/lib/firmware/liuqin-icp-test/CAMERA_ICP.mbn')
    if (m['firmware_sha256'] != '1dfc57b560935614742fe4dc90e03bc510b9b7bf67f9a01e13740bd71d4a4edc'
            or digest(firmware) != m['firmware_sha256']):
        raise ValueError('Unexpected ICP firmware')
    if set(m['modules']) != {'icp-overlay.ko', 'qcom-icp-test.ko'}:
        raise ValueError('Unexpected module bundle')
    for name, sha in m['modules'].items():
        p = directory / name
        trusted(p)
        if digest(p) != sha:
            raise ValueError('Module digest mismatch: ' + name)
        release = subprocess.check_output(['modinfo', '-F', 'vermagic', str(p)], text=True).split()[0]
        if release != m['kernel']:
            raise ValueError('Module vermagic mismatch: ' + name)
    root = Path('/sys/firmware/devicetree/base')
    if set(m['handles']) != set(RESOURCES):
        raise ValueError('Unexpected overlay resource set')
    for name, (node, compatible) in RESOURCES.items():
        p = root / node
        if (compatible.encode() not in (p / 'compatible').read_bytes().split(b'\0')
                or struct.unpack('>I', (p / 'phandle').read_bytes())[0] != m['handles'][name]):
            raise ValueError('Live overlay resource mismatch: ' + name)
    region = root / 'reserved-memory/memory@9f500000'
    if ((region / 'reg').read_bytes() != struct.pack('>4I', 0, 0x9f500000, 0, 0x800000)
            or not (region / 'no-map').exists()):
        raise ValueError('Unexpected ICP carveout')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('bundle', type=Path)
    p.add_argument('--verify-only', action='store_true')
    a = p.parse_args()
    if os.geteuid() != 0 or not a.bundle.is_absolute():
        p.error('Run as root with an absolute bundle path')
    verify(a.bundle)
    if a.verify_only:
        return
    if Path('/sys/bus/platform/devices/ac01000.icp').exists():
        raise ValueError('An ICP device already exists')
    for name in ('videobuf2-vmalloc', 'v4l2-mem2mem'):
        subprocess.run(['modprobe', name], check=True)
    subprocess.run(['insmod', str(a.bundle / 'icp-overlay.ko')], check=True)
    try:
        subprocess.run(['insmod', str(a.bundle / 'qcom-icp-test.ko')], check=True)
        nodes = Path('/sys/class/video4linux').glob('video*')
        if not any((n / 'name').read_text().strip() == 'qcom-ipe' for n in nodes):
            subprocess.run(['rmmod', 'qcom_icp_test'], check=True)
            raise ValueError('The IPE bridge did not probe successfully')
    except BaseException:
        subprocess.run(['rmmod', 'icp_overlay'], check=True)
        raise
    print('Loaded the verified local IPE bundle; sensor streaming starts on demand.')


if __name__ == '__main__':
    main()
