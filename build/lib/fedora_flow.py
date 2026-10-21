# SPDX-License-Identifier: MIT
"""Fedora target stages; the independent RAM installer retains its own runtime."""
import copy
import json
from pathlib import Path
import shutil
import sys
from source_flow import Flow, load_module
from source_build import PROJECT, download, sha, tree_identity


class FedoraFlow(Flow):
    def __init__(self,data,run,*,refresh=False):
        data=copy.deepcopy(data)
        fragment='build/kernel/configs/fedora.config'
        if fragment not in data['kernel_fragments']:data['kernel_fragments'].append(fragment)
        if '-fedora' not in data['kernel_localversion']:data['kernel_localversion']+='-fedora'
        super().__init__(data,run,refresh=refresh,distribution='fedora')
        fragment='build/kernel/configs/firmware-splash.config'
        if 'plymouth' in self.recipe['packages'] and fragment not in self.data['kernel_fragments']:
            # Keep explicit custom fragments after the distribution defaults.
            # kernel_fragments() inserts the common native fragment first.
            native='build/kernel/configs/native-display.config'
            index=self.data['kernel_fragments'].index(native)+1 if native in self.data['kernel_fragments'] else 0
            self.data['kernel_fragments'].insert(index,fragment)
        self.code['build/lib/fedora_flow.py']=sha(PROJECT/'build/lib/fedora_flow.py')

    def target_base(self):
        return download(self.source['url'],PROJECT/'inputs/fedora-base'/self.source['archive'],self.source['sha256'])

    def target_action(self,action,archive,out,**values):
        argv=[sys.executable,str(PROJECT/'build/scripts/build-fedora-root.py'),action,
              '--archive',str(archive),'--archive-sha256',sha(archive),'--out',str(out)]
        for name,value in values.items():argv+=['--'+name.replace('_','-'),str(value)]
        self.run(argv,root=True)

    def target_runtime_root(self):
        archive=self.target_base()
        def build(out):self.target_action('runtime',archive,out/'assembled')
        return self.stage('fedora-runtime-root',dict(base=sha(archive),
            builder=sha(PROJECT/'build/scripts/build-fedora-root.py'),oci=sha(PROJECT/'build/lib/fedora_oci.py'),
            refresh=self.refresh),build,['assembled/root.tar.gz','assembled/packages.tsv'])/'assembled/root.tar.gz'

    def boot_splash_runtime(self):
        if 'plymouth' not in self.recipe['packages']:return None
        archive=self.target_runtime_root()
        def build(out):self.target_action('splash',archive,out/'assembled')
        return self.stage('fedora-splash-runtime',dict(base=sha(archive),
            builder=sha(PROJECT/'build/scripts/build-fedora-root.py'),
            collector=sha(PROJECT/'build/lib/plymouth_runtime.py'),
            splash_assets=sha(PROJECT/'build/lib/splash_assets.py'),
            theme=tree_identity(PROJECT/'device/boot/splash'),refresh=self.refresh),
            build,['assembled/runtime.tar.gz','assembled/runtime.json'])/'assembled'

    def sensors(self):
        archive=self.target_runtime_root();board=self.board()
        def build(out):self.target_action('sensors',archive,out/'assembled',rom_sensors=board/'sensors',jobs=self.data.get('jobs',12))
        return self.stage('fedora-sensors',dict(base=sha(archive),config=tree_identity(board/'sensors'),
            sources=tree_identity(PROJECT/'device/sensors'),overlay=tree_identity(PROJECT/'device/sensors-overlay'),
            builder=sha(PROJECT/'build/scripts/build-liuqin-sensors-stack.sh')),
            build,['assembled/sensor-stack.tar'])/'assembled/sensor-stack.tar'

    def device_debs(self):
        sensors=self.sensors();board=self.board();tools=self.tools()
        def build(out):self.run([sys.executable,'build/scripts/build-liuqin-rpms.py',
            '--firmware',str(PROJECT/'device/firmware'),'--speakers',str(board/'speaker-firmware'),
            '--sensors',str(sensors),'--busybox',tools['BUSYBOX'],'--out',str(out/'rpms'),'--version',self.version])
        return self.stage('fedora-device-rpms',dict(sensors=sha(sensors),
            firmware=tree_identity(PROJECT/'device/firmware'),speakers=tree_identity(board/'speaker-firmware'),
            board_overlay=tree_identity(PROJECT/'device/board-overlay'),
            overlay_builder=sha(PROJECT/'build/lib/device_overlay.py'),busybox=sha(tools['BUSYBOX']),version=self.version,
            builder=sha(PROJECT/'build/scripts/build-liuqin-rpms.py'),packager=sha(PROJECT/'build/lib/rpm_package.py')),
            build,['rpms'])/'rpms'

    def stats_archive(self):
        record=json.loads((PROJECT/'build/rootfs/fedora-stats-source.json').read_text())
        return download(record['url'],PROJECT/'inputs/ksystemstats'/record['archive'],record['sha256'])

    def stats_rpm(self):
        archive=self.target_runtime_root();source=self.stats_archive()
        record=json.loads((PROJECT/'build/rootfs/fedora-stats-source.json').read_text())
        name='ksystemstats-'+record['version']+'-'+record['package_release']+'.aarch64.rpm'
        def build(out):
            self.target_action('stats',archive,out/'assembled',stats_source=source,jobs=self.data.get('jobs',12))
        return self.stage('fedora-adreno-stats',dict(base=sha(archive),source=sha(source),
            source_lock=sha(PROJECT/'build/rootfs/fedora-stats-source.json'),
            builder=sha(PROJECT/'build/lib/fedora_stats.py'),
            packager=sha(PROJECT/'build/lib/rpm_package.py'),
            adaptation=tree_identity(PROJECT/'device/ksystemstats'),refresh=self.refresh),
            build,['assembled/'+name,'assembled/stats-packages.tsv'])/'assembled'/name

    def fingerprint_sources(self):
        lock=json.loads((PROJECT/'device/fingerprint/oem/SOURCE_LOCK.json').read_text())
        return {name:download(lock[name]['source_url'],PROJECT/'inputs/fingerprint-src'/
                    lock[name]['source_url'].rsplit('/',1)[1],lock[name]['orig_sha256'])
                for name in ('libfprint','fprintd')}

    def fingerprint_image(self):
        image=self.resolve(self.data.get('rom_images','inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images'))/'NON-HLOS.bin'
        extractor=load_module('fpc_firmware_extractor',PROJECT/'build/scripts/extract-liuqin-fingerprint-firmware.py')
        return extractor.verify_image(image)

    def fingerprint_rpm(self):
        base=self.target_runtime_root();sources=self.fingerprint_sources()
        name='liuqin-fingerprint-0.1.0-7.liuqin.aarch64.rpm'
        def build(out):
            self.target_action('fingerprint',base,out/'assembled',
                fingerprint_libfprint_source=sources['libfprint'],
                fingerprint_fprintd_source=sources['fprintd'],jobs=self.data.get('jobs',12))
        return self.stage('fedora-fingerprint',dict(base=sha(base),
            sources={key:sha(path) for key,path in sources.items()},
            builder=sha(PROJECT/'build/lib/fedora_fingerprint.py'),
            packager=sha(PROJECT/'build/lib/rpm_package.py'),
            oem=tree_identity(PROJECT/'device/fingerprint/oem'),
            integration=tree_identity(PROJECT/'device/fingerprint/fedora'),
            interfaces=sha(PROJECT/'shared/liuqin_kernel.py'),refresh=self.refresh),
            build,['assembled/'+name])/'assembled'/name

    def apps(self):
        archive=self.target_runtime_root();devices=self.device_debs();stats=self.stats_rpm();fingerprint=self.fingerprint_rpm()
        inputs=dict(base=sha(archive),rpms=tree_identity(devices),recipe=self.recipe,
            builder=sha(PROJECT/'build/scripts/build-fedora-root.py'),
            input_env=sha(PROJECT/'build/rootfs/fedora-input-env.sh'),
            shell_defaults=sha(PROJECT/'build/lib/shell_defaults.py'),
            splash_assets=sha(PROJECT/'build/lib/splash_assets.py'),
            theme=tree_identity(PROJECT/'device/boot/splash'),refresh=self.refresh)
        inputs['stats_rpm']=sha(stats)
        inputs['fingerprint_rpm']=sha(fingerprint)
        if self.recipe.get('overlay'):inputs['overlay']=tree_identity(self.recipe['overlay'])
        if self.recipe.get('password_file'):inputs['password_sha256']=sha(self.recipe['password_file'])
        def build(out):
            rpms=out/'rpms';shutil.copytree(devices,rpms)
            shutil.copyfile(stats,rpms/stats.name)
            shutil.copyfile(fingerprint,rpms/fingerprint.name)
            snapshot=dict(self.recipe)
            if snapshot.get('overlay'):
                target=out/'overlay';shutil.copytree(snapshot['overlay'],target,symlinks=True)
                if tree_identity(target)!=inputs['overlay']:raise ValueError('overlay 在暂存期间变化。')
                snapshot['overlay']=str(target)
            if snapshot.get('password_file'):
                source=Path(snapshot['password_file'])
                if source.is_symlink() or source.stat().st_mode&0o077:raise ValueError('密码输入必须是私有文件。')
                target=out/'.password';target.write_bytes(source.read_bytes());target.chmod(0o600)
                if sha(target)!=inputs['password_sha256']:raise ValueError('密码文件在暂存期间变化。')
                snapshot['password_file']=str(target)
            recipe=out/'recipe.json';recipe.write_text(json.dumps(snapshot)+'\n')
            self.target_action('apps',archive,out/'assembled',recipe=recipe,rpms=rpms)
        artifacts=['assembled/root.tar.gz','assembled/packages.tsv']
        if not self.recipe['first_boot_setup']:artifacts.append('assembled/login.txt')
        return self.stage('fedora-apps',inputs,build,artifacts)/'assembled'

    def kernel_debs(self):
        source,kernel=self.kernel();modules=self.modules();boot=self.boot()
        if 'CONFIG_SECURITY_SELINUX=y' not in (kernel/'.config').read_text().splitlines():
            raise ValueError('Fedora 目标内核缺少 SELinux 支持。')
        def build(out):self.run([sys.executable,'build/scripts/build-liuqin-kernel-debs.py',
            '--kernel-source',str(source),'--kernel-out',str(kernel),'--modules-dir',str(modules),
            '--boot-image',str(boot),'--out',str(out/'rpms'),'--version',self.version,'--format','rpm'])
        return self.stage('fedora-kernel-rpms',dict(boot=sha(boot),modules=sha(modules/'kernel.build.json'),
            version=self.version,builder=sha(PROJECT/'build/scripts/build-liuqin-kernel-debs.py'),
            packager=sha(PROJECT/'build/lib/rpm_package.py'),tools=sha(PROJECT/'device/boot/boot-update.py'),
            interfaces=tree_identity(PROJECT/'shared')),build,['rpms'])/'rpms'

    def rootfs(self):
        firmware=self.fingerprint_image()
        apps=self.apps();rpms=self.kernel_debs();source,kernel=self.kernel();modules=self.modules()
        def build(out):
            self.target_action('kernel-files',apps/'root.tar.gz',out/'assembled',rpms=rpms)
            self.run([sys.executable,'build/scripts/extract-liuqin-fingerprint-firmware.py',
                '--image',str(firmware),'--root',str(out/'assembled/rootfs')],root=True)
            self.run([sys.executable,'build/scripts/build-liuqin-custom-root.py','--root',str(out/'assembled/rootfs'),
                '--kernel-out',str(kernel),'--modules-dir',str(modules),'--out',str(out/'packed'),'--selinux-labels'],root=True)
        return self.stage('fedora-rootfs',dict(apps=sha(apps/'root.tar.gz'),rpms=tree_identity(rpms),
            builder=sha(PROJECT/'build/scripts/build-liuqin-custom-root.py'),interfaces=tree_identity(PROJECT/'shared'),
            fingerprint_firmware=sha(firmware),
            fingerprint_extractor=sha(PROJECT/'build/scripts/extract-liuqin-fingerprint-firmware.py'),
            fingerprint_hashes=sha(PROJECT/'device/fingerprint/oem/firmware/SHA256.json'),
            fingerprint_provisioner=sha(PROJECT/'device/fingerprint/oem/src/fingerprint/deployment/provision_firmware.py')),
            build,['packed/rootfs.tar.gz','packed/native-root.contract'])/'packed'

    def plan(self):
        result=super().plan();result['steps'][0]='官方 Fedora 44 ARM64 基础系统'
        result['output']='out/releases/fedora-plasma-<内容标识>/'
        result['installer_base']=self.installer_source
        result['kernel_configuration']='额外启用 Fedora SELinux 片段；图形启动包含固件帧缓冲配置'
        result['package_format']='rpm'
        if self.recipe['first_boot_setup']:
            result['steps']=[step.replace('登录前初始化组件（独立 ARM64 包）','Fedora 官方 plasma-setup 软件包') for step in result['steps']]
        result['first_boot_setup_source']='Fedora official RPM'
        return result
