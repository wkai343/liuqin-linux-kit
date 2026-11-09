#!/usr/bin/python3
# SPDX-License-Identifier: MIT
import importlib.util,os,sys
from pathlib import Path
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE))
import liuqin_native_install as gate
def main():
    if len(sys.argv)==2 and sys.argv[1]=='failed':gate.interrupted();return
    if (len(sys.argv) not in (3,4) or sys.argv[3:] not in ([],['PREPARE-BTRFS']) or
            os.uname().machine!='aarch64'):raise RuntimeError('无效的原生 rootfs 安装入口。')
    s=gate.authorize_root(sys.argv[1],sys.argv[2])
    spec=importlib.util.spec_from_file_location('android_device',HERE/'android-device.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    h=m.Hardware(s['boot_id'],s['serial']);parts=h.inventory();h.sealed(list(parts.values()));gate.authorize_target(parts[sys.argv[1]]);gate.mark_write()
    if sys.argv[3:]==['PREPARE-BTRFS']:
        part=parts[sys.argv[1]]
        with h.writable(part,{part['name']}) as fd:m.clear_btrfs_superblocks(fd,part['bytes'])
        h.sealed(list(parts.values()))
if __name__=='__main__':main()
