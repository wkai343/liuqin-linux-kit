# SPDX-License-Identifier: MIT
"""Paired Recovery session over ADB shell v2 and loopback-only ADB reverse."""
import base64,contextlib,http.server,json,queue,re,subprocess,threading,time,uuid
from . import host
from .engine import Transport,InstallerError,digest_file

GATEWAY='/system/bin/liuqin-recovery'


class AdbError(InstallerError):
    """A failed ADB command, distinct from a rejected Recovery identity."""
    def __init__(self,output):
        self.output=output
        super().__init__('ADB 操作失败：'+output[-2000:])

    @property
    def reconnecting(self):
        # Retry only USB transport loss, never a missing gateway, permission
        # failure or a failed device/slot/build/unlock check.
        return bool(re.search(r"device (?:'[^'\r\n]+' )?not found|no devices/emulators found|device offline|error: (?:closed|device still connecting)",self.output))


class RecoveryTransport(Transport):
    http_peer='127.0.0.1'

    def __init__(self,engine,serial):
        super().__init__(engine,serial)
        self.adb_path=host.adb_path()

    def adb(self,*arguments,timeout=30):
        result=host.run_process([self.adb_path,'-s',self.serial,*arguments],capture_output=True,timeout=timeout)
        if result.returncode:raise AdbError((result.stderr+result.stdout).decode(errors='replace'))
        return result.stdout

    def probe(self):
        try:
            value=json.loads(self.adb('shell','-T',GATEWAY,'probe',timeout=10))
        except (ValueError,UnicodeError) as error:
            raise InstallerError('Recovery 没有返回有效的配套安装服务信息。') from error
        if not isinstance(value,dict):raise InstallerError('Recovery 安装服务信息格式不正确。')
        if value.get('serial')!=self.serial or value.get('build_id')!=self.engine.manifest['recovery_manifest']['build_id']:
            raise InstallerError('当前 Recovery 的设备或配套版本与电脑端不一致。')
        try:uuid.UUID(value.get('boot_id',''))
        except (ValueError,TypeError,AttributeError) as error:raise InstallerError('Recovery 启动会话标识无效。') from error
        if value.get('slot')!='a':raise InstallerError('配套 Recovery 管理要求 A 槽；当前槽位为 '+str(value.get('slot') or '未知')+'。')
        locked=value.get('locked')
        if locked in ('',None):
            # This TWRP omits ro.boot.flash.locked. Read affirmative AVB
            # evidence from this same boot, without modifying the image or
            # treating a missing property as an unlocked bootloader.
            output=self.adb('shell','-T','getprop ro.boot.vbmeta.device_state; '
                            'getprop ro.boot.verifiedbootstate; cat /proc/sys/kernel/random/boot_id',timeout=10)
            fields=output.decode().splitlines()
            if len(fields)!=3 or fields[2]!=value['boot_id']:
                raise InstallerError('读取解锁状态时 Recovery 会话改变或返回信息不完整。')
            # ORANGE itself is the bootloader's affirmative unlocked state.
            # With AVB verification disabled by the installed ROM, bootloaders
            # may omit vbmeta.device_state. An explicit contradiction still fails.
            if fields[1]!='orange' or fields[0] not in ('','unlocked'):
                raise InstallerError('Recovery 未确认 Bootloader 已解锁（vbmeta.device_state='+repr(fields[0])+', verifiedbootstate='+repr(fields[1])+'）。')
            value['unlock_source']='vbmeta.device_state + verifiedbootstate' if fields[0] else 'verifiedbootstate=orange'
        elif locked!='0':
            raise InstallerError('Recovery 报告 Bootloader 未解锁（ro.boot.flash.locked='+repr(locked)+'）。')
        return value

    def check_device(self):
        # Fastboot independently checks the product, unlock state and rollback
        # level. A running Recovery is first identified before leaving it.
        result=host.run_process([self.adb_path,'devices'],capture_output=True,text=True,timeout=10)
        state=next((f[1] for line in result.stdout.splitlines() if len(f:=line.split())>=2 and f[0]==self.serial),'')
        if state in ('unauthorized','offline','sideload'):
            raise InstallerError('当前 ADB 状态为 '+state+'；请先允许 USB 调试或退出 Sideload，再识别系统。')
        if state in ('device','recovery'):
            from . import compatibility
            environment=self.read_environment()
            if environment['kind']!='recovery':
                self.engine._running_recovery={}
                status=compatibility.assess(environment,self.engine.manifest['recovery_manifest'],self.engine.manifest['files']['recovery.img'])
                self.engine.receipt.update(environment=environment,recovery_compatibility=status);self.engine.save_receipt()
                if not status['ready']:raise InstallerError(status['reason'])
                self.bootstrap_profile=status['profile_id']
                self.engine.emit('notice',message='固件与配套 Recovery 匹配，正在进入 Bootloader Fastboot。')
                self.adb('reboot','bootloader')
                self.wait_bootloader()
                return super().check_device()
            value=self.probe();self.ram_active=True;self.boot_id=value['boot_id']
            ready=json.loads(self.enter_recovery(value['build_id']).splitlines()[-1])
            if ready.get('ready') is not True or ready.get('boot_id')!=self.boot_id or ready.get('serial')!=self.serial:
                raise InstallerError('当前 Recovery 未确认维护会话，保留现场。')
            self.verify_ram_interface()
            self.runtime_verified=self.boot_id
            compatibility.remember_running(self)
            if getattr(self.engine,'receipt',{}).get('operation')=='scan-partitions':
                size=int(self.remote('/bin/busybox blockdev --getsize64 /dev/disk/by-partlabel/boot_a').strip())
                if size!=192*1024**2:raise InstallerError('boot_a 容量不符合 liuqin，停止读取。')
                self.inspection_attached=True
                return dict(product='liuqin',serial=self.serial,unlocked='yes',**{'current-slot':value['slot']},boot_bytes=size)
            self.return_fastboot()
        return super().check_device()

    def read_environment(self):
        from .compatibility import PROBE,parse_probe
        return parse_probe(self.adb('shell','-T',PROBE,timeout=15),self.serial)

    def wait_bootloader(self):
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            self.engine.check_cancel()
            result=host.run_process([self.fastboot_path,'devices'],capture_output=True,text=True,timeout=10,check=True)
            if any(line.split()[:2]==[self.serial,'fastboot'] for line in result.stdout.splitlines()):return
            self.engine.pause(1)
        raise InstallerError('等待同一设备进入 Bootloader Fastboot 超时；没有下发镜像启动命令。')

    def wait_recovery(self,timeout=120):
        deadline=time.monotonic()+timeout;last='设备尚未出现在 ADB 中。';reported=None
        while time.monotonic()<deadline:
            self.engine.check_cancel()
            try:
                state=self.adb('get-state',timeout=5).decode().strip()
                if state in ('device','recovery'):
                    if state=='device':
                        environment=self.read_environment()
                        if environment['kind']!='recovery':
                            self.engine.receipt['recovery_boot_observed_environment']=environment;self.engine.save_receipt()
                            raise InstallerError('临时 Recovery 未建立连接，设备当前运行 '+environment['label']+'。已停止，不自动重试启动；请核对配套镜像与底层固件。')
                    return self.probe()
                if state not in ('offline','unknown'):
                    raise InstallerError('ADB 当前状态为 '+repr(state)+'，无法建立 Recovery 安装会话。')
                last='ADB 状态：'+state
            except AdbError as error:
                if not error.reconnecting:raise
                last=str(error)
            except subprocess.TimeoutExpired:
                last='ADB 暂未响应，正在等待 USB 重新连接。'
            if last!=reported:
                self.engine.emit('notice',message='正在等待 Recovery ADB：'+last[-500:]);reported=last
            self.engine.pause(1)
        raise InstallerError('临时 Recovery 未建立 ADB 连接；启动命令被接受不代表镜像已启动。未执行本阶段的分区写入，也不会自动重试。'+last[-500:])

    def require_boot_compatibility(self):
        from . import compatibility
        if not getattr(self,'bootstrap_profile',None) and not compatibility.has_running_witness(self):
            raise InstallerError('没有当前固件与这份 Recovery 匹配的启动依据。请先在 Android 中识别系统，或连接已运行的配套 Recovery；未下发临时启动命令。')
        if self.getvar('is-userspace')!='no':
            raise InstallerError('未确认处于 Bootloader Fastboot，不能在 fastbootd 或未知模式临时启动；未下发镜像。')

    def boot_ram(self):
        if getattr(self,'inspection_attached',False):return
        from . import compatibility
        self.require_boot_compatibility()
        self.boot_id=None
        if digest_file(self.engine.installer_snapshot)!=self.engine.manifest['files']['recovery.img']:
            raise InstallerError('配套 Recovery 镜像在读取后改变。')
        self.engine.receipt.update(recovery_boot_requested=True,recovery_image_sha256=self.engine.manifest['files']['recovery.img'],
            recovery_build_id=self.engine.manifest['recovery_manifest']['build_id']);self.engine.save_receipt()
        self.clear_boot_evidence()
        output=self.fastboot('boot',str(self.engine.installer_snapshot))
        self.engine.receipt['recovery_boot_command_output']=output[-2500:];self.engine.save_receipt()
        self.ram_active=True
        self.engine.phase('recovery_adb')
        value=self.wait_recovery()
        self.boot_id=value['boot_id']
        self.engine.phase('recovery_session')
        self.engine.emit('notice',message='配套 Recovery 已连接，正在暂停触屏操作并建立独立安装会话。')
        output=self.enter_recovery(value['build_id'])
        result=json.loads(output.splitlines()[-1])
        if result.get('ready') is not True or result.get('serial')!=self.serial or result.get('boot_id')!=self.boot_id:
            raise InstallerError('Recovery 没有确认独立安装会话。')
        if not set(self.engine.manifest['filesystems'])<=set(result.get('filesystems',[])):
            raise InstallerError('Recovery 内核缺少镜像声明的文件系统能力。')
        self.verify_ram_interface()
        self.runtime_verified=self.boot_id
        compatibility.remember_running(self)
        self.bootstrap_profile=None

    def clear_boot_evidence(self):
        self.bootstrap_profile=None;self.engine._running_recovery={};self.runtime_verified=None

    def validate_android_install(self):
        # Replacing vendor_boot/DTBO invalidates proof that the current RAM
        # image boots. Qualify the *destination* full ROM before any edits.
        from .compatibility import profiles
        digest=self.engine.manifest['files']['recovery.img']
        matches=[p for p in profiles() if p['image_sha256']==digest and
            p['build_id']==self.engine.manifest['recovery_manifest']['build_id'] and
            self.engine.android_profile_sha in p.get('android_profile_sha256',[])]
        if len(matches)!=1:
            raise InstallerError('这份 Recovery 尚未验证目标 Android ROM 的底层固件；停止安装规划，避免写入后无法重新进入安装环境。')

    def android_rpc(self,action,request):
        if action in ('install','android-direct'):
            self.validate_android_install()
            self.bootstrap_profile=None
        return super().android_rpc(action,request)

    def enter_recovery(self,build_id):
        for attempt in range(3):
            try:return self.shell([GATEWAY,'enter',build_id],timeout=120)
            except InstallerError as error:
                # Retry the read, never accept a GPT that fails validation and
                # never run a repair command. This handles a transient copy/CRC
                # discrepancy observed immediately after a slot transition.
                if attempt==2 or '读取 GPT 身份时收到警告' not in str(error):raise
                self.engine.emit('notice',message='分区表读取出现校验不一致，正在重新读取；校验仍不通过会停止。')
                self.engine.pause(2 if attempt==0 else 5)

    def shell(self,arguments,timeout=60,observe=None):
        # shell -T uses shell v2: raw bytes, separate protocol streams and an
        # actual remote exit code. exec-out does not guarantee an exit status.
        kwargs=dict(stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        if host.IS_WINDOWS:kwargs['creationflags']=getattr(subprocess,'CREATE_NO_WINDOW',0x08000000)
        process=subprocess.Popen([self.adb_path,'-s',self.serial,'shell','-T',*arguments],**kwargs)
        chunks=queue.Queue();output=bytearray();deadline=time.monotonic()+timeout
        def reader():
            try:
                while block:=process.stdout.read1(65536):chunks.put(block)
            finally:chunks.put(None)
        thread=threading.Thread(target=reader,daemon=True);thread.start()
        try:
            while True:
                self.engine.check_cancel()
                if time.monotonic()>=deadline:raise InstallerError('Recovery 命令超时；请查看写入记录后再操作。')
                try:block=chunks.get(timeout=.1)
                except queue.Empty:continue
                if block is None:break
                output.extend(block)
                if observe:observe(block)
            if process.wait(timeout=10):raise InstallerError('Recovery 命令失败：'+output[-3000:].decode(errors='replace'))
            return bytes(output)
        finally:
            if process.poll() is None:process.kill()
            process.wait();thread.join(timeout=2);process.stdout.close()

    def remote(self,text,timeout=60,observe=None):
        if not self.boot_id:raise InstallerError('没有经过验证的 Recovery 会话。')
        encoded=base64.b64encode(text.encode()).decode('ascii')
        return self.shell([GATEWAY,'run',self.boot_id,encoded],timeout,observe)

    def return_fastboot(self):
        self.ensure_fastboot_names()
        from .compatibility import remember_running
        if self.boot_id and getattr(self,'runtime_verified',None)==self.boot_id:remember_running(self)
        # Also works if enter failed before creating the private root.
        dispatch_error=''
        try:self.adb('shell','-T',GATEWAY,'leave')
        except (AdbError,subprocess.TimeoutExpired) as error:
            # adbd may disappear before shell v2 delivers its exit status.
            # Success is determined by the same serial appearing in Fastboot.
            dispatch_error=str(error)
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            self.engine.check_cancel()
            result=host.run_process([self.fastboot_path,'devices'],capture_output=True,text=True,timeout=10,check=True)
            if any(line.split()[:2]==[self.serial,'fastboot'] for line in result.stdout.splitlines()):
                self.ram_active=False;self.boot_id=None;return
            self.engine.pause(1)
        raise InstallerError('Recovery 返回 Fastboot 超时。'+dispatch_error[-500:])

    def cancel_cleanup(self):
        if getattr(self,'inspection_attached',False):
            self.engine.receipt['recovery_retained']=True;self.engine.save_receipt()
            self.engine.emit('notice',message='只读检查完成，已保持当前 Recovery 会话；没有请求重启。')
            return
        super().cancel_cleanup()

    @contextlib.contextmanager
    def http_server(self,handler):
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler);server.daemon_threads=True
        port='tcp:'+str(server.server_port);forwarded=False;started=False
        try:
            self.adb('reverse','--no-rebind',port,port);forwarded=True
            threading.Thread(target=server.serve_forever,daemon=True).start();started=True
            yield 'http://127.0.0.1:'+str(server.server_port)
        finally:
            if started:server.shutdown()
            server.server_close()
            if forwarded:
                try:self.adb('reverse','--remove',port)
                except (RuntimeError,OSError,subprocess.TimeoutExpired):pass
