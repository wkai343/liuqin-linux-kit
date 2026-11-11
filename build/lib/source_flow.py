# SPDX-License-Identifier: MIT
"""Shared Fedora and kernel build graph; no tablet access."""
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

from source_build import PROJECT, Cache, key, sha, tree_identity
from usb_identity import settings as usb_settings, environment as usb_environment


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def kernel_fragments(data):
    fragments=list(data.get('kernel_fragments',[]))
    native='build/kernel/configs/native-display.config'
    if native not in fragments:
        fragments.insert(0,native)
    return fragments


def kernel_source_record(data):
    return data.get('kernel_source_lock') or 'build/kernel/source.json'


class Flow:
    def __init__(self, data, run, *, refresh=False, distribution='fedora'):
        if distribution != 'fedora':raise ValueError('目标系统只支持 Fedora + Plasma。')
        self.data, self.run = data, run
        self.usb = usb_settings(data)
        self.root_builder=load_module('official_root',PROJECT/'build/scripts/build-official-root.py')
        self.board_builder=load_module('source_board',PROJECT/'build/scripts/prepare-source-board.py')
        self.distribution=distribution
        self.source=json.loads((PROJECT/('build/rootfs/'+distribution+'-source.json')).read_text())
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

    def kernel_source_lock(self):
        return self.resolve(kernel_source_record(self.data))

    def kernel_source(self):
        record=json.loads(self.kernel_source_lock().read_text())
        path=self.path('kernel_source')
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
        # Adapt the pinned base in a separate clean snapshot.
        if commit==record['commit'] and record.get('target_patches'):
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
            expected_tree=record.get('expected_source_tree') or record.get('tested_source_tree')
            if expected_tree:
                tree=subprocess.check_output(['git','-C',str(path),'rev-parse','HEAD^{tree}'],text=True).strip()
                if tree!=expected_tree:
                    raise ValueError('目标内核补丁生成的源码树与核验记录不同。')
        return path,commit

    def kernel(self):
        source,commit=self.kernel_source()
        fragments=kernel_fragments(self.data)
        inputs=dict(commit=commit,config=self.data.get('kernel_config'),
                    fragments={str(p):sha(p) for p in [self.resolve(v) for v in fragments]},
                    localversion=self.data.get('kernel_localversion','-liuqin1'),
                    builder=sha(PROJECT/'build/scripts/build-liuqin-kernel.py'))
        inputs['source_lock']=sha(self.kernel_source_lock())
        alignment=json.loads(self.kernel_source_lock().read_text()).get('load_alignment')
        if alignment:
            import arm64_alignment
            if alignment!=arm64_alignment.PROTOCOL:raise ValueError('不支持的 ARM64 对齐入口。')
            inputs['load_alignment']=arm64_alignment.inputs()
        if inputs['config']:inputs['config_sha']=sha(self.resolve(inputs['config']))
        compiler=shutil.which(os.environ.get('CROSS_COMPILE','aarch64-unknown-linux-musl-')+'gcc')
        if not compiler:raise ValueError('请在 Devbox 中构建。')
        inputs['compiler']=sha(Path(compiler).resolve())
        def build(out):
            argv=[sys.executable,'build/scripts/build-liuqin-kernel.py','--custom','--source',str(source),
                  '--source-lock',str(self.kernel_source_lock()),
                  '--out',str(out/'kernel'),'--jobs',str(self.data.get('jobs',12)),
                  '--localversion='+inputs['localversion']]
            if inputs['config']:argv+=['--config',str(self.resolve(inputs['config']))]
            for path in fragments:argv+=['--config-fragment',str(self.resolve(path))]
            self.run(argv,env={'NIX_HARDENING_ENABLE_aarch64_unknown_linux_musl':''})
        artifacts=['kernel/arch/arm64/boot/Image','kernel/arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dtb',
             'kernel/.config','kernel/build-info.json','kernel/SHA256SUMS','kernel/include/config/kernel.release',
             'kernel/modules','kernel/modules.order','kernel/Module.symvers','kernel/vmlinux','kernel/System.map',
             'kernel/scripts/dtc/dtc','kernel/scripts/dtc/fdtoverlay','kernel/source-lock.json']
        if alignment:artifacts+=['kernel/arch/arm64/boot/Image.raw','kernel/arm64-alignment.json']
        cached=self.stage('kernel',inputs,build,artifacts)
        return source,cached/'kernel'

    @staticmethod
    def resolve(value):
        path=Path(value).expanduser();return path.resolve() if path.is_absolute() else PROJECT/path

    def tools(self):
        builder=load_module('busybox_builder',PROJECT/'build/scripts/build-busybox.py')
        def build(out):
            self.run([sys.executable,'build/scripts/build-busybox.py','--out',str(out/'runtime'),
                      '--jobs',str(self.data.get('jobs',12))])
        result=self.stage('busybox',builder.inputs(),build,
            ['runtime/busybox','runtime/busybox.config','runtime/busybox-manifest.json',
             'runtime/busybox-sources.tar','runtime/LICENSE'])
        busy=result/'runtime/busybox'
        mk=PROJECT/'inputs/aosp-mkbootimg'
        self.run(['sh','build/scripts/fetch-aosp-mkbootimg.sh'])
        return dict(BUSYBOX=str(busy),MKBOOTIMG_DIR=str(mk))

    def board(self):
        if 'board' in self.memo:return self.memo['board']/'board'
        images=self.resolve(self.data.get('rom_images','inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images'))
        reference=self.board_builder.verify_rom(images)
        inputs=dict(rom={name:reference['images'][name]['sha256'] for name in ('vendor_boot.img','dtbo.img','super.img')},
                    firmware=tree_identity(PROJECT/'device/firmware'),
                    parser=sha(PROJECT/'build/lib/android_images.py'),parser_shared=sha(PROJECT/'shared/liuqin_android_images.py'),builder=sha(PROJECT/'build/scripts/prepare-source-board.py'),
                    speaker_builder=sha(PROJECT/'build/lib/speaker_firmware.py'))
        def build(out):self.board_builder.prepare(images,out/'board',verified=True)
        return self.stage('board',inputs,build,['board/board.json','board/firmware-pool','board/wlan',
                         'board/dtbs','board/dtbo','board/sensors','board/speaker-firmware','board/topology.bin'])/'board'

    def board_inputs(self):
        board=self.board();inputs=json.loads((board/'board.json').read_text())
        return dict({name:str(board/value) for name,value in inputs.items()},**self.tools())

    def boot_splash_runtime(self):
        return None

    def boot(self):
        source,kernel=self.kernel()
        inputs=self.board_inputs()
        dependency=dict(kernel=sha(kernel/'build-info.json'),board=tree_identity(self.board()),busybox=sha(inputs['BUSYBOX']),
                        builder=sha(PROJECT/'build/scripts/build-liuqin-native-boot.sh'),
                        initramfs=sha(PROJECT/'build/lib/build-initramfs.sh'),
                        usb_strings=self.usb,usb_builder=sha(PROJECT/'build/lib/usb_identity.py'),
                        filesystem_builder=sha(PROJECT/'build/lib/root_filesystems.py'),
                        filesystem_interface=sha(PROJECT/'shared/liuqin_filesystems.py'),
                        bootargs=sha(PROJECT/'device/native-bootargs.txt'),
                        init=tree_identity(PROJECT/'device/boot'),
                        dtb_tools=sha(PROJECT/'build/lib/build-bootimg.sh'),
                        abl_symbols=sha(PROJECT/'build/lib/abl-symbols.py'))
        env=dict(inputs,**usb_environment(self.usb),KERNEL_SOURCE=str(source),KERNEL_OUT=str(kernel))
        modules=self.modules();dependency['modules']=sha(modules/'kernel.build.json')
        splash=self.boot_splash_runtime()
        if splash:
            dependency.update(splash=tree_identity(splash),splash_stager=sha(PROJECT/'build/lib/boot_splash.py'),
                              splash_interface=sha(PROJECT/'build/lib/plymouth_runtime.py'),
                              splash_assets=sha(PROJECT/'build/lib/splash_assets.py'))
        def build(out):
            self.run(['sh','build/scripts/build-liuqin-native-boot.sh'],env=dict(env,
                KERNEL_MODULES_DIR=str(modules),LIUQIN_ROOT_PROFILE='native-updatable',OUT_DIR=str(out/'image'),INSTALLER_RUNTIME='',
                BOOT_SPLASH_RUNTIME=str(splash) if splash else ''))
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

    def plan(self):
        images=self.resolve(self.data.get('rom_images','inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images'))
        return dict(official_base=self.source,recipe=self.recipe,rom_images=str(images),usb_device=self.usb,
                    missing_rom_files=[name for name in ('vendor_boot.img','dtbo.img','super.img') if not (images/name).is_file()],
                    steps=['官方 Fedora '+self.source['release']+' ARM64 基础系统','原厂板级输入',
                           '自定义内核/模块/boot/更新包','传感器和设备包']+
                          (['Fedora 官方 plasma-setup 软件包'] if self.recipe['first_boot_setup'] else [])+
                          ['Plasma 和自定义软件','配套 boot/rootfs'],
                    output='out/build/from-source/（使用 outputs 查看配套 boot/rootfs）',tablet_access=False)


def execute(data,action,run,*,refresh=False,configure_only=False,export_dir=None):
    sys.path.insert(0,str(PROJECT/'shared'))
    recipe_path=Flow.resolve(data['rootfs_config'])
    if export_dir is not None:
        if action!='rootfs':raise ValueError('镜像导出只用于完整目标系统构建。')
        export_dir=Path(export_dir).expanduser()
        if not export_dir.is_absolute():export_dir=PROJECT/export_dir
        if export_dir.exists() or export_dir.is_symlink():
            raise ValueError('导出目录已存在，未覆盖：'+str(export_dir))
    if action not in ('plan','prepare','check-inputs','kernel','modules','boot','kernel-debs','rootfs'):
        raise ValueError('该构建阶段已退役；使用 devbox run build -- --target 选择目标，原生 Rec 独立构建。')
    distribution=json.loads(recipe_path.read_text()).get('distribution')
    if distribution!='fedora':raise ValueError('目标系统只支持 Fedora + Plasma。')
    from fedora_flow import FedoraFlow
    flow=FedoraFlow(data,run,refresh=refresh)
    data=flow.data
    if action=='plan':print(json.dumps(flow.plan(),ensure_ascii=False,indent=2));return
    if action=='check-inputs':
        flow.board_builder.verify_rom(flow.resolve(data.get('rom_images','inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images')))
        flow.fingerprint_image()
        print('PASS：官方系统配置和原厂 ROM 镜像输入有效。');return
    root=PROJECT/'out/build/from-source';root.mkdir(parents=True,exist_ok=True)
    with (root/'lock').open('a') as owner:
        fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if action in ('prepare','boot','kernel-debs','rootfs'):
            # Check model-specific input availability before downloads or compilation.
            if action in ('prepare','rootfs'):
                flow.fingerprint_image()
            flow.board()
        if action=='prepare':
            flow.tools();flow.kernel_source()
            flow.target_base();flow.stats_archive()
            return
        if action=='kernel' and configure_only:
            source,commit=flow.kernel_source()
            fragments=kernel_fragments(data)
            configuration=data.get('kernel_config')
            suffix=data['kernel_localversion']
            identity=key(dict(commit=commit,config=configuration,fragments={v:sha(flow.resolve(v)) for v in fragments},
                              localversion=suffix,source_lock=sha(flow.kernel_source_lock())))[:16]
            out=PROJECT/'out/build'/('kernel-config-'+identity)
            argv=[sys.executable,'build/scripts/build-liuqin-kernel.py','--custom','--source',str(source),
                  '--source-lock',str(flow.kernel_source_lock()),
                  '--out',str(out),'--localversion='+suffix,'--configure-only']
            if configuration:argv+=['--config',str(flow.resolve(configuration))]
            for fragment in fragments:argv+=['--config-fragment',str(flow.resolve(fragment))]
            run(argv,env={'NIX_HARDENING_ENABLE_aarch64_unknown_linux_musl':''})
            print('内核配置预览：'+str(out/'.config'));return
        methods={'kernel':flow.kernel,'modules':flow.modules,'boot':flow.boot,
                 'kernel-debs':flow.kernel_debs,'rootfs':flow.rootfs}
        if action not in methods:raise ValueError('官方系统工作流不支持此阶段：'+action)
        result=methods[action]()
        if result is not None:print('阶段产物：'+str(result),flush=True)
        if export_dir is not None:
            from image_export import export_images
            exported=export_images(flow.boot(),result/'rootfs.tar.gz',export_dir)
            print('本次配套镜像：'+str(exported),flush=True)
