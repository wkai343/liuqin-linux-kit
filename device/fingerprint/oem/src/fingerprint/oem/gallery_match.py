#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""One fresh hardware identification against one authorized account's gallery."""
import fcntl
import hashlib
import os
from pathlib import Path
import resource
import signal
import struct
import sys
import tempfile

import gallery_state as gallery
from native_enrol import Runtime

STATE_ROOT = Path('/var/lib/liuqin-fingerprint/native-enrollment')
FPRINT_ROOT = Path('/var/lib/fprint')


def read_request(path):
    data = gallery.private_read(path, gallery.MAX_DATABASE + 16384)
    if len(data) < 20 or data[:8] != b'FPCREQ2\0':
        raise ValueError('unknown gallery request')
    uid, count, size = struct.unpack_from('<III', data, 8)
    if not 1 <= count <= gallery.MAX_FINGERS or not 0 < size <= 4096 or len(data) < 20 + size:
        raise ValueError('invalid gallery request size')
    username = data[20:20+size].decode('utf-8')
    gallery.account(username, uid)
    offset = 20 + size; prints = []
    for _ in range(count):
        if len(data) < offset + 12:
            raise ValueError('truncated gallery request')
        finger, version, size = struct.unpack_from('<III', data, offset); offset += 12
        if (not 1 <= finger <= 10 or version != 2 or
                not 0 < size <= gallery.MAX_DATABASE or len(data) < offset + size):
            raise ValueError('invalid gallery item')
        item = dict(finger=finger, version=version, data=data[offset:offset+size]); offset += size
        if version == 2:
            gallery.parse_reference(item['data'], uid, finger)
        prints.append(item)
    if offset != len(data) or len({p['finger'] for p in prints}) != count:
        raise ValueError('duplicate or trailing gallery items')
    return username, uid, prints


def match(request_path, result_path, runtime):
    username, uid, requested = read_request(request_path)
    here = Path(__file__).resolve().parent
    gallery.private_directory(STATE_ROOT)
    directory = STATE_ROOT/str(0x60000000 | uid)
    gallery.private_directory(directory)
    lock = os.open(directory/'user-operation.lock', os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        catalog = gallery.read_catalog(here, username, uid, FPRINT_ROOT)
        if any(p not in catalog for p in requested):
            raise ValueError('requested fingerprint is no longer published')
        state = gallery.recover_state(directory, username, uid, catalog)
        active = gallery.resolve_bindings(state, requested, uid)
        if state is None: raise ValueError('missing gallery for published references')
        database = state['database']
        with tempfile.TemporaryDirectory(prefix='.match-', dir=directory) as temporary:
            root = Path(temporary); source = root/'database'; updated = root/'updated'; meta = root/'metadata'
            gallery.private_write(source, database)
            status = runtime.run([sys.executable, str(here/'oem_runtime.py'), '--ufs-rpmb-authenticated',
                                  '--match-gallery', str(source), str(updated), str(meta)], None)
            if runtime.cancelled or not runtime.cleaned:
                return 1
            # Only a completed match/miss or explicitly marked retry is useful.
            if not meta.exists():
                return 1
            before, after, matched, retry = gallery.metadata(meta)
            if set(before) != set(after) or not before:
                raise ValueError('hardware changed gallery membership during matching')
            bindings = {key: dict(value) for key, value in state['bindings'].items()}
            if set(before) != {item['id'] for item in bindings.values()}:
                raise ValueError('hardware inventory differs from bound gallery')
            if (retry and (status != 1 or matched) or not retry and
                    (status not in (0, 3) or (status == 0) != bool(matched))):
                raise ValueError('hardware result and status conflict')
            if matched and matched not in before:
                raise ValueError('unknown hardware match identifier')
            # Deleted or unrequested fingers are never authorized, even if the
            # encrypted working DB still contains their templates.
            selected = 0xffffffff
            for index, item in enumerate(requested):
                if bindings[str(item['finger'])]['id'] == matched:
                    selected = index
            if not gallery.same_catalog(catalog, gallery.read_catalog(here, username, uid, FPRINT_ROOT)):
                raise ValueError('published gallery changed during matching')
            if runtime.cancelled:
                return 1
            if updated.exists():
                data = gallery.private_read(updated, gallery.MAX_DATABASE) if updated.exists() else database
                gallery.save_state(directory, gallery.new_state(username, uid, bindings, data), state,
                                   cancelled=lambda: runtime.cancelled)
            if runtime.cancelled:
                return 1
            result = 2 if retry else 1 if selected != 0xffffffff else 0
            gallery.private_write(result_path, b'FPCRES2\0'+struct.pack('<II', result, selected))
            return 0
    finally:
        os.close(lock)


def main(arguments=None):
    args = sys.argv[1:] if arguments is None else arguments
    runtime = Runtime(); handlers = {}
    try:
        if (os.geteuid() != 0 or os.getegid() != 0 or len(args) != 4 or args[0] != '--request'
                or args[2] != '--result'):
            return 2
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); os.umask(0o077)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            handlers[sig] = signal.signal(sig, runtime.cancel)
        result = match(Path(args[1]), Path(args[3]), runtime)
        if result == 0 and not runtime.cancelled:
            print('gallery_match=OK cleanup_complete=1', flush=True)
            return 0
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print('gallery_match=FAILED private_state_preserved=1', flush=True)
        return 1
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    sys.exit(main())
