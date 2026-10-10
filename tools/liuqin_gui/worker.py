# SPDX-License-Identifier: MIT
"""One persistent worker: requests on stdin, ordered JSON events on stdout."""
import json
import os
from pathlib import Path
import sys
import threading

from .engine import Engine, InstallerError


def main(args):
    os.umask(0o077)
    output = sys.stdout
    lock = threading.Lock()
    sequence = 0
    disconnected = False

    def emit(event):
        nonlocal sequence, disconnected
        with lock:
            sequence += 1
            event = dict(schema_version=2, seq=sequence, **event)
            if not disconnected:
                try:
                    output.write(json.dumps(event, ensure_ascii=False) + '\n')
                    output.flush()
                except (BrokenPipeError, OSError):
                    disconnected = True

    state_dir = args.state_dir or Path.home() / '.local/state/liuqin-installer'
    if args.demo:
        from .demo import DemoEngine
        engine = DemoEngine(emit, state_dir)
    else:
        engine = Engine(emit, state_dir)
    gate = threading.Lock()
    busy = False
    thread = None
    emit(dict(type='ready', simulation=args.demo, state='idle'))

    def execute(request):
        nonlocal busy
        operation = request['action']
        engine.request_id = request.get('id', '')
        try:
            if operation == 'devices':
                engine.list_devices()
            elif operation == 'validate':
                engine.validate_bundle(request.get('bundle', ''))
            elif operation == 'prepare':
                engine.prepare(request)
            elif operation == 'backup':
                engine.create_backup(request)
            elif operation == 'find_backup':
                engine.find_backup(request)
            elif operation == 'select_backup':
                engine.select_backup(request)
            elif operation == 'install':
                engine.install(request)
            elif operation == 'cancel':
                engine.cancel_prepared()
            elif operation == 'verify':
                engine.verify_first_boot(request)
            else:
                raise InstallerError('未知操作：' + operation)
        except Exception as exc:
            # An invalid confirmation must not destroy a still valid plan.
            if operation == 'install' and engine.state == 'prepared':
                engine.emit('request_rejected', message=str(exc))
            else:
                engine.fail(exc)
        finally:
            with gate:
                busy = False
                emit(dict(type='activity', busy=False, request_id=request.get('id', '')))

    while True:
        line = sys.stdin.readline(1024 * 1024 + 1)
        if not line:
            # Losing the UI never terminates an in-flight write. Wait for the
            # job, then clean up a read-only prepared RAM session if possible.
            if thread:
                thread.join()
            if engine.state == 'prepared':
                try:
                    engine.cancel_prepared()
                except Exception as exc:
                    engine.fail(exc)
            break
        try:
            if len(line) > 1024 * 1024:
                raise InstallerError('请求过大。')
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get('action'), str):
                raise InstallerError('无效的请求。')
            action = request['action']
            with gate:
                if action == 'shutdown':
                    if busy or engine.state == 'prepared':
                        raise InstallerError('请先结束当前操作。')
                    break
                if action == 'cancel' and busy:
                    if engine.state not in ('preparing', 'backing_up'):
                        raise InstallerError('当前阶段不能取消。')
                    engine.request_cancel()
                    emit(dict(type='cancel_requested', message='正在有序结束准备。'))
                    continue
                if busy:
                    raise InstallerError('后端正在执行其他操作。')
                if engine.state == 'prepared' and action not in ('install', 'cancel'):
                    raise InstallerError('请确认当前方案或先取消准备。')
                busy = True
                emit(dict(type='activity', busy=True, request_id=request.get('id', '')))
                thread = threading.Thread(target=execute, args=(request,), daemon=False)
                thread.start()
        except Exception as exc:
            emit(dict(type='request_rejected', message=str(exc)))
    engine.close()
