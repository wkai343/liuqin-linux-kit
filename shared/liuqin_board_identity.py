# SPDX-License-Identifier: MIT
"""Read liuqin board identity, including Xiaomi ABL's compatible rewrite.

The generic cape compatible string is never sufficient. This fallback is for
the embedded mainline DT, whose exact model/board IDs are checked together
with the hardware project ID appended by ABL.
"""
from pathlib import Path
import struct


def identify(root=Path('/')):
    root=Path(root)
    dt=root/'proc/device-tree'
    if not dt.is_dir():dt=root/'sys/firmware/devicetree/base'
    def data(name):
        try:return (dt/name).read_bytes()
        except OSError:return b''
    compatible=data('compatible').rstrip(b'\0').split(b'\0')
    model=data('model').rstrip(b'\0')
    try:words=(root/'proc/cmdline').read_text().split()
    except OSError:words=[]
    projects={w.split('=',1)[1] for w in words if w.startswith('hwid.project=')}
    result=dict(device='unknown',source='unrecognized',model=model.decode(errors='replace'),
        compatible=[v.decode(errors='replace') for v in compatible if v],project=sorted(projects))
    # An explicit contradiction is not overridden by a compiled DT string.
    if projects and projects!={'16'}:return result
    if b'xiaomi,liuqin' in compatible:
        return dict(result,device='liuqin',source='dt-compatible')
    if (set(compatible)=={b'qcom,cape-mtp',b'qcom,cape',b'qcom,mtp'} and
        model==b'Liuqin based on Qualcomm Technologies, Inc SM8475' and
        data('xiaomi,miboard-id')==struct.pack('>II',0x10,0) and
        data('qcom,board-id')==struct.pack('>II',0x10008,0) and projects=={'16'}):
        return dict(result,device='liuqin',source='abl-project+xiaomi-board+liuqin-model')
    return result
