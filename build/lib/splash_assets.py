# SPDX-License-Identifier: MIT
"""Generate the four fixed-orientation themes and select firmware-splash bootargs."""
from pathlib import Path
import argparse
import json

PROJECT = Path(__file__).resolve().parents[2]
ANGLES = (0, 90, 180, 270)


def theme_files():
    template = (PROJECT/'device/boot/splash/theme.script.in').read_text()
    if template.count('@ROTATION@') != 1:
        raise ValueError('invalid oriented splash template')
    result = {}
    for angle in ANGLES:
        name = f'liuqin-rotate-{angle}'
        base = 'usr/share/plymouth/themes/' + name
        result[base+'/theme.script'] = template.replace('@ROTATION@', str(angle))
        result[base+'/'+name+'.plymouth'] = (
            '[Plymouth Theme]\nName=Fedora\nDescription=Fedora boot and shutdown screen\n'
            'ModuleName=script\n\n[script]\n'
            'ImageDir=/usr/share/plymouth/themes/spinner\n'
            'ScriptFile=/' + base + '/theme.script\n')
    return result


def install_themes(root):
    root = Path(root).resolve()
    for name, content in theme_files().items():
        path = root/name
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent.is_relative_to(root)):
            raise ValueError('splash destination traverses a symlink: '+name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o644)


def supports_firmware(runtime, kernel_config):
    if runtime is None:
        return False
    manifest = json.loads((Path(runtime)/'runtime.json').read_text())
    value = manifest.get('firmware_splash', False)
    if type(value) is not bool:
        raise ValueError('invalid firmware splash capability')
    config = Path(kernel_config).read_text().splitlines()
    return value and 'CONFIG_BACKLIGHT_KTZ8866=m' in config


def bootargs(text, enabled):
    if not enabled:
        return text.strip()
    words = text.split()
    output = []
    for word in words:
        if word.startswith('initcall_blacklist='):
            names = [n for n in word.split('=', 1)[1].split(',') if n != 'simplefb_driver_init']
            if names:
                output.append('initcall_blacklist='+','.join(names))
        elif word.startswith('fbcon='):
            if word != 'fbcon=map:9':
                raise ValueError('firmware splash requires fbcon=map:9')
            if word not in output:
                output.append(word)
        else:
            output.append(word)
    if 'fbcon=map:9' not in output:
        output.append('fbcon=map:9')
    for option in ('liuqin.firmware_splash=1', 'liuqin.splash_orientation=1'):
        key = option.split('=', 1)[0]
        if any(w.startswith(key+'=') and w != option for w in output):
            raise ValueError('conflicting splash boot option: '+key)
        if option not in output:
            output.append(option)
    return ' '.join(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--kernel-config', type=Path)
    parser.add_argument('--bootargs')
    args = parser.parse_args()
    if args.root is not None:
        install_themes(args.root)
    else:
        if args.kernel_config is None or args.bootargs is None:
            parser.error('bootargs selection needs --kernel-config and --bootargs')
        print(bootargs(args.bootargs, supports_firmware(args.runtime, args.kernel_config)))
