#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Allowlisted maintenance and boot controls on the Recovery UI socket."""
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import tarfile
import threading
import time
import unicodedata
import uuid

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import liuqin_native_install as gate


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


status = load_module('native_status', HERE/'native-status.py')
SOCKET='/run/liuqin-recovery-ui.sock'
EXITS={'system':'normal','fastboot':'bootloader','poweroff':'poweroff'}


class Operations:
    def __init__(self,root=Path('/'),runner=subprocess.run):
        self.root=Path(root);self.runner=runner;self.pending={};self.guard=threading.Lock()
        self.usb_snapshot=None

    def collect(self):
        value=status.collect(self.root)
        if value['device']!='liuqin' or not value['unlocked'] or value['slot']!='a':
            raise RuntimeError('设备身份或解锁状态未通过检查。')
        return value

    def summary(self):
        value=self.collect();value.pop('blocks',None)
        cached=self.usb_snapshot
        connection=(self.usb_connection() if cached is None else cached[1]
                    if 0<=time.monotonic()-cached[0]<=5 else dict(usb_state='unknown',adb_ready=False))
        value.update(connection)
        value['tablet_boot_switch'] = self.supports_switch(value)
        path=self.root/'sys/class/backlight/ktz8866-backlight'
        try:
            maximum=int((path/'max_brightness').read_text());level=int((path/'brightness').read_text())
            value['brightness']=round(level*100/maximum)
        except (OSError,ValueError,ZeroDivisionError):value['brightness']=None
        value['uptime_seconds']=int(float(status.read(self.root/'proc/uptime','0').split()[0]))
        value['memory_available_kib']=next((int(x.split()[1]) for x in status.read(self.root/'proc/meminfo').splitlines() if x.startswith('MemAvailable:')),0)
        value['partition_count']=len(self.partitions())
        return value

    def usb_connection(self):
        gadget=self.root/'sys/kernel/config/usb_gadget/liuqin-adb'
        controller=status.read(gadget/'UDC')
        ready=(status.read(gadget/'functions/ffs.adb/ready')=='1' and
               bool(re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',controller)))
        online=status.read(self.root/'sys/class/power_supply/qcom-battmgr-usb/online')
        vbus=True if online=='1' else False if online=='0' else None
        state=status.read(self.root/'sys/class/udc'/controller/'state','unknown') if ready else 'unknown'
        value=dict(adb_ready=ready,usb_vbus_online=vbus,usb_controller_state=state)
        # Fixed peripheral mode can retain "configured" after cable removal.
        # The PMIC's live input-power state takes precedence over that latch.
        if vbus is False:
            return dict(value,usb_state='disconnected')
        if not ready:
            return dict(value,usb_state='starting')
        connection={'not attached':'disconnected','configured':'connected',
                    'suspended':'suspended','attached':'connecting','powered':'connecting',
                    'default':'connecting','addressed':'connecting'}.get(state,'unknown')
        if vbus is None and connection in ('connected','suspended'):
            connection='unknown'
        # USB enumeration proves a host link, not an authorized ADB session.
        return dict(value,usb_state=connection)

    def publish_usb_status(self):
        value=self.usb_connection()
        snapshot=dict(value,boot_id=status.read(self.root/'proc/sys/kernel/random/boot_id'),
                      monotonic=time.monotonic())
        path=self.root/'run/liuqin-native-usb.json'
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(snapshot)+'\n')
        temporary.replace(path)
        self.usb_snapshot=(snapshot['monotonic'],value)
        return snapshot

    def partitions(self):
        rows=[]
        for path in (self.root/'sys/class/block').glob('sd*/uevent'):
            fields=dict(line.split('=',1) for line in path.read_text().splitlines() if '=' in line)
            if 'PARTNAME' not in fields:continue
            name=fields['PARTNAME']
            if not re.fullmatch('[A-Za-z0-9_-]{1,64}',name):raise RuntimeError('分区名称无效。')
            rows.append(dict(name=name,node=path.parent.name,bytes=int((path.parent/'size').read_text())*512,
                readonly=(path.parent/'ro').read_text().strip()=='1'))
        return sorted(rows,key=lambda r:(re.sub(r'\d+$','',r['node']),int(re.search(r'\d+$',r['node'])[0])))

    def logs(self,kind):
        if kind=='kernel':
            p=self.runner(['/bin/busybox','dmesg'],capture_output=True,timeout=8,check=True)
            text=p.stdout.decode(errors='replace')[-64000:]
        elif kind=='ui':
            text='\n'.join(status.read(self.root/name)[-24000:] for name in ('run/liuqin-native-ui.log','run/liuqin-native-status.log','run/liuqin-native-adb-start.log','run/liuqin-adbd.log'))
        else:raise ValueError('未知日志类型。')
        text=re.sub(r'[\x00-\x08\x0b-\x1f\x7f]','',text)
        return '\n'.join(text.splitlines()[-160:])

    def export_logs(self):
        value=self.summary()
        files={'device.json':json.dumps(value,ensure_ascii=False,indent=2).encode(),
            'partitions.json':json.dumps(self.partitions(),ensure_ascii=False,indent=2).encode(),
            'kernel.log':self.logs('kernel').encode(),'ui.log':self.logs('ui').encode()}
        for name in ('proc/cmdline','proc/mounts','etc/liuqin-native-recovery.json'):
            files[name.replace('/','_')+'.txt']=(self.root/name).read_bytes()[:65536]
        for name in ('snapshot.json', 'receipt.json'):
            path = self.root/'run/liuqin-bcb'/name
            if path.is_file() and not path.is_symlink():
                files['bcb-'+name] = path.read_bytes()[:16384]
        directory=self.root/'run/liuqin-ui-exports'
        if directory.is_symlink():raise RuntimeError('日志目录不是普通目录。')
        directory.mkdir(mode=0o700,exist_ok=True)
        path=directory/('recovery-log-'+secrets.token_hex(8)+'.tar.gz')
        with path.open('xb') as stream:
            with tarfile.open(fileobj=stream,mode='w:gz') as tar:
                for name,data in files.items():
                    info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o600
                    tar.addfile(info,io.BytesIO(data))
        body=path.read_bytes()
        if len(body)>2*1024**2:raise RuntimeError('日志包超过大小限制。')
        archives=[p for p in directory.iterdir() if re.fullmatch(r'recovery-log-[a-f0-9]{16}\.tar\.gz',p.name) and p.is_file() and not p.is_symlink()]
        for old in sorted(archives,key=lambda p:p.stat().st_mtime_ns,reverse=True)[3:]:old.unlink()
        return dict(file='/run/liuqin-ui-exports/'+path.name,bytes=len(body),sha256=hashlib.sha256(body).hexdigest(),
            message='日志已在内存中打包，请在电脑安装器点击“导出 Recovery 日志”。')

    def check_exit(self):
        value=self.collect()
        if not value['sealed']:raise RuntimeError('存储未确认只读，不能退出。')
        if value['phase'] == 'write-failed':
            raise RuntimeError('上次写入尚未安全结束，请留在 Recovery 检查运行日志。')
        if value['phase'] in ('reading','connected','installing'):
            raise RuntimeError('设备操作正在进行，请稍后重试。')
        from liuqin_partition_names import validate_names
        validate_names([p['name'] for p in self.partitions()])
        return value

    def bcb_backend(self, value):
        if not gate.capable(gate.marker(self.root) or {}):
            raise RuntimeError('当前 Recovery 没有 BCB 维护能力。')
        agent = load_module('bcb_device', HERE/'android-device.py')
        return load_module('native_bcb', HERE/'native-bcb.py'), agent.Hardware(value['boot_id'], value['serial']), agent

    def capture_bcb(self):
        if (gate.marker(self.root) or {}).get('bcb_cleanup_version') != 1:
            return
        with self.operation_lock():
            value = self.collect()
            if not value['sealed']:
                raise RuntimeError('BCB 启动快照要求存储已封存。')
            bcb, hardware, _agent = self.bcb_backend(value)
            bcb.capture(hardware, value, self.root)

    def exit_recovery(self, action, boot_id=None):
        if action not in EXITS.values():
            raise ValueError('未知电源操作。')
        with self.operation_lock():
            value = self.check_exit()
            if boot_id is not None and value['boot_id'] != boot_id:
                raise RuntimeError('启动会话已经变化。')
            if (gate.marker(self.root) or {}).get('bcb_cleanup_version') == 1:
                bcb, hardware, agent = self.bcb_backend(value)
                result = bcb.consume(hardware, agent, value, self.root)
                print('Recovery BCB exit:', json.dumps(result, ensure_ascii=False), flush=True)
            self.runner(['/usr/sbin/liuqin-ui-power', action], check=True, timeout=8)

    @contextlib.contextmanager
    def operation_lock(self):
        fd = os.open(self.root/'run/liuqin-android-operation.lock',
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+b') as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise RuntimeError('无效的操作锁。')
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('设备操作正在进行，请稍后重试。') from None
            yield

    def confirm(self, kind, value, **details):
        token = secrets.token_hex(16)
        self.pending = {token: dict(kind=kind, boot_id=value['boot_id'],
                                   expires=time.monotonic()+60, **details)}
        return token

    def consume_confirmation(self, kind, token):
        pending = self.pending.get(token) if isinstance(token, str) else None
        if not pending or pending['kind'] != kind or time.monotonic() > pending['expires']:
            raise RuntimeError('确认已过期，请重新选择操作。')
        return self.pending.pop(token)

    def supports_switch(self, value):
        manifest = gate.marker(self.root) or {}
        return (gate.capable(manifest) and manifest.get('boot_switch_version') == 1
                and manifest.get('tablet_boot_switch_version') == 1
                and manifest['build_id'] == value['build_id'])

    def check_switch(self):
        value = self.check_exit()
        if not self.supports_switch(value):
            raise RuntimeError('当前 Recovery 版本不支持平板端切换。')
        battery = value.get('battery', '')
        if not re.fullmatch(r'\d+%', battery) or int(battery[:-1]) < 30:
            raise RuntimeError('电量未知或低于 30%，请充电后再切换。')
        memory = status.read(self.root/'proc/meminfo')
        available = re.search(r'^MemAvailable:\s+(\d+)\s+kB$', memory, re.MULTILINE)
        if not available or int(available[1]) < 768*1024:
            raise RuntimeError('可用内存不足，无法校验和暂存引导镜像。')
        return value

    def switch_backend(self, value):
        native = load_module('native_install', HERE/'native-install.py')
        agent = load_module('android_device', HERE/'android-device.py')
        return native, agent.Hardware(value['boot_id'], value['serial']), agent

    def systems(self):
        with self.operation_lock():
            value = self.check_exit()
            if not self.supports_switch(value):
                return dict(systems=[], can_switch=False, message='请使用支持平板端切换的 Recovery。')
            native, hardware, agent = self.switch_backend(value)
            registry = native.switch_state(hardware, agent).get('registry') or {}
        default = registry.get('default_id')
        rows = []
        for system in registry.get('systems', []):
            kind = system['type']
            name = display_name(system['name'], 'Android' if kind == 'android' else 'Linux')
            rows.append(dict(id=system['id'], name=name, type=kind, is_default=system['id']==default,
                             partition=system['partition']['name'] if kind == 'linux' else 'userdata',
                             filesystem=system['target']['root_fstype'] if kind == 'linux' else ''))
        rows.sort(key=lambda row: not row['is_default'])
        return dict(systems=rows, can_switch=bool(default),
                    message=('' if default else '请在电脑安装器中登记默认系统。' if rows
                             else '暂无已登记的系统。'))

    def prepare_switch(self, system_id):
        from liuqin_multiboot import identifier

        with self.operation_lock():
            value = self.check_switch()
            native, hardware, agent = self.switch_backend(value)
            request = dict(system_id=identifier(system_id), transaction=str(uuid.uuid4()),
                           serial=value['serial'], boot_id=value['boot_id'])
            plan = native.preview_switch(request, hardware, agent, value, local=True)['plan']
        token = self.confirm('switch', value, plan=plan)
        return dict(token=token, previous_name=display_name(plan['previous_name'], '当前系统'),
                    target_name=display_name(plan['target_name'], '所选系统'),
                    previous_partition=plan['previous_partition'], target_partition=plan['target_partition'])

    def execute_switch(self, token):
        pending = self.consume_confirmation('switch', token)
        with self.operation_lock():
            value = self.check_switch()
            if value['boot_id'] != pending['boot_id']:
                raise RuntimeError('启动会话已经变化。')
            native, hardware, agent = self.switch_backend(value)
            plan = pending['plan']
            native.require_store_ready(hardware)
            scope = gate.scope(dict(kind='switch', switch_plan=plan))
            lease_token = secrets.token_hex(16)
            gate.begin(dict(token=lease_token, plan_id=gate.digest(plan), scope=scope,
                            boot_id=value['boot_id'], serial=value['serial'],
                            build_id=value['build_id']), value, self.root)
            prior_token = os.environ.get(gate.TOKEN)
            success = False
            try:
                result = native.apply_switch(dict(token=lease_token, plan=plan), hardware, agent,
                                             value, local=True)
                if (result.get('default_id') != plan['system_id'] or
                        result.get('boot_readback_verified') is not True):
                    raise RuntimeError('默认引导回读未确认。')
                success = True
            finally:
                if success:
                    backend = load_module('native_store', HERE/'native-store.py')
                    backend.auto_cleanup(dict(token=lease_token,success=True),hardware,agent,native,value,self.root)
                if prior_token is None:
                    os.environ.pop(gate.TOKEN, None)
                else:
                    os.environ[gate.TOKEN] = prior_token
                finished = gate.finish(dict(token=lease_token, success=success), self.collect(), self.root)
            if finished['state'] != 'complete':
                raise RuntimeError('切换已写入，请留在 Recovery 检查存储状态。')
        return dict(default_id=plan['system_id'], target_name=display_name(plan['target_name'], '所选系统'))

    def dispatch(self,request):
        if not isinstance(request,dict):raise ValueError('请求格式无效。')
        op=request.get('op')
        self.collect()
        if op=='status':return self.summary()
        if op=='partitions':return dict(partitions=self.partitions())
        if op=='logs':return dict(text=self.logs(request.get('kind','kernel')))
        if op=='export_logs':return self.export_logs()
        if op == 'systems':
            return self.systems()
        if op == 'prepare_switch':
            return self.prepare_switch(request.get('system_id'))
        if op == 'execute_switch':
            return self.execute_switch(request.get('token'))
        if op=='brightness':
            percent=request.get('percent')
            if type(percent)!=int or not 10<=percent<=100:raise ValueError('亮度范围为 10–100%。')
            path=self.root/'sys/class/backlight/ktz8866-backlight'
            maximum=int((path/'max_brightness').read_text());level=max(1,maximum*percent//100)
            (path/'brightness').write_text(str(level)+'\n')
            return dict(brightness=percent)
        if op=='prepare_exit':
            action=request.get('action')
            if action not in EXITS:raise ValueError('未知电源操作。')
            value=self.check_exit();token=self.confirm('exit', value, action=action)
            return dict(token=token,action=action,expires_seconds=60)
        if op=='execute_exit':
            pending=self.consume_confirmation('exit', request.get('token'))
            self.exit_recovery(EXITS[pending['action']], pending['boot_id'])
            return dict(message='正在退出 Recovery')
        raise ValueError('不支持此维护操作。')


def display_name(name, fallback):
    # A system name is shown in a one-line selection and confirmation label.
    if not isinstance(name, str):
        return fallback
    return ''.join(c for c in name[:96] if not unicodedata.category(c).startswith('C')).strip() or fallback


def request(op):
    # Host export entry only; power operations require the UI's two-step IPC.
    if op not in ('status','partitions','logs','export_logs'):raise ValueError('read-only request expected')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.settimeout(15);sock.connect(SOCKET);sock.sendall(json.dumps(dict(op=op)).encode()+b'\n')
        result=bytearray()
        while not result.endswith(b'\n'):
            part=sock.recv(65536)
            if not part or len(result)+len(part)>256*1024:raise RuntimeError('服务返回无效。')
            result.extend(part)
    value=json.loads(result)
    if not value.get('ok'):raise RuntimeError(value.get('error','操作失败。'))
    return value['result']


def supervise_ui(server, child):
    """Keep IPC operations on the main thread, including BCB signal guards."""
    stopping = threading.Event()
    def stop(_sig, _frame):
        stopping.set()
        if child.poll() is None:
            child.terminate()
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    def watch_ui():
        result = child.wait()
        print('LVGL UI exited:', result, flush=True)
        # shutdown() must run outside the thread running serve_forever().
        server.shutdown()
    watcher = threading.Thread(target=watch_ui, daemon=True)
    watcher.start()
    try:
        server.serve_forever()
    finally:
        if child.poll() is None:
            child.terminate()
        watcher.join(timeout=5)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return stopping.is_set()


def serve(supervise=True):
    if os.geteuid()!=0 or os.uname().machine!='aarch64':raise RuntimeError('not ARM64 Recovery root')
    operations=Operations();operations.collect()
    owner=open('/run/liuqin-native-ui.lock','a+b')
    fcntl.flock(owner.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        operations.capture_bcb()
    except (OSError, ValueError, RuntimeError) as error:
        # A missing startup snapshot disables cleanup for this boot.
        print('Recovery BCB snapshot unavailable:', error, flush=True)
    path=Path(SOCKET)
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):raise RuntimeError('invalid UI socket path')
        path.unlink()
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(5)
            try:
                line=self.rfile.readline(4097)
                if len(line)>4096 or not line.endswith(b'\n'):raise ValueError('请求过大。')
                with operations.guard:result=operations.dispatch(json.loads(line))
                value=dict(ok=True,result=result)
            except Exception as error:value=dict(ok=False,error=str(error)[:500])
            self.wfile.write(json.dumps(value,ensure_ascii=False).encode()+b'\n')
    with socketserver.UnixStreamServer(SOCKET,Handler) as server:
        os.chmod(SOCKET,0o600)
        Path('/run/liuqin-native-ui-service.pid').write_text(str(os.getpid())+'\n')
        usb_stop=threading.Event()
        def update_usb():
            while not usb_stop.is_set():
                try:operations.publish_usb_status()
                except (OSError,ValueError) as error:print('USB status unavailable:',error,flush=True)
                usb_stop.wait(.5)
        usb_worker=threading.Thread(target=update_usb,daemon=True)
        usb_worker.start()
        try:
            if not supervise:server.serve_forever();return
            Path('/proc/sys/kernel/printk').write_text('3 4 1 7\n')
            child=subprocess.Popen(['/usr/bin/liuqin-recovery-ui'])
            stopping=supervise_ui(server,child)
        finally:
            usb_stop.set()
            usb_worker.join(timeout=2)
    path.unlink(missing_ok=True)
    if supervise and stopping:return
    # Restore a visible status console if the optional graphical UI fails.
    subprocess.run(['/bin/busybox','chvt','1'],check=False)
    subprocess.run(['/usr/bin/python3','-I','-B',str(HERE/'native-status.py'),'serve'],check=False)


if __name__=='__main__':
    if sys.argv[1:]==['supervise']:serve()
    elif sys.argv[1:]==['serve']:serve(False)
    elif len(sys.argv)==3 and sys.argv[1]=='request':print(json.dumps(request(sys.argv[2]),ensure_ascii=False))
    else:raise SystemExit('usage: supervise | serve | request OP')
