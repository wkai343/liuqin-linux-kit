#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute candidate panel/host C against independently encoded ROM groups."""
from pathlib import Path
import argparse,json,re,subprocess,tempfile

def function(source,name):
 m=re.search(r'^(?:static )?[^\n]+\b'+name+r'\([^;]*?\)\s*\{',source,re.M)
 assert m,name
 pos=m.end();depth=1
 while depth:
  depth+=(source[pos]=='{')-(source[pos]=='}');pos+=1
 return source[m.start():pos]

def expected(commands,batch):
 output=[];buf=bytearray()
 for i,c in enumerate(commands):
  payload=bytes.fromhex(c['payload']);n=len(payload);packet=bytearray([n&255,n>>8,c['type']|(c['channel']<<6),64])+payload
  packet+=b'\xff'*((-len(packet))%4)
  final=not batch or not c['flags']&64 or len(buf)+len(packet)>240
  if final:packet[3]|=128
  buf+=packet
  if final:
   output.append('D '+buf.hex());buf.clear()
  if c['wait_ms']:output.append('W '+str(c['wait_ms']*1000))
 assert not buf
 output.append('R 0');return output

def manager_tests(source,cc,tmp):
    text=(source/'drivers/gpu/drm/msm/dsi/dsi_manager.c').read_text()
    body='\n'.join(function(text,n) for n in ['dsi_mgr_cmd_xfer','msm_dsi_manager_cmd_xfer','msm_dsi_manager_cmd_xfer_batch'])
    prefix=r'''
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <errno.h>
#define DSI_0 0
#define IS_SYNC_NEEDED() sync_needed
#define pr_err(...) ((void)0)
struct mipi_dsi_msg { const void *tx_buf;size_t tx_len;void *rx_buf;size_t rx_len; };
struct mipi_dsi_host { int id; };
struct msm_dsi { struct mipi_dsi_host *host; };
static struct mipi_dsi_host hosts[2]={{0},{1}};
static struct msm_dsi dsis[2]={{hosts},{hosts+1}};
static int sync_needed=1,prep_fail=-1,tx_fail,dsi_cmd_lock;
static struct msm_dsi *dsi_mgr_get_dsi(int i){return &dsis[i];}
static void mutex_lock(int *p){if(*p)abort();*p=1;puts("LOCK");}
static void mutex_unlock(int *p){if(!*p)abort();*p=0;puts("UNLOCK");}
static int msm_dsi_host_xfer_prepare(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m){(void)m;printf("P%d\n",h->id);return h->id==prep_fail?-5:0;}
static void msm_dsi_host_xfer_restore(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m){(void)m;printf("R%d\n",h->id);}
static int msm_dsi_host_cmd_tx(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m){printf("S%d\n",h->id);return m->tx_len;}
static int msm_dsi_host_cmd_rx(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m){printf("Q%d\n",h->id);return m->rx_len;}
static int msm_dsi_host_cmd_tx_batch(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m,size_t n){printf("B%d\n",h->id);int r=0;for(size_t i=0;i<n;i++)r+=m[i].tx_len;return tx_fail?-110:r;}
'''
    suffix=r'''
int main(int argc,char **argv){
 if(argc!=2)return 2;int c=atoi(argv[1]),data=0,ret;
 struct mipi_dsi_msg m[2]={{.tx_buf=&data,.tx_len=2},{.tx_buf=&data,.tx_len=3}};
 if(c==1)prep_fail=0;if(c==2)prep_fail=1;if(c==3)tx_fail=1;if(c==5)sync_needed=0;
 if(c==6){m[0].rx_buf=&data;m[0].rx_len=1;ret=msm_dsi_manager_cmd_xfer(0,m);}
 else if(c==7)ret=msm_dsi_manager_cmd_xfer(1,m);
 else if(c==8)ret=msm_dsi_manager_cmd_xfer_batch(1,NULL,2);
 else if(c==9)ret=msm_dsi_manager_cmd_xfer_batch(1,m,0);
 else ret=msm_dsi_manager_cmd_xfer_batch(c==4?0:1,m,2);
 if(dsi_cmd_lock)abort();printf("RESULT %d\n",ret);return 0;
}
'''
    c=Path(tmp)/'manager.c';exe=Path(tmp)/'manager';c.write_text(prefix+body+suffix)
    subprocess.run([cc,'-std=c11','-Wall','-Wextra','-Wno-misleading-indentation','-Werror','-fsanitize=undefined','-fno-sanitize-recover=all',str(c),'-o',str(exe)],check=True)
    expected=[['LOCK','P0','P1','B1','R1','R0','UNLOCK','RESULT 5'],['LOCK','P0','UNLOCK','RESULT -5'],['LOCK','P0','P1','R0','UNLOCK','RESULT -5'],['LOCK','P0','P1','B1','R1','R0','UNLOCK','RESULT -110'],['LOCK','UNLOCK','RESULT 5'],['LOCK','P1','B1','R1','UNLOCK','RESULT 5'],['LOCK','P0','Q0','R0','UNLOCK','RESULT 1'],['LOCK','P0','P1','S1','R1','R0','UNLOCK','RESULT 2'],['RESULT -22'],['RESULT -22']]
    for i,want in enumerate(expected):
        r=subprocess.run([str(exe),str(i)],capture_output=True,text=True,check=True)
        assert not r.stderr and r.stdout.splitlines()==want,(i,r.stdout,r.stderr)
    return [{'manager_case':i,'prepare_restore_and_locking_passed':True} for i in range(len(expected))]

def main():
 pa=argparse.ArgumentParser(description=__doc__);pa.add_argument('--cc',default='cc');pa.add_argument('--source',type=Path,required=True);pa.add_argument('--baseline',type=Path,required=True);pa.add_argument('--oem-packets',type=Path,required=True);pa.add_argument('--report',type=Path,required=True);a=pa.parse_args()
 panel=(a.source/'drivers/gpu/drm/panel/panel-novatek-nt36532.c').read_text();old=(a.baseline/'drivers/gpu/drm/panel/panel-novatek-nt36532.c').read_text();host=(a.source/'drivers/gpu/drm/msm/dsi/dsi_host.c').read_text();oldhost=(a.baseline/'drivers/gpu/drm/msm/dsi/dsi_host.c').read_text()
 for name in ['nt36532_prepare','nt36532_enable','nt36532_disable','nt36532_unprepare','nt36532_probe','liuqin_reset','pipa_reset','yudi_reset','nt36532_enable_supplies','nt36532_disable_supplies','pipa_init_sequence','yudi_init_sequence']:
  assert function(panel,name)==function(old,name),name
 for name in ['dsi_cmd_dma_add','dsi_cmd_dma_tx','dsi_cmds2buf_tx','dsi_host_transfer','msm_dsi_host_xfer_prepare','msm_dsi_host_xfer_restore']:
  assert function(host,name)==function(oldhost,name),name
 block=panel[panel.index('struct liuqin_oem_cmd {'):panel.index('static const struct drm_display_mode pipa_mode_120')]
 block=block.replace(function(panel,'liuqin_select_oem_panel'),'')
 prefix=r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>
#include <sys/types.h>
typedef uint8_t u8;
#define ALIGN(v,a) (((v)+(a)-1)&~((a)-1))
#define BIT(n) (1U<<(n))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define MIPI_DSI_DCS_LONG_WRITE 0x39
#define MIPI_DSI_MSG_USE_LPM 2
#define MIPI_DSI_MODE_VIDEO 1
#define GFP_KERNEL 0
#define IS_ERR(x) ((intptr_t)(x)<0 && (intptr_t)(x)>-4096)
#define PTR_ERR(x) ((int)(intptr_t)(x))
#define module_param(...)
#define MODULE_PARM_DESC(...)
#define dev_info(dev,fmt,...) do { (void)(dev); if(0)printf(fmt,__VA_ARGS__); } while(0)
static int allocation_fail,fail_dma=-1,fail_code=-110,dma_calls,get_fail,short_transfer;
static void *kcalloc(size_t n,size_t s,int flags){(void)flags;return allocation_fail?NULL:calloc(n,s);}
#define kfree free
struct mipi_dsi_msg { u8 channel,type; unsigned short flags; size_t tx_len;const void *tx_buf;size_t rx_len;void *rx_buf; };
struct mipi_dsi_host;
struct mipi_dsi_host_ops { ssize_t (*transfer)(struct mipi_dsi_host *,const struct mipi_dsi_msg *);ssize_t (*transfer_batch)(struct mipi_dsi_host *,const struct mipi_dsi_msg *,size_t); };
struct mipi_dsi_host { const struct mipi_dsi_host_ops *ops; };
struct fake_mode { int hdisplay; };
struct msm_dsi_host;
struct cfg_ops { void *(*tx_buf_get)(struct msm_dsi_host *); void (*tx_buf_put)(struct msm_dsi_host *); };
struct msm_dsi_cfg_handler { const struct cfg_ops *ops; };
struct msm_dsi_host { struct mipi_dsi_host base;const struct msm_dsi_cfg_handler *cfg_hnd;int tx_size;unsigned long mode_flags;struct fake_mode *mode;int format;unsigned char buf[4096]; };
static struct msm_dsi_host *to_msm_dsi_host(struct mipi_dsi_host *h){return (struct msm_dsi_host*)h;}
static int mipi_dsi_pixel_format_to_bpp(int f){(void)f;return 24;}
static void *getbuf(struct msm_dsi_host *h){return get_fail?(void*)(intptr_t)-ENOMEM:h->buf;}
static void putbuf(struct msm_dsi_host *h){(void)h;}
static int dsi_cmd_dma_tx(struct msm_dsi_host *h,int n){printf("D ");for(int i=0;i<n;i++)printf("%02x",h->buf[i]);puts("");if(dma_calls++==fail_dma)return fail_code;return short_transfer?n-1:n;}
struct mipi_dsi_device {struct mipi_dsi_host *host;u8 channel;};
struct drm_panel {void *dev;};
struct nt36532 {struct drm_panel panel;struct mipi_dsi_device *dsi[2];const struct liuqin_oem_panel *oem_panel;};
static struct mipi_dsi_device *nt36532_cmd_dsi(struct nt36532 *c){return c->dsi[1]?c->dsi[1]:c->dsi[0];}
static int pipa_init_sequence(struct nt36532 *c){(void)c;return 0;}
static void usleep_range(unsigned long lo,unsigned long hi){(void)hi;printf("W %lu\n",lo);}
'''
 header=(a.source/'drivers/gpu/drm/msm/dsi/dsi_cmd_batch.h').read_text()
 send=function(host,'msm_dsi_host_cmd_tx_batch')
 wrappers=r'''
static ssize_t txbatch(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m,size_t n){return msm_dsi_host_cmd_tx_batch(h,m,n);}
static ssize_t txone(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m){return msm_dsi_host_cmd_tx_batch(h,m,1);}
'''
 suffix=r'''
int main(int argc,char **argv){
 if(argc<2)return 2;
 if(!strcmp(argv[1],"invalid")){
  u8 data[600]={0};struct mipi_dsi_msg m[2]={{.type=0x39,.flags=2,.tx_buf=data,.tx_len=2},{.type=0x39,.flags=2,.tx_buf=data,.tx_len=2}};
  int x=atoi(argv[2]);size_t n=2,cap=4096;
  if(x==0)n=0;if(x==1)n=257;if(x==2)m[1].rx_len=1;if(x==3)m[1].rx_buf=data;if(x==4)m[1].type=0x06;
  if(x==5)m[1].flags=3;if(x==6)m[1].channel=1;if(x==7)m[1].tx_buf=NULL;if(x==8)m[1].tx_len=0;
  if(x==9)m[1].tx_len=65536;if(x==10)cap=4;if(x==11)m[0].channel=m[1].channel=4;if(x==12)m[1].flags=0;
  if(x==13)m[1].tx_len=509;
  int r=dsi_cmd_batch_validate(m,n,cap);printf("R %d\n",r);return 0;
 }
 const struct cfg_ops cfgops={getbuf,putbuf};const struct msm_dsi_cfg_handler cfg={&cfgops};struct fake_mode mode={1800};
 struct mipi_dsi_host_ops ops={txone,txbatch};struct msm_dsi_host h={.base={&ops},.cfg_hnd=&cfg,.tx_size=4096,.mode_flags=1,.mode=&mode};
 struct mipi_dsi_device d={.host=&h.base};struct nt36532 ctx={.dsi={&d,&d},.oem_panel=&liuqin_oem_panels[atoi(argv[2])]};
 if(argc>3 && !strcmp(argv[3],"fallback"))ops.transfer_batch=NULL;
 if(argc>3 && !strcmp(argv[3],"disabled"))oem_batch_commands=false;
 if(argc>3 && !strcmp(argv[3],"alloc"))allocation_fail=1;
 if(argc>3 && !strcmp(argv[3],"get"))get_fail=1;
 if(argc>3 && !strcmp(argv[3],"short"))short_transfer=1;
 if(argc>4){fail_dma=atoi(argv[4]);fail_code=-110;}
 const struct liuqin_oem_cmd *cmds=!strcmp(argv[1],"off")?liuqin_oem_off:ctx.oem_panel->on;
 size_t count=!strcmp(argv[1],"off")?ARRAY_SIZE(liuqin_oem_off):ctx.oem_panel->on_count;
 if(!strcmp(argv[1],"badtable")){
  struct liuqin_oem_cmd c[2]={{0x39,0x40,0,2,{0xff,0x10}},{0x39,0,0,2,{0xfb,1}}};
  int x=atoi(argv[3]);if(x==0)c[1].flags=0x40;if(x==1)c[1].flags=1;if(x==2)c[0].wait_ms=1;if(x==3)c[1].len=18;if(x==4)c[1].type=0x15;
  printf("R %d\n",liuqin_send_oem_cmds(&ctx,c,2));return 0;
 }
 printf("R %d\n",liuqin_send_oem_cmds(&ctx,cmds,count));return 0;
}
'''
 commands=json.loads(a.oem_packets.read_text());results=[]
 with tempfile.TemporaryDirectory(prefix='liuqin-oem-batches-') as tmp:
  c=Path(tmp)/'probe.c';exe=Path(tmp)/'probe';c.write_text(prefix+header+send+wrappers+block+suffix)
  subprocess.run([a.cc,'-std=c11','-Wall','-Wextra','-Wno-unused-function','-Wno-misleading-indentation','-Werror','-fsanitize=undefined','-fno-sanitize-recover=all','-g',str(c),'-o',str(exe)],check=True)
  def run(*args):
   r=subprocess.run([str(exe),*map(str,args)],capture_output=True,text=True,check=True);assert not r.stderr,r.stderr;return r.stdout.splitlines()
  for variant,key in enumerate(['m81_36_02_0a','m81_42_02_0b']):
   for kind in ['on','off']:
    cs=commands[key][kind]['commands']
    for choice,batch in [('batch',True),('fallback',False),('disabled',False)]:
     got=run(kind,variant,choice);want=expected(cs,batch);assert got==want,(key,kind,choice,got,want)
     results.append({'variant':key,'set':kind,'mode':choice,'dma_transfers':sum(x.startswith('D ') for x in got),'passed':True})
    normal=expected(cs,True);dmas=[i for i,x in enumerate(normal) if x.startswith('D ')]
    for index in range(len(dmas)):
     got=run(kind,variant,'batch',index);assert got==normal[:dmas[index]+1]+['R -110'],(key,kind,index,got)
     results.append({'variant':key,'set':kind,'failure_at_dma':index,'stopped_without_replay':True})
   for failure,code in [('alloc',-12),('get',-12),('short',-5)]:
    got=run('on',variant,failure);assert got[-1]=='R '+str(code)
    assert len(got)==(2 if failure=='short' else 1),got
    results.append({'variant':key,'failure':failure,'passed':True})
  for i in range(14):
   got=run('invalid',i);assert len(got)==1 and got[0] in ['R -22','R -90'],got;results.append({'invalid_message':i,'no_dma':True})
  for i in range(5):
   got=run('badtable',0,i);assert got==['R -22'];results.append({'invalid_table':i,'no_dma':True})
  results.extend(manager_tests(a.source,a.cc,tmp))
 report={'cases':len(results),'all_passed':True,'sanitizer':'undefined','rom_group_headers_payload_padding_and_waits_match':True,'prevalidation_no_partial_writes':True,'errors_no_replay':True,'power_reset_touch_and_guard_waits_unchanged':True,'legacy_host_single_transfer_unchanged':True,'cases_detail':results}
 a.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps({k:v for k,v in report.items() if k!='cases_detail'}))
if __name__=='__main__':main()
