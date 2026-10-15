#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate clean, deterministic board-source assembly without changing user Git."""
from pathlib import Path
import sys,tempfile,subprocess,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'build/lib'))
from kernel_adaptation import assemble

class Adaptation(unittest.TestCase):
 def test_snapshot_determinism_and_original_checkout_preservation(self):
  with tempfile.TemporaryDirectory() as temp:
   work=Path(temp);source=work/'user-kernel';source.mkdir()
   subprocess.run(['git','init','-q',str(source)],check=True)
   (source/'driver.c').write_text('old\n')
   subprocess.run(['git','-C',str(source),'add','driver.c'],check=True)
   subprocess.run(['git','-C',str(source),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false','commit','-qm','base'],check=True)
   base=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
   patch=work/'board.patch';patch.write_text('--- a/driver.c\n+++ b/driver.c\n@@ -1 +1 @@\n-old\n+fixed\n')
   commits=[]
   for name in ('one','two'):
    out=work/name;out.mkdir();commits.append(assemble(source,base,[patch],out))
    self.assertEqual((out/'source/driver.c').read_text(),'fixed\n')
    self.assertEqual((out/'source.commit').read_text().strip(),commits[-1])
    self.assertEqual(subprocess.check_output(['git','-C',str(out/'source'),'status','--porcelain']),b'')
   self.assertEqual(commits[0],commits[1]);self.assertNotEqual(commits[0],base)
   self.assertEqual((source/'driver.c').read_text(),'old\n')
   self.assertEqual(subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip(),base)
   self.assertEqual(subprocess.check_output(['git','-C',str(source),'status','--porcelain']),b'')
if __name__=='__main__':unittest.main()
