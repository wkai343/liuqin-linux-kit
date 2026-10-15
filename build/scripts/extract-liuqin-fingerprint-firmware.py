#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Extract only ten pinned FPC TA files from the user's verified stock image."""
import argparse,hashlib,json,os,shutil,subprocess,tempfile
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[2]
def extract(image,output):
 image=Path(image);output=Path(output)
 record=json.loads((PROJECT/'shared/liuqin-rom-images.json').read_text())['images']['NON-HLOS.bin']
 if image.is_symlink() or image.stat().st_size!=record['bytes'] or hashlib.sha256(image.read_bytes()).hexdigest()!=record['sha256']:
  raise ValueError('NON-HLOS.bin 不是经过验证的 OS2.0.6.0.VMYCNXM 镜像。')
 if output.exists():raise ValueError('保留已有目录，请使用新的输出目录。')
 tool=shutil.which('mcopy')
 if not tool:raise ValueError('需要 mtools 中的 mcopy。')
 expected=json.loads((PROJECT/'device/fingerprint/oem/firmware/SHA256.json').read_text())
 output.parent.mkdir(parents=True,exist_ok=True)
 with tempfile.TemporaryDirectory(prefix='.fpc-rom-',dir=output.parent) as temporary:
  stage=Path(temporary)/'firmware';stage.mkdir(mode=0o700)
  for name,digest in expected.items():
   target=stage/name
   subprocess.run([tool,'-i',str(image),'::/image/'+name,str(target)],check=True)
   if hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise ValueError('原厂指纹固件校验失败：'+name)
   target.chmod(0o600)
  os.rename(stage,output)
 print('已提取并校验 10 个指纹固件文件；未访问平板、未刷写分区。')
if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--image',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
 args=parser.parse_args();extract(args.image,args.out)
