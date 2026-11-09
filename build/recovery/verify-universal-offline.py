#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verify universal v2 candidate with both actual ABLs and three ROM snapshots.

Only host snapshot files and emulator memory are accessed; no device tools.
"""
import argparse,contextlib,gc,json,os,struct,sys
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[2]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('image','miui-evidence','snapshots','rom-evidence','out'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();out=a.out.resolve()
    if out.exists() or not out.is_relative_to(PROJECT/'out'):raise ValueError('use a new output directory below project/out')
    os.environ.update(LIUQIN_ABL_EVIDENCE=str(a.miui_evidence.resolve()),LIUQIN_FIXED_EVIDENCE=str(a.snapshots.resolve()),
        LIUQIN_HYPER_ABL_EVIDENCE=str((a.rom_evidence/'hyperos206').resolve()),LIUQIN_ROM_EVIDENCE=str(a.rom_evidence.resolve()))
    sys.path[:0]=[str(PROJECT/'shared'),str(PROJECT/'build/recovery/verify')]
    from liuqin_universal_recovery import inspect,avb_measurement
    from liuqin_fixed_recovery import sha
    from universal_models import make_avb,rom_parts,CurrentTrees,HyperTrees
    checked=inspect(a.image);policy=checked['manifest']['universal_policy'];out.mkdir(parents=True)
    for rom in policy['roms']:
        raw,system=rom_parts(rom['id']);v=avb_measurement(raw);s=avb_measurement(system)
        if v['normalized_flags_sha256']!=rom['vbmeta']['normalized_flags_sha256'] or s['metadata_sha256']!=rom['vbmeta_system_metadata_sha256']:raise ValueError('ROM metadata inputs differ from embedded policy')
    rows=[]
    for base in policy['baselines']:
        for rom in policy['roms']:
            for flags in range(4):
                name=base['id']+'-'+rom['id']+'-flags'+str(flags)
                m=make_avb(base['id'],a.image,rom['id'],flags=flags)
                with (out/(name+'.log')).open('w') as f,contextlib.redirect_stdout(f):r=m.verify()
                permitted=not base['requires_verification_disabled'] or bool(flags&2)
                if r['slot_data']!=permitted:raise ValueError('AVB result disagrees with profile: '+name)
                if permitted:
                    if r['result'] not in (0,3,4,5) or [x['name'] for x in r['loaded_partitions']]!=['recovery']:raise ValueError('unexpected universal payloads: '+name)
                    if any(x['name'] in ('boot_a','vendor_boot_a','dtbo_a') for x in r['reads']):raise ValueError('external boot/DTBO payload dependency')
                elif not any(x.get('oversized_allocation',0)>policy['partition_bytes'] for x in r['events']):raise ValueError('expected old ABL length guard did not fire')
                r.update(case=name,baseline=base['id'],rom=rom['id'],root_flags=flags,expected_conditions_satisfied=permitted);rows.append(r);del m;gc.collect()
            print('AVB matrix:',base['id'],rom['id'],'passed',flush=True)
    controls=[('miui-missing-system','miui-abl-20230422','miui14',2,True,True),
        ('hyper-retained-missing-system','hyperos-abl-20250318','hyperos206',0,True,False),
        ('miku-miui-missing-system','miui-abl-20230422','miku26101322',3,True,True),
        ('miku-hyper-missing-system','hyperos-abl-20250318','miku26101322',3,True,True)]
    for name,base,rom,flags,missing,expected in controls:
        m=make_avb(base,a.image,rom,flags=flags,missing_system=missing)
        with (out/(name+'.log')).open('w') as f,contextlib.redirect_stdout(f):r=m.verify()
        if r['slot_data']!=expected:raise ValueError('dependency control failed: '+name)
        r['case']=name;rows.append(r);del m;gc.collect()
    m=make_avb('hyperos-abl-20250318',a.image,'hyperos206',unlocked=False)
    with (out/'locked.log').open('w') as f,contextlib.redirect_stdout(f):r=m.verify()
    if r['slot_data']:raise ValueError('locked control returned usable data')
    r['case']='locked-hyperos';rows.append(r);del m;gc.collect()
    (out/'avb-matrix.json').write_text(json.dumps(rows,indent=2)+'\n')
    tree_results=[];gzip_results=[]
    for name,cls in [('miui',CurrentTrees),('hyperos',HyperTrees)]:
        m=cls();r=m.choose_v2(a.image)
        if (not r['soc_match'] or r['dtbo_index']!=policy['embedded_dtbo']['liuqin_index'] or r['full_load_status']!='0x0' or
            r['merged_sha256']!='b20616df2c60a7a77e3c6e4a64e3e3a09b0ceedad38cca8d08e2d1df3a2c9a26'):raise ValueError('DT matching/merge failed: '+name)
        (out/(name+'-merged.dtb')).write_bytes(m.merged_dtb);r['baseline']=name;tree_results.append(r);del m;gc.collect();print(name,'actual ABL embedded DTBO merge passed',flush=True)
        m=cls();b=a.image.read_bytes();size=struct.unpack_from('<I',b,8)[0];kernel=b[4096:4096+size];src=m.allocate(len(kernel));m.u.mem_write(src,kernel);dst=m.allocate(64*1024**2);meta=0x302000
        code=m.run(m.gzip_entry,(src,len(kernel),dst,64*1024**2,meta,meta+4),count=1500000000)
        n=m.get32(meta+4);result=dict(baseline=name,result=code,kernel_bytes=n,gzip_consumed=m.get32(meta),kernel_sha256=sha(m.read(dst,n)))
        if code!=0 or result['kernel_sha256']!=policy['kernel_sha256'] or result['gzip_consumed']!=len(kernel):raise ValueError('kernel decoder differs: '+name)
        gzip_results.append(result);del m,b,kernel;gc.collect();print(name,'actual ABL gzip decode passed',flush=True)
    (out/'device-trees.json').write_text(json.dumps(tree_results,indent=2)+'\n');(out/'gzip.json').write_text(json.dumps(gzip_results,indent=2)+'\n')
    report=dict(status='OFFLINE_VALIDATED_HARDWARE_BOOT_UNTESTED',image_sha256=checked['image_sha256'],build_id=checked['manifest']['build_id'],
        policy_id=policy['id'],roms=[r['id'] for r in policy['roms']],abl_baselines=[b['id'] for b in policy['baselines']],avb_cases=len(rows),
        device_access=False,root_vbmeta_modified=False,hardware_boot_tested=False,
        modeled_services=['EFI heap/thread stack, equivalent libc operations and SHA256 protocol','snapshot I/O, captured OEM root key, GUID lookup','TEE rollback counters modeled; no physical TEE access','SoC 531/revision 1.0/project 16; other board inputs modeled'],
        untested=['physical fixed cold boot and rollback','secure-world handoff and UEFI exit','actual panel/USB/touch behavior under HyperOS or MikuUI firmware'],
        sources={str(f.relative_to(PROJECT)):sha(f.read_bytes()) for f in [Path(__file__).resolve(),*sorted((PROJECT/'build/recovery/verify').glob('*.py'))]})
    (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print('PASS: universal v2 offline matrix',flush=True)
if __name__=='__main__':main()
