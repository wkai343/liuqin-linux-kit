# SPDX-License-Identifier: MIT
"""ADB transport for the native Linux Recovery; no USB IP interface required."""
import base64,contextlib,http.server,json,os,re,shlex,subprocess,threading,time,uuid
from pathlib import Path
from . import host
from .engine import InstallerError,digest_file

GATEWAY='/usr/lib/liuqin/native-adb-shell.py'

class ReverseBindError(InstallerError):
    pass

def reverse_reply(raw,port):
    # `adb raw` returns the device service's own status, including FAIL even
    # when the host CLI exits successfully. Do not confuse the two statuses.
    if len(raw)>4096 or len(raw)<8 or raw[:4] not in (b'OKAY',b'FAIL') or not re.fullmatch(b'[0-9a-fA-F]{4}',raw[4:8]):
        raise InstallerError(f'ADB 环回映射返回了无效响应（{len(raw)} 字节，前缀 {raw[:16].hex() or "空"}）。')
    body=raw[8:]
    if len(body)!=int(raw[4:8],16):raise InstallerError('ADB 环回映射响应不完整。')
    if raw[:4]==b'FAIL':raise ReverseBindError(body.decode(errors='replace'))
    if body!=str(port).encode('ascii'):raise InstallerError('ADB 环回映射端口与请求不一致。')
    return port

def features(raw):
    return set(re.split(r'[,\s]+',raw.decode('ascii').strip()))

def probe_adb(serial,timeout=8):
    result=host.run_process([host.adb_path(),'-s',serial,'shell','-T','/usr/bin/python3','-I','-B','/usr/lib/liuqin/native-status.py','probe'],capture_output=True,timeout=timeout)
    if result.returncode or len(result.stdout)>256*1024:raise InstallerError('ADB 中没有就绪的原生 Recovery。')
    value=json.loads(result.stdout)
    if (value.get('interface')!='liuqin-native-recovery-v1' or value.get('device')!='liuqin' or value.get('serial')!=serial or value.get('usb_transport')!='adb'):raise InstallerError('ADB 设备身份或原生接口不匹配。')
    uuid.UUID(value['boot_id']);return value

class NativeAdbMixin:
    http_peer='127.0.0.1'
    def __init__(self,engine,serial):
        manifest=engine.manifest.get('native_recovery_manifest',{})
        if manifest.get('native_adb_version')!=1 or manifest.get('usb_transport')!='adb':
            raise InstallerError('当前安装器仅支持 ADB Rec；旧 USB 网络镜像请保留作历史备份，手动回到 Fastboot 后选择新版。')
        super().__init__(engine,serial)
        self.adb_path=host.adb_path()
    def adb(self,*args,timeout=30):
        command = [self.adb_path, '-s', self.serial, *args]
        if args[:1] == ('raw',):
            # adb raw forwards stdin EOF to the service before its response
            # may arrive. Keep a private, empty pipe open until the service
            # closes, without borrowing the GUI worker's JSON input stream.
            read_fd, write_fd = os.pipe()
            try:
                p = host.run_process(command, stdin=read_fd, capture_output=True, timeout=timeout)
            finally:
                os.close(read_fd)
                os.close(write_fd)
        else:
            p = host.run_process(command, input=b'', capture_output=True, timeout=timeout)
        if p.returncode:
            error=(p.stderr+p.stdout).decode(errors='replace')[-2500:]
            if args[:1]==('raw',) and 'unknown command' in error.lower():
                raise InstallerError('当前 adb 不支持原生 Rec 的环回映射，请更新 Android Platform-Tools。'+error)
            raise InstallerError(error)
        return p.stdout
    def probe_current(self,timeout=8):return probe_adb(self.serial,timeout)
    def remote(self,text,timeout=60,observe=None):
        if not self.boot_id:raise InstallerError('没有绑定的 ADB Rec 启动会话。')
        if not getattr(self,'adb_v2_verified',False):
            if 'shell_v2' not in features(self.adb('features',timeout=5)):raise InstallerError('ADB 缺少 shell_v2；不会执行安装命令。')
            self.adb_v2_verified=True
        from .recovery_transport import RecoveryTransport
        encoded=base64.b64encode(text.encode()).decode('ascii')
        return RecoveryTransport.shell(self,['/usr/bin/python3','-I','-B',GATEWAY,self.boot_id,encoded],timeout,observe)
    @contextlib.contextmanager
    def http_server(self,handler):
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler);server.daemon_threads=True;remote_port=None;started=False
        try:
            # Bare tcp:0 asks adbd to bind IPv6 ANY, unavailable in this kernel.
            # Its localhost service explicitly prefers IPv4. The ordinary CLI
            # rejects host-qualified reverse specs, so use its raw service entry.
            # Port 0 with a hostname is also rejected by adbd: choose an IPv4
            # port in the bound RAM session and retry only genuine bind races.
            choose="import socket; s=socket.socket(socket.AF_INET); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()"
            for attempt in range(4):
                # Network-free init leaves lo down. Only raise this RAM-local
                # interface, never a USB NIC, route or external address.
                value=self.remote('/bin/busybox ip link set dev lo up && /usr/bin/python3 -I -B -c '+shlex.quote(choose),timeout=10).decode().strip()
                if not value.isascii() or not value.isdigit() or not 1024<=int(value)<=65535:raise InstallerError('Rec 未返回有效的 IPv4 环回端口。')
                port=int(value)
                reply=self.adb('raw',f'reverse:forward:norebind:tcp:localhost:{port};tcp:{server.server_port}',timeout=10)
                # An OKAY proves this attempt created the listener, even if a
                # malformed payload below makes us abort before any transfer.
                if reply.startswith(b'OKAY'):remote_port='tcp:'+str(port)
                try:reverse_reply(reply,port)
                except ReverseBindError as error:
                    if str(error) in ('cannot bind listener: Address already in use','cannot rebind existing socket') and attempt<3:continue
                    raise InstallerError('ADB 无法建立 IPv4 环回传输：'+str(error)) from error
                break
            threading.Thread(target=server.serve_forever,daemon=True).start();started=True
            yield 'http://127.0.0.1:'+str(port)
        finally:
            if started:server.shutdown()
            server.server_close()
            if remote_port:
                try:self.adb('reverse','--remove',remote_port)
                except (RuntimeError,OSError,subprocess.SubprocessError):pass
    def boot_adb_ram(self):
        image=self.engine.installer_snapshot
        if digest_file(image)!=self.engine.manifest['files'][self.engine.manifest.get('runtime_file','native-recovery.img')]:raise InstallerError('ADB Rec 镜像发生变化。')
        self.boot_id=None;self.fastboot('boot',str(image));self.ram_active=True;self.engine.phase('recovery_adb')
        deadline=time.monotonic()+120;last=''
        while time.monotonic()<deadline:
            self.engine.check_cancel()
            try:
                value=self.probe_current(timeout=5);self.validate_probe(value)
                if 'shell_v2' not in features(self.adb('features',timeout=5)):raise InstallerError('ADB 缺少可靠返回退出状态的 shell_v2。')
                self.boot_id=value['boot_id'];self.verify_ram_interface();return
            except (OSError,RuntimeError,ValueError,KeyError,subprocess.SubprocessError) as error:last=str(error)
            self.engine.pause(1)
        raise InstallerError('ADB Rec 未在等待时间内就绪；保持设备现状。'+last[-500:])
