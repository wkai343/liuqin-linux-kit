#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
import gzip
import hashlib
import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'installer'))
from liuqin_gui import recovery,workflow,partitioning
from liuqin_gui.demo import DemoEngine
from liuqin_gui.partition_demo import inspect
from liuqin_gui.engine import digest_file,InstallerError
from liuqin_partitions import snapshot_id
from liuqin_boot_target import _record


def make_image(device='liuqin',compression='gzip',binary=True,kernel=True):
    records=[('prop.default',('ro.product.device='+device+'\nro.twrp.version=3.7-test\n').encode())]
    if binary:records.append(('system/bin/recovery',b'DEMO RECOVERY'))
    raw=b''.join(_record(name,data) for name,data in records)+_record('TRAILER!!!',b'')
    if compression=='gzip':ram=gzip.compress(raw)
    elif compression=='legacy':
        import lz4.block
        block=lz4.block.compress(raw,store_size=False);ram=b'\x02\x21\x4c\x18'+struct.pack('<I',len(block))+block
    else:
        import lz4.frame
        ram=lz4.frame.compress(raw)
    header=bytearray(4096);header[:8]=b'ANDROID!'
    for offset,value in ((8,16 if kernel else 0),(12,len(ram)),(20,1584),(40,4),(1580,0)):struct.pack_into('<I',header,offset,value)
    return bytes(header)+(b'DEMO KERNEL'.ljust(4096,b'\0') if kernel else b'')+ram+bytes(-len(ram)%4096)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.path=self.root/'recovery.img';self.path.write_bytes(make_image())
    def test_image_identity_and_structure(self):
        self.assertEqual(recovery.inspect_image(self.path)['device'],'liuqin')
        for data in (make_image('pipa'),make_image(binary=False),make_image()[:8193],b'ANDROID!'+bytes(4096)):
            self.path.write_bytes(data)
            with self.assertRaises((InstallerError,ValueError)):recovery.inspect_image(self.path)
    @unittest.skipUnless(importlib.util.find_spec('lz4'),'run with installer uv environment for LZ4')
    def test_lz4_legacy_and_frame(self):
        for mode in ('legacy','frame'):
            self.path.write_bytes(make_image(compression=mode));self.assertEqual(recovery.inspect_image(self.path)['version'],'3.7-test')
    def connect(self):
        engine=DemoEngine(lambda _:None,self.root/'state');engine.pause=lambda _:None;self.addCleanup(engine.close)
        state=partitioning.connect(engine,dict(serial='DEMO-LIUQIN',bundle='ignored',layout='partition-only'),'wizard')
        return engine,state
    def test_fixed_install_writes_only_recovery_slots(self):
        self.path.write_bytes(make_image(kernel=False))
        engine,state=self.connect();before=engine.transport.current_boot_digest()
        # Exercise dormant rollback machinery without enabling it in production.
        with patch.object(recovery,'validate_usage'):
            recovery.install(engine,recovery.inspect_image(self.path),state,self.root/'rec-backup')
        self.assertTrue(engine.receipt['recovery_verified'])
        flashes=[c for c in engine.transport.calls if c.startswith('fastboot:flash')]
        self.assertEqual(len(flashes),2);self.assertTrue(all('recovery_' in c for c in flashes))
        self.assertEqual(engine.transport.current_boot_digest(),before)
        self.assertTrue(all((self.root/'rec-backup'/(name+'.img')).stat().st_size==recovery.LIMIT for name in recovery.SLOTS))
    def test_partial_flash_rolls_back_both_attempted_slots(self):
        self.path.write_bytes(make_image(kernel=False))
        engine,state=self.connect();original={name:digest_file(path) for name,path in engine.demo_recovery_partitions.items()}
        original_fastboot=engine.transport.fastboot;failed=False
        def fastboot(*args):
            nonlocal failed
            original_fastboot(*args)
            if args[:2]==('flash','recovery_b') and not failed:failed=True;raise RuntimeError('interrupted flash')
        with patch.object(engine.transport,'fastboot',side_effect=fastboot),patch.object(recovery,'validate_usage'):
            with self.assertRaisesRegex(RuntimeError,'interrupted'):recovery.install(engine,recovery.inspect_image(self.path),state,self.root/'rec-backup')
        self.assertTrue(engine.receipt['recovery_rollback_verified'])
        self.assertEqual({name:digest_file(path) for name,path in engine.demo_recovery_partitions.items()},original)
    def test_temporary_image_cannot_be_written_to_dedicated_slots(self):
        engine,state=self.connect();transport=engine.transport;before=list(transport.calls)
        record=recovery.inspect_image(self.path)
        # A caller cannot turn a temporary image into a fixed one by editing metadata.
        record.update(kernel_bytes=0,temporary_boot=False)
        with self.assertRaisesRegex(InstallerError,'不能刷入'):
            recovery.install(engine,record,state,self.root/'rec-backup')
        self.assertEqual(transport.calls,before);self.assertFalse((self.root/'rec-backup').exists())
    def test_kernel_less_is_not_proof_of_qualified_fixed_boot(self):
        self.path.write_bytes(make_image(kernel=False));engine,state=self.connect()
        calls=list(engine.transport.calls)
        with self.assertRaisesRegex(InstallerError,'暂未开放'):
            recovery.install(engine,recovery.inspect_image(self.path),state,self.root/'rec-backup')
        self.assertEqual(calls,engine.transport.calls)
    def test_temporary_boot_uses_no_flash(self):
        engine=DemoEngine(lambda _:None,self.root/'state');engine.pause=lambda _:None;self.addCleanup(engine.close)
        workflow.scan(engine,dict(serial='DEMO-LIUQIN'))
        workflow.prepare(engine,dict(serial='DEMO-LIUQIN',snapshot_id=snapshot_id(inspect(engine)),backup_parent=str(self.root/'backups'),
            actions=[],partition_config=dict(roots=[],userdata_gib=0,boot_gib=2),target='__recovery__',recovery_mode='boot',recovery_path=str(self.path)))
        transport=engine.transport
        workflow.install(engine,dict(session_id=engine.session_id,plan_id=engine.plan_id,confirmation=dict(serial='DEMO-LIUQIN',accept_operations=True,allow_experimental=True)))
        self.assertTrue(engine.receipt['recovery_boot_requested']);self.assertFalse(engine.receipt['write_dispatched'])
        self.assertTrue(any(c.startswith('fastboot:boot ') for c in transport.calls));self.assertFalse(any(c.startswith('fastboot:flash') for c in transport.calls))

if __name__=='__main__':unittest.main(verbosity=2)
