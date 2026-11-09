# SPDX-License-Identifier: MIT
"""GPT labels compatible with liuqin ABL's 32-byte Fastboot variable names."""
import re
import uuid

MAX_NAME_BYTES = 16  # len('partition-size:') + name + NUL <= 32
ROOT_PREFIX = 'lqr_'


def root_name(os_id):
    if not isinstance(os_id, str) or str(uuid.UUID(os_id)) != os_id:
        raise ValueError('rootfs 身份必须是规范的小写 UUID。')
    return ROOT_PREFIX + os_id.replace('-', '')[:12]


def is_root_name(name):
    return isinstance(name, str) and bool(re.fullmatch(r'lqr_[0-9a-f]{12}', name))


def validate_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]*', name) or len(name)>MAX_NAME_BYTES:
        raise ValueError('GPT 分区名必须为 1–16 个 ASCII 字符：' + repr(name))
    return name


def validate_names(names):
    for name in names:
        try:
            validate_name(name)
        except ValueError as error:
            raise ValueError('发现 Bootloader 不兼容的分区名 ' + repr(name) +
                '。已停止写入及自动返回 Fastboot；保留当前 Recovery 会话，先核对备份并修复 GPT 名称。') from error
