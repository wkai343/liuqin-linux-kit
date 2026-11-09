# SPDX-License-Identifier: MIT
"""One persistent worker: requests on stdin, ordered JSON events on stdout."""
import json
import os
from pathlib import Path
import signal
import sys
import threading

from .engine import Engine, InstallerError
from .host import configure_stdio


def main(args):
    # The UI owns cancellation and closing. Terminal Ctrl+C must not unwind
    # this reader while the operation thread or its child process writes boot.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    configure_stdio()
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
        previous_session=engine.session_id
        try:
            if operation == 'devices':
                engine.list_devices()
            elif operation == 'inspect_environment':
                from .compatibility import inspect
                inspect(engine,request)
            elif operation == 'native_export_logs':
                from .native_logs import export
                export(engine,request)
            elif operation == 'rom_preview':
                from .rom_preview import preview
                preview(engine,request)
            elif operation == 'validate':
                if request.get('layout') in ('android-manage', 'partition-only', 'partition-reset'):
                    engine.validate_installer(request.get('bundle', ''))
                else:
                    engine.validate_bundle(request.get('bundle', ''))
            elif operation == 'prepare':
                engine.prepare(request)
            elif operation in ('wizard_scan','wizard_prepare','wizard_resume_prepare','wizard_install'):
                from . import workflow
                {'wizard_scan':workflow.scan,'wizard_prepare':workflow.prepare,'wizard_resume_prepare':workflow.prepare_resume,'wizard_install':workflow.install}[operation](engine,request)
            elif operation == 'wizard_switch_prepare':
                from .boot_switch import prepare as prepare_switch
                prepare_switch(engine,request)
            elif operation in ('boot_store_inspect','boot_store_prepare'):
                from . import boot_store
                (boot_store.inspect if operation=='boot_store_inspect' else boot_store.prepare)(engine,request)
            elif operation == 'scan_partitions':
                from .partitioning import scan
                scan(engine, request)
            elif operation == 'scan_layout':
                from .managed_linux import scan_layout
                scan_layout(engine, request)
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
            if operation in ('wizard_scan','wizard_prepare','wizard_resume_prepare','wizard_switch_prepare','boot_store_inspect','boot_store_prepare') and engine.session_id!=previous_session and not engine.receipt.get('write_dispatched') and engine.transport:
                try:engine.transport.cancel_cleanup()
                except Exception as cleanup_error:
                    engine.emit('notice',message='只读准备未能返回 Fastboot：'+str(cleanup_error))
            # An invalid confirmation must not destroy a still valid plan.
            if operation in ('inspect_environment','native_export_logs','rom_preview') or (operation in ('install','wizard_install') and engine.state == 'prepared'):
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
                    if getattr(engine,'workflow_committing',False):raise InstallerError('已开始执行完整方案，不能中断写入。')
                    if engine.state not in ('preparing', 'backing_up','rom_analyzing'):
                        raise InstallerError('当前阶段不能取消。')
                    engine.request_cancel()
                    emit(dict(type='cancel_requested', message='正在有序结束准备。'))
                    continue
                if busy:
                    raise InstallerError('后端正在执行其他操作。')
                if engine.state == 'prepared' and action not in ('install', 'wizard_install', 'cancel'):
                    raise InstallerError('请确认当前方案或先取消准备。')
                busy = True
                emit(dict(type='activity', busy=True, request_id=request.get('id', '')))
                thread = threading.Thread(target=execute, args=(request,), daemon=False)
                thread.start()
        except Exception as exc:
            emit(dict(type='request_rejected', message=str(exc)))
    engine.close()
