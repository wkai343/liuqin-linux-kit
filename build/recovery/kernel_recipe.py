# SPDX-License-Identifier: MIT
"""Pinned Recovery sources and build identities, independent of a device fork."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[2]
DEFAULT_LOCK = PROJECT / 'build/recovery/kernel/source.json'
sys.path.insert(0, str(PROJECT / 'build/lib'))
from kernel_adaptation import assemble


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_configuration(original, generated):
    def values(text):
        return dict(re.findall(r'^(CONFIG_\w+)=(.*)$', text, re.M))
    before, after = values(original), values(generated)
    if before.get('CONFIG_RUST') == 'y' or before.get('CONFIG_DEBUG_INFO_BTF') == 'y':
        raise ValueError('unsupported Recovery Rust/BTF configuration')
    changed = {name: (before.get(name), after.get(name)) for name in before.keys() | after.keys()
               if before.get(name) != after.get(name)}
    # Neither Rust nor BTF code is built by these profiles; only their tool
    # detection metadata is allowed to differ from the recovered config.
    unexpected = {name: value for name, value in changed.items()
                  if not name.startswith('CONFIG_RUSTC_') and name not in
                  ('CONFIG_RUST_IS_AVAILABLE', 'CONFIG_PAHOLE_VERSION')}
    if unexpected:
        raise ValueError('Recovery kernel capabilities changed: ' + json.dumps(unexpected))
    return changed


def git(source, *args):
    return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()


def project_file(name):
    if not isinstance(name, str):
        raise ValueError('invalid Recovery recipe path')
    path = PROJECT / name
    if (Path(name).is_absolute() or
            '..' in Path(name).parts or not path.is_file() or path.is_symlink() or
            not path.resolve().is_relative_to(PROJECT)):
        raise ValueError('invalid Recovery recipe path: ' + str(name))
    return path


def load(path=DEFAULT_LOCK):
    path = Path(path).resolve()
    if not path.is_relative_to(PROJECT / 'build/recovery/kernel'):
        raise ValueError('Recovery source lock must be in build/recovery/kernel')
    record = json.loads(path.read_text())
    if (record.get('schema') != 1 or record.get('device') != 'liuqin' or
            record.get('repo') not in (
                'https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git',
                'https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git') or
            any(not re.fullmatch('[a-f0-9]{40}', record.get(k, ''))
                for k in ('commit', 'expected_source_tree')) or
            not isinstance(record.get('patches'), list) or not record['patches']):
        raise ValueError('invalid Recovery source lock')
    names = set()
    inputs = [record['config'], *record['patches'], *record['dtb_inputs'],
              *record.get('touch_patches', [])]
    if record.get('shared_kernel'):
        inputs.append(record['shared_kernel'])
    for entry in inputs:
        name = entry['path']
        if name in names or sha(project_file(name)) != entry['sha256']:
            raise ValueError('Recovery recipe input changed: ' + name)
        names.add(name)
    if record.get('shared_kernel'):
        shared = json.loads(project_file(record['shared_kernel']['path']).read_text())
        patches = shared['target_patches']
        if (any(record[key] != shared[key] for key in ('repo', 'commit', 'dtb')) or
                not patches or
                [p['path'] for p in record['patches'][:len(patches)]] != patches):
            raise ValueError('Recovery must retain the pinned shared kernel and patch order')
    return record


def prepare_touch(output, path=DEFAULT_LOCK):
    """Apply profile-only changes to a private copy, without modifying the module sources."""
    record = load(path)
    output = Path(output)
    shutil.copytree(PROJECT/'device/recovery/touch', output)
    for entry in record.get('touch_patches', []):
        subprocess.run(['patch', '-p1', '--batch', '--forward',
                        '-i', str(project_file(entry['path']))], cwd=output, check=True)
    return output


def identity(path=DEFAULT_LOCK):
    path = Path(path).resolve()
    record = load(path)
    return dict(lock=str(path.relative_to(PROJECT)), lock_sha256=sha(path),
                tree=record['expected_source_tree'], config_sha256=record['config']['sha256'])


def verify_source(source, path=DEFAULT_LOCK):
    record = load(path)
    if git(source, 'rev-parse', 'HEAD^{tree}') != record['expected_source_tree']:
        raise ValueError('Recovery source tree differs from its recipe')
    if git(source, 'status', '--porcelain', '--untracked-files=normal'):
        raise ValueError('Recovery source must be clean')
    return identity(path)


def prepare_source(cache, output, path=DEFAULT_LOCK):
    record = load(path)
    cache, output = Path(cache).resolve(), Path(output).resolve()
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'init', '--bare', str(cache)], check=True)
        subprocess.run(['git', '-C', str(cache), 'remote', 'add', 'origin', record['repo']], check=True)
    if git(cache, 'remote', 'get-url', 'origin') != record['repo']:
        raise ValueError('Recovery source cache must use the pinned kernel.org repository')
    if subprocess.run(['git', '-C', str(cache), 'cat-file', '-e', record['commit']+'^{commit}'],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        subprocess.run(['git', '-C', str(cache), 'fetch', '--depth=1', 'origin', record['commit']], check=True)
    # FETCH_HEAD alone is not transferred when cloning a shallow source cache.
    subprocess.run(['git', '-C', str(cache), 'update-ref',
                    'refs/heads/recovery-'+record['commit'], record['commit']], check=True)
    if output.exists():
        verify_source(output / 'source', path)
        return output / 'source'
    output.mkdir(parents=True)
    assemble(cache, record['commit'], [project_file(p['path']) for p in record['patches']], output)
    verify_source(output / 'source', path)
    return output / 'source'


def verify_build(source, kernel, path=DEFAULT_LOCK):
    source_identity = verify_source(source, path)
    recipe = load(path)
    kernel = Path(kernel)
    info = json.loads((kernel / 'recovery-build.json').read_text())
    if info['source'] != source_identity:
        raise ValueError('Recovery build belongs to another source recipe')
    required = {'arch/arm64/boot/Image', 'arch/arm64/boot/dts/'+recipe['dtb'],
                '.config', 'vmlinux', 'System.map', 'include/config/kernel.release', 'vmlinux.symvers'}
    if not required <= info['files'].keys():
        raise ValueError('Recovery build record is incomplete')
    for name, digest in info['files'].items():
        if Path(name).is_absolute() or '..' in Path(name).parts or sha(kernel / name) != digest:
            raise ValueError('Recovery build output changed: ' + name)
    if info['config_sha256'] != sha(kernel / '.config'):
        raise ValueError('Recovery build configuration changed')
    check_configuration(project_file(recipe['config']['path']).read_text(), (kernel/'.config').read_text())
    return source_identity


def verify_components(directory):
    directory = Path(directory)
    record = json.loads((directory / 'kernel-manifest.json').read_text())
    if record.get('schema') != 2 or record.get('device') != 'liuqin':
        raise ValueError('expected source-built Recovery kernel components')
    source = record['source']
    path = project_file(source['lock'])
    if source != identity(path):
        raise ValueError('Recovery component recipe changed')
    required = {'Image.gz', 'recovery.dtb', 'liuqin_bootloader_panel.ko', 'kernel.config', 'source-lock.json'}
    recipe = load(path)
    patches = [*recipe['patches'], *recipe.get('touch_patches', [])]
    required.update(Path(p['path']).name for p in patches)
    if recipe.get('shared_kernel'):
        required.add('shared-source-lock.json')
    if not required <= record['files'].keys():
        raise ValueError('Recovery kernel bundle is incomplete')
    for name, digest in record['files'].items():
        if Path(name).name != name or sha(directory / name) != digest:
            raise ValueError('Recovery kernel component changed: ' + name)
    for name, digest in record['source_files'].items():
        if sha(project_file(name)) != digest:
            raise ValueError('Recovery builder changed: ' + name)
    if sha(directory/'source-lock.json') != source['lock_sha256']:
        raise ValueError('embedded Recovery recipe differs')
    for entry in patches:
        if sha(directory/Path(entry['path']).name) != entry['sha256']:
            raise ValueError('embedded Recovery patch differs: ' + entry['path'])
    if (recipe.get('shared_kernel') and
            sha(directory/'shared-source-lock.json') != recipe['shared_kernel']['sha256']):
        raise ValueError('embedded shared source recipe differs')
    if (sha(directory/'kernel.config') != record['config_sha256'] or
            sha(directory/'recovery.dtb') != record['dtb_sha256']):
        raise ValueError('Recovery configuration/DTB identity differs')
    check_configuration(project_file(load(path)['config']['path']).read_text(),
                        (directory/'kernel.config').read_text())
    return record
