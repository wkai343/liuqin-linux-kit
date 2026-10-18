#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check the guarded ROM PHY profile against actual calculator/MMIO C."""
from pathlib import Path
import argparse,hashlib,json,re,subprocess,tempfile,xml.etree.ElementTree as ET

def function(s,n):
 m=re.search(r'^(?:static )?(?:inline )?(?:bool|int|s32)\s+'+n+r'\([^;{]*\)\s*\{',s,re.M)
 assert m,n
 i=m.end();depth=1
 while depth:depth+=(s[i]=='{')-(s[i]=='}');i+=1
 return s[m.start():i]

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--source',type=Path,required=True);ap.add_argument('--baseline',type=Path,required=True);ap.add_argument('--rom-proof',type=Path,required=True);ap.add_argument('--cc',default='cc');ap.add_argument('--report',type=Path,required=True);a=ap.parse_args();a.report.unlink(missing_ok=True)
 path='drivers/gpu/drm/msm/dsi/phy/dsi_phy_7nm.c';s=(a.source/path).read_text();old=(a.baseline/path).read_text();calc=(a.baseline/'drivers/gpu/drm/msm/dsi/phy/dsi_phy.c').read_text();h=(a.baseline/'drivers/gpu/drm/msm/dsi/phy/dsi_phy.h').read_text();dh=(a.baseline/'drivers/gpu/drm/msm/dsi/dsi.h').read_text()
 restored=s[:s.index('static bool liuqin_oem_dphy_timing')]+s[s.index('static int dsi_7nm_phy_enable('):]
 restored=restored.replace('#include <linux/ctype.h>\n#include <linux/module.h>\n#include <linux/of.h>\n#include <linux/of_graph.h>\n','').replace('\tstruct msm_dsi_dphy_timing oem_timing;\n','').replace('\tif (dsi_7nm_liuqin_oem_timing(phy, clk_req, &oem_timing))\n\t\ttiming = &oem_timing;\n\n','')
 assert restored==old,'Changes outside the isolated profile'
 assert 'of_machine_is_compatible' not in s
 proof=json.loads(a.rom_proof.read_text());want=proof['oem_values'];native=proof['native_values'];assert len(want)==len(native)==14 and len(proof['differences'])==5
 shared=re.search(r'struct msm_dsi_phy_shared_timings \{.*?\n\};',dh,re.S).group();req=re.search(r'struct msm_dsi_phy_clk_request \{.*?\n\};',dh,re.S).group();timing=re.search(r'struct msm_dsi_dphy_timing \{.*?\n\};',h,re.S).group()
 xml=ET.parse(a.baseline/'drivers/gpu/drm/msm/registers/display/dsi_phy_7nm.xml');offsets={int(x.attrib['name'].rsplit('_',1)[1]):int(x.attrib['offset'],16) for x in xml.iter() if re.fullmatch('TIMING_CTRL_[0-9]+',x.attrib.get('name',''))};assert len(offsets)==14
 assert [offsets[i] for i in range(14)]==proof['rom_register_offsets']
 macros='\n'.join(f'#define REG_DSI_7nm_PHY_CMN_TIMING_CTRL_{i} {offsets[i]}u' for i in range(14))
 rounding=re.search(r'#define S_DIV_ROUND_UP.*?\n\n',calc,re.S).group()
 emit=s[s.index('\t/* DSI PHY timings */'):s.index('\t/* DSI lane settings */',s.index('\t/* DSI PHY timings */'))]
 prefix=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>
#include <limits.h>
typedef uint32_t u32;typedef uint8_t u8;typedef int32_t s32;
#define BIT(n) (1U<<(n))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define DSI_PHY_7NM_QUIRK_V4_3 BIT(3)
#define NSEC_PER_MSEC 1000000L
#define DIV_ROUND_UP(n,d) (((n)+(d)-1)/(d))
#define max_t(t,a,b) ((t)(a)>(t)(b)?(t)(a):(t)(b))
#define mult_frac(x,n,d) ((uint64_t)(x)*(n)/(d))
#define DBG(...) ((void)0)
struct device_node {int refs;bool present,available,known,dual;};
struct device {struct device_node *of_node;};struct platform_device {struct device dev;};
static struct device_node chosen,panels[2],hosts[2],phys[2];
static bool node_present=true,read_error,liuqin_oem_dphy_timing=true;
static bool graph_present[2],phandle_present[2],wrong_phy[2];
static const char *bootargs;
static int refs,logs,cases;
static struct device_node *node_get(struct device_node *n){if(n){n->refs++;refs++;}return n;}
static void of_node_put(struct device_node *n){if(n){assert(n->refs>0 && refs>0);n->refs--;refs--;}}
static struct device_node *of_find_node_by_path(const char *n){assert(!strcmp(n,"/chosen"));return node_present?node_get(&chosen):NULL;}
static int of_property_read_string(struct device_node *n,const char *key,const char **v){assert(n==&chosen && !strcmp(key,"bootargs"));if(read_error)return -EINVAL;*v=bootargs;return 0;}
static struct device_node *of_find_compatible_node(struct device_node *from,const char *type,const char *compat){
 assert(type==NULL && !strcmp(compat,"xiaomi,liuqin-nt36532"));int first=from?(int)(from-panels)+1:0;of_node_put(from);for(int i=first;i<2;i++)if(panels[i].present && panels[i].known)return node_get(&panels[i]);return NULL;
}
#define for_each_compatible_node(n,t,c) for(n=of_find_compatible_node(NULL,t,c);n;n=of_find_compatible_node(n,t,c))
static bool of_device_is_available(struct device_node *n){assert(n>=panels && n<panels+2);return n->available;}
static struct device_node *of_graph_get_remote_node(struct device_node *n,u32 port,u32 endpoint){assert(n>=panels && n<panels+2 && port<2 && endpoint==(u32)-1);return graph_present[port] && hosts[port].available?node_get(&hosts[port]):NULL;}
static bool of_device_is_compatible(struct device_node *n,const char *compat){assert((n==&hosts[0] || n==&hosts[1]) && !strcmp(compat,"qcom,sm8450-dsi-ctrl"));return n->known;}
static bool of_property_read_bool(struct device_node *n,const char *key){assert((n==&hosts[0] || n==&hosts[1]) && !strcmp(key,"qcom,dual-dsi-mode"));return n->dual;}
static struct device_node *of_parse_phandle(struct device_node *n,const char *key,int index){assert((n==&hosts[0] || n==&hosts[1]) && !strcmp(key,"phys") && index==0);int i=(int)(n-hosts);return phandle_present[i]?node_get(&phys[wrong_phy[i]?1-i:i]):NULL;}
static void dev_info(void *dev,const char *fmt,...) __attribute__((format(printf,2,3)));
static void dev_info(void *dev,const char *fmt,...){(void)dev;(void)fmt;logs++;}
'''
 structs=r'''
struct msm_dsi_phy_cfg {u32 quirks;};
struct msm_dsi_phy {int id;bool cphy_mode;struct platform_device *pdev;const struct msm_dsi_phy_cfg *cfg;struct msm_dsi_dphy_timing timing;unsigned char *base;};
static uint32_t mmio[128];static int writes;
static void writel(u32 v,void *p){size_t offset=(unsigned char*)p-(unsigned char*)mmio;assert(offset>=0xb4 && offset<=0xe8 && !(offset%4));mmio[offset/4]=v;writes++;}
'''
 body=function(calc,'linear_inter')+'\n'+function(calc,'msm_dsi_dphy_timing_calc_v4')+'\n'+function(s,'dsi_7nm_liuqin_phy_connected')+'\n'+function(s,'dsi_7nm_liuqin_panel_known')+'\n'+function(s,'dsi_7nm_liuqin_oem_timing')
 emitter='\nstatic void emit(struct msm_dsi_phy *phy,struct msm_dsi_dphy_timing *timing) {\nunsigned char *base=phy->base;\n'+emit+'}\n'
 suffix=r'''
static const char *tm="msm_drm.dsi_display0=qcom,mdss_dsi_m81_36_02_0a_dualdsi_dsc_lcd_video";
static const char *csot="msm_drm.dsi_display0=qcom,mdss_dsi_m81_42_02_0b_dualdsi_dsc_lcd_video";
static struct platform_device pdev;static struct msm_dsi_phy_cfg cfg;static struct msm_dsi_phy phy;static struct msm_dsi_phy_clk_request req;
static void setup(void){assert(refs==0);node_present=liuqin_oem_dphy_timing=true;read_error=false;bootargs=tm;logs=writes=0;memset(&chosen,0,sizeof(chosen));for(int i=0;i<2;i++){panels[i]=(struct device_node){.present=i==0,.available=true,.known=true};hosts[i]=(struct device_node){.available=true,.known=true,.dual=true};phys[i]=(struct device_node){0};graph_present[i]=phandle_present[i]=true;wrong_phy[i]=false;}pdev.dev.of_node=&phys[0];cfg.quirks=BIT(3);phy=(struct msm_dsi_phy){.id=0,.cfg=&cfg,.pdev=&pdev,.base=(unsigned char*)mmio};req=(struct msm_dsi_phy_clk_request){1007032320,19200000};assert(!msm_dsi_dphy_timing_calc_v4(&phy.timing,&req));phy.timing.ta_go=0x123;phy.timing.ta_get=0x456;phy.timing.hs_rqst_ckln=0x789;cases++;}
static void bank(struct msm_dsi_dphy_timing *t,uint32_t *out){memset(mmio,0xa5,sizeof(mmio));writes=0;emit(&phy,t);memcpy(out,mmio,sizeof(mmio));}
static void rejected(void){struct msm_dsi_dphy_timing n=phy.timing,out;memset(&out,0x5a,sizeof(out));struct msm_dsi_dphy_timing untouched=out;assert(!dsi_7nm_liuqin_oem_timing(&phy,&req,&out));assert(!memcmp(&out,&untouched,sizeof(out)) && !memcmp(&phy.timing,&n,sizeof(n)) && !refs && !logs);}
int main(void){
 const unsigned int native[]={NATIVE_VALUES};const unsigned int target[]={OEM_VALUES};
 const unsigned long rates[]={1007031320UL,1007031592UL,1007031744UL,1007032320UL,1007033320UL};
 for(int panel=0;panel<2;panel++)for(int link=0;link<2;link++)for(size_t rate=0;rate<ARRAY_SIZE(rates);rate++){
  setup();bootargs=panel?csot:tm;phy.id=link;pdev.dev.of_node=&phys[link];req.bitclk_rate=rates[rate];assert(!msm_dsi_dphy_timing_calc_v4(&phy.timing,&req));struct msm_dsi_dphy_timing save=phy.timing,out;uint32_t before[128],after[128];bank(&phy.timing,before);assert(writes==14);for(int i=0;i<14;i++)assert(before[0xb4/4+i]==native[i]);
  assert(dsi_7nm_liuqin_oem_timing(&phy,&req,&out));assert(!refs && logs==1 && !memcmp(&save,&phy.timing,sizeof(save)));assert(out.ta_go==save.ta_go && out.ta_get==save.ta_get && out.hs_rqst_ckln==save.hs_rqst_ckln);bank(&out,after);assert(writes==14);int changed=0;for(int i=0;i<128;i++){if(before[i]!=after[i])changed++;if(i<0xb4/4 || i>0xe8/4)assert(after[i]==0xa5a5a5a5U);}assert(changed==5);for(int i=0;i<14;i++)assert(after[0xb4/4+i]==target[i]);assert(phy.timing.shared_timings.clk_pre==31 && phy.timing.shared_timings.clk_post==25);
 }
 for(int bad=0;bad<12;bad++){setup();if(bad==0)liuqin_oem_dphy_timing=false;if(bad==1)panels[0].known=false;if(bad==2)phy.cphy_mode=true;if(bad==3)phy.id=-1;if(bad==4)phy.id=2;if(bad==5)cfg.quirks=BIT(2);if(bad==6)cfg.quirks=BIT(3)|BIT(1);if(bad==7)node_present=false;if(bad==8)read_error=true;if(bad==9)req.bitclk_rate=0;if(bad==10)req.bitclk_rate=1007031319UL;if(bad==11)req.bitclk_rate=1007033321UL;rejected();}
 // Root compatible is intentionally unused: the exact runtime ABL value
 // qcom,cape-mtp must not defeat a verified panel -> host -> PHY topology.
 for(int link=0;link<2;link++)for(int bad=0;bad<9;bad++){
  setup();phy.id=link;pdev.dev.of_node=&phys[link];
  switch(bad){case 0:pdev.dev.of_node=NULL;break;case 1:panels[0].present=false;break;case 2:panels[0].available=false;break;case 3:graph_present[link]=false;break;case 4:hosts[link].available=false;break;case 5:hosts[link].known=false;break;case 6:hosts[link].dual=false;break;case 7:phandle_present[link]=false;break;case 8:wrong_phy[link]=true;break;}
  rejected();
 }
 // A disabled matching node must not hide another available connected panel.
 setup();panels[0].available=false;panels[1].present=true;struct msm_dsi_dphy_timing second;assert(dsi_7nm_liuqin_oem_timing(&phy,&req,&second) && !refs);
 // Iteration releases both skipped nodes on complete lookup failure.
 setup();panels[0].available=false;panels[1].present=true;panels[1].available=false;rejected();
 setup();req.bitclk_rate=ULONG_MAX;rejected();
 for(int bad=0;bad<12;bad++){setup();switch(bad){case 0:phy.timing.clk_zero++;break;case 1:phy.timing.clk_prepare++;break;case 2:phy.timing.clk_trail++;break;case 3:phy.timing.hs_exit++;break;case 4:phy.timing.hs_zero++;break;case 5:phy.timing.hs_prepare++;break;case 6:phy.timing.hs_trail++;break;case 7:phy.timing.hs_rqst++;break;case 8:phy.timing.shared_timings.clk_pre++;break;case 9:phy.timing.shared_timings.clk_post++;break;case 10:phy.timing.shared_timings.clk_pre_inc_by_2=true;break;case 11:phy.timing.shared_timings.byte_intf_clk_div_2=false;break;}rejected();}
 const char *invalid[]={NULL,""," ","msm_drm.dsi_display0=","msm_drm.dsi_display0=unknown","msm_drm.dsi_display1=qcom,mdss_dsi_m81_36_02_0a_dualdsi_dsc_lcd_video","xmsm_drm.dsi_display0=qcom,mdss_dsi_m81_36_02_0a_dualdsi_dsc_lcd_video","msm_drm.dsi_display0=qcom,mdss_dsi_m81_36_02_0a_dualdsi_dsc_lcd_video_extra"};
 for(size_t i=0;i<ARRAY_SIZE(invalid);i++){setup();bootargs=invalid[i];assert(!dsi_7nm_liuqin_panel_known(bootargs));rejected();}
 char args[512];setup();snprintf(args,sizeof(args),"quiet\t%s:config0 root=x\n",tm);assert(dsi_7nm_liuqin_panel_known(args));bootargs=args;struct msm_dsi_dphy_timing out;assert(dsi_7nm_liuqin_oem_timing(&phy,&req,&out) && !refs);
 setup();snprintf(args,sizeof(args),"%s %s",tm,tm);assert(dsi_7nm_liuqin_panel_known(args));
 setup();snprintf(args,sizeof(args),"%s %s",tm,csot);bootargs=args;rejected();
 setup();snprintf(args,sizeof(args),"%s msm_drm.dsi_display0=unknown",tm);bootargs=args;rejected();
 setup();snprintf(args,sizeof(args),"msm_drm.dsi_display0=unknown %s",tm);bootargs=args;rejected();
 printf("%d\n",cases);return 0;
}
'''.replace('NATIVE_VALUES',','.join(map(str,native))).replace('OEM_VALUES',','.join(map(str,want)))
 with tempfile.TemporaryDirectory(prefix='liuqin-phy-timing-') as tmp:
  src=Path(tmp)/'test.c';exe=Path(tmp)/'test';src.write_text(prefix+shared+req+timing+structs+rounding+macros+'\n'+body+emitter+suffix)
  subprocess.run([a.cc,'-std=c11','-Wall','-Wextra','-Werror','-Wno-misleading-indentation','-fsanitize=undefined','-fno-sanitize-recover=all',str(src),'-o',str(exe)],check=True)
  count=int(subprocess.check_output([str(exe)],text=True,timeout=20))
 report={'cases':count,'all_passed':True,'actual_calculator_and_candidate_c_executed':True,'actual_phy_mmio_block_executed':True,'exactly_five_phy_register_values_changed':True,'shared_host_timings_preserved':True,'both_links_panels_and_rate_boundaries_checked':True,'unknown_panel_topologies_versions_modes_and_changed_calculators_unchanged':True,'abl_root_rewrite_does_not_control_scope':True,'both_link_topologies_checked':True,'all_device_node_references_balanced':True,'chosen_node_reference_balanced':True,'pll_analog_voltage_reset_panel_and_backlight_code_unchanged':True,'candidate_sha256':hashlib.sha256(s.encode()).hexdigest(),'rom_proof_sha256':hashlib.sha256(a.rom_proof.read_bytes()).hexdigest(),'hardware_validation_required':True};a.report.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
if __name__=='__main__':main()
