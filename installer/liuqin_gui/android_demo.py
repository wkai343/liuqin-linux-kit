# SPDX-License-Identifier: MIT
"""Synthetic Android plus two Linux systems; no device/network access."""
import copy
import hashlib
import json
from pathlib import Path
import uuid
from liuqin_android import load_profile, canonical, check_registry_transition
from liuqin_multiboot import plan


def initialize(engine, layout):
    if hasattr(engine,'demo_android_scan'):return
    directory=engine.state_dir/'demo-android-rom';directory.mkdir(exist_ok=True)
    profile=copy.deepcopy(load_profile());profile['rom']='DEMO-ANDROID'
    for name,entry in profile['images'].items():
        data=('DEMO ONLY ANDROID '+name).encode().ljust(16384 if name=='boot.img' else 65536 if name=='super.img' else 4096,b'\0')
        (directory/name).write_bytes(data);entry.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest());entry.pop('sparse',None)
    super_entry=profile['images']['super.img']
    profile['super']=dict(source_sha256=super_entry['sha256'],expanded_bytes=super_entry['bytes'],
                          verification_ranges=[dict(offset=0,bytes=super_entry['bytes'],sha256=super_entry['sha256'])])
    engine.demo_android_profile=profile;engine.demo_android_rom=directory
    disk=layout.reference_stock_disk();disk.by_name('super').last_sector=disk.by_name('super').first_sector+15
    generation='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    ids=['11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222']
    config=dict(schema_version=1,userdata_gib=32,boot_gib=2,boot_action='set-default',default_id=ids[0],roots=[
        dict(id=id,name=name,size_gib=32,filesystem=fs) for id,name,fs in zip(ids,('Fedora Plasma','Ubuntu Plasma'),('ext4','btrfs'))])
    planned=plan(disk,config,generation)
    inventory={}
    for p in planned['prefix']+planned['partitions']:
        name=p['name'];number=p['number'];start=p['first_sector']*4096;size=(p['last_sector']-p['first_sector']+1)*4096
        inventory[name]=dict(name=name,node='sda'+str(number),dev='8:'+str(number),parent='sda',parent_dev='8:0',
            number=number,partuuid=p.get('partuuid',str(uuid.uuid5(uuid.UUID(generation),name))),start=start,bytes=size)
        if p.get('role')in('store','root'):inventory[name].update(filesystem=p['filesystem'],fsuuid=p['fsuuid'])
    cursor=0
    for name in ('boot_a','boot_b','vendor_boot_a','dtbo_a','vbmeta_a','modem_a','bluetooth_a','dsp_a'):
        source=next((x for x in profile['images'].values() if x['partition']==name),None)
        size=source['bytes'] if source else 16384;index=len([p for p in inventory.values() if p['parent']=='sde'])+1
        inventory[name]=dict(name=name,node='sde'+str(index),dev='8:'+str(64+index),parent='sde',parent_dev='8:64',
            number=index,partuuid=str(uuid.uuid5(uuid.UUID(generation),name)),start=cursor,bytes=size)
        cursor+=size
    engine.demo_recovery_partitions={}
    for name in ('recovery_a','recovery_b'):
        index=len([p for p in inventory.values() if p['parent']=='sde'])+1
        inventory[name]=dict(name=name,node='sde'+str(index),dev='8:'+str(64+index),parent='sde',parent_dev='8:64',
            number=index,partuuid=str(uuid.uuid5(uuid.UUID(generation),name)),start=cursor,bytes=100*1024**2)
        cursor+=100*1024**2
        path=engine.state_dir/(name+'.img')
        with path.open('wb') as stream:stream.write(('DEMO '+name).encode());stream.truncate(100*1024**2)
        engine.demo_recovery_partitions[name]=path
    # vbmeta_system_a remains in the real sda prefix; pad its small image in the plan.
    files={};systems=[]
    for system in planned['systems']:
        payload=('DEMO LINUX '+system['id']).encode();image='systems/'+system['id']+'/images/'+generation+'.img'
        digest=hashlib.sha256(payload).hexdigest();files[image]=dict(bytes=len(payload),sha256=digest)
        systems.append(dict(id=system['id'],name=system['name'],type='linux',partition=system['partition'],
                            target=system['target'],boot_sha256=digest,image=image))
    android_id=str(uuid.uuid5(uuid.UUID(generation),'android'));boot=profile['images']['boot.img']
    image='systems/'+android_id+'/images/'+generation+'.img';files[image]=dict(bytes=boot['bytes'],sha256=boot['sha256'])
    systems.append(dict(id=android_id,name='Android DEMO',type='android',rom=profile['rom'],slot='a',generation=generation,
                        userdata=inventory['userdata'],metadata=inventory['metadata'],boot_sha256=boot['sha256'],image=image,status='preserved-unverified'))
    registry=dict(schema_version=1,serial='DEMO-LIUQIN',layout_generation=generation,default_id=ids[0],
                  default_generation=generation,systems=systems,userdata_state='android-preserved-unverified')
    raw=canonical(registry);files['registry.json']=dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    files['SHA256SUMS']=dict(bytes=0,sha256=hashlib.sha256(b'').hexdigest())
    engine.demo_android_scan=dict(inventory=inventory,registry=registry,registry_sha256=files['registry.json']['sha256'],
        files=files,free_bytes=1024**3,store_fsuuid=planned['store']['fsuuid'],userdata_blank=False,
        userdata_linux_signature=False,pending_transactions=[])


def report(engine, layout):
    initialize(engine,layout);disk=layout.reference_stock_disk()
    lines=[f'Disk /dev/sda: {disk.total_sectors} sectors','Sector size (logical/physical): 4096/4096 bytes',
           f'First usable sector is {disk.first_usable}, last usable sector is {disk.last_usable}',
           'Partition table holds up to 64 entries','Number  Start (sector) End (sector) Size Code Name']
    for p in sorted((p for p in engine.demo_android_scan['inventory'].values() if p['parent']=='sda'),key=lambda p:p['start']):
        lines.append(f"{p['number']} {p['start']//4096} {(p['start']+p['bytes'])//4096-1} 1.0 GiB 8300 {p['name']}")
    return '\n'.join(lines)+'\n'


def rpc(transport, action, request):
    import base64,gzip
    engine=transport.engine;scan=engine.demo_android_scan
    transport.calls.append('android:'+action)
    if action=='inspect':return copy.deepcopy(scan)
    if action=='export-boot':return dict(boot_gzip_base64=base64.b64encode(gzip.compress((engine.demo_android_rom/'boot.img').read_bytes())).decode())
    plan=request['plan']
    if action.startswith('linux-'):
        from liuqin_managed import validate_linux_plan
        validate_linux_plan(plan,transport.serial)
        if action=='linux-stage':
            data=engine.android_boot.read_bytes()
            scan['files'][plan['system']['image']]=dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        elif action=='linux-commit':
            scan['registry']=copy.deepcopy(request['registry']);raw=canonical(scan['registry'])
            scan['registry_sha256']=hashlib.sha256(raw).hexdigest()
            scan['files']['registry.json']=dict(bytes=len(raw),sha256=scan['registry_sha256'])
        elif action not in ('linux-guard','linux-finish'):raise RuntimeError('unknown demo Linux operation')
        return dict(completed=True)
    if action=='guard':
        if scan['inventory']!=plan['inventory'] or scan['registry']!=plan['registry'] or transport.current_boot_digest()!=plan['previous_boot_sha256']:
            raise RuntimeError('demo Android snapshot changed')
        return dict(guarded=True)
    if action=='stage':
        image='systems/'+plan['android_id']+'/images/'+plan['transaction']+'.img'
        entry=engine.demo_android_profile['images']['boot.img']
        scan['files'][image]=dict(bytes=entry['bytes'],sha256=entry['sha256'])
        scan['pending_transactions'].append(plan['transaction'])
        return dict(staged=True)
    if action=='install':
        if plan['mode']!='reinstall' or request.get('erase_android_data')is not True:raise RuntimeError('demo erasure missing')
        return dict(rom_written=True,android_data_initialized=True)
    if action=='commit':
        registry=request['registry'];check_registry_transition(plan['registry'],registry,plan['android_id'],plan['set_default'])
        scan['registry']=copy.deepcopy(registry);raw=canonical(registry)
        scan['registry_sha256']=hashlib.sha256(raw).hexdigest();scan['files']['registry.json']=dict(bytes=len(raw),sha256=scan['registry_sha256'])
        scan['pending_transactions']=[];scan['userdata_blank']=False
        return dict(committed=True,linux_preserved=True)
    raise RuntimeError('unknown demo Android action')


def adopt_layout(engine, archive, layout):
    """Persist a simulated partition-only transaction across GUI operations."""
    import tarfile
    initialize(engine,layout)
    scan=engine.demo_android_scan;planned=engine.multi_plan
    inventory={name:p for name,p in scan['inventory'].items() if p['parent']!='sda'}
    for p in planned['prefix']+planned['partitions']:
        name=p['name'];number=p['number']
        item=dict(name=name,node='sda'+str(number),dev='8:'+str(number),parent='sda',parent_dev='8:0',
            number=number,partuuid=p.get('partuuid',str(uuid.uuid5(uuid.UUID(planned['generation']),name))),
            start=p['first_sector']*4096,bytes=(p['last_sector']-p['first_sector']+1)*4096)
        if p.get('role')=='store' or (p.get('role')=='root' and not engine.multi_config.get('partition_only')):
            item.update(filesystem=p['filesystem'],fsuuid=p['fsuuid'])
        elif p.get('role')=='root':item.update(filesystem='unformatted',fsuuid=None)
        inventory[name]=item
    files={}
    with tarfile.open(archive) as stream:
        for entry in stream:
            data=stream.extractfile(entry).read()
            files[entry.name]=dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
            if entry.name=='registry.json':registry=json.loads(data)
    scan.update(inventory=inventory,registry=registry,registry_sha256=files['registry.json']['sha256'],files=files,
        store_fsuuid=planned['store']['fsuuid'],userdata_blank=True,userdata_linux_signature=False,pending_transactions=[])
