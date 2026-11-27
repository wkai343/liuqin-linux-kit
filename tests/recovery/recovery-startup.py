#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise actual Rec init ordering and isolated battery reads; no devices."""
import ctypes,os,subprocess,sys,tempfile,unittest
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[2]

class StartupTests(unittest.TestCase):
 def test_recovery_adsp_does_not_instantiate_disabled_audio_clocks(self):
  # Compile the actual helper introduced by the shared patch, then apply
  # Recovery's patch. No duplicated Python model of the probe condition.
  shared=(PROJECT/'build/kernel/patches/linux-7.3/0056-liuqin-audio-sleep-and-dsp-recovery.patch').read_text()
  start=shared.index('+static int liuqin_create_prm_clocks(')
  end=shared.index('\n+}\n',start)+3
  helper='\n'.join(line[1:] for line in shared[start:end].splitlines())+'\n'
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);source=root/'drivers/remoteproc/qcom_q6v5_pas.c';source.parent.mkdir(parents=True);source.write_text(helper)
   subprocess.run(['patch','-s','-p1','-i',str(PROJECT/'build/recovery/kernel/patches/linux-7.3/0003-recovery-skip-audio-prm-clocks.patch')],cwd=root,check=True)
   stub='''#include <assert.h>
#include <stdbool.h>
#include <string.h>
#include <errno.h>
#define __free(x)
struct device_node { int unused; } node;
struct device { struct device_node *of_node; } device;
struct qcom_pas { const char *info_name; struct device *dev; void *prm_clocks; };
static struct device_node *of_root=&node;
static bool board=true,recovery=false,missing=false,disabled=false;
static unsigned lookups,creates;
static bool liuqin_board_quirks(void){return board;}
static bool of_property_read_bool(struct device_node *n,const char *key){assert(n==of_root);assert(!strcmp(key,"liuqin,recovery"));return recovery;}
static struct device_node *of_get_child_by_name(struct device_node *n,const char *key){(void)n;(void)key;lookups++;return &node;}
static struct device_node *of_get_compatible_child(struct device_node *n,const char *key){(void)n;(void)key;lookups++;return missing?NULL:&node;}
static void *of_platform_device_create(struct device_node *n,void *id,struct device *dev){(void)n;(void)id;(void)dev;creates++;return disabled?NULL:&device;}
'''
   checks='''int main(void){
struct qcom_pas p={.info_name="adsp",.dev=&device};
recovery=true;missing=true;disabled=true;
assert(liuqin_create_prm_clocks(&p)==0 && lookups==0 && creates==0);
recovery=false;assert(liuqin_create_prm_clocks(&p)==-ENODEV);
missing=false;assert(liuqin_create_prm_clocks(&p)==-ENOMEM && creates==1);
disabled=false;assert(liuqin_create_prm_clocks(&p)==0 && p.prm_clocks);
lookups=creates=0;board=false;assert(liuqin_create_prm_clocks(&p)==0 && lookups==0);
board=true;p.info_name="cdsp";assert(liuqin_create_prm_clocks(&p)==0 && lookups==0);
return 0;}
'''
   source.write_text(stub+source.read_text()+checks);binary=root/'test'
   subprocess.run(['cc','-Wall','-Wextra','-Werror',str(source),'-o',str(binary)],check=True)
   subprocess.run([str(binary)],check=True)

 def test_parallel_preparation_and_final_storage_gate(self):
  source=(PROJECT/'device/boot/initramfs/init').read_text()
  fragment=source[source.index('recovery_touch_pid=\n'):source.index('# ABL uses androidboot.mode=charger')]
  for seal in (True,False):
   with self.subTest(seal=seal),tempfile.TemporaryDirectory() as temp:
    root=Path(temp)
    def put(name,data,mode=0o644):
     p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(data);p.chmod(mode)
    put('proc/cmdline','');put('proc/sys/kernel/random/boot_id','current-boot\n')
    put('etc/liuqin-native-recovery.json','{}');put('usr/lib/liuqin/native-status.py','')
    put('usr/bin/liuqin-recovery-ui','',0o755)
    (root/'usr/bin/python3').symlink_to(sys.executable)
    put('bb','#!/bin/sh\nexec "$@"\n',0o755)
    common='from pathlib import Path\nimport os,time\nr=Path(os.environ["FIXTURE_ROOT"])\n'
    put('usr/lib/liuqin/native-touch.py',common+'''
(r/'touch-started').touch()
while not (r/'storage-started').exists():time.sleep(.005)
time.sleep(.05)
(r/'touch-done').touch()
''')
    put('usr/lib/liuqin/native-ui-service.py',common+'''
(r/'ui-started').touch()
p=r/'run/liuqin-native-startup'
while not p.exists():time.sleep(.005)
assert (r/'final-proof').exists() and (r/'touch-done').exists()
assert p.read_text().splitlines()[1]=='current-boot'
(r/('service-started' if p.read_text().startswith('ready\\n') else 'service-blocked')).touch()
''')
    (root/'run').mkdir()
    script=fragment
    for prefix in ('/usr/','/etc/','/run/','/proc/'):
     script=script.replace(prefix,str(root)+prefix)
    prefix='''log() { :; }
BB="$FIXTURE_ROOT/bb"
storage_wait_mode=recovery
storage_ufs_only=false
storage_wait_for_ufs() {
 touch "$FIXTURE_ROOT/storage-started"
 for n in $(seq 1 200); do
  if [ -e "$FIXTURE_ROOT/touch-started" ] && [ -e "$FIXTURE_ROOT/ui-started" ]; then found_block=true; return 0; fi
  sleep .01
 done
 exit 90
}
storage_lock_all() { touch "$FIXTURE_ROOT/final-proof"; [ "$SEAL_OK" = 1 ]; }
'''
    result=subprocess.run(['sh','-c',prefix+script+'\nwait "$recovery_ui_pid"\n'],env=dict(os.environ,FIXTURE_ROOT=temp,SEAL_OK='1' if seal else '0'),capture_output=True,text=True,timeout=8)
    self.assertEqual(result.returncode,0,result.stderr)
    self.assertEqual((root/'service-started').exists(),seal)
    self.assertEqual((root/'service-blocked').exists(),not seal)

 def test_battery_unknown_retries_and_real_zero(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);library=root/'battery.so'
   subprocess.run(['cc','-shared','-fPIC','-pthread','-Wall','-Wextra','-Werror',str(PROJECT/'device/recovery/ui/battery.c'),'-o',str(library)],check=True)
   api=ctypes.CDLL(str(library));api.recovery_battery_read.argtypes=[ctypes.c_char_p];api.recovery_battery_read.restype=ctypes.c_int
   api.recovery_battery_interval.argtypes=[ctypes.c_int];api.recovery_battery_interval.restype=ctypes.c_uint
   self.assertEqual(api.recovery_battery_read(os.fsencode(root)),-1)
   for text,expected in [('0\n',0),('100\n',100),('42',42),('',-1),('-1\n',-1),('101\n',-1),('12x\n',-1),('12\n34',-1),('9'*100,-1)]:
    (root/'capacity').write_text(text)
    with self.subTest(text=text):self.assertEqual(api.recovery_battery_read(os.fsencode(root)),expected)
   self.assertEqual(api.recovery_battery_interval(-1),200)
   for value in (0,42,100):self.assertEqual(api.recovery_battery_interval(value),2000)

if __name__=='__main__':unittest.main()
