#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise public theme generation, boot selection and shutdown cache boundaries."""
from pathlib import Path
import importlib.util
import json
import os
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT/'build/lib'), str(PROJECT/'shared')]
from splash_assets import bootargs, install_themes, supports_firmware, theme_files

D = runpy.run_path(str(PROJECT/'device/board-overlay/usr/libexec/liuqin-splash-orientation'))
G = D['prepare'].__globals__
spec = importlib.util.spec_from_file_location('fedora_root', PROJECT/'build/scripts/build-fedora-root.py')
root_builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(root_builder)


class Assets(unittest.TestCase):
    def test_bootargs_preserve_unrelated_options_and_are_idempotent(self):
        original = 'quiet root=UUID=test initcall_blacklist=simplefb_driver_init,other rhgb'
        result = bootargs(original, True)
        self.assertEqual(result, bootargs(result, True))
        self.assertIn('initcall_blacklist=other', result)
        self.assertIn('root=UUID=test', result)
        self.assertNotIn('simplefb_driver_init', result)
        self.assertIn('fbcon=map:9', result)
        self.assertEqual(bootargs(original, False), original)
        for argument in ('fbcon=map:0', 'liuqin.firmware_splash=0', 'liuqin.splash_orientation=0'):
            with self.assertRaises(ValueError): bootargs(original+' '+argument, True)

    def test_legacy_runtime_and_builtin_backlight_do_not_enable_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); config = root/'config'; manifest = root/'runtime.json'
            for record, kernel, expected in [({}, 'm', False), ({'firmware_splash':True}, 'y', False),
                                              ({'firmware_splash':True}, 'm', True)]:
                manifest.write_text(json.dumps(record));config.write_text('CONFIG_BACKLIGHT_KTZ8866='+kernel+'\n')
                self.assertEqual(supports_firmware(root,config),expected)
            manifest.write_text('{"firmware_splash": "yes"}')
            with self.assertRaises(ValueError): supports_firmware(root,config)

    def test_all_theme_files_install_without_following_destination_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'root';root.mkdir()
            install_themes(root)
            files=theme_files()
            self.assertEqual(len(files),8)
            for name,content in files.items():
                self.assertEqual((root/name).read_text(),content)
                self.assertNotIn('@ROTATION@',content)
                self.assertEqual((root/name).stat().st_mode&0o777,0o644)
            for angle in (0,90,180,270):
                theme=files[f'usr/share/plymouth/themes/liuqin-rotate-{angle}/theme.script']
                self.assertIn(f'rotation = {angle};',theme)
            outside=Path(tmp)/'outside';outside.mkdir()
            (root/'usr/share/plymouth/themes/liuqin-rotate-0/theme.script').unlink()
            (root/'usr/share/plymouth/themes/liuqin-rotate-0/theme.script').symlink_to(outside/'escaped')
            with self.assertRaises(ValueError):install_themes(root)
            self.assertFalse((outside/'escaped').exists())


class Direction(unittest.TestCase):
    def test_four_transforms(self):
        for transform,angle in [(0,0),(1,270),(2,180),(3,90)]:
            self.assertEqual(D['parse_observation']({'version':1,'output':'DSI-1','transform':transform,'angle':angle}),angle)

    def test_reject_unsupported_or_inconsistent_events(self):
        for message in [None,{}, {'version':1,'output':'HDMI-1','transform':3,'angle':90},
                        {'version':1,'output':'DSI-1','transform':True,'angle':270},
                        {'version':1,'output':'DSI-1','transform':4,'angle':0},
                        {'version':1,'output':'DSI-1','transform':3,'angle':270}]:
            with self.assertRaises(ValueError):D['parse_observation'](message)

    def test_teardown_keeps_last_direction_then_new_session_replaces_it(self):
        cache=D['DirectionCache']()
        self.assertEqual(cache.selection(180),(180,'boot-fallback',None))
        cache.observe({'version':1,'output':'DSI-1','transform':3,'angle':90},'c1',989,3)
        cache.observe({'version':1,'unavailable':True},'c1',989,4)
        self.assertEqual(cache.selection(180),(90,'wayland-output-cache','c1'))
        cache.observe({'version':1,'output':'DSI-1','transform':1,'angle':270},'2',1000,5)
        self.assertEqual(cache.selection(180),(270,'wayland-output-cache','2'))

    def test_command_line_preserves_args_and_replaces_only_theme(self):
        self.assertEqual(D['command_line'](270,['rhgb','quiet','plymouth.splash=old','root=UUID=example']),
                         'rhgb quiet root=UUID=example plymouth.splash=liuqin-rotate-270')
        for angle in [True,'90',45]:
            with self.assertRaises(ValueError):D['command_line'](angle,[])
        with self.assertRaises(ValueError):D['command_line'](90,['bad\narg'])

    def test_direct_daemon_entrypoint_and_single_environment_argument(self):
        for name,mode in [('poweroff','shutdown'),('halt','shutdown'),('reboot','reboot')]:
            text=root_builder.boot_splash_files()['etc/systemd/system/plymouth-'+name+'.service.d/30-liuqin-orientation.conf']
            start=next(line for line in text.splitlines() if line.startswith('ExecStart=/'))
            self.assertTrue(start.startswith('ExecStart=/usr/bin/plymouthd --mode='+mode))
            self.assertIn('--kernel-command-line=${LIUQIN_PLYMOUTH_CMDLINE}',start)
            self.assertIn('ExecStartPre=-/usr/bin/python3',text)
            self.assertNotIn('/bin/sh',start)

    def prepared(self,record,enabled=True,symlink=False):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp);cache=state/'shutdown.json'
            if record is not None:cache.write_text(json.dumps(record))
            if symlink:
                cache.rename(state/'other');cache.symlink_to(state/'other')
            read_text=Path.read_text
            def read(path,*args,**kwargs):
                if path==Path('/proc/cmdline'):return 'rhgb quiet root=UUID=test plymouth.splash=old'
                return read_text(path,*args,**kwargs)
            # Ownership is checked against the executing account in this
            # isolated test; the installed helper always requires UID 0.
            fake_stat=os.stat_result((0o100600,0,0,0,0,0,100,0,0,0))
            real_lstat=Path.lstat
            def lstat(path,*args,**kwargs):
                if path==cache and not symlink:return fake_stat
                return real_lstat(path,*args,**kwargs)
            with patch.dict(G,STATE=state,ensure_state=lambda:None,enabled=lambda:enabled,
                            boot_id=lambda:'current',fallback=lambda:90), \
                 patch.object(Path,'read_text',read),patch.object(Path,'lstat',lstat), \
                 patch.object(G['time'],'monotonic',return_value=100):
                D['prepare']()
            return json.loads((state/'plymouth.env').read_text().split('=',1)[1])

    def test_prepare_accepts_only_recent_same_boot_valid_cached_angle(self):
        record={'version':2,'boot_id':'current','angle':270,'monotonic':90,'source':'wayland-output-cache'}
        self.assertTrue(self.prepared(record).endswith('liuqin-rotate-270'))
        for change in ({'boot_id':'other'},{'monotonic':-30},{'monotonic':101},{'monotonic':float('nan')},
                       {'version':1},{'angle':True},{'angle':45}):
            self.assertTrue(self.prepared(dict(record,**change)).endswith('liuqin-rotate-90'))
        self.assertTrue(self.prepared(None).endswith('liuqin-rotate-90'))
        self.assertTrue(self.prepared(record,symlink=True).endswith('liuqin-rotate-90'))
        self.assertEqual(self.prepared(record,enabled=False),'rhgb quiet root=UUID=test plymouth.splash=old')


if __name__=='__main__': unittest.main()
