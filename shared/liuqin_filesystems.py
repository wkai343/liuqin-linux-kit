# SPDX-License-Identifier: MIT
"""Root filesystem choices; capabilities are read from the selected artifacts."""
SUPPORTED = ('ext4', 'btrfs')
CAPABILITY_PATH = 'etc/liuqin-filesystems'


def validate(value):
    if value not in SUPPORTED:
        raise ValueError('根文件系统只支持 ext4 或 Btrfs。')
    return value


def parse_support(content):
    # Previously built artifacts only admitted ext4 at every boot gate.
    if content is None:
        return ['ext4']
    if content not in (b'ext4\n', b'ext4\nbtrfs\n'):
        raise ValueError('文件系统支持信息无效。')
    return content.decode('ascii').splitlines()


def kernel_support(config):
    lines = set(config.splitlines())
    if 'CONFIG_EXT4_FS=y' not in lines:
        raise ValueError('安装环境和引导需要内置 ext4 驱动，以读取原厂校准分区。')
    result = ['ext4']
    if {'CONFIG_BTRFS_FS=y', 'CONFIG_BTRFS_FS_POSIX_ACL=y'} <= lines:
        result.append('btrfs')
    return result
