# SPDX-License-Identifier: MIT
"""Locate the bundled RAM environment independently of OS install materials."""
import json
import os
from pathlib import Path
import struct


def settings_scope():
    project=Path(__file__).resolve().parents[2]
    for p in (project/'installer/runtime/native-recovery-manifest.json',project/'out/releases/native-recovery-flash/native-recovery-manifest.json',project/'out/releases/native-recovery-adb/native-recovery-manifest.json'):
        try:
            manifest=json.loads(p.read_text())
            if manifest.get('combined_install_version')==1:return 'native-combined-v1'
            if manifest.get('generic_ota_version',0)>=2:return 'native-rom-v2'
            if manifest.get('usb_transport')=='adb':return 'native-adb'
        except (OSError,ValueError):pass
    return 'native-recovery' if (project/'installer/runtime/native-recovery.img').is_file() else 'standard'


def backend_hint(path):
    """Presentation hint only; Engine always validates the complete image."""
    try:
        with Path(path).open('rb') as stream:header=stream.read(44)
        try:
            if json.loads(Path(path).with_name('native-recovery-manifest.json').read_text()).get('usb_transport')=='adb':return 'adb-native'
        except (OSError,ValueError):pass
        return 'adb' if len(header)==44 and header[:8]==b'ANDROID!' and struct.unpack_from('<I',header,40)[0] in (3,4) else 'network'
    except OSError:return ''


def locate(explicit='',bundle='',state_dir=None):
    project=Path(__file__).resolve().parents[2]
    candidates=[]
    if explicit:
        path=Path(explicit).expanduser()
        return str(path.resolve()) if path.is_file() and not path.is_symlink() else ''
    if os.environ.get('LIUQIN_RECOVERY_IMAGE'):candidates.append(Path(os.environ['LIUQIN_RECOVERY_IMAGE']).expanduser())
    candidates += [project/'installer/runtime/native-recovery.img',project/'runtime/native-recovery.img']
    candidates += [project/'out/releases/native-recovery-flash/recovery.img',project/'out/releases/native-recovery-adb/recovery.img']
    for path in (project/'installer/runtime/recovery.img',project/'runtime/recovery.img',project/'out/releases/recovery/recovery.img'):
        if path.is_file() and not path.is_symlink():
            from liuqin_recovery_image import inspect_paired
            try:
                if inspect_paired(path):candidates.append(path)
            except (ValueError,OSError):pass
    for path in candidates:
        if path.is_file() and not path.is_symlink():return str(path.resolve())
    if state_dir:
        try:
            saved=json.loads((Path(state_dir)/'runtime.json').read_text())
            if saved.get('scope','standard')==settings_scope():
                path=Path(saved['installer_path'])
                if path.is_file() and not path.is_symlink():return str(path.resolve())
        except (OSError,ValueError,KeyError,TypeError):pass
    return ''


def native_available():
    project=Path(__file__).resolve().parents[2]
    roots=(project/'installer/runtime',project/'runtime',project/'out/releases')
    return any((r/'native-recovery.img').is_file() or any((r/'native-recoveries').glob('*/recovery.img')) for r in roots) or (project/'out/releases/native-recovery-flash/recovery.img').is_file() or (project/'out/releases/native-recovery-adb/recovery.img').is_file()
