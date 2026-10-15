# SPDX-License-Identifier: MIT
"""USB display strings shared by build configuration and initramfs packaging."""
import argparse
import os
from pathlib import Path
import sys

DEFAULTS = {'usb_manufacturer': 'Xiaomi', 'usb_product': 'Pad 6 Pro'}
ENVIRONMENT = {'usb_manufacturer': 'LIUQIN_USB_MANUFACTURER',
               'usb_product': 'LIUQIN_USB_PRODUCT'}


def settings(data):
    result = {}
    for name, default in DEFAULTS.items():
        value = data.get(name, default)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(name + ' 必须是非空字符串。')
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
            raise ValueError(name + ' 不能包含换行或控制字符。')
        try:
            length = len(value.encode('utf-8'))
        except UnicodeEncodeError:
            raise ValueError(name + ' 必须是有效的 UTF-8 文本。') from None
        # The liuqin kernel's usb_string_copy/get_string cap UTF-8 input at
        # USB_MAX_STRING_LEN (126 bytes), before conversion to UTF-16.
        if length > 126:
            raise ValueError(name + ' 最多允许 126 个 UTF-8 字节。')
        result[name] = value
    return result


def environment(data):
    return {ENVIRONMENT[name]: value for name, value in settings(data).items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory', type=Path)
    args = parser.parse_args()
    values = settings({name: os.environ[variable] for name, variable in ENVIRONMENT.items()
                       if variable in os.environ})
    if args.output_directory is not None:
        for name, value in values.items():
            path = args.output_directory / ('liuqin-' + name.replace('_', '-'))
            with path.open('xb') as target:
                target.write(value.encode('utf-8') + b'\n')
            path.chmod(0o644)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        print('USB 名称配置错误：' + str(error), file=sys.stderr)
        sys.exit(1)
