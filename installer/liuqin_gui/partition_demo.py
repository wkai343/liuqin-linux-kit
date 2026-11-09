# SPDX-License-Identifier: MIT
"""Persistent synthetic GPT used by read/append GUI tests; no device access."""
import copy
import hashlib
import uuid
from .engine import cli,canonical
from .demo import demo_report
from .android_demo import initialize


def inspect(engine):
    initialize(engine,cli.layout)
    report=getattr(engine,'demo_partition_report',getattr(engine,'demo_raw_report',demo_report()))
    disk=cli.layout.parse_print(report)
    persisted=hasattr(engine,'demo_partition_report') and {p.name for p in disk.partitions}=={n for n,p in engine.demo_android_scan['inventory'].items() if p['parent']=='sda'}
    if disk.by_name('linux_boot') or persisted:
        result=copy.deepcopy(engine.demo_android_scan)
    else:
        inventory={name:copy.deepcopy(p) for name,p in engine.demo_android_scan['inventory'].items() if p['parent']!='sda'}
        for p in disk.partitions:
            inventory[p.name]=dict(name=p.name,node='sda'+str(p.number),dev='8:'+str(p.number),parent='sda',parent_dev='8:0',
                number=p.number,start=p.start,bytes=p.size,partuuid=str(uuid.uuid5(uuid.NAMESPACE_DNS,'demo-'+p.name)))
        result=dict(inventory=inventory,registry=None,files={})
    details={str(p['number']):canonical({k:p[k] for k in ('name','number','start','bytes','partuuid')}).decode()
        for p in result['inventory'].values() if p['parent']=='sda'}
    return dict(result,report=report,details=details)


def apply(engine,plan):
    from liuqin_partitions import validate_append,snapshot_id
    validate_append(plan,'DEMO-LIUQIN')
    if snapshot_id(inspect(engine))!=snapshot_id(plan['source']):raise RuntimeError('demo GPT changed')
    old=copy.deepcopy(plan['source']);inventory=old['inventory']
    for p in plan['partitions']:
        inventory[p['name']]=dict(name=p['name'],node='sda'+str(p['number']),dev='8:'+str(p['number']),parent='sda',parent_dev='8:0',
            number=p['number'],start=p['start'],bytes=p['bytes'],partuuid=p['partuuid'])
        if p['role']=='store':inventory[p['name']].update(filesystem='ext4',fsuuid=p['fsuuid'])
        elif p['role']=='root':inventory[p['name']].update(filesystem='unformatted',fsuuid=None)
    lines=old['report'].split('Number  Start (sector)',1)[0].splitlines()+['Number  Start (sector) End (sector) Size Code Name']
    for p in sorted((p for p in inventory.values() if p['parent']=='sda'),key=lambda p:p['start']):
        lines.append(f"{p['number']} {p['start']//4096} {(p['start']+p['bytes'])//4096-1} 1.0 GiB 8300 {p['name']}")
    engine.demo_partition_report='\n'.join(lines)+'\n'
    registry=copy.deepcopy(plan['candidate_registry']);raw=canonical(registry);files=old['files']
    files['registry.json']=dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    files.setdefault('SHA256SUMS',dict(bytes=0,sha256=hashlib.sha256(b'').hexdigest()))
    engine.demo_android_scan=dict(inventory=inventory,registry=registry,registry_sha256=files['registry.json']['sha256'],files=files,
        free_bytes=1024**3,store_fsuuid=plan['store']['fsuuid'],userdata_blank='userdata' not in old['inventory'],
        userdata_linux_signature=False,pending_transactions=[])
    return dict(partitions_created=True,existing_partitions_preserved=True)


def edit(engine,plan,resume=False,check_only=False):
    from liuqin_partition_editor import validate_edit
    from liuqin_partitions import snapshot_id
    validate_edit(plan,'DEMO-LIUQIN')
    current=inspect(engine)
    if resume:
        expected=plan['projected']['inventory']
        if (plan['source']['inventory'].get('linux_boot') or plan['formats'] or current.get('registry') or
            set(current['inventory'])!=set(expected) or any(any(current['inventory'][n].get(k)!=p.get(k)
                for k in ('parent','number','start','bytes','partuuid')) for n,p in expected.items()) or
            any(current['inventory'][p['name']].get('filesystem')!='unformatted' for p in plan['partitions'])):
            raise RuntimeError('demo pending GPT/initialization mismatch')
        if check_only:return dict(resumable=True)
    elif snapshot_id(current)!=snapshot_id(plan['source']):raise RuntimeError('demo GPT changed before editing')
    state=copy.deepcopy(plan['projected']);engine.demo_partition_report=state['report']
    for p in state['inventory'].values():
        if p.get('node')=='pending':p.update(node='sda'+str(p['number']),dev='8:'+str(p['number']))
    state.setdefault('free_bytes',1024**3);state.setdefault('userdata_blank',False)
    state.setdefault('userdata_linux_signature',False);state.setdefault('pending_transactions',[])
    if 'linux_boot' in state['inventory']:
        state['store_fsuuid']=state['inventory']['linux_boot']['fsuuid']
        state['files'].setdefault('SHA256SUMS',dict(bytes=0,sha256=hashlib.sha256(b'').hexdigest()))
    engine.demo_android_scan=state
    return dict(edited=True)
