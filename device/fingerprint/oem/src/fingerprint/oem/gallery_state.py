#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Private per-account gallery storage and immutable fprintd finger references.

Only encrypted OEM databases are stored. A reference is not an authentication
result: matching still requires a fresh hardware operation and matching HAT.
"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import struct
import subprocess
import tempfile

MAX_FINGERS = 5  # Stock HAL's 11/8 inventory has five template slots.
MAX_DATABASE = 16 * 1024 * 1024
REFERENCE = struct.Struct('<8sII32s')
MAGIC = b'FPCREF2\0'
SLOTS = '123456789a'


def private_read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                info.st_mode & 0o777 != 0o600 or not 0 < info.st_size <= limit):
            raise ValueError('unsafe private gallery file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            return stream.read(limit + 1)
    finally:
        os.close(fd)


def private_directory(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o777 != 0o700:
        raise ValueError('unsafe gallery directory')


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(data); stream.flush()
        os.fsync(fd)
    finally:
        os.close(fd)


def account(username, uid=None):
    entry = pwd.getpwnam(username)
    if (entry.pw_name != username or not 0 < entry.pw_uid < 0x10000000 or
            username in ('.', '..') or '/' in username or '\0' in username or
            pwd.getpwuid(entry.pw_uid).pw_name != username or
            uid is not None and entry.pw_uid != uid):
        raise ValueError('ambiguous gallery account')
    return entry.pw_uid


def reference(uid, finger, token):
    if type(uid) is not int or not 0 < uid < 0x10000000 or type(finger) is not int or not 1 <= finger <= 10:
        raise ValueError('invalid gallery reference account')
    if len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('invalid gallery reference token')
    return REFERENCE.pack(MAGIC, uid, finger, bytes.fromhex(token))


def parse_reference(data, uid, finger):
    if len(data) != REFERENCE.size:
        raise ValueError('invalid reference length')
    magic, owner, slot, token = REFERENCE.unpack(data)
    if magic != MAGIC or owner != uid or slot != finger:
        raise ValueError('reference belongs to another account or finger')
    return token.hex()


def decode_print(here, username, path):
    """Deserialize through the same pinned libfprint ABI, without probing hardware."""
    program = (here/'fpc-oem-print').lstat()
    if not stat.S_ISREG(program.st_mode) or program.st_uid != 0 or program.st_mode & 0o022:
        raise ValueError('untrusted stored-print decoder')
    source = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(source)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o777 != 0o600 or
                not 0 < info.st_size <= MAX_DATABASE + 1024 * 1024):
            raise ValueError('unsafe stored fingerprint')
        with tempfile.TemporaryFile() as output:
            os.fchmod(output.fileno(), 0o600)
            command = subprocess.run([str(here/'fpc-oem-print'), '--decode', username,
                                      str(source), str(output.fileno())],
                                     pass_fds=(source, output.fileno()), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            if command.returncode:
                raise ValueError('stored fingerprint cannot be decoded')
            output.seek(0); data = output.read(MAX_DATABASE + 9)
        if len(data) < 9:
            raise ValueError('empty stored fingerprint')
        version, finger = struct.unpack_from('<II', data)
        payload = data[8:]
        if version != 2 or not 1 <= finger <= 10 or not 0 < len(payload) <= MAX_DATABASE:
            raise ValueError('unknown stored fingerprint format')
        return {'version': version, 'finger': finger, 'data': payload}
    finally:
        os.close(source)


def read_catalog(here, username, uid, root=Path('/var/lib/fprint')):
    directory = root/username/'fpc1264_oem/liuqin-fpc1264-oem'
    # Every existing parent must be a trusted real directory, even when empty.
    for parent in (root, root/username, root/username/'fpc1264_oem', directory):
        if not os.path.lexists(parent):
            return []
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unsafe fingerprint store parent')
    prints = []
    for finger, name in enumerate(SLOTS, 1):
        path = directory/name
        if not os.path.lexists(path):
            continue
        item = decode_print(here, username, path)
        if item['finger'] != finger:
            raise ValueError('stored finger metadata differs from path')
        if item['version'] == 2:
            parse_reference(item['data'], uid, finger)
        prints.append(item)
    if len(prints) > MAX_FINGERS:
        raise ValueError('stored gallery exceeds firmware capacity')
    return prints


def read_state(directory, username, uid, filename='gallery.json', allow_pending=False):
    credential = directory/'credential.json'
    if os.path.lexists(credential):
        binding = json.loads(private_read(credential, 4096))
        if (binding.get('linux_username'), binding.get('linux_uid'), binding.get('native_uid')) != (username, uid, 0x60000000 | uid):
            raise ValueError('credential belongs to a different account')
    if not allow_pending and os.path.lexists(directory/'gallery.previous'):
        raise ValueError('uncertain gallery transaction; preserve previous state')
    path = directory/filename
    if not os.path.lexists(path):
        return None
    encoded = private_read(path, 24 * 1024 * 1024)
    state = json.loads(encoded)
    if (set(state) != {'schema', 'username', 'uid', 'bindings', 'database', 'sha256'} or
            state['schema'] != 3 or state['username'] != username or state['uid'] != uid or
            not isinstance(state['bindings'], dict) or not 1 <= len(state['bindings']) <= MAX_FINGERS):
        raise ValueError('gallery account metadata mismatch')
    data = base64.b64decode(state['database'], validate=True)
    if not 0 < len(data) <= MAX_DATABASE or hashlib.sha256(data).hexdigest() != state['sha256']:
        raise ValueError('gallery database digest mismatch')
    ids = []; tokens = []
    for finger, item in state['bindings'].items():
        if finger not in [str(i) for i in range(1, 11)] or set(item) != {'id', 'token'}:
            raise ValueError('invalid gallery binding')
        if type(item['id']) is not int or not 0 < item['id'] <= 0xffffffff:
            raise ValueError('invalid template identifier')
        reference(uid, int(finger), item['token'])
        ids.append(item['id']); tokens.append(item['token'])
    if len(set(ids)) != len(ids) or len(set(tokens)) != len(tokens):
        raise ValueError('duplicate gallery binding')
    state['database'] = data
    state['_source'] = encoded
    return state


def new_state(username, uid, bindings, database):
    if not 0 < len(database) <= MAX_DATABASE or not 1 <= len(bindings) <= MAX_FINGERS:
        raise ValueError('invalid gallery database')
    ids = []; tokens = []
    for finger, item in bindings.items():
        reference(uid, int(finger), item['token'])
        if type(item['id']) is not int or not 0 < item['id'] <= 0xffffffff:
            raise ValueError('invalid gallery template ID')
        ids.append(item['id']); tokens.append(item['token'])
    if len(set(ids)) != len(ids) or len(set(tokens)) != len(tokens):
        raise ValueError('duplicate gallery membership')
    return dict(schema=3, username=username, uid=uid, bindings=bindings,
                database=database, sha256=hashlib.sha256(database).hexdigest())


def save_state(directory, state, previous, cancelled=lambda: False):
    private_directory(directory)
    target = directory/'gallery.json'; backup = directory/'gallery.previous'
    if cancelled() or os.path.lexists(backup):
        raise ValueError('gallery commit cancelled or interrupted')
    if previous is None:
        if os.path.lexists(target):
            raise ValueError('gallery appeared during operation')
    elif private_read(target, 24 * 1024 * 1024) != previous['_source']:
        raise ValueError('gallery changed during operation')
    public = {key: value for key, value in state.items() if not key.startswith('_')}
    public['database'] = base64.b64encode(state['database']).decode('ascii')
    encoded = (json.dumps(public, sort_keys=True, separators=(',', ':'))+'\n').encode()
    fd, temporary = tempfile.mkstemp(prefix='.gallery-', dir=directory)
    installed = False
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(encoded); stream.flush()
        os.fsync(fd)
        if previous is not None:
            os.link(target, backup, follow_symlinks=False); sync_directory(directory)
        if cancelled():
            raise InterruptedError('gallery commit cancelled')
        os.replace(temporary, target); installed = True
        sync_directory(directory)
    except BaseException:
        if previous is not None and os.path.lexists(backup):
            if installed:
                os.replace(backup, target)
            else:
                # rename of two hardlinks to the same inode is a no-op; remove
                # only the backup when the original was never replaced.
                backup.unlink()
            sync_directory(directory)
        elif installed:
            # A first install remains a complete, fsynced file or is removed.
            target.unlink(); sync_directory(directory)
        raise
    finally:
        os.close(fd)
        if os.path.lexists(temporary):
            os.unlink(temporary)
    if previous is not None:
        backup.unlink(); sync_directory(directory)


def resolve_bindings(state, catalog, uid):
    if state is None:
        if catalog: raise ValueError('published references require their original gallery')
        return {}
    active = {}
    for item in catalog:
        binding = state['bindings'].get(str(item['finger']))
        if not binding:
            raise ValueError('finger is not bound to this gallery')
        if item['version'] != 2: raise ValueError('unsupported fingerprint format')
        token = parse_reference(item['data'], uid, item['finger'])
        if token != binding['token']:
            raise ValueError('finger reference is stale')
        active[str(item['finger'])] = dict(binding)
    return active


def metadata(path):
    data = private_read(path, 64)
    if len(data) != 64 or data[:8] != b'FPCMETA2':
        raise ValueError('invalid hardware gallery metadata')
    before_count = struct.unpack_from('<I', data, 8)[0]
    after_count = struct.unpack_from('<I', data, 32)[0]
    if before_count > MAX_FINGERS or after_count > MAX_FINGERS:
        raise ValueError('hardware gallery capacity exceeded')
    before = list(struct.unpack_from('<5I', data, 12))[:before_count]
    after = list(struct.unpack_from('<5I', data, 36))[:after_count]
    id_, retry = struct.unpack_from('<II', data, 56)
    for ids in (before, after):
        if len(set(ids)) != len(ids) or any(not n for n in ids):
            raise ValueError('invalid hardware template inventory')
    if retry not in (0, 1):
        raise ValueError('unknown hardware match status')
    return before, after, id_, retry


def recover_state(directory, username, uid, catalog):
    """Complete only a proven atomic commit, or restore a missing current file.

    The caller holds the account operation lock. Both files must be valid for
    this account; no damaged or differently bound data is discarded.
    """
    backup = directory/'gallery.previous'
    if not os.path.lexists(backup):
        return read_state(directory, username, uid)
    old = read_state(directory, username, uid, 'gallery.previous', allow_pending=True)
    current = read_state(directory, username, uid, allow_pending=True)
    if old is None:
        raise ValueError('missing recovery gallery')
    if current is not None:
        resolve_bindings(current, catalog, uid)
        backup.unlink(); sync_directory(directory)
    else:
        resolve_bindings(old, catalog, uid)
        os.replace(backup, directory/'gallery.json'); sync_directory(directory)
    return read_state(directory, username, uid)


def same_catalog(a, b):
    return a == b
