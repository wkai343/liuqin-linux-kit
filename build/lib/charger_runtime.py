# SPDX-License-Identifier: MIT
"""Stage the Fedora ARM64 Python used by the initramfs charging services."""
import argparse
import filecmp
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

from plymouth_runtime import rooted


PROJECT = Path(__file__).resolve().parents[2]
SERVICES = (PROJECT/'device/charger-mode/liuqin-charger-auth',
            PROJECT/'device/board-overlay/usr/local/libexec/liuqin-mipps-auth',
            PROJECT/'device/board-overlay/usr/local/libexec/liuqin-charge-thermal')
PROBE = """
import argparse, hashlib, hmac, json, secrets, sys
from pathlib import Path
for name, source in json.load(sys.stdin).items():
    exec(compile(source, name, 'exec'), {'__name__': 'charger_import', '__file__': name})
argparse.ArgumentParser().format_help()
hmac.new(b'key', secrets.token_bytes(16), hashlib.sha256).digest()
print(json.dumps(sorted({p.name for mod in sys.modules.copy().values()
                         if (file := getattr(mod, '__file__', None))
                         and (p := Path(file)).parent.name == 'lib-dynload'})))
"""


def collect(root, output, chroot):
    from elftools.elf.elffile import ELFFile

    python = rooted(root, 'usr/bin/python3')
    if not re.fullmatch(r'python3\.\d+', python.name):
        raise ValueError('missing versioned Fedora interpreter')
    std = rooted(root, 'usr/lib64/' + python.name)
    files = {}
    elf_sources = set()
    pending = {'usr/bin/python3': python}
    # Discover the running Fedora interpreter's extensions, including private
    # imports made by its version of math, hashlib, subprocess and ctypes.
    result = chroot(root, ['/usr/bin/python3', '-IB', '-c', PROBE],
                    input=json.dumps({p.name:p.read_text() for p in SERVICES}),
                    text=True, capture_output=True)
    extensions = set(json.loads(result.stdout))
    for file in sorted(std.rglob('*')):
        rel = file.relative_to(std)
        if {'site-packages', '__pycache__', 'test', 'tests'} & set(rel.parts):
            continue
        dest = 'usr/lib64/' + python.name + '/' + rel.as_posix()
        if file.suffix == '.py' or rel.as_posix() == 'LICENSE.txt':
            files[dest] = rooted(root, dest)
        elif rel.parts[0] == 'lib-dynload' and file.name in extensions:
            pending[dest] = rooted(root, dest)
            extensions.remove(file.name)
    if extensions:
        raise ValueError('missing target Python extensions: ' + ', '.join(sorted(extensions)))

    while pending:
        name, source = pending.popitem()
        if name in files:
            continue
        elf_sources.add('/' + source.relative_to(root).as_posix())
        with source.open('rb') as stream:
            elf = ELFFile(stream)
            if elf.header.e_machine != 'EM_AARCH64' or not elf.little_endian or elf.elfclass != 64:
                raise ValueError('unexpected charger ELF architecture: ' + name)
            for seg in elf.iter_segments():
                if seg.header.p_type == 'PT_INTERP':
                    interp = seg.get_interp_name()
                    if interp != '/lib/ld-linux-aarch64.so.1':
                        raise ValueError('unexpected charger ELF interpreter: ' + interp)
                    pending[interp.lstrip('/')] = rooted(root, interp)
                if seg.header.p_type == 'PT_DYNAMIC':
                    for tag in seg.iter_tags('DT_NEEDED'):
                        if not re.fullmatch(r'[\w.+-]+\.so(?:\.[\w.+-]+)?', tag.needed):
                            raise ValueError('invalid charger ELF dependency')
                        dep = ('lib/' if tag.needed == 'ld-linux-aarch64.so.1' else 'usr/lib64/') + tag.needed
                        pending[dep] = rooted(root, dep)
        files[name] = source

    licenses = chroot(root, ['/usr/bin/rpm', '-qf', '--licensefiles', *sorted(elf_sources)],
                      text=True, capture_output=True)
    for path in licenses.stdout.splitlines():
        if path.startswith('/usr/'):
            name = path.lstrip('/')
            source = rooted(root, name)
            if source.is_file():
                files[name] = source
    for name, source in files.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)


def stage_adc(config, modules, contract, staging):
    values = config.read_text().splitlines()
    if 'CONFIG_QCOM_SPMI_ADC5=y' in values:
        return
    if 'CONFIG_QCOM_SPMI_ADC5=m' not in values:
        raise ValueError('charger mode requires the PMIC ADC driver')
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'shared'))
    from liuqin_kernel import extract_modules, parse_contract
    identity = contract.read_bytes()
    with tempfile.TemporaryDirectory(dir=staging.parent) as work:
        extract_modules(modules, Path(work), identity)
        tree = Path(work) / 'usr/lib/modules' / parse_contract(identity)['release']
        deps = dict(line.split(':', 1) for line in (tree/'modules.dep').read_text().splitlines())
        adc, = (name for name in deps if Path(name).name == 'qcom-spmi-adc5.ko')
        # depmod lists all dependencies in removal order, including transitive ones.
        order = [*reversed(deps[adc].split()), adc]
        prefix = staging / 'usr/lib/liuqin-charger'
        prefix.mkdir(parents=True, exist_ok=True)
        names = []
        for name in order:
            path = Path(name)
            if path.is_absolute() or '..' in path.parts or not re.fullmatch(r'[\w-]+\.ko', path.name):
                raise ValueError('invalid charging ADC dependency')
            shutil.copyfile(tree/path, prefix/path.name)
            names.append(path.name)
        (prefix/'modules.order').write_text('\n'.join(names) + '\n')


def stage(runtime, staging):
    python = runtime / 'usr/bin/python3'
    with python.open('rb') as stream:
        header = stream.read(20)
    if header[:6] != b'\x7fELF\x02\x01' or int.from_bytes(header[18:20], 'little') != 183:
        raise ValueError('expected the Fedora ARM64 charger runtime')
    for file in sorted(runtime.rglob('*')):
        if file.is_symlink():
            raise ValueError('charger runtime must contain materialized files')
        if file.is_dir():
            continue
        target = staging / file.relative_to(runtime)
        if target.is_symlink() or not target.resolve().is_relative_to(staging.resolve()):
            raise ValueError('invalid charger staging path')
        if target.exists():
            if not filecmp.cmp(target, file, shallow=False):
                raise ValueError('charger and early display runtime differ: ' + str(target))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    parser.add_argument('staging', type=Path)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--modules', type=Path, required=True)
    parser.add_argument('--contract', type=Path, required=True)
    args = parser.parse_args()
    stage(args.runtime, args.staging)
    stage_adc(args.config, args.modules, args.contract, args.staging)
