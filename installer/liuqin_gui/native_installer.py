# SPDX-License-Identifier: MIT
"""Native candidate integration: scoped writes, in-RAM boot commit, no reboots."""
import contextlib,hashlib,http.server,json,shlex,uuid
from pathlib import Path
from liuqin_native_install import capable,scope,WRITE_ACTIONS,TOKEN
from .engine import InstallerError,canonical,digest_file,DEVICE_SCRIPTS

class NativeInstallerMixin:
    @property
    def installer_enabled(self):return capable(self.engine.manifest.get('native_recovery_manifest',{}))

    def native_rpc(self,action,request,image=None):
        if not self.installer_enabled:raise InstallerError('当前 Rec 没有安装能力，请使用独立安装候选。')
        token=uuid.uuid4().hex;payload={};peer=self.http_peer
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if self.client_address[0]!=peer:self.send_error(404);return
                if self.path=='/'+token+'/request':
                    data=payload['request'];self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                elif self.path=='/'+token+'/boot' and image is not None:
                    with Path(image).open('rb') as f:
                        self.send_response(200);self.send_header('Content-Length',str(Path(image).stat().st_size));self.end_headers()
                        while b:=f.read(1024**2):self.wfile.write(b)
                else:self.send_error(404)
        with self.http_server(Handler) as endpoint:
            request=dict(request)
            if image is not None:request['url']=endpoint+'/'+token+'/boot'
            payload['request']=canonical(request);digest=hashlib.sha256(payload['request']).hexdigest()
            directory='/run/liuqin-native-host/'+token;path=directory+'/request'
            self.remote('mkdir -p '+directory+' && /bin/busybox wget -O '+path+' '+endpoint+'/'+token+'/request',120)
            self.remote('chmod 0600 '+path)
            raw=self.remote('/usr/bin/python3 -I -B /usr/lib/liuqin/native-install.py '+shlex.join([action,path,digest]),600)
            return json.loads(raw)

    def require_native_write(self):
        s=getattr(self.engine,'native_write_session',None)
        if not self.installer_enabled or not s or not s.active or s.boot_id!=self.boot_id:raise InstallerError('尚未确认绑定当前 Rec 的完整安装计划。')
        return s

    def write_native_boot(self,image,expected,previous):
        s=self.require_native_write();image=Path(image)
        if image.stat().st_size!=self.engine.device['boot_bytes'] or digest_file(image)!=expected or self.current_boot_digest()!=previous:raise InstallerError('新旧 boot 材料或设备状态变化。')
        e=self.engine;e.receipt.update(boot_flash_started=True,boot_expected_sha256=expected,boot_finalization_pending=True);e.save_receipt();e.phase('flash_boot')
        result=self.native_rpc('boot-write',dict(token=s.token,sha256=expected,previous_sha256=previous,bytes=image.stat().st_size),image)
        if not result.get('boot_written') or not result.get('boot_readback_verified') or result.get('sha256')!=expected:raise InstallerError('Rec 未确认 boot_a 的完整回读。')
        e.receipt.update(boot_written=True,boot_readback_verified=True,active_slot_verified=True,active_slot_set=False,boot_finalization_pending=False,recovery_retained=True);e.save_receipt()

    def validate_linux_install(self):
        if not self.installer_enabled:raise InstallerError('只读 Rec 不能安装 rootfs。')
        expected=self.engine.manifest['native_recovery_manifest']['payload_sha256']['usr/lib/liuqin/install-root.sh']
        if digest_file(DEVICE_SCRIPTS/'install-root.sh')!=expected:raise InstallerError('电脑 rootfs 安装脚本与 Rec 中绑定的版本不同。')

    def install_root(self,*args,**kwargs):
        self.validate_linux_install()
        self.require_native_write()
        return super().install_root(*args,**kwargs)

    def root_command_prefix(self):
        return TOKEN+'='+self.require_native_write().token+' '

    def validate_android_install(self):
        if not self.installer_enabled:raise InstallerError('只读 Rec 不能安装 Android。')
        anti=self.engine.device.get('anti_version');profile=self.engine.android_profile
        if profile.get('generic_ota_version') in (1,2):
            if self.engine.manifest['native_recovery_manifest'].get('generic_ota_version',0)<profile['generic_ota_version'] or (profile['generic_ota_version']==1 and (type(anti) is not int or anti<0)):raise InstallerError('通用 OTA 需要新版执行器及本次 Fastboot anti 观测；不会猜测回滚状态。')
            self.engine.ota_device_check=self.android_rpc('android-check',{})
            if not self.engine.ota_device_check.get('compatible'):raise InstallerError('通用 OTA 设备检查未通过。')
            return
        if self.engine.device.get('native_ota_only'):raise InstallerError('当前固件仅允许经过逐项比对的通用 OTA，不能套用固定 ROM 的固件规则。')
        if type(anti) is not int or anti>profile['anti_version']:raise InstallerError('缺少本次启动绑定的 Fastboot anti 观测，或设备防回滚等级不兼容。请经安装器从 Fastboot 临时启动此候选。')

class Session:
    def __init__(self,engine,snapshot):
        self.engine=engine;self.transport=engine.transport;self.boot_id=self.transport.boot_id;self.token=uuid.uuid4().hex;self.active=False
        self.firmware_sha256=snapshot['native_firmware_sha256']
        self.request=dict(token=self.token,plan_id=engine.plan_id,boot_id=self.boot_id,serial=engine.device['serial'],
            build_id=engine.manifest['native_recovery_manifest']['build_id'],scope=scope(snapshot))
        self.fixed_image=snapshot.get('fixed_recovery',{}).get('path')
        if snapshot.get('android_profile_sha256'):self.request['android_profile']=engine.android_profile
    def start(self):
        if getattr(self.engine,'native_write_session',None):raise InstallerError('已有原生安装确认尚未结束。')
        self.transport.preflight(require_archive=False)
        if hashlib.sha256(canonical(self.engine.receipt['firmware_fingerprint'])).hexdigest()!=self.firmware_sha256:raise InstallerError('固件在确认方案后发生变化，未开始写入。')
        self.engine.native_write_session=self;self.active=True
        self.engine.receipt['native_session']=dict(token=self.token,plan_id=self.request['plan_id'],boot_id=self.boot_id);self.engine.save_receipt()
        result=self.transport.native_rpc('begin',self.request)
        if result.get('state')!='active':raise InstallerError('Rec 未接受安装确认。')
        self.engine.receipt['native_scope_sha256']=hashlib.sha256(canonical(self.request['scope'])).hexdigest();self.engine.save_receipt()
        fixed=self.request['scope'].get('fixed_recovery')
        if fixed:
            self.engine.phase('revalidate',detail='校验并暂存固定 Recovery 与回退镜像')
            result=self.transport.native_rpc('recovery-stage',dict(token=self.token),self.fixed_image)
            if not result.get('staged') or result.get('sha256')!=fixed['image_sha256']:
                raise InstallerError('固定 Recovery 尚未完整暂存，未开始安装。')
            self.engine.receipt['recovery_staged']=True;self.engine.save_receipt()
    def finish(self,success):
        if not self.active:return
        result=self.transport.native_rpc('finish',dict(token=self.token,success=success))
        self.active=False;self.engine.native_write_session=None
        if 'retention' in result:self.engine.receipt['boot_store_retention']=result['retention']
        self.engine.receipt.update(native_install_state=result['state'],recovery_retained=True);self.engine.save_receipt()
        self.transport.status('complete' if result['state']=='complete' else 'error')
        if success and result['state']!='complete':raise InstallerError('写入后存储未确认重新只读；保持 Rec。')
