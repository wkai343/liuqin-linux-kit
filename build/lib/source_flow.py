# SPDX-License-Identifier: MIT
"""Configuration-driven Ubuntu/Plasma build graph; no tablet access."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from source_build import PROJECT, Cache, download, key, sha, tree_identity
from usb_identity import settings as usb_settings, environment as usb_environment


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def kernel_fragments(data, installer=False):
    prefix='installer_' if installer else ''
    fragments=list(data.get(prefix+'kernel_fragments',[]))
    native='build/kernel/configs/native-display.config'
    if not installer and native not in fragments:
        fragments.insert(0,native)
    return fragments


class Flow:
    def __init__(self, data, run, *, refresh=False, distribution='ubuntu'):
        self.data, self.run = data, run
        self.usb = usb_settings(data)
        self.root_builder=load_module('official_root',PROJECT/'build/scripts/build-official-root.py')
        self.board_builder=load_module('source_board',PROJECT/'build/scripts/prepare-source-board.py')
        self.distribution=distribution
        self.source=json.loads((PROJECT/('build/rootfs/'+distribution+'-source.json')).read_text())
        self.installer_source=json.loads((PROJECT/'build/rootfs/ubuntu-source.json').read_text())
        self.recipe_path=self.path('rootfs_config')
        self.recipe=self.root_builder.recipe(self.recipe_path,distribution)
        self.cache=Cache(PROJECT/'out/build/from-source',run)
        self.memo={}
        self.refresh=str(__import__('time').time_ns()) if refresh else None
        self.version=data.get('package_version','0.1.0')
        if not re.fullmatch(r'[0-9][a-zA-Z0-9.+:~\-]*',self.version):raise ValueError('包版本无效。')
        suffix=data.get('kernel_localversion','-liuqin1')
        if not re.fullmatch(r'-[a-z0-9.+-]*liuqin[a-z0-9.+-]*',suffix):
            raise ValueError('可更新内核后缀需要包含 liuqin，并使用小写字母、数字、点、加号或连字符。')
        self.code={str(p.relative_to(PROJECT)):sha(p) for p in
                   [PROJECT/'build/lib/source_flow.py',PROJECT/'build/lib/source_build.py',PROJECT/'build/lib/aosp_mkbootimg.py',
                    PROJECT/'build/devbox/nix/flake.lock']}

    def path(self,name):
        value=self.data[name];path=Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (PROJECT/path).resolve()

    def stage(self,name,inputs,build,artifacts):
        if name not in self.memo:
            self.memo[name]=self.cache.stage(name,dict(code=self.code,inputs=inputs),build,artifacts)
        return self.memo[name]

    def root_action(self,action,archive,out,**values):
        argv=[sys.executable,str(PROJECT/'build/scripts/build-official-root.py'),action,
              '--archive',str(archive),'--archive-sha256',sha(archive),'--out',str(out)]
        for name,value in values.items():argv+=['--'+name.replace('_','-'),str(value)]
        self.run(argv,root=True)

    def kernel_source(self,installer=False):
        record=json.loads((PROJECT/'build/kernel/source.json').read_text())
        if installer:
            path=Path(self.data.get('source_installer_kernel','inputs/sources/linux-installer'))
            path=path.resolve() if path.is_absolute() else PROJECT/path
        else:path=self.path('kernel_source')
        if not path.exists():
            path.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.kernel-source-',dir=path.parent) as directory:
                temporary=Path(directory)/'checkout';temporary.mkdir()
                for command in (['git','init',str(temporary)],
                                ['git','-C',str(temporary),'remote','add','origin',record['repo']],
                                ['git','-C',str(temporary),'fetch','--depth=1','origin',record['commit']],
                                ['git','-C',str(temporary),'checkout','--detach','FETCH_HEAD']):self.run(command)
                os.rename(temporary,path)
        commit=subprocess.check_output(['git','-C',str(path),'rev-parse','HEAD'],text=True).strip()
        if subprocess.check_output(['git','-C',str(path),'status','--porcelain']):raise ValueError('内核源码有未提交修改：'+str(path))
        if installer and 'source_installer_kernel' not in self.data and commit!=record['commit']:
            raise ValueError('安装内核源码与独立版本锁不一致。')
        # The independent RAM installer keeps its original kernel revision.
        # Apply target board patches only to the pinned base, in a separate
        # clean snapshot; custom kernel commits remain under the user's control.
        if not installer and commit==record['commit'] and record.get('target_patches'):
            from kernel_adaptation import assemble
            patches=[self.resolve(name) for name in record['target_patches']]
            for patch in patches:
                if not patch.is_relative_to(PROJECT/'build/kernel/patches') or patch.is_symlink():
                    raise ValueError('默认内核补丁需要位于 build/kernel/patches/。')
            def build(out):assemble(path,commit,patches,out)
            cached=self.stage('target-kernel-source',dict(base=commit,
                patches={str(p.relative_to(PROJECT)):sha(p) for p in patches},
                assembler=sha(PROJECT/'build/lib/kernel_adaptation.py')),
                build,['source','source.commit'])
            path=cached/'source';commit=(cached/'source.commit').read_text().strip()
            if subprocess.check_output(['git','-C',str(path),'rev-parse','HEAD'],text=True).strip()!=commit:
                raise ValueError('内核适配快照的 Git 身份与完成记录不同。')
        return path,commit

    def kernel(self,installer=False):
        source,commit=self.kernel_source(installer)
        prefix='installer_' if installer else ''
        fragments=kernel_fragments(self.data,installer)
        inputs=dict(commit=commit,config=self.data.get(prefix+'kernel_config'),
                    fragments={str(p):sha(p) for p in [self.resolve(v) for v in fragments]},
                    localversion=self.data.get(prefix+'kernel_localversion','-liuqin-installer' if installer else '-liuqin1'),
                    board_configs=tree_identity(PROJECT/'device/configs'),
                    builder=sha(PROJECT/'build/scripts/build-liuqin-kernel.py'))
        inputs['source_lock']=sha(PROJECT/'build/kernel/source.json')
        if inputs['config']:inputs['config_sha']=sha(self.resolve(inputs['config']))
        compiler=shutil.which(os.environ.get('CROSS_COMPILE','aarch64-unknown-linux-musl-')+'gcc')
        if not compiler:raise ValueError('请在 Devbox 中构建。')
        inputs['compiler']=sha(Path(compiler).resolve())
        def build(out):
            argv=[sys.executable,'build/scripts/build-liuqin-kernel.py','--custom','--source',str(source),
                  '--out',str(out/'kernel'),'--jobs',str(self.data.get('jobs',12)),
                  '--localversion='+inputs['localversion']]
            if inputs['config']:argv+=['--config',str(self.resolve(inputs['config']))]
            for path in fragments:argv+=['--config-fragment',str(self.resolve(path))]
            self.run(argv,env={'NIX_HARDENING_ENABLE_aarch64_unknown_linux_musl':''})
        cached=self.stage('installer-kernel' if installer else 'kernel',inputs,build,
            ['kernel/arch/arm64/boot/Image','kernel/arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dtb',
             'kernel/.config','kernel/build-info.json','kernel/SHA256SUMS','kernel/include/config/kernel.release',
             'kernel/modules','kernel/modules.order','kernel/Module.symvers','kernel/vmlinux','kernel/System.map',
             'kernel/scripts/dtc/dtc','kernel/scripts/dtc/fdtoverlay'])
        return source,cached/'kernel'

    @staticmethod
    def resolve(value):
        path=Path(value).expanduser();return path.resolve() if path.is_absolute() else PROJECT/path

    def base(self):
        return download(self.installer_source['url'],PROJECT/'inputs/ubuntu-base'/self.installer_source['archive'],self.installer_source['sha256'])

    def setup_archive(self):
        record=json.loads((PROJECT/'build/rootfs/plasma-setup-source.json').read_text())
        return download(record['url'],PROJECT/'inputs/plasma-setup'/record['archive'],record['sha256'])

    def tools(self):
        busy=PROJECT/'inputs/busybox-arm64/usr/bin/busybox'
        mk=PROJECT/'inputs/aosp-mkbootimg'
        if not busy.exists():self.run(['sh','build/scripts/fetch-busybox-arm64.sh'])
        self.run(['sh','build/scripts/fetch-aosp-mkbootimg.sh'])
        return dict(BUSYBOX=str(busy),MKBOOTIMG_DIR=str(mk))

    def board(self):
        if 'board' in self.memo:return self.memo['board']/'board'
        images=self.resolve(self.data.get('rom_images','inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images'))
        reference=self.board_builder.verify_rom(images)
        inputs=dict(rom={name:reference['images'][name]['sha256'] for name in ('vendor_boot.img','dtbo.img','super.img')},
                    firmware=tree_identity(PROJECT/'device/firmware'),
                    parser=sha(PROJECT/'build/lib/android_images.py'),builder=sha(PROJECT/'build/scripts/prepare-source-board.py'),
                    speaker_builder=sha(PROJECT/'build/lib/speaker_firmware.py'))
        def build(out):self.board_builder.prepare(images,out/'board',verified=True)
        return self.stage('board',inputs,build,['board/board.json','board/firmware-pool','board/wlan',
                         'board/dtbs','board/dtbo','board/sensors','board/speaker-firmware','board/topology.bin'])/'board'

    def board_inputs(self):
        board=self.board();inputs=json.loads((board/'board.json').read_text())
        return dict({name:str(board/value) for name,value in inputs.items()},**self.tools())

    def runtime_root(self):
        archive=self.base()
        inputs=dict(base=sha(archive),builder=sha(PROJECT/'build/scripts/build-official-root.py'),refresh=self.refresh)
        def build(out):self.root_action('runtime',archive,out/'assembled')
        return self.stage('runtime-root',inputs,build,['assembled/root.tar.gz','assembled/packages.tsv'])/'assembled/root.tar.gz'

    def runtime(self):
        archive=self.runtime_root()
        def build(out):
            self.root_action('extract',archive,out/'input')
            self.run([sys.executable,'build/lib/build-installer-runtime.py','--root',str(out/'input/rootfs'),
                      '--out',str(out/'runtime')])
        return self.stage('runtime',dict(base=sha(archive),builder=sha(PROJECT/'build/lib/build-installer-runtime.py')),
                          build,['runtime'])/'runtime'

    def boot_splash_runtime(self):
        return None

    def boot(self,installer=False):
        source,kernel=self.kernel(installer)
        inputs=self.board_inputs()
        dependency=dict(kernel=sha(kernel/'build-info.json'),board=tree_identity(self.board()),
                        builder=sha(PROJECT/'build/scripts/build-liuqin-native-boot.sh'),
                        initramfs=sha(PROJECT/'build/lib/build-initramfs.sh'),
                        usb_strings=self.usb,usb_builder=sha(PROJECT/'build/lib/usb_identity.py'),
                        filesystem_builder=sha(PROJECT/'build/lib/root_filesystems.py'),
                        filesystem_interface=sha(PROJECT/'shared/liuqin_filesystems.py'),
                        bootargs=sha(PROJECT/'device'/('installer-bootargs.txt' if installer else 'native-bootargs.txt')),
                        init=tree_identity(PROJECT/'device/boot'),
                        dtb_tools=sha(PROJECT/'build/lib/build-bootimg.sh'))
        if not installer:
            dependency.update(reclaim_mte=bool(self.data.get('kernel_reclaim_mte',False)),
                memory_helper=sha(PROJECT/'build/lib/reclaim-memory.py'))
        env=dict(inputs,**usb_environment(self.usb),KERNEL_SOURCE=str(source),KERNEL_OUT=str(kernel))
        if installer:
            runtime=self.runtime();dependency['runtime']=tree_identity(runtime)
            def build(out):
                self.run(['sh','build/scripts/build-liuqin-installer.sh'],env=dict(env,
                    INSTALLER_KERNEL_SOURCE=str(source),INSTALLER_KERNEL_OUT=str(kernel),
                    INSTALLER_RUNTIME=str(runtime),OUT_DIR=str(out/'image')))
            result=self.stage('installer',dependency,build,['image/installer.img'])
            return result/'image/installer.img'
        modules=self.modules();dependency['modules']=sha(modules/'kernel.build.json')
        splash=self.boot_splash_runtime()
        if splash:
            dependency.update(splash=tree_identity(splash),splash_stager=sha(PROJECT/'build/lib/boot_splash.py'),
                              splash_interface=sha(PROJECT/'build/lib/plymouth_runtime.py'))
        def build(out):
            self.run(['sh','build/scripts/build-liuqin-native-boot.sh'],env=dict(env,
                KERNEL_MODULES_DIR=str(modules),LIUQIN_ROOT_PROFILE='native-updatable',OUT_DIR=str(out/'image'),INSTALLER_RUNTIME='',
                BOOT_SPLASH_RUNTIME=str(splash) if splash else '',
                LIUQIN_RECLAIM_MTE='1' if self.data.get('kernel_reclaim_mte',False) else '0'))
        result=self.stage('boot',dependency,build,['image/boot-liuqin-native.img','image/native-root.contract'])
        return result/'image/boot-liuqin-native.img'

    def modules(self):
        source,kernel=self.kernel()
        def build(out):self.run(['sh','build/scripts/build-liuqin-kernel-modules.sh'],env=dict(
            KERNEL_DIR=str(source),KERNEL_OUT=str(kernel),OUT_DIR=str(out/'modules')))
        result=self.stage('modules',dict(kernel=sha(kernel/'build-info.json'),
            builder=sha(PROJECT/'build/scripts/build-liuqin-kernel-modules.sh')),
            build,['modules/modules.tar','modules/modules.manifest','modules/kernel.build.json','modules/kernel.release','modules/kernel.commit'])
        return result/'modules'

    def kernel_debs(self):
        source,kernel=self.kernel();modules=self.modules();boot=self.boot()
        def build(out):
            self.run([sys.executable,'build/scripts/build-liuqin-kernel-debs.py','--kernel-source',str(source),
                      '--kernel-out',str(kernel),'--modules-dir',str(modules),'--boot-image',str(boot),
                      '--out',str(out/'debs'),'--version',self.version])
        inputs=dict(boot=sha(boot),modules=sha(modules/'kernel.build.json'),version=self.version,
                    builder=sha(PROJECT/'build/scripts/build-liuqin-kernel-debs.py'),
                    tools=sha(PROJECT/'device/boot/boot-update.py'),interfaces=tree_identity(PROJECT/'shared'))
        return self.stage('kernel-debs',inputs,build,['debs'])/'debs'

    def sensors(self):
        archive=self.runtime_root();board=self.board()
        def build(out):self.root_action('sensors',archive,out/'assembled',rom_sensors=board/'sensors',jobs=self.data.get('jobs',12))
        inputs=dict(base=sha(archive),config=tree_identity(board/'sensors'),
                    sources=tree_identity(PROJECT/'device/sensors'),overlay=tree_identity(PROJECT/'device/sensors-overlay'),
                    builder=sha(PROJECT/'build/scripts/build-liuqin-sensors-stack.sh'))
        return self.stage('sensors',inputs,build,['assembled/sensor-stack.tar'])/'assembled/sensor-stack.tar'

    def device_debs(self):
        sensors=self.sensors();tools=self.tools();speaker_firmware=self.board()/'speaker-firmware'
        def build(out):
            env=dict(OUT_DIR=str(out/'debs'),DEVICE_DESKTOP='generic',BUSYBOX=tools['BUSYBOX'],
                     SENSOR_STACK_TAR=str(sensors),SENSOR_STACK_SHA256=sha(sensors),LIUQIN_DEB_VERSION=self.version,
                     SPEAKER_FIRMWARE_DIR=str(speaker_firmware))
            for name in ('firmware','device-support','sensors','meta'):
                self.run(['sh','build/scripts/build-liuqin-debs.sh',name],env=env)
        return self.stage('device-debs',dict(sensors=sha(sensors),firmware=tree_identity(PROJECT/'device/firmware'),
            overlay=tree_identity(PROJECT/'device/gnome-overlay'),busybox=sha(tools['BUSYBOX']),version=self.version,
            speaker_firmware=tree_identity(speaker_firmware),speaker_builder=sha(PROJECT/'build/lib/speaker_firmware.py'),
            builder=sha(PROJECT/'build/scripts/build-liuqin-debs.sh')),build,['debs'])/'debs'

    def first_boot_deb(self):
        if not self.recipe['first_boot_setup']:return None
        from plasma_setup import RUNTIME_PACKAGES, BUILD_PACKAGES
        archive=self.runtime_root();upstream=self.setup_archive()
        record=json.loads((PROJECT/'build/rootfs/plasma-setup-source.json').read_text())
        name='liuqin-plasma-setup_'+record['version']+'-liuqin'+str(record.get('package_revision',1))+'_arm64.deb'
        def build(out):
            self.root_action('setup',archive,out/'assembled',setup_source=upstream,jobs=self.data.get('jobs',12))
        inputs=dict(base=sha(archive),source=sha(upstream),packages=BUILD_PACKAGES+RUNTIME_PACKAGES,
                    source_lock=sha(PROJECT/'build/rootfs/plasma-setup-source.json'),
                    patches=tree_identity(PROJECT/'build/rootfs/plasma-setup'),
                    panel_helper=tree_identity(PROJECT/'build/rootfs/plasma-panel'),
                    builder=sha(PROJECT/'build/lib/plasma_setup.py'),
                    root_builder=sha(PROJECT/'build/scripts/build-official-root.py'),refresh=self.refresh)
        return self.stage('first-boot-setup',inputs,build,['assembled/'+name,'assembled/setup-packages.tsv'])/'assembled'/name

    def apps(self):
        archive=self.runtime_root();debs=self.device_debs()
        setup_deb=self.first_boot_deb()
        inputs=dict(base=sha(archive),debs=tree_identity(debs),recipe=self.recipe,
                    builder=sha(PROJECT/'build/scripts/build-official-root.py'),refresh=self.refresh)
        if setup_deb:inputs['setup_deb']=sha(setup_deb)
        if self.recipe.get('overlay'):inputs['overlay']=tree_identity(self.recipe['overlay'])
        if self.recipe.get('password_file'):inputs['password_sha256']=sha(self.recipe['password_file'])
        def build(out):
            snapshot=dict(self.recipe)
            if snapshot.get('overlay'):
                target=out/'overlay'
                shutil.copytree(snapshot['overlay'],target,symlinks=True)
                if tree_identity(target)!=inputs['overlay']:raise ValueError('overlay 在暂存期间发生变化。')
                snapshot['overlay']=str(target)
            if snapshot.get('password_file'):
                source=Path(snapshot['password_file'])
                if source.is_symlink() or source.stat().st_mode & 0o077:raise ValueError('密码输入必须是私有普通文件。')
                target=out/'.password'
                target.write_bytes(source.read_bytes());target.chmod(0o600)
                if sha(target)!=inputs['password_sha256']:raise ValueError('密码文件在暂存期间发生变化。')
                snapshot['password_file']=str(target)
            path=out/'recipe.json';path.write_text(json.dumps(snapshot)+'\n')
            values=dict(recipe=path,debs=debs)
            if setup_deb:values['setup_deb']=setup_deb
            self.root_action('apps',archive,out/'assembled',**values)
        artifacts=['assembled/root.tar.gz','assembled/packages.tsv']
        if not self.recipe['first_boot_setup']:artifacts.append('assembled/login.txt')
        return self.stage('apps',inputs,build,artifacts)/'assembled'

    def rootfs(self):
        apps=self.apps();debs=self.kernel_debs();source,kernel=self.kernel();modules=self.modules()
        def build(out):
            self.root_action('kernel-files',apps/'root.tar.gz',out/'assembled',debs=debs)
            self.run([sys.executable,'build/scripts/build-liuqin-custom-root.py','--root',str(out/'assembled/rootfs'),
                      '--kernel-out',str(kernel),'--modules-dir',str(modules),'--out',str(out/'packed')],root=True)
        return self.stage('rootfs',dict(apps=sha(apps/'root.tar.gz'),debs=tree_identity(debs),
            builder=sha(PROJECT/'build/scripts/build-liuqin-custom-root.py'),interfaces=tree_identity(PROJECT/'shared')),
            build,['packed/rootfs.tar.gz','packed/native-root.contract'])/'packed'

    def images(self):
        installer=self.boot(True);boot=self.boot();root=self.rootfs()
        inputs=dict(installer=sha(installer),boot=sha(boot),root=sha(root/'rootfs.tar.gz'))
        identity=key(inputs)[:16]
        destination=PROJECT/'out/releases'/(self.distribution+'-plasma-'+identity)
        from liuqin_artifacts import inspect_package
        if destination.exists():
            inspect_package(destination)
            if any(sha(destination/name)!=inputs[field] for field,name in
                   (('installer','installer.img'),('boot','boot.img'),('root','rootfs.tar.gz'))):
                raise ValueError('已有发行目录内容变化，未覆盖。')
        else:
            destination.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.publish-',dir=destination.parent) as directory:
                temporary=Path(directory)/'bundle';temporary.mkdir()
                for path,name in ((installer,'installer.img'),(boot,'boot.img'),(root/'rootfs.tar.gz','rootfs.tar.gz')):
                    shutil.copyfile(path,temporary/name)
                inspect_package(temporary);os.rename(temporary,destination)
        login=None
        if not self.recipe['first_boot_setup']:
            login=PROJECT/'out/login'/(self.distribution+'-plasma-'+identity+'.txt');login.parent.mkdir(exist_ok=True)
            shutil.copyfile(self.apps()/'login.txt',login);login.chmod(0o600)
        report=dict(status='BUILT_NOT_DEVICE_TESTED',files=inputs,official_base=self.source,
                    kernel_release=(self.kernel()[1]/'include/config/kernel.release').read_text().strip(),
                    rootfs_recipe=self.recipe,packages=str(self.apps()/'packages.tsv'),
                    account_setup='first-boot' if self.recipe['first_boot_setup'] else 'preconfigured',
                    login_file=str(login) if login else None)
        reports=PROJECT/'out/reports';reports.mkdir(exist_ok=True)
        (reports/(identity+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print('三个安装文件：'+str(destination),flush=True)
        if login:print('本机登录信息文件（不包含在安装目录）：'+str(login),flush=True)
        else:print('首次启动将在平板上创建账号和密码；本机不生成登录凭据。',flush=True)
        return destination

    def plan(self):
        images=self.resolve(self.data.get('rom_images','inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images'))
        return dict(official_base=self.source,recipe=self.recipe,rom_images=str(images),usb_device=self.usb,
                    missing_rom_files=[name for name in ('vendor_boot.img','dtbo.img','super.img') if not (images/name).is_file()],
                    steps=['官方 Ubuntu Base','原厂板级输入','独立安装内核/runtime/installer',
                           '自定义内核/模块/boot/更新包','传感器和设备包']+
                          (['登录前初始化组件（独立 ARM64 包）'] if self.recipe['first_boot_setup'] else [])+
                          ['Plasma 和自定义软件','根归档','三文件发布'],
                    output='out/releases/ubuntu-plasma-<内容标识>/',tablet_access=False)


def execute(data,action,run,*,refresh=False,configure_only=False):
    sys.path.insert(0,str(PROJECT/'shared'))
    recipe_path=Flow.resolve(data['rootfs_config'])
    distribution=json.loads(recipe_path.read_text()).get('distribution')
    if distribution=='fedora':
        from fedora_flow import FedoraFlow
        flow=FedoraFlow(data,run,refresh=refresh)
    else:flow=Flow(data,run,refresh=refresh)
    data=flow.data
    if action=='plan':print(json.dumps(flow.plan(),ensure_ascii=False,indent=2));return
    if action=='check-inputs':
        flow.board_builder.verify_rom(flow.resolve(data.get('rom_images','inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images')))
        print('PASS：官方系统配置和原厂 ROM 镜像输入有效。');return
    root=PROJECT/'out/build/from-source';root.mkdir(parents=True,exist_ok=True)
    with (root/'lock').open('a') as owner:
        fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if action in ('all','prepare','boot','kernel-debs','rootfs','images','installer','build-installer'):
            # Check model-specific input availability before downloads or compilation.
            flow.board()
        if action=='prepare':
            flow.base();flow.tools();flow.kernel_source()
            if distribution=='fedora':flow.target_base();flow.stats_archive()
            if flow.recipe['first_boot_setup'] and distribution!='fedora':flow.setup_archive()
            return
        if action in ('kernel','installer-kernel') and configure_only:
            installer=action=='installer-kernel';prefix='installer_' if installer else ''
            source,commit=flow.kernel_source(installer)
            fragments=kernel_fragments(data,installer)
            configuration=data.get(prefix+'kernel_config')
            suffix=data[prefix+'kernel_localversion']
            identity=key(dict(commit=commit,config=configuration,fragments={v:sha(flow.resolve(v)) for v in fragments},
                              localversion=suffix))[:16]
            out=PROJECT/'out/build'/('kernel-config-'+identity)
            argv=[sys.executable,'build/scripts/build-liuqin-kernel.py','--custom','--source',str(source),
                  '--out',str(out),'--localversion='+suffix,'--configure-only']
            if configuration:argv+=['--config',str(flow.resolve(configuration))]
            for fragment in fragments:argv+=['--config-fragment',str(flow.resolve(fragment))]
            run(argv,env={'NIX_HARDENING_ENABLE_aarch64_unknown_linux_musl':''})
            print('内核配置预览：'+str(out/'.config'));return
        methods={'all':flow.images,'kernel':flow.kernel,'modules':flow.modules,'boot':flow.boot,
                 'kernel-debs':flow.kernel_debs,'rootfs':flow.rootfs,'images':flow.images,
                 'build-installer':lambda:flow.boot(True),'installer':lambda:flow.boot(True),
                 'installer-kernel':lambda:flow.kernel(True),'runtime':flow.runtime}
        if action not in methods:raise ValueError('官方系统工作流不支持此阶段：'+action)
        result=methods[action]()
        if result is not None:print('阶段产物：'+str(result),flush=True)
