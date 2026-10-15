#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Input preparation failures must precede durable credential creation intent."""
import importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('native_enrol',PROJECT/'device/fingerprint/oem/src/fingerprint/oem/native_enrol.py')
native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)
class PreflightTests(unittest.TestCase):
 def test_denied_memfd_mode_does_not_create_identity_record_or_intent(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);bundle=root/'bundle';bundle.mkdir(mode=0o700)
   (bundle/'native_credentials.py').touch()
   (bundle/'CANDIDATE.json').write_text(json.dumps(dict(account_profile='primary-uid-1000',linux_uid=1000)))
   db=root/'db';db.mkdir(mode=0o700);state=root/'state'
   user=SimpleNamespace(pw_name='wk',pw_uid=1000)
   with patch.object(native,'__file__',str(bundle/'native_enrol.py')),patch.object(native,'STATE_ROOT',state),patch.object(native,'FPRINT_ROOT',root/'prints'),patch.object(native.pwd,'getpwnam',return_value=user),patch.object(native.pwd,'getpwuid',return_value=user),patch.object(native.os,'fchmod',side_effect=PermissionError('fixture denial')):
    with self.assertRaises(PermissionError):native.enrol('wk',db/'output',SimpleNamespace(cancelled=False))
   account=state/str(0x60000000|1000)
   self.assertFalse((account/'credential.json').exists())
   self.assertFalse((account/'create.requested').exists())
   self.assertFalse((account/'gatekeeper.handle').exists())
if __name__=='__main__':
 if os.getuid()!=0:raise SystemExit('Run in a root-mapped isolated user namespace')
 unittest.main(verbosity=2)
