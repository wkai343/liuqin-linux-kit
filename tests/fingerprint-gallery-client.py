#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the actual gallery C protocol/matching code against a fake TEE."""
from pathlib import Path
import os
import subprocess
import tempfile
PROJECT=Path(__file__).resolve().parents[1]
source=(PROJECT/'device/fingerprint/oem/src/fingerprint/oem/fpc_build_info.c').read_text()
def function(name):
 start=source.index('static int '+name+'(');brace=source.index('{',start);depth=1;end=brace+1
 while depth:
  if source[end]=='{':depth+=1
  elif source[end]=='}':depth-=1
  end+=1
 return source[start:end]+'\n'
header=PROJECT/'device/fingerprint/oem/src/fingerprint/protocol/fpc_gallery.h'
code=r'''
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <unistd.h>
'''+('#include "'+str(header)+'"\n')+r'''
#define PAYLOAD_SIZE 4096u
#define HAT_SIZE 69u
struct shared {void *address;size_t size;};
static struct fpc_inventory current;
static uint32_t matched_id=20;
static uint64_t challenge;
static int captures,deletes,auth_requests,invalid_hat,identify_status,corrupt_update;
static int allocate_shared(int fd,size_t size,struct shared *p){(void)fd;p->address=calloc(1,size);p->size=size;return !p->address;}
static int fixture_unmap(void *p,size_t size){(void)size;free(p);return 0;}
#define munmap fixture_unmap
static int invoke_payload(int fd,uint32_t session,struct shared *p){
 (void)fd;(void)session;uint32_t *w=p->address;
 if(w[0]==11&&w[1]==8){w[2]=current.count;for(unsigned i=0;i<current.count&&i<5;i++)w[3+i]=current.ids[i];return 0;}
 if(w[0]==11&&w[1]==11){uint64_t id=42;memcpy((char *)p->address+16,&id,8);return 0;}
 if(w[0]==11&&w[1]==9){
  deletes++;unsigned at=current.count;for(unsigned i=0;i<current.count;i++)if(current.ids[i]==w[2])at=i;
  assert(at<current.count);for(unsigned i=at;i+1<current.count;i++)current.ids[i]=current.ids[i+1];current.count--;return 0;
 }
 if(w[0]==3&&w[1]==1){memcpy(&challenge,(char *)p->address+8,8);assert(challenge);return 0;}
 if(w[0]==3&&w[1]==4){auth_requests++;assert(w[3]==69);uint64_t c=challenge+(invalid_hat?1:0);memset((char *)p->address+16,0,69);memcpy((char *)p->address+17,&c,8);return 0;}
 assert(w[0]==11&&w[1]==4);w[2]=matched_id;return identify_status;
}
enum query_kind {BIO_INIT,BIO_SET_GID,BIO_SET_ACTIVE_GROUP,BIO_LOAD_INSTANCE_DATA,BIO_UPDATE_TEMPLATE};
static int query(int fd,uint32_t session,enum query_kind kind){(void)fd;(void)session;(void)kind;return 0;}
static int init_hw_auth(int fd,uint32_t session){(void)fd;(void)session;return 0;}
static int load_database(int fd,uint32_t session,const char *path){(void)fd;(void)session;(void)path;return 0;}
static int capture_with_wait(int fd,uint32_t session,int power){(void)fd;(void)session;(void)power;captures++;return 0;}
static int query_output(int fd,uint32_t session,enum query_kind kind,uint32_t *changed){
 (void)fd;(void)session;assert(kind==BIO_UPDATE_TEMPLATE);*changed=1;if(corrupt_update)current.ids[0]=999;return 0;
}
static int store_database(int fd,uint32_t session,const char *path){
 (void)fd;(void)session;int out=open(path,O_WRONLY|O_CREAT|O_EXCL,0600);if(out<0)return 1;
 assert(write(out,"opaque fake DB",14)==14);close(out);return 0;
}
'''
for name in ('read_template_inventory','gallery_prepare','gallery_metadata','set_auth_challenge','identify_finger','get_auth_result','match_existing'):code+=function(name)
code+=r'''
static void active_file(const char *path,uint32_t count,const uint32_t *ids){
 uint8_t data[24]={};fpc_put32(data,count);unsigned len=4;
 if(count!=UINT32_MAX){for(uint32_t i=0;i<count;i++)fpc_put32(data+4+4*i,ids[i]);len+=count*4;}
 int fd=open(path,O_WRONLY|O_CREAT|O_TRUNC,0600);assert(fd>=0);assert(write(fd,data,len)==len);close(fd);
}
static void reset(void){current=(struct fpc_inventory){.count=3,.ids={10,20,30}};matched_id=20;
 captures=deletes=auth_requests=invalid_hat=identify_status=corrupt_update=0;}
int main(void){
 assert(geteuid()==0);char root[]="/tmp/fpc-gallery-client-XXXXXX";assert(mkdtemp(root));
 char active[256],meta[256],updated[256];snprintf(active,sizeof active,"%s/active",root);
 snprintf(meta,sizeof meta,"%s/meta",root);snprintf(updated,sizeof updated,"%s/updated",root);
 reset();struct fpc_inventory before=current,kept={};uint32_t ids[]={10,30};
 active_file(active,2,ids);assert(!gallery_prepare(1,1,active,&before,&kept));
 assert(deletes==1&&current.count==2&&fpc_has_id(&current,10)&&fpc_has_id(&current,30));
 reset();before=current;uint32_t invalid[]={40};active_file(active,1,invalid);
 assert(gallery_prepare(1,1,active,&before,&kept));assert(deletes==0);
 active_file(active,UINT32_MAX,NULL);assert(gallery_prepare(1,1,active,&before,&kept));
 current=(struct fpc_inventory){.count=1,.ids={77}};before=current;
 assert(!gallery_prepare(1,1,active,&before,&kept));assert(kept.count==1&&kept.ids[0]==77);
 current=(struct fpc_inventory){.count=2,.ids={1,1}};assert(read_template_inventory(1,1,&kept));
 current=(struct fpc_inventory){.count=1,.ids={0}};assert(read_template_inventory(1,1,&kept));
 current=(struct fpc_inventory){.count=6};assert(read_template_inventory(1,1,&kept));
 reset();assert(match_existing(1,1,1,"unused",updated,0,meta)==0);assert(captures==1&&auth_requests==1);
 uint8_t wire[64];int fd=open(meta,O_RDONLY);assert(read(fd,wire,64)==64);close(fd);
 assert(!memcmp(wire,"FPCMETA2",8)&&fpc_le32(wire+8)==3&&fpc_le32(wire+32)==3&&fpc_le32(wire+56)==20);
 assert(gallery_metadata(meta,&current,&current,20,0)); /* never replace results */
 unlink(meta);unlink(updated);
 reset();matched_id=999;assert(match_existing(1,1,1,"unused",updated,0,meta)==1);assert(auth_requests==0&&access(meta,F_OK));
 reset();invalid_hat=1;assert(match_existing(1,1,1,"unused",updated,0,meta)==1);assert(auth_requests==1&&access(meta,F_OK));unlink(updated);
 reset();identify_status=12;assert(match_existing(1,1,1,"unused",updated,0,meta)==1);
 fd=open(meta,O_RDONLY);assert(read(fd,wire,64)==64);close(fd);assert(fpc_le32(wire+56)==0&&fpc_le32(wire+60)==1);unlink(meta);unlink(updated);
 reset();corrupt_update=1;assert(match_existing(1,1,1,"unused",updated,0,meta)==1);assert(access(meta,F_OK));unlink(updated);
 reset();assert(match_existing(1,1,1,"unused",updated,1,meta)==1);assert(captures==0&&access(meta,F_OK));
 unlink(active);rmdir(root);puts("Gallery C inventory, exact deletion, one capture, fresh HAT, ID validation and fail-closed results: PASS");return 0;
}
'''
with tempfile.TemporaryDirectory(prefix='fpc-gallery-client-build-') as directory:
 root=Path(directory);path=root/'test.c';path.write_text(code)
 subprocess.run([os.environ.get('CC','cc'),'-std=c11','-Wall','-Wextra','-Werror','-O2',str(path),'-o',str(root/'test')],check=True)
 command=[str(root/'test')]
 if os.geteuid()!=0:command=['unshare','-Ur',*command]
 result=subprocess.run(command,capture_output=True,text=True)
 if result.returncode:print(result.stdout);print(result.stderr);result.check_returncode()
 print(result.stdout.splitlines()[-1])
