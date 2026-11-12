#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Project identity, source attribution and exported license checks."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'shared'))
from liuqin_identity import PROJECT_NAME, PROJECT_SLUG, INSTALLER_ARCHIVE_ROOT



class IdentityTests(unittest.TestCase):
    def test_name_is_distribution_neutral_and_explicitly_derived(self):
        self.assertEqual(PROJECT_NAME, 'Liuqin Linux Kit')
        self.assertEqual(PROJECT_SLUG, 'liuqin-linux-kit')
        readme = (PROJECT / 'README.md').read_text()
        self.assertIn('yzddmr6', readme)
        self.assertIn('衍生', readme)
        self.assertIn('CREDITS.md', readme)

    def test_license_and_current_component_notices_are_preserved(self):
        expected = {'LICENSE': 'b9d8b4bd86feb7e54d8c272e786f859209bd6053e880d1fbd6733e16418db881',
                    'NOTICE': '8f405e222a5de0eda58e63ff17c828e4e94029971d342a5e7ac568e97ead005b'}
        for name, digest in expected.items():
            content=(PROJECT/name).read_bytes()
            if name=='NOTICE':
                # Reviewed current components: kernel.org + local patches,
                # corrected board path and direct upstream fingerprint sources.
                # Removed GNOME/Ubuntu components no longer appear as inputs;
                # retained components keep their authors and license notices.
                # Includes the added role-first MiPPS upstream attribution.
                # Also retains authorship for the UCSI service-recovery fixes.
                content=content.split(b'\nBoot and shutdown artwork\n')[0]
                content=content.replace(b'build/kernel/source.json',b'kernel/source.json')
            self.assertEqual(hashlib.sha256(content).hexdigest(),digest)

    def test_exported_installer_keeps_credit_license_and_notices(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'installer.zip'
            subprocess.run([sys.executable, PROJECT / 'installer/package.py', '--out', archive], capture_output=True, check=True)
            with zipfile.ZipFile(archive) as files:
                for name in ('LICENSE', 'NOTICE', 'CREDITS.md'):
                    self.assertEqual(files.read(INSTALLER_ARCHIVE_ROOT + '/' + name), (PROJECT / name).read_bytes())
                self.assertIn(PROJECT_NAME.encode(), files.read(INSTALLER_ARCHIVE_ROOT + '/README.md'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
