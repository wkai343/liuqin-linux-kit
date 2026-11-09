#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check the GPU-only RPM payload, library dependencies, and official-core routing."""
import json
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'build/lib'))
import fedora_stats as stats
from fedora_flow import FedoraFlow


class GpuPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.base=Path(cls.temp.name)
        compiler=next((shutil.which(name) for name in ('aarch64-linux-gnu-gcc',
            'aarch64-unknown-linux-musl-gcc') if shutil.which(name)),None)
        if compiler is None and platform.machine()=='aarch64':
            compiler=shutil.which('cc')
        if not compiler:
            cls.temp.cleanup()
            raise RuntimeError('GPU 打包检查需要 ARM64 C 编译器；请使用 Devbox。')
        (cls.base/'dependency.c').write_text('int reading(void) { return 7; }\n')
        (cls.base/'plugin.c').write_text('extern int reading(void); int sensor(void) { return reading(); }\n')
        subprocess.run([compiler,'-shared','-fPIC','-nostdlib','-Wl,-soname,libgpu-fixture.so.1',
            str(cls.base/'dependency.c'),'-o',str(cls.base/'libgpu-fixture.so.1')],check=True)
        cls.plugin=cls.base/'plugin.so'
        subprocess.run([compiler,'-shared','-fPIC','-nostdlib',str(cls.base/'plugin.c'),
            '-L'+str(cls.base),'-Wl,--no-as-needed','-l:libgpu-fixture.so.1',
            '-o',str(cls.plugin)],check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.tempdir=tempfile.TemporaryDirectory()
        self.work=Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_dependency_reader_rejects_wrong_architecture_and_symlinks(self):
        self.assertIn('libgpu-fixture.so.1()(64bit)',stats.plugin_dependencies(self.plugin))
        wrong=self.work/'wrong.so'
        content=bytearray(self.plugin.read_bytes());content[18:20]=b'\x3e\x00'
        wrong.write_bytes(content)
        with self.assertRaisesRegex(ValueError,'ARM64'):
            stats.plugin_dependencies(wrong)
        link=self.work/'link.so';link.symlink_to(self.plugin)
        with self.assertRaisesRegex(ValueError,'符号链接'):
            stats.plugin_dependencies(link)

    def test_rpm_leaves_official_files_unowned_and_bundles_corresponding_source(self):
        record=json.loads((PROJECT/'build/rootfs/fedora-stats-source.json').read_text())
        plugin=self.work/'build/bin/ksystemstats_plugin_gpu.so'
        plugin.parent.mkdir(parents=True);shutil.copyfile(self.plugin,plugin)
        upstream=self.work/('ksystemstats-'+record['version'])
        (upstream/'LICENSES').mkdir(parents=True)
        (upstream/'LICENSES/GPL-2.0-only.txt').write_text('fixture license\n')
        (upstream/'plugins/gpu').mkdir(parents=True)
        (upstream/'plugins/gpu/reader.cpp').write_text('fixture corresponding source\n')
        versions='qt6-qtbase >= 6.11.2\nlibksysguard >= 6.8.2\nglibc >= 2.44\nlibstdc++ >= 16.2.1\n'
        def chroot(root,command,**kwargs):
            if command[:2]==['rpm','-q']:
                return subprocess.CompletedProcess(command,0,stdout=versions)
            self.assertEqual(command[:2],['rpm','-qa'])
            return subprocess.CompletedProcess(command,0,stdout=b'qt6-qtbase\t6.11.2\taarch64\n')
        built=stats.pack(self.work/'root',self.work,self.work/'out',chroot)
        name=subprocess.check_output(['rpm','-qp','--qf','%{NAME}',str(built)],text=True)
        self.assertEqual(name,'liuqin-ksystemstats-gpu')
        files=subprocess.check_output(['rpm','-qpl',str(built)],text=True).splitlines()
        runtime={path for path in files if not path.startswith('/usr/share/licenses/')}
        self.assertEqual(runtime,{'/'+stats.PLUGIN,'/'+stats.DROP_IN})
        requires=subprocess.check_output(['rpm','-qp','--requires',str(built)],text=True).splitlines()
        self.assertIn('ksystemstats >= '+record['version'],requires)
        self.assertIn('libgpu-fixture.so.1()(64bit)',requires)
        self.assertIn('qt6-qtbase >= 6.11.2',requires)
        self.assertIn('libksysguard >= 6.8.2',requires)
        self.assertFalse(any(line.startswith('ksystemstats =') for line in requires))
        provides=subprocess.check_output(['rpm','-qp','--provides',str(built)],text=True).splitlines()
        self.assertFalse(any(line.startswith('ksystemstats ') for line in provides))
        self.assertEqual(subprocess.check_output(['rpm','-qp','--conflicts',str(built)],text=True).strip(),'')
        self.assertEqual(subprocess.check_output(['rpm','-qp','--obsoletes',str(built)],text=True).strip(),'')
        payload=self.work/'gpu-package'
        self.assertEqual((payload/stats.PLUGIN).read_bytes(),self.plugin.read_bytes())
        unit=(payload/stats.DROP_IN).read_text()
        self.assertIn('Environment=QT_PLUGIN_PATH=/usr/lib64/liuqin/qt6/plugins',unit)
        docs=payload/'usr/share/licenses'/name
        build=json.loads((docs/'BUILD.json').read_text())
        self.assertEqual(build['requires'],['ksystemstats >= '+record['version'],
            *stats.plugin_dependencies(self.plugin),*versions.splitlines()])
        with tarfile.open(docs/build['source_archive']) as archive:
            self.assertEqual(archive.extractfile(upstream.name+'/plugins/gpu/reader.cpp').read(),
                             b'fixture corresponding source\n')
        self.assertTrue((docs/'adreno-source/LinuxAdrenoGpu.cpp').is_file())
        self.assertTrue((self.work/'out/stats-packages.tsv').is_file())

    def test_pipeline_expects_the_independent_package(self):
        data=dict(rootfs_config=str(PROJECT/'build/rootfs/fedora-plasma.json'),kernel_fragments=[],
            kernel_localversion='-liuqin-test',kernel_source='sources/test',package_version='0.1.0')
        flow=FedoraFlow(data,lambda *_a,**_k:self.fail('unexpected build'))
        base=self.work/'base';base.write_bytes(b'base')
        source=self.work/'source';source.write_bytes(b'source')
        with patch.object(flow,'target_runtime_root',return_value=base), \
             patch.object(flow,'stats_archive',return_value=source), \
             patch.object(flow,'stage',return_value=self.work/'cache') as stage:
            result=flow.stats_rpm()
        record=json.loads((PROJECT/'build/rootfs/fedora-stats-source.json').read_text())
        expected='liuqin-ksystemstats-gpu-'+record['version']+'-'+record['package_release']+'.aarch64.rpm'
        self.assertEqual(result.name,expected)
        self.assertEqual(stage.call_args.args[0],'fedora-adreno-gpu')
        self.assertIn('assembled/'+expected,stage.call_args.args[3])


if __name__=='__main__':
    unittest.main()
