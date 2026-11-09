# SPDX-License-Identifier: MIT
"""Export one completed build's boot/rootfs pair, never a cache-wide glob."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'shared'))
from liuqin_kernel import parse_contract


def recorded_hash(path):
    stage = path.parent.parent
    receipt = stage / 'build-receipt.json'
    if (stage.name.startswith('.') or receipt.is_symlink() or not receipt.is_file()
            or path.is_symlink() or not path.is_file() or path.stat().st_size == 0
            or not path.resolve().is_relative_to(stage.resolve())):
        raise ValueError('导出产物缺失、未完成或路径无效：' + str(path))
    record = json.loads(receipt.read_text())
    artifacts = record.get('artifacts') if isinstance(record, dict) else None
    expected = artifacts.get(path.relative_to(stage).as_posix()) if isinstance(artifacts, dict) else None
    if (not isinstance(expected, dict) or set(expected) != {'file'}
            or not isinstance(expected['file'], str) or not re.fullmatch('[0-9a-f]{64}', expected['file'])):
        raise ValueError('导出产物缺少完成校验值：' + str(path))
    return expected['file']


def export_images(boot, rootfs, output):
    """Copy only the current pair and its contract; publish after receipt checks."""
    boot, rootfs, output = Path(boot), Path(rootfs), Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('导出目录已存在，未覆盖：' + str(output))
    contracts = []
    for source in (boot, rootfs):
        contract = source.parent / 'native-root.contract'
        expected = recorded_hash(contract)
        content = contract.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError('导出内核标识与完成记录不一致：' + str(contract))
        parse_contract(content)
        contracts.append(content)
    if contracts[0] != contracts[1]:
        raise ValueError('导出 boot/rootfs 的内核和模块不配套。')
    files = {'boot.img': (boot, recorded_hash(boot)),
             'rootfs.tar.gz': (rootfs, recorded_hash(rootfs))}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.image-export-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'images'
        staged.mkdir()
        hashes = {}
        for name, (source, expected) in files.items():
            target = staged / name
            shutil.copyfile(source, target)
            with target.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != expected:
                raise ValueError('导出产物与完成记录不一致：' + str(source))
            hashes[name] = actual
        (staged / 'native-root.contract').write_bytes(contracts[0])
        hashes['native-root.contract'] = hashlib.sha256(contracts[0]).hexdigest()
        (staged / 'SHA256SUMS').write_text(''.join(
            digest + '  ' + name + '\n' for name, digest in sorted(hashes.items())))
        if output.exists() or output.is_symlink():
            raise ValueError('导出目录已存在，未覆盖：' + str(output))
        staged.rename(output)
    return output
