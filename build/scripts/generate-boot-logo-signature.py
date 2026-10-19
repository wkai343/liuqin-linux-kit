#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reproduce Liuqin logo detection features from the pinned OEM imagefv input."""
from pathlib import Path
import argparse
import hashlib
import lzma
import struct
import zlib

SHA256 = '423555b84edafde237a03f73b9e8acb08bc33f7c5d2d5fb5486463bc07698b3b'
DECODED_SHA256 = '676a71b65155c817eec06f9225144bed83e93bfa86226db84fdf72e065c07646'
LOGOS = (0x25fc60, 0x283c98, 0x2a7ccc, 0x2d2d08)


def samples(imagefv):
    path=Path(imagefv)
    if path.is_symlink() or not path.is_file() or path.stat().st_size!=6298968:
        raise ValueError('expected the pinned imagefv.elf regular file')
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=SHA256:
        raise ValueError('imagefv.elf SHA256 mismatch')
    start=4192;size=int.from_bytes(data[start:start+3],'little')
    if size!=217690 or data[start+3]!=2:
        raise ValueError('unexpected compressed firmware section')
    decoder=lzma.LZMADecompressor(format=lzma.FORMAT_ALONE,memlimit=256*1024*1024)
    raw=decoder.decompress(data[start+24:start+size],max_length=4*1024*1024)
    if not decoder.eof or hashlib.sha256(raw).hexdigest()!=DECODED_SHA256:
        raise ValueError('unexpected decompressed firmware volume')
    images=[]
    for offset in LOGOS:
        if raw[offset:offset+8]!=b'LOGO!!!!':raise ValueError('missing logo table')
        compressed=struct.unpack_from('<I',raw,offset+12)[0]
        decoder=zlib.decompressobj(31)
        frames=decoder.decompress(raw[offset+4096:offset+4096+compressed],128*1024*1024)
        if not decoder.eof or decoder.unconsumed_tail:raise ValueError('invalid bounded logo stream')
        pos,length=struct.unpack_from('<II',raw,offset+16)
        frame=frames[pos:pos+length]
        if len(frame)!=length or frame[:2]!=b'BM':raise ValueError('invalid logo bitmap')
        pixel_offset=struct.unpack_from('<I',frame,10)[0]
        header,width,height,planes,bits,compression=struct.unpack_from('<IiiHHI',frame,14)
        if (header,width,height,planes,bits,compression,pixel_offset)!=(40,1800,2880,1,32,0,54):
            raise ValueError('unexpected logo bitmap layout')
        if len(frame)!=54+1800*2880*4:raise ValueError('truncated bitmap')
        images.append(frame)
    def value(frame,x,y):
        start=54+((2879-y)*1800+x)*4
        return max(frame[start:start+3])
    buckets={}
    for y in range(1200,1681,5):
        for x in range(720,1081,5):
            bits=[]
            for image in images:
                values=[value(image,x+dx,y+dy) for dx,dy in ((0,0),(-1,0),(1,0),(0,-1),(0,1))]
                if max(values)<32:bits.append(0)
                elif min(values)>223:bits.append(1)
                else:break
            if len(bits)!=4:continue
            mask=sum(bit<<i for i,bit in enumerate(bits))
            if mask not in (0,15):buckets.setdefault(mask,[]).append((x,y,mask))
    points=[]
    for _,items in sorted(buckets.items()):
        count=min(len(items),24)
        points.extend(items[i*len(items)//count] for i in range(count))
    if len(points)!=224:raise ValueError('unexpected signature size')
    return points


def header(points):
    return ('/* SPDX-License-Identifier: MIT */\n'
            '/* Detection features from OS2.0.6.0.VMYCNXM imagefv.elf.\n'
            ' * SHA256: '+SHA256+'\n'
            ' * Reproduce with build/scripts/generate-boot-logo-signature.py.\n'
            ' * Only coordinates and four bright/dark class bits are embedded. */\n'
            'struct logo_point { unsigned short x,y; unsigned char mask; };\n'
            'static const struct logo_point logo_points[] = {\n'+
            ''.join(f' {{{x},{y},{mask}}},\n' for x,y,mask in points)+
            '};\nstatic const unsigned int logo_angles[4] = {0,180,90,270};\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--imagefv',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists() or args.output.is_symlink():parser.error('output already exists')
    args.output.write_text(header(samples(args.imagefv)))
