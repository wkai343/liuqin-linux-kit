# SPDX-License-Identifier: MIT
"""Canonical, OS-independent firmware fingerprints and native Recovery catalog."""
import hashlib
import json
from pathlib import Path
import re
import uuid

SCHEMA=1
ROLES=('abl','dtbo','vbmeta','vbmeta_system')
LAYOUT=('boot','vendor_boot','recovery')
HEX=re.compile(r'[a-f0-9]{64}')
CATALOG=Path(__file__).resolve().with_name('liuqin-native-recovery-profiles.json')

def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def digest(value):return hashlib.sha256(encoded(value)).hexdigest()
def uint(value,maximum):return type(value) is int and 0<value<=maximum

def validate_measured(value,complete=False):
    if not isinstance(value,dict) or set(value)-set(ROLES)-{'layout'}:raise ValueError('invalid firmware measurement set')
    if complete and set(value)!=set(ROLES)|{'layout'}:raise ValueError('firmware match is incomplete')
    for role in ROLES:
        if role not in value:continue
        part=value[role]
        if (not isinstance(part,dict) or set(part)!={'partition_bytes','hash_bytes','sha256'} or
            not uint(part['partition_bytes'],64*1024**2) or not uint(part['hash_bytes'],part['partition_bytes']) or
            not isinstance(part['sha256'],str) or not HEX.fullmatch(part['sha256']) or
            (role!='dtbo' and part['hash_bytes']!=part['partition_bytes'])):raise ValueError('invalid firmware measurement: '+role)
    layout=value.get('layout',{})
    if not isinstance(layout,dict) or set(layout)-set(LAYOUT) or any(not uint(n,512*1024**2) for n in layout.values()):raise ValueError('invalid boot partition layout')
    if complete and set(layout)!=set(LAYOUT):raise ValueError('boot partition layout is incomplete')
    return value

def validate_match(value):return validate_measured(value,complete=True)

def fingerprint_id(match):return digest(dict(schema=SCHEMA,device='liuqin',match=validate_match(match)))

def validate_snapshot(value,serial=None,boot_id=None,build_id=None):
    if (not isinstance(value,dict) or value.get('schema')!=SCHEMA or value.get('device')!='liuqin' or
        not isinstance(value.get('serial'),str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value['serial']) or
        value.get('active_slot') not in ('a','b') or value.get('unlocked') is not True or value.get('sealed') is not True or
        not isinstance(value.get('runtime_build_id'),str) or not HEX.fullmatch(value['runtime_build_id']) or
        not isinstance(value.get('slots'),dict) or set(value['slots'])!={'a','b'}):raise ValueError('invalid firmware session identity')
    uuid.UUID(value['boot_id'])
    for key,expected in [('serial',serial),('boot_id',boot_id),('runtime_build_id',build_id)]:
        if expected is not None and value.get(key)!=expected:raise ValueError('firmware snapshot belongs to a different '+key)
    for slot in value['slots'].values():
        if not isinstance(slot,dict) or slot.get('status') not in ('complete','incomplete'):raise ValueError('invalid slot fingerprint')
        if slot['status']=='complete':
            if slot.get('fingerprint_id')!=fingerprint_id(slot.get('match')):raise ValueError('firmware fingerprint digest mismatch')
        elif (slot.get('fingerprint_id') or not isinstance(slot.get('errors'),list) or not slot['errors'] or
              any(not isinstance(e,str) or len(e)>500 for e in slot['errors'])):raise ValueError('incomplete slot must explain missing evidence')
        measured=slot.get('measured',slot.get('match',{}));validate_measured(measured)
        if slot['status']=='complete' and measured!=slot['match']:raise ValueError('conflicting slot measurements')
        for name,role in [('avb','vbmeta'),('avb_system','vbmeta_system')]:
            a=slot.get(name)
            if a is None:continue
            if not isinstance(a,dict) or a.get('status') not in ('complete','unavailable'):raise ValueError('invalid AVB observation')
            if a['status']=='complete':
                if (type(a.get('flags')) is not int or a['flags'] not in (0,1,2,3) or
                    not uint(a.get('metadata_bytes'),65536) or a['metadata_bytes']<256 or
                    any(not isinstance(a.get(k),str) or not HEX.fullmatch(a[k]) for k in ('metadata_sha256','normalized_flags_sha256','partition_sha256')) or
                    a['partition_sha256']!=measured.get(role,{}).get('sha256')):raise ValueError('AVB observation not bound to measured partition')

    return value

def load_catalog(path=CATALOG):
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value,dict) or value.get('schema')!=SCHEMA or not isinstance(value.get('profiles'),list) or len(value['profiles'])>128:raise ValueError('invalid native Recovery catalog')
    ids=set();matches=set()
    for p in value['profiles']:
        if (not isinstance(p,dict) or not isinstance(p.get('id'),str) or not re.fullmatch('[a-z0-9][a-z0-9._-]{0,95}',p['id']) or
            p['id'] in ids or p.get('device')!='liuqin' or not isinstance(p.get('label'),str) or not 0<len(p['label'])<=160):raise ValueError('invalid/duplicate native profile')
        fid=fingerprint_id(p.get('match'))
        if fid in matches:raise ValueError('ambiguous native firmware profiles')
        ids.add(p['id']);matches.add(fid)
        artifacts=p.get('artifacts')
        if not isinstance(artifacts,dict) or set(artifacts)-{'temporary','fixed_candidate'} or 'temporary' not in artifacts:raise ValueError('native profile lacks a temporary artifact')
        for role,a in artifacts.items():
            state='TEMPORARY_BOOT_VERIFIED' if role=='temporary' else 'OFFLINE_VALIDATED_HARDWARE_BOOT_UNTESTED'
            if (not isinstance(a,dict) or a.get('validation')!=state or
                any(not isinstance(a.get(k),str) or not HEX.fullmatch(a[k]) for k in ('image_sha256','build_id')) or
                not isinstance(a.get('evidence'),str) or not 0<len(a['evidence'])<=1024 or
                not isinstance(a.get('slots'),list) or not a['slots'] or not set(a['slots'])<={'a','b'} or len(set(a['slots']))!=len(a['slots'])):raise ValueError('artifact qualification is incomplete: '+role)
    return value['profiles']

def match_slot(snapshot,slot,profiles):
    validate_snapshot(snapshot)
    s=snapshot['slots'][slot]
    if s['status']!='complete':return dict(status='incomplete',profile=None,mismatches=s['errors'])
    matches=[p for p in profiles if p['match']==s['match']]
    if len(matches)>1:raise ValueError('multiple native profiles match this firmware')
    if matches:return dict(status='matched',profile=matches[0],mismatches=[])
    near=[p for p in profiles if p['match']['abl']==s['match']['abl']]
    differences=sorted({role for p in near for role in (*ROLES,'layout') if p['match'][role]!=s['match'][role]})
    return dict(status='mismatch' if near else 'unknown',profile=None,mismatches=differences)
