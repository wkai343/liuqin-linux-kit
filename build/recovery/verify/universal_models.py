# SPDX-License-Identifier: MIT
"""Two pinned ABL models. Only snapshot I/O and hardware input services vary."""
import os,struct,hashlib
from pathlib import Path
from emulator_core import Core,ROOT,OLD
from hyper_core import HyperCore
from verify_avb_chain import Avb,public_key,INVENTORY
from verify_device_tree import Trees

ROM_EVIDENCE=Path(os.environ['LIUQIN_ROM_EVIDENCE']).resolve()
class CurrentAvb(Avb):
    def guid(self):
        if self.string(self.reg(1)) not in INVENTORY:self.ret(3);return
        super().guid()

class HyperAvb(Avb,HyperCore):
    def __init__(self,image,unlocked=True):
        HyperCore.__init__(self);self.unlocked=unlocked;self.rollback_value=0;self.rollback_writes={};self.slot='_a';self.reads=[];self.callbacks=[]
        self.parts={'recovery_a':Path(image).read_bytes(),'vbmeta_a':(OLD/'vbmeta_a.img').read_bytes(),
            'vbmeta_system_a':(Path(os.environ['LIUQIN_FIXED_EVIDENCE'])/'vbmeta_system_a.img').read_bytes()}
        self.trusted=public_key(self.parts['vbmeta_a']);self.ops=0x300000;self.request=0x301000;self.output=0x302000
        for offset,fn in [(0x18,self.read_partition),(0x20,self.write_forbidden),(0x28,self.check_key),(0x30,self.rollback),(0x38,self.rollback_write),(0x40,self.device_unlocked),(0x48,self.guid),(0x50,self.part_size),(0x58,self.unsupported)]:
            pc=0x601000+offset*4;self.put64(self.ops+offset,pc);self.hook(pc,fn)
        self.u.mem_write(0x301100,b'recovery\0');self.put64(self.request,0x301100);self.u.mem_write(0x301200,b'_a\0')
    def guid(self):
        if self.string(self.reg(1)) not in INVENTORY:self.ret(3);return
        super().guid()
    def verify(self):
        result=self.run(0xdce8,(self.ops,self.request,0x301200,int(self.unlocked),0,self.output),count=500000000)
        data=self.get64(self.output);r=dict(result=result,slot_data=bool(data),reads=self.reads,callbacks=self.callbacks,events=self.events,loaded_partitions=[])
        if data:
            rows=self.get64(data+0x18)
            for i in range(self.get64(data+0x20)):
                p=rows+i*24;r['loaded_partitions'].append(dict(name=self.string(self.get64(p)),bytes=self.get64(p+16)))
            r['cmdline']=self.string(self.get64(data+0x28))
        return r

def rom_parts(rom):
    if rom=='miui14':return (OLD/'vbmeta_a.img').read_bytes(),(Path(os.environ['LIUQIN_FIXED_EVIDENCE'])/'vbmeta_system_a.img').read_bytes()
    directory=ROM_EVIDENCE/('miku' if rom=='miku26101322' else 'hyperos206')
    return (directory/'vbmeta.img').read_bytes().ljust(131072,b'\0'),(directory/'vbmeta_system.img').read_bytes().ljust(131072,b'\0')

def make_avb(base,image,rom,flags=None,missing_system=False,unlocked=True):
    model=CurrentAvb(image,unlocked=unlocked) if base=='miui-abl-20230422' else HyperAvb(image,unlocked=unlocked)
    vb,system=rom_parts(rom);meta=bytearray(vb)
    if flags is not None:struct.pack_into('>I',meta,120,flags)
    model.parts={'recovery_a':Path(image).read_bytes(),'vbmeta_a':bytes(meta)}
    if not missing_system:model.parts['vbmeta_system_a']=system
    return model

class V2Trees:
    def choose_v2(self,image_path,matching_only=False):
        b=Path(image_path).read_bytes();u=lambda n:struct.unpack_from('<I',b,n)[0];a=lambda n:(n+4095)//4096*4096
        assert u(40)==2;dtbo_offset=struct.unpack_from('<Q',b,1636)[0];dtb_offset=dtbo_offset+a(u(1632))
        full=self.allocate(len(b)+4096);self.u.mem_write(full,b);target=self.allocate(2*1024**2)
        soc=self.run(self.soc_entry,(full,dtb_offset+u(1648),dtb_offset,target))
        table=full+dtbo_offset;board=self.run(self.board_entry,(0x302000,table));index=None
        data=b[dtbo_offset:dtbo_offset+u(1632)];_,_,_,es,count,start,_,_=struct.unpack_from('>8I',data)
        for i in range(count):
            size,offset=struct.unpack_from('>II',data,start+i*es)
            if board==table+offset:index=i
        r=dict(soc_match=bool(soc),soc_offset=soc-full if soc else None,expected_soc_offset=dtb_offset,dtbo_index=index)
        if matching_only:return r
        info=0x302000;params=0x303000;self.u.mem_write(info,b'\1\1');self.put32(info+0x458,2);self.put64(info+0x428,1)
        self.u.mem_write(0x304000,b'recovery\0');self.u.mem_write(info+0x128,struct.pack('<QQQ',0x304000,full,len(b)))
        for off,val in [(8,full),(16,len(b)),(0x58,target)]:self.put64(params+off,val)
        for off,val in [(0x6c,4096),(0x70,u(8)),(0x78,u(16))]:self.put32(params+off,val)
        code=self.run(self.load_entry,(info,params),count=1000000000)
        r['full_load_status']=hex(code)
        if code==0:
            size=struct.unpack('>I',self.read(target+4,4))[0];assert 0<size<=2*1024**2
            self.merged_dtb=self.read(target,size);r['merged_sha256']=hashlib.sha256(self.merged_dtb).hexdigest()
        r['provided_images']=['recovery'];return r

class CurrentTrees(V2Trees,Trees):
    soc_entry=0x22d00;board_entry=0x24318;load_entry=0x1d090;gzip_entry=0x20b40

class HyperTrees(V2Trees,HyperCore):
    soc_entry=0x22dc0;board_entry=0x24420;load_entry=0x1d120;gzip_entry=0x20c00
    def __init__(self,project=16,foundry=0,subtype=0,version=0x10000,ddr=0,pmic0=0x2f,soc_id=531):
        HyperCore.__init__(self);self.pmic0=pmic0;self.pmic_queries=[]
        for offset,value in [(0,8),(4,version),(8,subtype),(0x10,soc_id),(0x24,0x10000),(0x28,foundry),(0x2c,ddr)]:self.put32(0xc2dcc+offset,value)
        self.put32(0xc2e04+0x10,project);self.hook(0x17a08,self.pmic)
    pmic=Trees.pmic
