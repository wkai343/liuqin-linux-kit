# SPDX-License-Identifier: MIT
"""Identity checks for a self-contained-kernel, embedded-DTB RAM Recovery."""
import hashlib
import json
import re

MARKER='etc/liuqin-native-recovery.json'
AGENT='usr/lib/liuqin/native-status.py'
BOARD_HELPER='usr/lib/liuqin/liuqin_board_identity.py'
UI_FILES=('usr/bin/liuqin-recovery-ui','usr/lib/liuqin/native-ui-service.py','usr/sbin/liuqin-ui-power')
TOUCH_FILES=('usr/lib/liuqin/liuqin_bootloader_panel.ko','usr/lib/liuqin/native-touch.py',
    'lib/firmware/novatek/liuqin/novatek_nt36532_m81_fw_csot.bin','lib/firmware/novatek/liuqin/novatek_nt36532_m81_fw_tm.bin')
INSTALL_FILES=tuple('usr/lib/liuqin/'+name for name in ('native-install.py','native-root-guard.py','liuqin_native_install.py',
    'android-device.py','install-root.sh','install-layout.sh','multiboot-layout.sh','multiboot-store.sh',
    'provision.sh','root-interface-v2.sh','boot-target-v1.sh',
    'liuqin_android.py','liuqin_android_images.py','liuqin_layout.py','liuqin_multiboot.py','liuqin_managed.py',
    'liuqin_boot_target.py','liuqin_partitions.py','liuqin_partition_names.py','liuqin_partition_editor.py','liuqin_block_identity.py',
    'liuqin-android-profile.json','liuqin-miku-profile.json','liuqin-rom-images.json',
    'liuqin_rom_admission.py','liuqin-android-roms.json','liuqin-lineage-profile.json','liuqin_ota.py','liuqin_super.py','liuqin_ota_v1.py'))
ADMISSION_FILES=tuple('usr/lib/liuqin/'+n for n in ('liuqin_rom_admission.py','liuqin-android-roms.json','liuqin-lineage-profile.json'))
OTA_FILES=('usr/lib/liuqin/liuqin_ota.py','usr/lib/liuqin/liuqin_super.py')
FULL_OTA_FILES=('usr/lib/liuqin/liuqin_ota_v1.py',)
FIXED_RESTORE_FILES=('usr/lib/liuqin/liuqin_recovery_preserve.py',)
TABLET_SWITCH_FILES=tuple('usr/lib/liuqin/'+name for name in (
    'liuqin_universal_recovery.py', 'liuqin_fixed_recovery.py', 'liuqin_artifacts.py',
    'liuqin_native_recovery.py', 'liuqin_kernel.py', 'liuqin_filesystems.py'))
LEGACY_INSTALL_FILES=tuple(n for n in INSTALL_FILES if n not in ADMISSION_FILES+OTA_FILES+FULL_OTA_FILES)
ADB_FILES=('usr/bin/adbd','usr/lib/liuqin/native-adb-start.py','usr/lib/liuqin/native-adb-shell.py')
STORAGE_SEAL='bin/liuqin-storage-seal'
BCB_FILES=('usr/lib/liuqin/native-bcb.py','usr/sbin/liuqin-reboot')
STORE_FILES=('usr/lib/liuqin/native-store.py',)
RETENTION_FILES=('usr/lib/liuqin/liuqin_boot_retention.py',)
ALL_PAYLOADS=('init',AGENT,BOARD_HELPER,*UI_FILES,*TOUCH_FILES,*INSTALL_FILES,*FIXED_RESTORE_FILES,*TABLET_SWITCH_FILES,*ADB_FILES,STORAGE_SEAL,*BCB_FILES,*STORE_FILES,*RETENTION_FILES)


def validate(content,kernel_digest,dtb,files,usage='temporary-boot-only'):
    m=json.loads(content)
    from liuqin_native_install import capable
    install=capable(m)
    if (m.get('schema')!=1 or m.get('interface')!='liuqin-native-recovery-v1' or
        m.get('device')!='liuqin' or m.get('usage')!=usage or
        (m.get('inspection_only') is not True and not install) or m.get('status')!='DEVICE_UNTESTED' or
        m.get('kernel_sha256')!=kernel_digest or
        m.get('dtb_sha256')!=hashlib.sha256(dtb).hexdigest() or
        not re.fullmatch('[a-f0-9]{64}',m.get('build_id',''))):
        raise ValueError('原生 Recovery 的角色、内核或设备树身份不一致。')
    names=('init',AGENT,BOARD_HELPER) if m.get('board_identity_version')==1 else ('init',AGENT)
    if m.get('board_identity_version',0) not in (0,1):raise ValueError('unsupported native board identity interface')
    if m.get('ui_version',0) not in (0,1):raise ValueError('unsupported native UI interface')
    if m.get('ui_version')==1:names+=UI_FILES
    if m.get('touch_version',0) not in (0,1):raise ValueError('unsupported native touch interface')
    if m.get('touch_version')==1:names+=TOUCH_FILES
    if m.get('native_adb_version') is not None:
        if m['native_adb_version']!=1 or m.get('usb_transport')!='adb' or files.get('etc/liuqin-native-adb',(0,b''))[1]!=b'1\n':raise ValueError('invalid native ADB interface')
        names+=ADB_FILES
    fast_start = m.get('recovery_fast_start_version')
    if fast_start is not None:
        if type(fast_start) is not int or fast_start != 1 or m.get('native_adb_version') != 1:
            raise ValueError('invalid native Recovery startup capability')
        names += (STORAGE_SEAL,)
    if m.get('installer_version') is not None and not install:raise ValueError('invalid native installer capability declaration')
    if m.get('generic_ota_version') is not None and (m['generic_ota_version'] not in (1,2) or not install):raise ValueError('invalid generic OTA capability')
    if m.get('combined_install_version') is not None and (m['combined_install_version']!=1 or not install):raise ValueError('invalid combined installer capability')
    if m.get('boot_switch_version') is not None and (m['boot_switch_version']!=1 or not install):raise ValueError('invalid default boot switch capability')
    bcb_cleanup = m.get('bcb_cleanup_version')
    if bcb_cleanup is not None:
        if type(bcb_cleanup) is not int or bcb_cleanup != 1 or not install or m.get('ui_version') != 1:
            raise ValueError('invalid Recovery BCB cleanup capability')
        names += BCB_FILES
    boot_store = m.get('boot_store_version')
    if boot_store is not None:
        if type(boot_store) is not int or boot_store != 1 or not install or m.get('boot_switch_version') != 1:
            raise ValueError('invalid boot store maintenance capability')
        names += STORE_FILES
    retention = m.get('boot_store_retention_version')
    if retention is not None:
        if type(retention) is not int or retention != 1 or boot_store != 1:
            raise ValueError('invalid boot store retention capability')
        names += RETENTION_FILES
    tablet_switch = m.get('tablet_boot_switch_version')
    if tablet_switch is not None:
        if (type(tablet_switch) is not int or tablet_switch != 1 or not install or
                m.get('boot_switch_version') != 1 or m.get('ui_version') != 1 or
                m.get('fixed_recovery_restore_version') != 1):
            raise ValueError('invalid tablet boot switch capability')
        names += TABLET_SWITCH_FILES
    witness = m.get('fixed_recovery_witness')
    if witness is not None and (
            tablet_switch != 1 or usage != 'temporary-boot-only' or
            not isinstance(witness, dict) or set(witness) != {'serial', 'partition', 'image_sha256'} or
            witness['partition'] != 'recovery_a' or not isinstance(witness['serial'], str) or
            not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', witness['serial']) or
            not isinstance(witness['image_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', witness['image_sha256'])):
        raise ValueError('invalid qualified Recovery evidence')
    if m.get('android_sparse_store_version') is not None and (m['android_sparse_store_version']!=1 or not install):raise ValueError('invalid sparse Android boot store capability')
    if m.get('sparse_boot_store_version') is not None and (m['sparse_boot_store_version']!=1 or not install):raise ValueError('invalid sparse boot store capability')
    if m.get('fixed_recovery_restore_version') is not None and (m['fixed_recovery_restore_version']!=1 or not install or m.get('generic_ota_version')!=2):raise ValueError('invalid fixed Recovery preservation capability')
    if install:
        names+=LEGACY_INSTALL_FILES
        if m.get('generic_ota_version') in (1,2):names+=ADMISSION_FILES+OTA_FILES
        if m.get('generic_ota_version')==2:names+=FULL_OTA_FILES
        elif not m.get('generic_ota_version') and any(n in m.get('payload_sha256',{}) for n in ADMISSION_FILES):names+=ADMISSION_FILES
        if m.get('fixed_recovery_restore_version')==1:names+=FIXED_RESTORE_FILES
        if files.get('etc/liuqin-storage-mode',(0,b''))[1]!=b'readonly\n':raise ValueError('native installer must boot with storage sealed')
    if install or m.get('native_adb_version')==1:
        identity=m.get('identity',{})
        encoded=(json.dumps(identity,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
        if (identity.get('boot_switch_version',0)!=m.get('boot_switch_version',0) or identity.get('combined_install_version',0)!=m.get('combined_install_version',0) or identity.get('generic_ota_version',0)!=m.get('generic_ota_version',0) or
                identity.get('tablet_boot_switch_version') != tablet_switch or identity.get('fixed_recovery_witness') != witness or
                identity.get('recovery_fast_start_version') != fast_start or
                identity.get('bcb_cleanup_version') != bcb_cleanup or
                identity.get('boot_store_version') != boot_store or
                identity.get('boot_store_retention_version') != retention or
                identity.get('android_sparse_store_version',0)!=m.get('android_sparse_store_version',0) or
                identity.get('sparse_boot_store_version',0)!=m.get('sparse_boot_store_version',0) or
                identity.get('fixed_recovery_restore_version',0)!=m.get('fixed_recovery_restore_version',0) or
                identity.get('fixed_installer_version',0)!=m.get('fixed_installer_version',0) or hashlib.sha256(encoded).hexdigest()!=m['build_id'] or identity.get('payload_sha256')!=m['payload_sha256'] or
            any(identity.get(k)!=m.get(k) for k in ('kernel_sha256','dtb_sha256','inspection_only')) or identity.get('installer_capabilities',[])!=m.get('installer_capabilities',[])):raise ValueError('native installer build identity mismatch')
    for name in names:
        mode,data=files.get(name,(0,b''))
        if mode&0o170000!=0o100000 or hashlib.sha256(data).hexdigest()!=m.get('payload_sha256',{}).get(name):
            raise ValueError('原生 Recovery 启动或状态程序校验失败：'+name)
        if name in ('usr/bin/liuqin-recovery-ui','usr/sbin/liuqin-ui-power','usr/sbin/liuqin-reboot','usr/bin/adbd',STORAGE_SEAL) and not mode&0o111:
            raise ValueError('原生 Recovery 图形程序不可执行。')
    return m
