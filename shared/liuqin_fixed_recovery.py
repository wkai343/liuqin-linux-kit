# SPDX-License-Identifier: MIT
"""Strict offline inspection for the liuqin v0 fixed-Recovery candidate.

This module never authorizes a flash or changes normal installer eligibility.
The AVB signature is checked against its included key, not an OEM trust anchor.
"""
import hashlib
import json
from pathlib import Path
import struct
import zlib
from liuqin_native_recovery import MARKER,validate as validate_native
from liuqin_artifacts import bounded_initramfs,cpio_files

LIMIT=100*1024**2
USAGE='fixed-recovery-candidate'
STATUS='DEVICE_UNTESTED'

def encoded(value):return (json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+'\n').encode()
def sha(data):return hashlib.sha256(data).hexdigest()
def align(n):return (n+4095)//4096*4096

def kernel_and_dtb(blob):
    if len(blob)>64*1024**2 or not blob.startswith(b'\x1f\x8b'):raise ValueError('expected bounded gzip kernel with appended DTB')
    decoder=zlib.decompressobj(31)
    kernel=decoder.decompress(blob,128*1024**2+1)
    if len(kernel)>128*1024**2 or not decoder.eof or decoder.unconsumed_tail:raise ValueError('invalid or oversized gzip kernel')
    dtb=decoder.unused_data
    if len(dtb)<40 or len(dtb)>2*1024**2 or dtb[:4]!=b'\xd0\x0d\xfe\xed':raise ValueError('missing appended FDT')
    if struct.unpack_from('>I',dtb,4)[0]!=len(dtb) or b'xiaomi,liuqin\0' not in dtb:raise ValueError('invalid liuqin FDT boundary')
    return kernel,dtb,len(blob)-len(dtb)

def pack_v0(kernel,ramdisk,dtb,source_header):
    if len(source_header)!=4096:raise ValueError('expected v2 source header page')
    header=bytearray(source_header);combined=kernel+dtb
    struct.pack_into('<I',header,8,len(combined));struct.pack_into('<I',header,16,len(ramdisk))
    struct.pack_into('<I',header,24,0);struct.pack_into('<I',header,40,0);header[1632:]=bytes(4096-1632)
    digest=hashlib.sha1()
    for part in (combined,ramdisk,b''):digest.update(part);digest.update(struct.pack('<I',len(part)))
    header[576:608]=digest.digest()+bytes(12)
    return bytes(header)+combined+bytes(-len(combined)%4096)+ramdisk+bytes(-len(ramdisk)%4096)

def avb_info(data):
    if len(data)!=LIMIT:raise ValueError('fixed Recovery must fill the exact 100 MiB partition')
    footer=data[-64:]
    magic,major,minor,original,offset,size=struct.unpack_from('>4sIIQQQ',footer)
    if (magic!=b'AVBf' or (major,minor)!=(1,0) or not 4096<=original<=offset or
        offset%4096 or not 256<=size<=65536 or offset+size>len(data)-4096):raise ValueError('invalid AVB footer ranges')
    meta=data[offset:offset+size]
    if meta[:4]!=b'AVB0':raise ValueError('missing Recovery vbmeta')
    be64=lambda n:struct.unpack_from('>Q',meta,n)[0]
    auth,aux=be64(12),be64(20);algorithm=struct.unpack_from('>I',meta,28)[0]
    if 256+auth+aux!=size or algorithm!=1 or auth%64 or aux%64:raise ValueError('expected bounded SHA256_RSA2048 metadata')
    def area(base,extent,start,length):
        if start>extent or length>extent-start:raise ValueError('AVB field exceeds its block')
        return meta[base+start:base+start+length]
    auxbase=256+auth
    stored_hash=area(256,auth,be64(32),be64(40));signature=area(256,auth,be64(48),be64(56))
    key=area(auxbase,aux,be64(64),be64(72));desc=area(auxbase,aux,be64(96),be64(104))
    digest=hashlib.sha256(meta[:256]+meta[auxbase:]).digest()
    if stored_hash!=digest or len(signature)!=256 or len(key)!=520 or struct.unpack_from('>I',key)[0]!=2048:raise ValueError('invalid AVB hash/key/signature shape')
    modulus=int.from_bytes(key[8:264],'big');sig=int.from_bytes(signature,'big')
    if modulus.bit_length()!=2048 or sig>=modulus:raise ValueError('invalid RSA key or signature')
    digest_info=bytes.fromhex('3031300d060960864801650304020105000420')+digest
    expected=b'\0\1'+b'\xff'*(256-len(digest_info)-3)+b'\0'+digest_info
    if pow(sig,65537,modulus).to_bytes(256,'big')!=expected:raise ValueError('AVB RSA signature does not verify')
    if struct.unpack_from('>I',meta,120)[0]!=0:raise ValueError('verification-disable flags are forbidden')
    hashes=[];pos=0
    while pos<len(desc):
        if len(desc)-pos<16:raise ValueError('truncated AVB descriptor')
        tag,length=struct.unpack_from('>QQ',desc,pos);end=pos+16+length
        if length%8 or end>len(desc):raise ValueError('invalid AVB descriptor length')
        entry=desc[pos:end]
        if tag!=2 or len(entry)<132:raise ValueError('fixed Recovery may only carry its own hash descriptor')
        image_size=struct.unpack_from('>Q',entry,16)[0];n,s,d,flags=struct.unpack_from('>4I',entry,56)
        if 132+n+s+d>len(entry) or entry[24:56].rstrip(b'\0')!=b'sha256' or d!=32 or flags!=0:raise ValueError('invalid Recovery hash descriptor')
        name=entry[132:132+n];salt=entry[132+n:132+n+s];expected_hash=entry[132+n+s:132+n+s+d]
        if name!=b'recovery' or image_size!=original or hashlib.sha256(salt+data[:image_size]).digest()!=expected_hash:raise ValueError('Recovery payload hash mismatch')
        hashes.append(dict(name='recovery',bytes=image_size));pos=end
    if len(hashes)!=1:raise ValueError('exactly one Recovery hash descriptor required')
    return dict(original_bytes=original,vbmeta_offset=offset,vbmeta_bytes=size,public_key_sha256=sha(key),rollback_index=be64(112),self_signature_verified=True,oem_trust_verified=False)

def inspect(path):
    data=Path(path).read_bytes();avb=avb_info(data)
    u=lambda n:struct.unpack_from('<I',data,n)[0]
    if data[:8]!=b'ANDROID!' or (u(36),u(40),u(24))!=(4096,0,0):raise ValueError('fixed candidate requires v0, 4096-byte pages and no second stage')
    ks,rs=u(8),u(16);ramstart=4096+align(ks)
    if not ks or not rs or ramstart+align(rs)!=avb['original_bytes']:raise ValueError('boot and AVB payload boundaries differ')
    kernel,dtb,gzip_bytes=kernel_and_dtb(data[4096:4096+ks]);ram=data[ramstart:ramstart+rs]
    legacy=hashlib.sha1()
    for part in (data[4096:4096+ks],ram,b''):legacy.update(part);legacy.update(struct.pack('<I',len(part)))
    if data[576:608]!=legacy.digest()+bytes(12):raise ValueError('legacy image ID mismatch')
    files=cpio_files(bounded_initramfs(ram,256*1024**2))
    mode,raw=files.get(MARKER,(0,b''))
    if mode&0o170000!=0o100000:raise ValueError('missing fixed Recovery identity')
    m=json.loads(raw)
    if m.get('usage')!=USAGE or m.get('fixed_version')!=1 or m.get('fixed_boot_header')!=0:raise ValueError('not a fixed Recovery candidate')
    # Reuse payload checks without making this role eligible in the GUI.
    native=dict(m,usage='temporary-boot-only');validate_native(encoded(native),sha(kernel),dtb,files)
    identity=m.get('identity',{})
    if (sha(encoded(identity))!=m['build_id'] or identity.get('kernel_sha256')!=m['kernel_sha256'] or
        identity.get('dtb_sha256')!=m['dtb_sha256'] or identity.get('payload_sha256')!=m['payload_sha256']):raise ValueError('fixed build identity mismatch')
    profile=m.get('fixed_profile',{})
    if (profile.get('device')!='liuqin' or profile.get('unlocked_required') is not True or
        profile.get('partition_bytes')!=LIMIT or profile.get('boot_header_version')!=0 or
        profile.get('kernel_sha256')!=sha(kernel) or profile.get('dtb_sha256')!=sha(dtb) or
        profile.get('recovery_rollback_index')!=avb['rollback_index'] or
        identity.get('fixed_profile')!=profile or identity.get('avb_public_key_sha256')!=avb['public_key_sha256']):raise ValueError('fixed image differs from its firmware/signing profile')
    if files.get('etc/liuqin-storage-mode',(0,b''))[1]!=b'readonly\n':raise ValueError('fixed Recovery must remain read-only')
    return dict(image_bytes=len(data),image_sha256=sha(data),manifest=m,avb=avb,kernel_sha256=sha(kernel),dtb_sha256=sha(dtb),gzip_kernel_bytes=gzip_bytes,ramdisk_bytes=rs,status='OFFLINE_CANDIDATE_NOT_HARDWARE_BOOT_TESTED')
