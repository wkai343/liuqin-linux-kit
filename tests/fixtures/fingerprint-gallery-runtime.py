#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Synthetic Identify/enrollment process for real TOD API tests; no hardware."""
import os
from pathlib import Path
import pwd
import signal
import struct
import sys
import time

mode=os.environ['FPC_GALLERY_TEST_CASE']
if sys.argv[1]=='--enrol':
 assert len(sys.argv)==5
 username,finger,output=sys.argv[2:];uid=pwd.getpwnam(username).pw_uid
 data=b'FPCREF2\0'+struct.pack('<II',uid,int(finger))+b'\x42'*32
 fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 os.write(fd,data);os.close(fd)
 print('native_enrol_cleanup=OK',flush=True)
 print('native_enrol=OK finger_reference=2',flush=True)
 sys.exit(0)
assert sys.argv[1]=='--request' and sys.argv[3]=='--result'
request=Path(sys.argv[2]).read_bytes();output=Path(sys.argv[4])
assert request[:8]==b'FPCREQ2\0'
uid,count,length=struct.unpack_from('<III',request,8);assert uid and 1<=count<=5
username=request[20:20+length].decode();assert pwd.getpwnam(username).pw_uid==uid
if mode=='cancel':
 def cancel(signum,frame):
  time.sleep(.2);print('gallery_match=FAILED cleanup_complete=1',flush=True);sys.exit(1)
 signal.signal(signal.SIGTERM,cancel);Path(__file__).with_name('ready').touch()
 while True:time.sleep(.02)
status=2 if mode=='retry' else 0 if mode=='miss' else 1
index=0xffffffff if status!=1 else 1 if mode=='second' else 0
if mode=='bad-index':index=99
result=b'FPCRES2\0'+struct.pack('<II',status,index)
if mode=='bad-magic':result=b'INVALID!'+result[8:]
if mode=='partial':result=result[:7]
fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
os.write(fd,result);os.close(fd)
if mode=='public-result':output.chmod(0o644)
if mode!='missing-marker':print('gallery_match=OK cleanup_complete=1',flush=True)
