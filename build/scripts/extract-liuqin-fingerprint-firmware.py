#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Extract only ten pinned FPC TA files from the user's verified stock image."""
import argparse,hashlib,importlib.util,json,os,shutil,subprocess,tempfile
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[2]
def verify_image(image):
 image=Path(image)
 record=json.loads((PROJECT/'shared/liuqin-rom-images.json').read_text())['images']['NON-HLOS.bin']
 if image.is_symlink() or not image.is_file():
  raise ValueError('原厂 ROM 缺少普通镜像文件：'+str(image))
 if image.stat().st_size!=record['bytes'] or hashlib.sha256(image.read_bytes()).hexdigest()!=record['sha256']:
  raise ValueError('NON-HLOS.bin 不是经过验证的 OS2.0.6.0.VMYCNXM 镜像。')
 return image
def extract(image,output):
 image=verify_image(image);output=Path(output)
 if output.exists() or output.is_symlink():raise ValueError('保留已有目录，请使用新的输出目录。')
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
def install(image,root):
 """Populate an offline root's verified cache; never execute target programs."""
 if os.geteuid()!=0:raise RuntimeError('写入目标指纹固件缓存需要 root。')
 root=Path(root)
 if root.is_symlink() or not root.is_dir() or root.resolve()==Path('/'):
  raise ValueError('需要已装配的目标根目录，不能使用主机根目录。')
 root=root.resolve()
 directory=root/'usr/local/lib/liuqin-fpc-oem/firmware'
 for path in (root/'usr',root/'usr/local',root/'usr/local/lib',directory.parent,directory):
  if path.is_symlink() or not path.is_dir():
   raise ValueError('目标指纹缓存目录缺失或包含符号链接：'+str(path))
 spec=importlib.util.spec_from_file_location('fpc_firmware_provision',
  PROJECT/'device/fingerprint/oem/src/fingerprint/deployment/provision_firmware.py')
 provision=importlib.util.module_from_spec(spec);spec.loader.exec_module(provision)
 with tempfile.TemporaryDirectory(prefix='liuqin-fpc-image-') as temporary:
  source=Path(temporary)/'firmware'
  extract(image,source)
  provision.provision_from_directory(source,directory)
 print('已将校验后的指纹固件写入目标 rootfs；未创建指纹凭据或模板。')
if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--image',type=Path,required=True)
 target=parser.add_mutually_exclusive_group(required=True)
 target.add_argument('--out',type=Path,help='提取到新目录')
 target.add_argument('--root',type=Path,help='写入已装配 rootfs 的指纹固件缓存')
 args=parser.parse_args()
 if args.root is not None:install(args.image,args.root)
 else:extract(args.image,args.out)
