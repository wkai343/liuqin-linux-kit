# SPDX-License-Identifier: MIT
"""The stock liuqin protection program and tuning, without factory calibration."""
from pathlib import Path
import hashlib
import json
import shutil

# OS2.0.6.0.VMYCNXM /vendor/firmware. Mainline wm_adsp uses lowercase
# cs35l41-dsp1-spk-prot-<component>.bin instead of the vendor's <TL>-... name.
FILES = (
    ('cs35l41-dsp1-spk-prot.wmfw', 'cs35l41-dsp1-spk-prot.wmfw',
     '1bb7ea70dd6b8e01ca5563cb051c025e1c8f361c57c28780ac559c0fc6b55f87'),
    ('TL-cs35l41-dsp1-spk-prot.bin', 'cs35l41-dsp1-spk-prot-tl.bin',
     '082721e8d36e4afa5892f056297b887962ffdda308d7c4752511a176cc85d84c'),
    ('TR-cs35l41-dsp1-spk-prot.bin', 'cs35l41-dsp1-spk-prot-tr.bin',
     '121b0ff4e6bd14cadc852ae1588ec8f309da883b49b95eeda973372244546953'),
    ('BL-cs35l41-dsp1-spk-prot.bin', 'cs35l41-dsp1-spk-prot-bl.bin',
     '9f879b9298dd6460e9e938d2dde271e750778a83dee398176292eb1fbd7e7418'),
    ('BR-cs35l41-dsp1-spk-prot.bin', 'cs35l41-dsp1-spk-prot-br.bin',
     '426417eb23849d539699a6a8fafd0ce7f4733a5841951f76ada5450f4bb7cad6'),
)

# The M81 overlay assigns both files to each position. OS2 ships identical
# deltas within a use case, but the mixer selects them by amplifier name.
DELTAS = {
    'music': 'bf098152d6470fb92575880c073e18cecb5d8fb79b5b39af23206a805ac34ad4',
    'voice': '82653f70617c64df0f84fd6ba01d6665c17c0175102daa3b007388ab737071e7',
}
FILES += tuple((f'{amp}-{mode}.txt', f'{amp}-{mode}.txt', digest)
               for amp in ('TL', 'TR', 'BL', 'BR') for mode, digest in DELTAS.items())


def checked(path, expected):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError('原厂扬声器保护固件校验失败：'+str(path))
    return path


def extract(vendor, output):
    """Read the program, positional tuning and use-case deltas from stock vendor."""
    vendor, output = Path(vendor), Path(output)
    if output.exists(): raise ValueError('扬声器固件输出已存在。')
    sources = [(checked(vendor/'firmware'/name, digest), target) for name, target, digest in FILES]
    directory = output/'usr/lib/firmware/cirrus'
    directory.mkdir(parents=True)
    for source, target in sources:
        shutil.copyfile(source, directory/target); (directory/target).chmod(0o644)


def install(source, root):
    """Install the pinned generic files, keeping each tablet's calibration separate."""
    source, root = Path(source), Path(root)
    files = [(checked(source/'usr/lib/firmware/cirrus'/target, digest), target)
             for _, target, digest in FILES]
    firmware = root/'usr/lib/firmware/cirrus'
    tuning = root/'usr/share/liuqin/speaker-tuning'
    manifest = tuning/'sha256.json'
    destinations = [(src, (tuning if name.endswith('.txt') else firmware)/name)
                    for src, name in files]
    for destination in [path for _, path in destinations] + [manifest]:
        if destination.exists() or destination.is_symlink():
            raise ValueError('扬声器固件目标已有文件：'+str(destination))
    for directory in (firmware, tuning):
        directory.mkdir(parents=True, exist_ok=True)
    for src, destination in destinations:
        shutil.copyfile(src, destination)
        destination.chmod(0o644)
    deltas = {target: digest for _, target, digest in FILES if target.endswith('.txt')}
    manifest.write_text(json.dumps(deltas, indent=2, sort_keys=True) + '\n')
    manifest.chmod(0o644)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    install(args.source, args.root)
