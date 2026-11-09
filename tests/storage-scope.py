#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the real scoped sealer against independent UFS and USB sysfs fixtures."""
from pathlib import Path
import importlib.util,os,re,shlex,shutil,subprocess,sys,tempfile,unittest
import hashlib
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1]

class ScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work=tempfile.TemporaryDirectory();cls.binary=Path(cls.work.name)/'scope'
        subprocess.run(['cc','-std=c11','-O2','-Wall','-Wextra','-Werror',str(PROJECT/'tests/storage-scope-mock.c'),'-o',str(cls.binary)],check=True)
    @classmethod
    def tearDownClass(cls):cls.work.cleanup()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.dev=self.root/'dev';self.sys=self.root/'sys';self.state=self.root/'state'
        for p in [self.dev,self.state,self.sys/'class/block']:p.mkdir(parents=True)
        self.controller=self.sys/'devices/platform/soc@0/1d84000.ufshc'
        self.controller.mkdir(parents=True)
        driver=self.sys/'bus/platform/drivers/ufshcd-qcom';driver.mkdir(parents=True)
        (self.controller/'driver').symlink_to(os.path.relpath(driver,self.controller))
        self.ufs=self.controller/'host0/target0:0:0/0:0:0:0/block/sda'
        self.usb=self.sys/'devices/platform/usb1/1-1/host1/block/sdg'
        for name,parent,num in [('sda',self.ufs,'8:0'),('sda1',self.ufs/'sda1','8:1'),('sdb',self.controller/'host0/block/sdb','8:16'),('sdg',self.usb,'8:96')]:
            self.node(name,parent,num)
    def node(self,name,real,num,ro=0,pending=False):
        real.mkdir(parents=True,exist_ok=True);(real/'dev').write_text(num+'\n')
        (self.sys/'class/block'/name).symlink_to(os.path.relpath(real,self.sys/'class/block'))
        (self.dev/('.pending' if pending else name)).touch();(self.state/name).write_text(str(ro)+'\n')
    def run_scope(self,mode='--seal-ufs',mutation='none',success=True):
        r=subprocess.run([self.binary,self.dev,self.sys,self.state,mode,mutation],capture_output=True,text=True)
        self.assertEqual(r.returncode,0 if success else 1,r.stdout+r.stderr);return r
    def test_only_internal_ufs_is_sealed(self):
        self.node('sdg1',self.usb/'sdg1','8:97')
        r=self.run_scope();self.assertEqual(r.stdout,'3\n')
        self.assertNotIn('sdg',r.stderr)
        self.assertEqual([(self.state/n).read_text().strip() for n in ['sda','sda1','sdb','sdg','sdg1']],['1','1','1','0','0'])
    def test_originally_readonly_usb_is_never_reopened(self):
        (self.state/'sdg').write_text('1\n');r=self.run_scope();self.assertNotIn('sdg',r.stderr)
        self.assertEqual((self.state/'sdg').read_text(),'1\n')
    def test_list_is_readonly_and_does_not_open_any_block_device(self):
        r=self.run_scope('--list-ufs');self.assertEqual(r.stdout,'sda\nsda1\nsdb\n');self.assertEqual(r.stderr,'')
        self.assertEqual((self.state/'sda').read_text(),'0\n')

    def test_static_aarch64_musl_glob_ownership_under_qemu(self):
        cc=shutil.which('aarch64-unknown-linux-musl-gcc')
        qemu=shutil.which('qemu-aarch64')
        if not cc or not qemu:self.skipTest('Devbox ARM64 musl compiler and QEMU are required')
        binary=self.root/'scope-arm64'
        subprocess.run([cc,'-static','-Os','-Wall','-Wextra','-Werror',str(PROJECT/'tests/storage-scope-mock.c'),'-o',str(binary)],check=True)
        r=subprocess.run([qemu,binary,self.dev,self.sys,self.state,'--list-ufs','none'],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual(r.stdout,'sda\nsda1\nsdb\n');self.assertEqual(r.stderr,'')
        r=subprocess.run([qemu,binary,self.dev,self.sys,self.state,'--seal-ufs','none'],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr);self.assertNotIn('sdg',r.stderr)
        self.assertEqual((self.state/'sdg').read_text(),'0\n')
    def test_late_usb_partition_does_not_change_the_ufs_snapshot_or_flags(self):
        self.node('sdg1',self.usb/'sdg1','8:97',pending=True)
        r=self.run_scope(mutation='usb');self.assertEqual(r.stdout,'3\n');self.assertNotIn('sdg',r.stderr)
        self.assertEqual((self.state/'sdg1').read_text(),'0\n')
    def test_added_internal_partition_prevents_a_successful_seal(self):
        self.node('sda2',self.ufs/'sda2','8:2',pending=True);r=self.run_scope(mutation='ufs',success=False)
        self.assertEqual(r.stdout,'');self.assertIn('namespace changed',r.stderr)
    def test_controller_driver_is_required(self):
        (self.controller/'driver').unlink();r=self.run_scope(success=False);self.assertNotIn('SET',r.stderr)
    def test_prefix_similar_controller_is_excluded(self):
        real=self.sys/'devices/platform/soc@0/1d84000.ufshc-extra/block/sdc'
        self.node('sdc',real,'8:32');r=self.run_scope();self.assertNotIn('sdc',r.stderr)
        self.assertEqual((self.state/'sdc').read_text(),'0\n')
    def test_selected_alias_is_rejected(self):
        p=self.dev/'sda1';p.rename(self.dev/'real-sda1');p.symlink_to('real-sda1')
        r=self.run_scope(success=False);self.assertEqual(r.stdout,'')
    def test_missing_sysfs_identity_stops_scope_selection(self):
        # The existing low-level sealer fault suite exercises number/inode
        # replacement. Here an unresolvable sysfs node must fail the scope.
        (self.sys/'class/block/sdb').unlink();r=self.run_scope(success=False);self.assertEqual(r.stdout,'')
    def test_recovery_all_scsi_seal_still_locks_external_usb(self):
        self.node('sdg1',self.usb/'sdg1','8:97');r=self.run_scope('--seal')
        self.assertIn('SET sdg\n',r.stderr);self.assertIn('SET sdg1\n',r.stderr)
        self.assertEqual((self.state/'sdg1').read_text(),'1\n')

    def run_init_scope(self, abort=False, add_usb=False):
        source=(PROJECT/'device/boot/initramfs/init').read_text()
        names=['storage_path_is_block','storage_collect_sd_nodes','storage_snapshot_sd_nodes',
               'storage_lock_all','storage_snapshot_all_ro','storage_snapshot_persistent_ok','storage_abort']
        functions='\n'.join(re.search(r'^'+name+r'\(\) \{\n.*?^\}',source,re.M|re.S)[0] for name in names)
        scope=self.root/'scope'
        scope.write_text('#!/bin/sh\nexec '+shlex.join([str(self.binary),str(self.dev),str(self.sys),str(self.state)])+' "$1" none\n')
        scope.chmod(0o755)
        bb=self.root/'bb'
        bb.write_text('#!'+sys.executable+'\n'+'''import os,sys
from pathlib import Path
args=sys.argv[1:]
if args[0]=='blockdev':
 p=Path(os.environ['STATE'])/Path(args[2]).name
 if args[1]=='--getro':print(p.read_text().strip())
 elif args[1]=='--setro':p.write_text('1\\n')
 else:raise AssertionError(args)
else:os.execv(os.environ['REAL_BB'],[os.environ['REAL_BB'],*args])
''');bb.chmod(0o755)
        env=dict(os.environ,BB=str(bb),STATE=str(self.state),REAL_BB=shutil.which('busybox'),
                 LIUQIN_INIT_STORAGE_TEST_ONLY='1',storage_ufs_only='true',storage_mode='persistent',
                 storage_dev_root=str(self.dev),storage_sys_block=str(self.sys/'class/block'),
                 storage_newroot=str(self.root/'newroot'),storage_seal_helper=str(scope),
                 storage_target_name='sda1',storage_parent=str(self.dev/'sda'),storage_target=str(self.dev/'sda1'),
                 storage_root_mounted='false',storage_probe_mounted='false',storage_sd_nodes_snapshot_taken='false',
                 storage_snapshot_ufs_only='false')
        extra='mv "$storage_dev_root/.pending" "$storage_dev_root/sdg1"\n' if add_usb else ''
        script='set -e\nlog() { :; }\n'+functions+'''\nstorage_lock_all
storage_snapshot_sd_nodes
printf '0\\n' >"$STATE/sda"
printf '0\\n' >"$STATE/sda1"
'''+extra+'storage_snapshot_persistent_ok\n'+('storage_abort\n[ "$storage_ufs_only" = false ]\n[ "$storage_fallback_safe" = true ]\n' if abort else '')
        r=subprocess.run(['sh','-c',script],env=env,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)

    def test_real_init_gate_ignores_late_usb_partition(self):
        self.node('sdg1',self.usb/'sdg1','8:97',pending=True)
        self.run_init_scope(add_usb=True)
        self.assertEqual((self.state/'sdg').read_text(),'0\n')
        self.assertEqual((self.state/'sdg1').read_text(),'0\n')
        self.assertEqual((self.state/'sdb').read_text(),'1\n')

    def test_rejected_desktop_returns_to_all_scsi_ram_baseline(self):
        self.node('sdg1',self.usb/'sdg1','8:97')
        self.run_init_scope(abort=True)
        self.assertTrue(all(p.read_text()=='1\n' for p in self.state.iterdir()))

    def test_real_init_selector_uses_moved_dev_and_sys_before_switch_root(self):
        newroot=self.root/'newroot';newroot.mkdir()
        olddev,oldsys=self.dev,self.sys
        self.dev.rename(newroot/'dev');self.sys.rename(newroot/'sys')
        self.dev=newroot/'dev';self.sys=newroot/'sys'
        helper=self.root/'scope-after-move'
        prefix=[str(self.binary),str(olddev),str(oldsys),str(self.state),'--list-ufs','none']
        helper.write_text('#!/bin/sh\n[ "$1" = --list-ufs ] || exit 2\nexec '+shlex.join(prefix)+' "$2" "$3"\n');helper.chmod(0o755)
        source=(PROJECT/'device/boot/initramfs/init').read_text()
        function=re.search(r'^storage_collect_sd_nodes\(\) \{\n.*?^\}',source,re.M|re.S)[0]
        env=dict(os.environ,storage_ufs_only='true',storage_dev_root=str(olddev),storage_sys_block=str(oldsys/'class/block'),storage_newroot=str(newroot),storage_seal_helper=str(helper),NEWDEV=str(self.dev))
        r=subprocess.run(['sh','-c',function+'\nstorage_collect_sd_nodes "$NEWDEV"\n'],env=env,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.stdout,'sda\nsda1\nsdb\n')

class MigrationTests(unittest.TestCase):
    def test_upgrade_removes_only_exact_old_payload_and_preserves_modified_files(self):
        sys.path.insert(0,str(PROJECT/'build/lib'))
        import storage_scope
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);content=b'old project file\n';digest=hashlib.sha256(content).hexdigest()
            names=['etc/test/old.service','usr/libexec/old-helper','etc/test/alias.conf']
            for name in names:
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(content)
            (root/names[1]).write_text('administrator changes\n')
            target=root/'private-target';target.write_bytes(content)
            (root/names[2]).unlink();(root/names[2]).symlink_to(target)
            with patch.object(storage_scope,'LEGACY_FILES',{name:digest for name in names}), patch.object(storage_scope,'LEGACY_LINKS',{}):
                script=storage_scope.legacy_cleanup()
            for name in names:script=script.replace('/'+name,str(root/name))
            script=script.replace('/run/systemd/system',str(root/'run/systemd/system'))
            subprocess.run(['sh','-eu','-c',script],check=True,capture_output=True)
            self.assertFalse((root/names[0]).exists())
            self.assertEqual((root/names[1]).read_text(),'administrator changes\n')
            self.assertTrue((root/names[2]).is_symlink());self.assertEqual(target.read_bytes(),content)

    def test_retired_activation_and_empty_marker_are_removed_without_following_other_links(self):
        sys.path.insert(0,str(PROJECT/'build/lib'))
        import storage_scope
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);marker='etc/test/rescue-enabled';link='etc/test/wants/old.service';other='etc/test/wants/custom.service'
            (root/marker).parent.mkdir(parents=True);(root/marker).touch()
            (root/link).parent.mkdir();(root/link).symlink_to('../old.service');(root/other).symlink_to('../administrator.service')
            with patch.object(storage_scope,'LEGACY_FILES',{marker:hashlib.sha256(b'').hexdigest()}), \
                 patch.object(storage_scope,'LEGACY_LINKS',{link:'../old.service',other:'../old.service'}):
                script=storage_scope.legacy_cleanup()
            for name in (marker,link,other):script=script.replace('/'+name,str(root/name))
            script=script.replace('/run/systemd/system',str(root/'run/systemd/system'))
            subprocess.run(['sh','-eu','-c',script],check=True,capture_output=True)
            self.assertFalse((root/marker).exists());self.assertFalse((root/link).is_symlink())
            self.assertEqual(os.readlink(root/other),'../administrator.service')

if __name__=='__main__':unittest.main(verbosity=2)
