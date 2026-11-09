#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate a fixed candidate using the acquired liuqin ABL and offline snapshots.

Requires Unicorn. All guest I/O uses host snapshot files; no adb/fastboot/network.
EFI SHA256 and memory services are modeled; ABL parsing, RSA, descriptors, AVB
policy, image sizing, matching, overlays and gzip decompression execute unchanged.
"""
import argparse
import contextlib
import gc
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
PROJECT=Path(__file__).resolve().parents[2]

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--image',type=Path,required=True);p.add_argument('--abl-evidence',type=Path,required=True);p.add_argument('--snapshots',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
 out=a.out.resolve()
 if out.exists() or not out.is_relative_to(PROJECT/'out'):raise ValueError('use a new report directory below project/out')
 os.environ['LIUQIN_ABL_EVIDENCE']=str(a.abl_evidence.resolve());os.environ['LIUQIN_FIXED_EVIDENCE']=str(a.snapshots.resolve())
 sys.path[:0]=[str(PROJECT/'shared'),str(PROJECT/'build/recovery/verify')]
 from liuqin_fixed_recovery import inspect,sha
 from emulator_core import Core,OLD,STOP
 from verify_avb_chain import Avb,HERE
 from verify_device_tree import Trees
 image=a.image.resolve();checked=inspect(image);profile=checked['manifest']['fixed_profile']
 for path,digest in [(OLD/'vbmeta_a.img',profile['vbmeta_partition_sha256']),(HERE/'vbmeta_system_a.img',profile['vbmeta_system_partition_sha256']),(OLD/'dtbo_a.payload.img',profile['external_dtbo_payload_sha256'])]:
  if sha(path.read_bytes())!=digest:raise ValueError('snapshot does not match fixed profile: '+str(path))
 out.mkdir(parents=True);rows=[]
 cases=[('stock_recovery',HERE/'stock-recovery-sparse-snapshot.img',{},0),('fixed_unlocked',image,{},5),
  ('fixed_locked',image,{'unlocked':False},5),('fixed_high_rollback',image,{'rollback_value':2**32},None),
  ('normal_android_with_fixed_recovery',image,{'normal':True},5),('missing_vbmeta_system',image,{'missing_system':True},2)]
 old_v2=OLD/'OFFLINE-ONLY-native-recovery-with-avb.img'
 if old_v2.exists():cases.append(('rejected_v2_specimen',old_v2,{},1))
 for name,path,options,expected in cases:
  with (out/(name+'.log')).open('w') as log,contextlib.redirect_stdout(log):
   m=Avb(path,**options);r=m.verify()
  if expected is None:
   if r['result'] not in (4,5):raise ValueError('unexpected rollback-policy result')
  elif r['result']!=expected:raise ValueError('unexpected AVB result: '+name)
  if name in ('fixed_locked','missing_vbmeta_system','rejected_v2_specimen'):
   if r['slot_data']:raise ValueError('negative AVB case returned usable slot data')
  elif not r['slot_data']:raise ValueError('no usable AVB result: '+name)
  expected_payloads={'boot','dtbo','vendor_boot'} if name=='normal_android_with_fixed_recovery' else {'recovery','dtbo'}
  if name in ('fixed_unlocked','normal_android_with_fixed_recovery') and {x['name'] for x in r['loaded_partitions']}!=expected_payloads:raise ValueError('unexpected loaded partitions')
  r['case']=name;rows.append(r);del m;gc.collect();print('AVB:',name,'passed',flush=True)
 with tempfile.TemporaryDirectory(prefix='avb-negative-',dir=out) as temp:
  raw=bytearray(image.read_bytes());raw[-64:-60]=b'BAD!';bad=Path(temp)/'bad-footer.img';bad.write_bytes(raw)
  with (out/'bad_footer.log').open('w') as log,contextlib.redirect_stdout(log):m=Avb(bad);r=m.verify()
  if r['result']!=6 or r['slot_data']:raise ValueError('invalid footer did not fail closed')
  r['case']='bad_recovery_footer';rows.append(r);del m,raw;gc.collect()
 (out/'avb-chain.json').write_text(json.dumps(rows,indent=2)+'\n')
 variations=[{},dict(foundry=1),dict(version=0x10001),dict(version=0x20000),dict(subtype=1),dict(subtype=3),dict(ddr=0x700),dict(pmic0=0),dict(pmic0=0x41),dict(project=14),dict(soc_id=0x1c9)]
 dtrows=[]
 for values in variations:
  m=Trees(**values);r=m.choose(image,matching_only=True)
  if 'project' in values:
   if r['board_matches'] and r['board_matches'][0]['index']==15:raise ValueError('wrong project matched liuqin overlay')
  elif 'soc_id' in values:
   if r['soc_match']:raise ValueError('wrong SoC matched kernel DTB')
  elif not r['soc_match'] or [x['index'] for x in r['board_matches']]!=[15]:raise ValueError('device-tree match changed')
  r['modeled_inputs']=values;dtrows.append(r);del m;gc.collect()
 m=Trees();dt=m.choose(image)
 if dt['full_load_status']!='0x0' or dt['board_matches'][0]['index']!=profile['expected_dtbo_index']:raise ValueError('ABL overlay merge failed')
 if dt['merged_sha256']!='b20616df2c60a7a77e3c6e4a64e3e3a09b0ceedad38cca8d08e2d1df3a2c9a26':raise ValueError('merged DTB differs from the validated display/storage baseline')
 (out/'merged-by-abl.dtb').write_bytes(m.merged_dtb);del m;gc.collect()
 (out/'device-tree.json').write_text(json.dumps(dict(full_load=dt,sensitivity=dtrows),indent=2)+'\n');print('ABL device-tree selection and merge: passed',flush=True)
 # Execute the original ABL gzip decoder on the final combined kernel blob.
 m=Core();b=image.read_bytes();ks=struct.unpack_from('<I',b,8)[0];k=b[4096:4096+ks]
 src=m.allocate(len(k));m.u.mem_write(src,k);dest=m.allocate(64*1024**2);meta=0x302000
 code=m.run(0x20b40,(src,len(k),dest,64*1024**2,meta,meta+4),count=1500000000)
 size=m.get32(meta+4);gzip_result=dict(result=code,kernel_bytes=size,dtb_offset=m.get32(meta),kernel_sha256=sha(m.read(dest,size)))
 if code!=0 or gzip_result['kernel_sha256']!=profile['kernel_sha256'] or gzip_result['dtb_offset']!=checked['gzip_kernel_bytes']:raise ValueError('ABL gzip boundary/content mismatch')
 (out/'gzip.json').write_text(json.dumps(gzip_result,indent=2)+'\n');del m,b,k;gc.collect();print('ABL gzip decoding: passed',flush=True)
 # Record the modeled inputs and do not conflate this with physical boot.
 report=dict(status='OFFLINE_VALIDATED_HARDWARE_BOOT_UNTESTED',image_sha256=checked['image_sha256'],build_id=checked['manifest']['build_id'],profile=profile,
  avb_cases=len(rows),device_tree_cases=len(dtrows),actual_abl_sha256=sha((OLD/'LinuxLoader.mapped.bin').read_bytes()),
  modeled_services=['EFI heap/thread stack, libc memory/string operations and SHA256 protocol','read-only snapshot partition I/O and GUIDs','trust only the captured OEM root public key','TEE rollback values: zero/high scenarios; locked control writes only a simulated in-memory counter','board SoC 531/revision 1.0 and Xiaomi project 16; other input fields tested for sensitivity'],
  not_tested=['physical recovery boot/cold boot','secure-world root-of-trust handoff and real rollback counters','UEFI exit and kernel handoff','fixed installation and rollback'],device_access=False,flash_requested=False,
  sources={str(path.relative_to(PROJECT)):sha(path.read_bytes()) for path in [Path(__file__).resolve(),*sorted((PROJECT/'build/recovery/verify').glob('*.py'))]})
 (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print('PASS: fixed candidate offline validation',flush=True)
if __name__=='__main__':main()
