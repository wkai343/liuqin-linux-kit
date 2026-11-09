# SPDX-License-Identifier: MIT
"""ADB backend for read-only and scoped-install native Recovery."""
import json
import re
import subprocess
import time
import uuid

from . import host
from .engine import Transport,InstallerError
from .native_installer import NativeInstallerMixin
from .native_adb import NativeAdbMixin

AGENT='/usr/lib/liuqin/native-status.py'


def probe_native(serial,timeout=8):
    from .native_adb import probe_adb
    return probe_adb(serial,timeout)

class NativeRecoveryTransport(NativeAdbMixin,NativeInstallerMixin,Transport):
    def fastboot(self,*args,**kwargs):
        from liuqin_native_install import FIXED_USAGE
        if args and args[0]=='boot' and self.engine.manifest['native_recovery_manifest'].get('usage')==FIXED_USAGE:
            raise InstallerError('固定安装版需要从 Recovery 分区启动；临时启动请使用配套临时镜像。')
        if not args or args[0] not in ('getvar','boot'):
            raise InstallerError('原生 Recovery 测试版仅开放检查，禁止引导刷写、切槽和自动重启。')
        return super().fastboot(*args,**kwargs)

    def validate_probe(self,value):
        m=self.engine.manifest['native_recovery_manifest']
        if (value.get('fixed_installer_version',0)!=m.get('fixed_installer_version',0) or value.get('fixed_recovery_restore_version',0)!=m.get('fixed_recovery_restore_version',0) or value.get('generic_ota_version',0)!=m.get('generic_ota_version',0) or value.get('usb_transport')!='adb' or value.get('serial')!=self.serial or value.get('build_id')!=m['build_id'] or
            value.get('kernel')!=m['kernel_release'] or value.get('slot')!='a' or
            value.get('inspection_only') is not (not self.installer_enabled) or value.get('sealed') is not True or value.get('unlocked') is not True or
            (self.installer_enabled and value.get('installer_version')!=1)):
            raise InstallerError('原生 Recovery 的设备、版本、槽位或存储只读状态不匹配；保留当前环境。')
        uuid.UUID(value['boot_id'])

    def check_device(self):
        operation=self.engine.receipt.get('operation')
        maintenance = operation == 'boot-store' and self.engine.manifest['native_recovery_manifest'].get('boot_store_version') == 1
        if operation!='scan-partitions' and (not self.installer_enabled or (operation not in ('backup','wizard','linux-managed','android','scan-layout') and not maintenance)):
            raise InstallerError('原生 Recovery 当前仅供启动与只读分区检查；安装、格式化和系统切换尚未开放。')
        try:value=self.probe_current()
        except (OSError,RuntimeError,ValueError,KeyError,subprocess.SubprocessError):value=None
        if value and value.get('serial')==self.serial:
            self.validate_probe(value);self.boot_id=value['boot_id'];self.ram_active=True
            self.verify_ram_interface();self.inspection_attached=True
            size=int(self.remote('/bin/busybox blockdev --getsize64 /dev/disk/by-partlabel/boot_a').strip())
            if size!=192*1024**2:raise InstallerError('boot_a 容量不符合 liuqin。')
            result=dict(product='liuqin',serial=self.serial,unlocked='yes',**{'current-slot':'a'},boot_bytes=size)
            if type(value.get('anti_version')) is int:result['anti_version']=value['anti_version']
            return result
        from .compatibility import inspect_device
        environment=inspect_device(self.serial)
        self.engine.receipt['environment']=environment;self.engine.save_receipt()
        if environment['kind']=='android':
            props=environment['properties']
            if 'liuqin' not in (props.get('device'),props.get('vendor_device')):
                raise InstallerError('当前 Android 设备不是 liuqin。')
            self.engine.emit('notice',message='将临时启动原生 Recovery 工程镜像，只检查分区，不安装或修改系统。')
            host.run_process([host.adb_path(),'-s',self.serial,'reboot','bootloader'],capture_output=True,timeout=15,check=True)
            from .recovery_transport import RecoveryTransport
            RecoveryTransport.wait_bootloader(self)
        elif environment['kind']!='fastboot':
            raise InstallerError('请使用 Bootloader Fastboot、已授权的 Android，或已经运行的原生 Recovery。')
        result=super().check_device()
        if self.getvar('is-userspace')!='no':raise InstallerError('原生 Recovery 只能从 Bootloader Fastboot 临时启动，不能使用 fastbootd。')
        return result

    def boot_ram(self):
        if getattr(self,'inspection_attached',False):return
        from liuqin_native_install import FIXED_USAGE
        if self.engine.manifest['native_recovery_manifest'].get('usage')==FIXED_USAGE:
            raise InstallerError('请先在平板进入已安装的固定 Recovery，再读取分区。')
        self.engine.receipt.update(native_recovery_test=True,recovery_boot_requested=True,
            recovery_image_sha256=self.engine.manifest['files']['native-recovery.img'])
        self.engine.save_receipt()
        self.boot_adb_ram()
        probe=json.loads(self.remote('/usr/bin/python3 -I -B '+AGENT+' probe'));self.validate_probe(probe)
        if self.installer_enabled and type(self.engine.device.get('anti_version')) is int:
            self.native_rpc('record-fastboot',dict(boot_id=self.boot_id,serial=self.serial,build_id=probe['build_id'],anti_version=self.engine.device['anti_version']))
        self.status('connected')

    def verify_ram_interface(self):
        super().verify_ram_interface()
        expected=self.engine.manifest['native_recovery_manifest']['payload_sha256']
        from liuqin_native_recovery import ALL_PAYLOADS
        allowed=set(ALL_PAYLOADS)
        if not set(expected)<=allowed:raise InstallerError('原生 Recovery 声明了未知的校验路径。')
        names=sorted(expected);output=[]
        for start in range(0,len(names),6):output+=self.remote('/bin/busybox sha256sum '+' '.join('/'+name for name in names[start:start+6])).decode().splitlines()
        actual={f[1].lstrip('/'):f[0] for line in output if len(f:=line.split())==2}
        if actual!=expected:raise InstallerError('原生 Recovery 启动或状态程序与电脑端镜像不一致。')

    def status(self,phase):
        if phase not in ('ready','connected','reading','complete','error'):raise ValueError('invalid status')
        try:self.remote('/usr/bin/python3 -I -B '+AGENT+' show '+phase,timeout=5)
        except (RuntimeError,OSError,subprocess.SubprocessError):pass

    def preflight(self,require_archive=True):
        if require_archive:raise InstallerError('原生 Rec 使用分区优先的安装流程，不支持旧版整盘安装入口。')
        value=json.loads(self.remote('/usr/bin/python3 -I -B '+AGENT+' probe'))
        self.validate_probe(value)
        from .native_profiles import assess_connected_transport
        snapshot=assess_connected_transport(self,value)
        from .native_profiles import installer_status
        support=installer_status(snapshot,self.engine.manifest['native_recovery_manifest']) if self.installer_enabled else {}
        if self.installer_enabled and self.engine.receipt.get('operation') not in ('scan-partitions','scan-layout','backup'):
            if not support.get('install_allowed'):raise InstallerError(support.get('reason','当前固件未适配原生安装。'))
            if support.get('ota_only') and self.engine.receipt.get('operation') not in ('wizard','android'):raise InstallerError('当前固件只开放通用 OTA 准备检查，不允许其他写入。')
            maintenance = (self.engine.receipt.get('operation')=='boot-store' and
                           self.engine.manifest['native_recovery_manifest'].get('boot_store_version')==1)
            if value.get('phase')=='write-failed' and not maintenance:raise InstallerError('前次写入未安全结束；保留 Rec 并检查记录。')
        self.remote('test "$(cat /sys/class/block/sda/queue/logical_block_size)" = 4096 && '
                    'test "$(/bin/busybox blockdev --getro /dev/sda)" = 1')
        memory=int(self.remote("awk '/^MemAvailable:/ {print $2}' /proc/meminfo").strip())*1024
        battery=value.get('battery','')
        # Battery telemetry is diagnostic in a read-only test, never guessed
        # to satisfy the normal installer's write/format power gate.
        charge=int(battery[:-1]) if re.fullmatch(r'\d+%',battery) else None
        if self.installer_enabled and self.engine.receipt.get('operation') not in ('scan-partitions','scan-layout','backup'):
            if charge is None or charge<30:raise InstallerError('电量未知或低于 30%，不能准备安装。')
            if memory<512*1024**2:raise InstallerError('Rec 可用内存不足。')
        return dict(battery=charge,available_memory_bytes=memory,inspection_only=not self.installer_enabled,native_install_allowed=support.get('install_allowed',False),native_ota_only=support.get('ota_only',False))

    def android_rpc(self,action,request):
        from liuqin_native_install import READ_ACTIONS,WRITE_ACTIONS
        if not self.installer_enabled and action!='partition-inspect':raise InstallerError('原生 Recovery 测试版拒绝修改分区或系统。')
        if self.installer_enabled:
            if action in WRITE_ACTIONS:request=dict(request,native_token=self.require_native_write().token)
            elif action not in READ_ACTIONS:raise InstallerError('不支持该原生安装操作。')
        self.status('reading')
        try:
            result=super().android_rpc(action,request);self.status('complete');return result
        except Exception:
            self.status('error');raise

    def return_fastboot(self):
        raise InstallerError('原生 Recovery 测试版保留当前检查环境，不自动重启。')

    def cancel_cleanup(self):
        if not self.boot_id:return
        self.engine.receipt['recovery_retained']=True;self.engine.save_receipt()
        if not getattr(self.engine,'native_write_session',None):self.status('complete')
        if self.installer_enabled:
            self.engine.emit('notice',message='已保持原生 Recovery；没有自动重启或切槽。')
        else:self.engine.emit('notice',message='已保持原生 Recovery，只读检查结束；没有重启、改写分区或默认系统。')
