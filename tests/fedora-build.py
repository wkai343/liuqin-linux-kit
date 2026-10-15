#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check Fedora routing, OCI rejection and the actual cross-architecture RPM format."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'build/lib'),str(PROJECT/'shared')]
import fedora_oci
from fedora_flow import FedoraFlow
from rpm_package import build as rpm
spec=importlib.util.spec_from_file_location('fedora_root_builder',PROJECT/'build/scripts/build-fedora-root.py')
root_builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(root_builder)


class FedoraTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.work=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def image(self, architecture='arm64', member='etc/test'):
        layer=io.BytesIO()
        with tarfile.open(fileobj=layer,mode='w') as tar:
            entry=tarfile.TarInfo(member);data=b'test';entry.size=len(data)
            entry.uid=os.getuid();entry.gid=os.getgid();entry.mode=0o644
            tar.addfile(entry,io.BytesIO(data))
        blobs={}
        def add(data,media):
            digest=hashlib.sha256(data).hexdigest();blobs['blobs/sha256/'+digest]=data
            return dict(mediaType=media,digest='sha256:'+digest,size=len(data))
        config=add(json.dumps(dict(architecture=architecture,os='linux')).encode(),'application/vnd.oci.image.config.v1+json')
        descriptor=add(layer.getvalue(),'application/vnd.oci.image.layer.v1.tar')
        manifest=add(json.dumps(dict(schemaVersion=2,config=config,layers=[descriptor])).encode(),'application/vnd.oci.image.manifest.v1+json')
        blobs['index.json']=json.dumps(dict(schemaVersion=2,manifests=[manifest])).encode()
        target=self.work/'image.tar'
        with tarfile.open(target,'w') as tar:
            for name,data in blobs.items():
                entry=tarfile.TarInfo(name);entry.size=len(data);tar.addfile(entry,io.BytesIO(data))
        return target,hashlib.sha256(target.read_bytes()).hexdigest()

    def test_oci_valid_layer_uses_numeric_owners(self):
        archive,digest=self.image();root=self.work/'root';fedora_oci.extract(archive,digest,root)
        self.assertEqual((root/'etc/test').read_bytes(),b'test')
        self.assertEqual((root/'etc/test').stat().st_uid,os.getuid())

    def test_oci_wrong_architecture_stops_before_creating_root(self):
        archive,digest=self.image(architecture='amd64');root=self.work/'root'
        with self.assertRaisesRegex(ValueError,'ARM64'):fedora_oci.extract(archive,digest,root)
        self.assertFalse(root.exists())

    def test_oci_traversal_stops_before_creating_root(self):
        archive,digest=self.image(member='../escape');root=self.work/'root'
        with self.assertRaisesRegex(ValueError,'越界'):fedora_oci.extract(archive,digest,root)
        self.assertFalse(root.exists());self.assertFalse((self.work/'escape').exists())

    def test_fedora_target_retains_an_independent_ubuntu_installer(self):
        original=dict(rootfs_config=str(PROJECT/'build/rootfs/fedora-plasma.json'),kernel_fragments=[],
            kernel_localversion='-liuqin-test',kernel_source='sources/test',package_version='0.1.0',jobs=2)
        flow=FedoraFlow(original,lambda *_a,**_k:self.fail('unexpected build'))
        self.assertEqual(original['kernel_fragments'],[])
        self.assertIn('build/kernel/configs/fedora.config',flow.data['kernel_fragments'])
        self.assertIn('-fedora',flow.data['kernel_localversion'])
        self.assertEqual(flow.source['release'],'44')
        with patch('source_flow.download',return_value=self.work/'installer-base') as download:
            self.assertEqual(flow.base(),self.work/'installer-base')
        self.assertIn('ubuntu-base',str(download.call_args.args[1]))
        self.assertIn('fedora-plasma',flow.plan()['output'])

    def test_v2_root_accepts_only_the_known_fedora_sbin_merge(self):
        sys.path.insert(0,str(PROJECT/'tests'))
        from kernel_fixtures import kernel_fixture,install_fixture,archive_root
        from liuqin_artifacts import inspect_root,contract_hashes,ArtifactError
        from liuqin_kernel import verify_installed
        fixture=kernel_fixture(self.work/'kernel','6.17.0-liuqin-fedora')
        root=self.work/'root';root.mkdir();install_fixture(root,'6.17.0-liuqin-fedora',fixture)
        directory=root/'usr/local/sbin';directory.rename(root/'usr/local/bin');directory.symlink_to('bin')
        archive=self.work/'root.tar.gz';archive_root(root,archive)
        inspect_root(archive,contract_hashes(fixture[3]),profile='native-updatable')
        verify_installed(root,fixture[3])
        directory.unlink();directory.symlink_to('/other/bin');archive_root(root,archive)
        with self.assertRaises(ValueError):verify_installed(root,fixture[3])
        with self.assertRaisesRegex(ArtifactError,'存储保护'):
            inspect_root(archive,contract_hashes(fixture[3]),profile='native-updatable')

    def test_installed_fedora_guard_alias_keeps_owner_and_executable_checks(self):
        sys.path.insert(0,str(PROJECT/'tests'))
        from kernel_fixtures import kernel_fixture,install_fixture
        from liuqin_kernel import verify_installed
        fixture=kernel_fixture(self.work/'kernel','6.17.0-liuqin-fedora')
        root=self.work/'root';root.mkdir();install_fixture(root,'6.17.0-liuqin-fedora',fixture)
        directory=root/'usr/local/sbin';directory.rename(root/'usr/local/bin');directory.symlink_to('bin')
        guard=root/'usr/local/bin/liuqin-gnome-storage-guard'
        guard.chmod(0o777)
        with self.assertRaisesRegex(ValueError,'权限'):verify_installed(root,fixture[3])
        guard.chmod(0o755);guard.rename(guard.with_name('real-guard'));guard.symlink_to('real-guard')
        with self.assertRaisesRegex(ValueError,'普通文件'):verify_installed(root,fixture[3])

    def test_input_environment_preserves_native_wayland_and_enables_x11_modules(self):
        script=PROJECT/'build/rootfs/fedora-input-env.sh'
        shell='GTK_IM_MODULE=ibus; QT_IM_MODULE=ibus; QT_IM_MODULES="wayland;fcitx"; SDL_IM_MODULE=ibus; . "$1"; printf "%s\\n" "$XMODIFIERS" "${QT_IM_MODULES-unset}" "${GTK_IM_MODULE-unset}" "${QT_IM_MODULE-unset}" "${SDL_IM_MODULE-unset}"'
        for session,expected in [('wayland',['@im=fcitx','unset','unset','unset','unset']),
                                 ('x11',['@im=fcitx','unset','fcitx','fcitx','fcitx'])]:
            result=subprocess.run(['sh','-c',shell,'sh',str(script)],env=dict(os.environ,XDG_SESSION_TYPE=session),capture_output=True,text=True,check=True)
            self.assertEqual(result.stdout.splitlines(),expected)

    def test_fedora_can_replace_maliit_but_keeps_the_first_setup_keyboard(self):
        recipe=json.loads((PROJECT/'build/rootfs/fedora-plasma.json').read_text())
        path=self.work/'recipe.json';path.write_text(json.dumps(recipe))
        root_builder.common.recipe(path,'fedora')
        recipe['remove'].append('plasma-keyboard');path.write_text(json.dumps(recipe))
        with self.assertRaisesRegex(ValueError,'首次初始化'):
            root_builder.common.recipe(path,'fedora')

    def test_fcitx_profile_does_not_start_a_second_wayland_input_method(self):
        root=self.work/'root';binary=root/'usr/bin/fcitx5';binary.parent.mkdir(parents=True);binary.touch()
        root_builder.configure_input(root)
        self.assertIn('org.kde.plasma.keyboard.desktop',(root/'etc/xdg/kwinrc').read_text())
        self.assertIn('DefaultIM=pinyin',(root/'etc/xdg/fcitx5/profile').read_text())
        self.assertEqual((root/'etc/xdg/plasma-workspace/env/90-liuqin-fcitx5.sh').stat().st_mode&0o777,0o755)
        script=root/'usr/libexec/liuqin-fcitx5-autostart'
        self.assertEqual(script.stat().st_mode&0o777,0o755)
        subprocess.run(['sh',str(script)],env=dict(os.environ,XDG_SESSION_TYPE='wayland'),check=True)

    def test_firefox_chinese_registration_uses_local_packs_and_preserves_existing_files(self):
        root=self.work/'root';base=root/'usr/lib64/firefox';(base/'langpacks').mkdir(parents=True)
        (base/'firefox').touch()
        for locale in ('zh-CN','zh-TW'):
            (base/'langpacks'/('langpack-'+locale+'@firefox.mozilla.org.xpi')).write_bytes(b'package-managed XPI')
        root_builder.configure_firefox_languages(root)
        for locale in ('zh-CN','zh-TW'):
            link=base/'browser/extensions'/('langpack-'+locale+'@firefox.mozilla.org.xpi')
            self.assertEqual(link.read_bytes(),b'package-managed XPI')
            self.assertTrue(link.is_symlink())
        existing=base/'browser/extensions/langpack-zh-CN@firefox.mozilla.org.xpi'
        existing.unlink();existing.write_bytes(b'existing distribution registration')
        root_builder.configure_firefox_languages(root)
        self.assertEqual(existing.read_bytes(),b'existing distribution registration')
        self.assertFalse((root/'home').exists())

    def test_firefox_language_registration_cannot_write_outside_root(self):
        root=self.work/'root';base=root/'usr/lib64/firefox';(base/'langpacks').mkdir(parents=True)
        (base/'firefox').touch();(base/'langpacks/langpack-zh-CN@firefox.mozilla.org.xpi').touch()
        outside=self.work/'outside';outside.mkdir();(base/'browser').symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'离开根文件系统'):
            root_builder.configure_firefox_languages(root)
        self.assertEqual(list(outside.iterdir()),[])

    def test_flathub_configuration_preserves_remote_signature_verification(self):
        root=self.work/'root';binary=root/'usr/bin/flatpak';binary.parent.mkdir(parents=True);binary.touch()
        with patch.object(root_builder,'chroot') as command:root_builder.configure_flatpak(root)
        self.assertEqual(command.call_args.args[1],['flatpak','--system','remote-add','--if-not-exists','--from','flathub','https://dl.flathub.org/repo/flathub.flatpakrepo'])

    def test_flatpak_cleanup_only_stops_ostree_agents_in_private_pid_namespace(self):
        root=self.work/'root'
        for pid,home in [(42,b'/tmp/ostree-gpg-test'),(43,b'/root/.gnupg')]:
            process=root/'proc'/str(pid);process.mkdir(parents=True)
            (process/'cmdline').write_bytes(b'qemu-aarch64\0/usr/bin/gpg-agent\0--homedir\0'+home+b'\0')
            (process/'status').write_text('State:\tZ (zombie)\n')
        with patch.object(root_builder.os,'getpid',return_value=1),patch.object(root_builder.os,'kill') as terminate:
            root_builder.stop_flatpak_agents(root)
        terminate.assert_called_once_with(42,root_builder.signal.SIGTERM)
        with patch.object(root_builder.os,'getpid',return_value=999),patch.object(root_builder.os,'kill') as terminate:
            with self.assertRaisesRegex(RuntimeError,'PID 命名空间'):root_builder.stop_flatpak_agents(root)
        terminate.assert_not_called()

    def test_missing_battery_service_prevents_desktop_publication(self):
        root=self.work/'root'
        # A working Plasma session and a CLI alone are insufficient: the
        # on-demand system service must be present even without weak deps.
        for name in ('usr/bin/dbus-run-session','usr/lib64/security/pam_systemd.so',
                     'usr/sbin/ip','usr/bin/startplasma-wayland','usr/bin/startplasma-login-wayland',
                     'usr/bin/upower','usr/libexec/upowerd'):
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.touch()
        with patch.object(root_builder,'chroot',side_effect=AssertionError('must not run')):
            with self.assertRaisesRegex(ValueError,'upower.service'):
                root_builder.check_desktop_runtime(root)

    def test_empty_shell_template_stops_before_account_or_system_configuration(self):
        root=self.work/'root';skel=root/'etc/skel';skel.mkdir(parents=True)
        (skel/'.bashrc').write_text('');(skel/'.bash_profile').write_text('. ~/.bashrc\n')
        with patch.object(root_builder,'chroot',side_effect=AssertionError('must not run')):
            with self.assertRaisesRegex(ValueError,'模板为空'):
                root_builder.configure(root,{'hostname':'liuqin','first_boot_setup':True},self.work)
        self.assertFalse((root/'etc/hostname').exists())

    def desktop_fixture(self):
        root=self.work/'root'
        for name in ('usr/bin/dbus-run-session','usr/lib64/security/pam_systemd.so',
                     'usr/sbin/ip','usr/bin/startplasma-wayland','usr/bin/startplasma-login-wayland',
                     'usr/bin/upower','usr/libexec/upowerd','usr/lib/systemd/system/upower.service',
                     'usr/share/dbus-1/system-services/org.freedesktop.UPower.service'):
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.touch()
        return root

    def test_desktop_probe_does_not_require_a_live_power_daemon(self):
        root=self.desktop_fixture();probed=[]
        def chroot(_root,command,**_kwargs):
            if command[0] in ('/usr/bin/upower','/usr/libexec/upowerd'):
                if command[1:]!=['--help']:
                    raise subprocess.CalledProcessError(1,command,stderr='No system bus in image assembly')
                probed.append(command[0])
        with patch.object(root_builder,'chroot',side_effect=chroot):
            root_builder.check_desktop_runtime(root)
        self.assertEqual(set(probed),{'/usr/bin/upower','/usr/libexec/upowerd'})

    def test_power_daemon_loader_failure_still_stops_image_publication(self):
        root=self.desktop_fixture()
        def chroot(_root,command,**_kwargs):
            if command[0]=='/usr/libexec/upowerd':
                raise subprocess.CalledProcessError(127,command,stderr='missing shared library')
        with patch.object(root_builder,'chroot',side_effect=chroot),self.assertRaises(subprocess.CalledProcessError):
            root_builder.check_desktop_runtime(root)

    def test_preconfigured_account_also_repairs_incomplete_skeleton_copy(self):
        root=self.work/'root';skel=root/'etc/skel';skel.mkdir(parents=True)
        for name in ('.bashrc','.bash_profile'):(skel/name).write_text('# official default '+name+'\n')
        def create_account(*_args,**_kwargs):
            home=root/'home/pad';home.mkdir(parents=True);(home/'.bashrc').touch()
            (root/'etc/passwd').write_text(f'pad:x:{os.getuid()}:{os.getgid()}::/home/pad:/bin/bash\n')
        with patch.object(root_builder,'chroot'),patch.object(root_builder.common,'configure_account',side_effect=create_account):
            root_builder.configure(root,{'hostname':'liuqin','first_boot_setup':False,'user':'pad'},self.work)
        for name in ('.bashrc','.bash_profile'):
            self.assertEqual((root/'home/pad'/name).read_bytes(),(skel/name).read_bytes())
        self.assertEqual((root/'etc/plasma-setup-done').read_text(),'preconfigured\n')

    def test_first_boot_uses_repository_package_without_a_setup_compilation_stage(self):
        data=dict(rootfs_config=str(PROJECT/'build/rootfs/fedora-plasma.json'),kernel_fragments=[],
            kernel_localversion='-liuqin-test',kernel_source='sources/test',package_version='0.1.0')
        flow=FedoraFlow(data,lambda *_a,**_k:self.fail('unexpected build'))
        archive=self.work/'base.tar';archive.write_bytes(b'base')
        devices=self.work/'device-rpms';devices.mkdir();(devices/'liuqin-device.rpm').write_bytes(b'device')
        stats=self.work/'ksystemstats.rpm';stats.write_bytes(b'stats')
        fingerprint=self.work/'liuqin-fingerprint.rpm';fingerprint.write_bytes(b'fingerprint')
        def stage(_name,_inputs,build,_artifacts):
            output=self.work/'stage';output.mkdir();build(output);return output
        with patch.object(flow,'target_runtime_root',return_value=archive), \
             patch.object(flow,'device_debs',return_value=devices), \
             patch.object(flow,'stats_rpm',return_value=stats), \
             patch.object(flow,'fingerprint_rpm',return_value=fingerprint), \
             patch.object(flow,'first_boot_deb',side_effect=AssertionError('must not compile setup')), \
             patch.object(flow,'setup_archive',side_effect=AssertionError('must not download setup source')), \
             patch.object(flow,'stage',side_effect=stage),patch.object(flow,'target_action') as action:
            flow.apps()
        self.assertEqual(action.call_args.args[0],'apps')
        rpms=action.call_args.kwargs['rpms']
        self.assertEqual(sorted(p.name for p in rpms.iterdir()),['ksystemstats.rpm','liuqin-device.rpm','liuqin-fingerprint.rpm'])
        self.assertIn('Fedora 官方 plasma-setup 软件包',flow.plan()['steps'])

    def test_official_setup_installation_follows_first_boot_recipe(self):
        for enabled in (False,True):
            with self.subTest(enabled=enabled),patch.object(root_builder,'dnf') as dnf:
                root_builder.install_desktop_packages(self.work,dict(first_boot_setup=enabled,packages=['kate']))
                requested=dnf.call_args_list[0].args
                self.assertEqual('plasma-setup' in requested,enabled)
                self.assertIn('plasma-keyboard',requested)
                self.assertIn('kate',requested)

    def test_documentation_filter_preserves_other_transaction_flags_and_settings(self):
        root=self.work/'root';config=root/'etc/dnf/dnf.conf';config.parent.mkdir(parents=True)
        config.write_text('[main]\ntsflags = nodocs,nocontexts # keep this comment\ngpgcheck=True\n')
        root_builder.enable_documentation(root)
        self.assertEqual(config.read_text(),'[main]\ntsflags = nocontexts # keep this comment\ngpgcheck=True\n')
        self.assertIn('%_excludedocs 0',(root/'etc/rpm/macros.liuqin').read_text())
        original=config.read_bytes();root_builder.enable_documentation(root)
        self.assertEqual(config.read_bytes(),original)

    def test_missing_base_manual_is_reinstalled_without_repeating_existing_manuals(self):
        root=self.work/'root';present=root/'usr/share/man/man1/ls.1.gz';present.parent.mkdir(parents=True);present.write_bytes(b'page')
        missing=root/'usr/share/man/man1/bash.1.gz';transactions=[]
        def chroot(_root,args,**_kw):
            text='bash\ncoreutils\n' if args[1]=='-qa' else 'bash\t/usr/share/man/man1/bash.1.gz\t2\ncoreutils\t/usr/share/man/man1/ls.1.gz\t2\ncoreutils\t/usr/share/man/man1/optional.1.gz\t66\n'
            return subprocess.CompletedProcess(args,0,stdout=text)
        def dnf(_root,*args):
            transactions.append(args)
            if args[0]=='reinstall':missing.write_bytes(b'restored page')
        with patch.object(root_builder,'chroot',side_effect=chroot),patch.object(root_builder,'dnf',side_effect=dnf):
            root_builder.restore_core_manpages(root)
            root_builder.restore_core_manpages(root)
        self.assertEqual(transactions,[('upgrade','bash'),('reinstall','bash')])

    def test_image_cleanup_removes_host_keys_without_touching_ssh_configuration_or_link_targets(self):
        root=self.work/'root';ssh=root/'etc/ssh';ssh.mkdir(parents=True)
        config=ssh/'sshd_config';config.write_text('configuration')
        (ssh/'ssh_host_ed25519_key').write_text('image-only key')
        outside=self.work/'outside';outside.write_text('must remain')
        (ssh/'ssh_host_ed25519_key.pub').symlink_to(outside)
        root_builder.clean_ssh_identity(root)
        self.assertEqual([p.name for p in ssh.iterdir()],['sshd_config'])
        self.assertEqual(config.read_text(),'configuration');self.assertEqual(outside.read_text(),'must remain')

    @unittest.skipUnless(shutil.which('rpmbuild') and shutil.which('rpm'),'RPM tools unavailable')
    def test_real_rpm_has_aarch64_header_dependencies_and_paths(self):
        payload=self.work/'payload';binary=payload/'usr/libexec/liuqin-test'
        binary.parent.mkdir(parents=True);binary.write_text('#!/bin/sh\nexit 0\n');binary.chmod(0o755)
        package=rpm(payload,'liuqin-format-test','0.1.0',self.work/'packages',requires=['python3 >= 3.11'])
        arch=subprocess.check_output(['rpm','-qp','--qf','%{ARCH}',str(package)],text=True)
        dependencies=subprocess.check_output(['rpm','-qp','--requires',str(package)],text=True)
        provides=subprocess.check_output(['rpm','-qp','--provides',str(package)],text=True)
        files=subprocess.check_output(['rpm','-qpl',str(package)],text=True)
        self.assertEqual(arch,'aarch64');self.assertIn('python3 >= 3.11',dependencies)
        self.assertIn('liuqin-format-test(aarch-64) = 0.1.0-1.liuqin',provides)
        self.assertIn('/usr/libexec/liuqin-test',files);self.assertNotIn('/etc/apt',files)

    @unittest.skipUnless(shutil.which('rpmbuild') and shutil.which('rpm'),'RPM tools unavailable')
    def test_arch_qualified_consumer_accepts_native_provider_and_rejects_noarch(self):
        payload=self.work/'payload';file=payload/'usr/share/liuqin-test/payload'
        file.parent.mkdir(parents=True);file.write_text('test data\n')
        native=rpm(payload,'liuqin-provider','1.0',self.work/'native')
        noarch=rpm(payload,'liuqin-provider','1.0',self.work/'noarch',architecture='noarch')
        consumer=rpm(payload,'liuqin-consumer','1.0',self.work/'consumer',
                     requires=['liuqin-provider(aarch-64) >= 1.0'])
        db=self.work/'rpmdb';db.mkdir()
        command=['rpm','--dbpath',str(db),'--test','--ignorearch','-U',str(consumer)]
        accepted=subprocess.run([*command,str(native)],text=True,capture_output=True)
        self.assertEqual(accepted.returncode,0,accepted.stdout+accepted.stderr)
        rejected=subprocess.run([*command,str(noarch)],text=True,capture_output=True)
        self.assertNotEqual(rejected.returncode,0)
        self.assertIn('liuqin-provider(aarch-64)',rejected.stdout+rejected.stderr)
        self.assertEqual(subprocess.check_output(['rpm','--dbpath',str(db),'-qa'],text=True),'')


if __name__=='__main__':unittest.main(verbosity=2)
