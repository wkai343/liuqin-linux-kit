#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run sensor environment helpers without mounts, root, network or devices."""
import json
import contextlib
import importlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
HELPER = PROJECT / 'device/sensors/build-environment.sh'


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.tools=self.root/'tools';self.tools.mkdir()
        tool=self.tools/'chroot'
        # Exercise the real /usr/bin/env after a fake root transition.
        tool.write_text('#!'+sys.executable+'\nimport os, sys\n'
                        'assert sys.argv[1] == "/fixture-root"\n'
                        'assert sys.argv[2] == "/usr/bin/env"\n'
                        'os.execv(sys.argv[2],sys.argv[2:])\n')
        tool.chmod(0o755)

    def tearDown(self):self.temporary.cleanup()

    def guest(self,code):
        env=dict(os.environ,PATH=str(self.tools)+os.pathsep+os.environ['PATH'],JOBS='7',LC_ALL='C',
                 PKG_CONFIG_PATH='/nix/fixture/pkgconfig',CROSS_COMPILE='fixture-cross-',CC='/nix/fixture/gcc',
                 HOST_ONLY_SETTING='fixture',http_proxy='http://proxy.invalid:8080',
                 https_proxy='http://proxy.invalid:8443',no_proxy='localhost')
        result=subprocess.run(['/bin/sh','-c','. "$1"; run_sensor_chroot /fixture-root "$2" -c "$3"',
                               'sensor-test',str(HELPER),sys.executable,code],env=env,capture_output=True,text=True,check=True)
        return json.loads(result.stdout)

    def test_guest_gets_ubuntu_paths_and_never_nix_compiler_environment(self):
        keys=('PATH','JOBS','LANG','DEBIAN_FRONTEND','PKG_CONFIG_PATH','CROSS_COMPILE','CC','HOST_ONLY_SETTING')
        data=self.guest('import json, os; print(json.dumps({key:os.environ.get(key) for key in '+repr(keys)+'}))')
        self.assertEqual(data['PATH'],'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
        self.assertEqual(data['JOBS'],'7');self.assertEqual(data['LANG'],'C.UTF-8')
        self.assertEqual(data['DEBIAN_FRONTEND'],'noninteractive')
        for key in ('PKG_CONFIG_PATH','CROSS_COMPILE','CC','HOST_ONLY_SETTING'):self.assertIsNone(data[key])

    def test_upstream_numeric_locale_override_is_not_blocked(self):
        data=self.guest('import json, os; os.environ["LC_NUMERIC"]="fr_FR.UTF-8"; '
                        'print(json.dumps({"LC_ALL":os.environ.get("LC_ALL"),"LC_NUMERIC":os.environ["LC_NUMERIC"]}))')
        self.assertIsNone(data['LC_ALL']);self.assertEqual(data['LC_NUMERIC'],'fr_FR.UTF-8')

    def test_network_proxy_settings_are_retained(self):
        data=self.guest('import json, os; print(json.dumps({key:os.environ.get(key) for key in '
                        '("http_proxy","https_proxy","no_proxy")}))')
        self.assertEqual(data,{'http_proxy':'http://proxy.invalid:8080','https_proxy':'http://proxy.invalid:8443','no_proxy':'localhost'})

    def qemu(self,dynamic=False,static_name=False):
        for name in ('qemu-aarch64','readelf'):
            p=self.tools/name;p.write_text('#!/bin/sh\nprintf "%s\\n" "${SENSOR_TEST_HEADER:-LOAD}"\n');p.chmod(0o755)
        if static_name:shutil.copyfile(self.tools/'qemu-aarch64',self.tools/'qemu-aarch64-static');(self.tools/'qemu-aarch64-static').chmod(0o755)
        (self.tools/'grep').symlink_to(shutil.which('grep'))
        env=dict(os.environ,PATH=str(self.tools),SENSOR_TEST_HEADER='INTERP' if dynamic else 'LOAD',QEMU_AARCH64='')
        return subprocess.run(['/bin/sh','-c','. "$1"; sensor_static_qemu','sensor-test',str(HELPER)],
                              env=env,capture_output=True,text=True)

    def test_devbox_static_qemu_name_is_supported(self):
        result=self.qemu();self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),str(self.tools/'qemu-aarch64'))

    def test_explicit_static_binary_name_is_preferred(self):
        result=self.qemu(static_name=True);self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),str(self.tools/'qemu-aarch64-static'))

    def test_dynamic_qemu_is_rejected_before_build_work(self):
        result=self.qemu(dynamic=True);self.assertNotEqual(result.returncode,0)
        self.assertIn('statically linked',result.stderr)

    def python_preflight(self):
        body=(PROJECT/'build/scripts/build-liuqin-sensors-stack.sh').read_text()
        code=body.split('run_sensor_chroot "$merged" python3 - <<\'PY\'\n',1)[1].split('\nPY\n',1)[0]
        return compile(code,'sensor-python-preflight','exec')

    def test_python_preflight_reports_missing_modules_before_compilation(self):
        def dependency(name):
            if name in ('psutil','gi'):raise ImportError('fixture module unavailable')
            return object()
        with patch.object(importlib,'import_module',side_effect=dependency),self.assertRaises(SystemExit) as error:
            exec(self.python_preflight(),{})
        self.assertIn('psutil',str(error.exception));self.assertIn('gi',str(error.exception))

    def test_python_preflight_accepts_complete_test_environment(self):
        with patch.object(importlib,'import_module',return_value=object()) as dependency, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            exec(self.python_preflight(),{})
        self.assertIn('dependencies:',output.getvalue())
        self.assertIn(('psutil',),[call.args for call in dependency.call_args_list])

    def test_needed_tags_are_parsed_with_translated_labels(self):
        readelf=self.tools/'readelf'
        readelf.write_text('#!/bin/sh\ncat <<EOF\n'
                          '0x0001 (NEEDED) 共享库：[libssc.so.2]\n'
                          '0x0001 (NEEDED) Shared library: [libc.so.6]\n'
                          '0x000e (SONAME) Library soname: [not-a-dependency.so]\nEOF\n')
        readelf.chmod(0o755)
        env=dict(os.environ,PATH=str(self.tools)+os.pathsep+os.environ['PATH'],LC_ALL='C.UTF-8',LANGUAGE='zh_CN')
        result=subprocess.run(['/bin/sh','-c','. "$1"; sensor_elf_needed /fixture-elf','sensor-test',str(HELPER)],
                              env=env,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.splitlines(),['libssc.so.2','libc.so.6'])

    def test_readelf_failure_is_not_silently_treated_as_no_dependencies(self):
        readelf=self.tools/'readelf';readelf.write_text('#!/bin/sh\nexit 1\n');readelf.chmod(0o755)
        env=dict(os.environ,PATH=str(self.tools)+os.pathsep+os.environ['PATH'])
        result=subprocess.run(['/bin/sh','-c','. "$1"; sensor_elf_needed /broken-elf','sensor-test',str(HELPER)],
                              env=env,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)


if __name__=='__main__':unittest.main(verbosity=2)
