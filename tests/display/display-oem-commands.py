#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Replay actual candidate C through a fake DSI host against ROM packet data."""
from pathlib import Path
import argparse,json,re,subprocess,tempfile


def function(source,name):
    match=re.search(r'^static[^;{]+\b'+re.escape(name)+r'\([^;{]*\)\s*\{',source,re.M)
    if not match:raise AssertionError('missing function '+name)
    depth=1;pos=match.end()
    while depth:
        if source[pos]=='{':depth+=1
        elif source[pos]=='}':depth-=1
        pos+=1
    return source[match.start():pos]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--oem-packets',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args();source=args.candidate.read_text();old=args.baseline.read_text()
    oem=json.loads(args.oem_packets.read_text())
    for name in ['pipa_init_sequence','yudi_init_sequence','pipa_reset','yudi_reset','liuqin_reset','nt36532_unprepare','nt36532_enable_supplies','nt36532_disable_supplies']:
        assert function(source,name)==function(old,name),name
    for board in ['pipa','yudi']:
        for kind in ['mode_120','desc']:
            pattern=r'static const struct [^\n]+ '+board+'_'+kind+r' = \{.*?\n\};'
            assert re.search(pattern,source,re.S).group()==re.search(pattern,old,re.S).group()
    probe=function(source,'nt36532_probe')
    assert 'if (ctx->desc == &liuqin_desc)\n\t\tliuqin_select_oem_panel(ctx);' in probe
    start=source.index('struct liuqin_oem_cmd {');end=source.index('static const struct drm_display_mode pipa_mode_120',start)
    block=source[start:end].replace(function(source,'liuqin_select_oem_panel'),'')
    prefix=r'''
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>
#include <sys/types.h>
typedef uint8_t u8;
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define MIPI_DSI_MSG_USE_LPM 2
#define MIPI_DSI_MODE_LPM (1U << 11)
#define KERN_INFO ""
#define DUMP_PREFIX_NONE 0
#define print_hex_dump(...) ((void)0)
#define dev_err(device,...) ((void)(device))
struct device { int unused; };
struct mipi_dsi_msg { unsigned char channel,type; unsigned short flags; size_t tx_len; const void *tx_buf; };
struct mipi_dsi_host;
struct mipi_dsi_host_ops { ssize_t (*transfer)(struct mipi_dsi_host *,const struct mipi_dsi_msg *); };
struct mipi_dsi_host { const struct mipi_dsi_host_ops *ops; };
struct mipi_dsi_device { struct device dev; struct mipi_dsi_host *host; unsigned char channel; unsigned long mode_flags; };
struct drm_panel { struct device *dev; };
struct drm_dsc_picture_parameter_set { unsigned char bytes[128]; };
struct nt36532;
struct panel_desc { int (*init_sequence)(struct nt36532 *); void (*reset_sequence)(struct nt36532 *); };
struct nt36532 { struct drm_panel panel; struct mipi_dsi_device *dsi[2]; const struct panel_desc *desc; const struct liuqin_oem_panel *oem_panel; int dsc; int reset_gpio; };
static int fail_index=-1,fail_status=-110,transfers;
static ssize_t transfer(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m) {
    (void)h; printf("T %u %u %u ",m->type,m->flags,m->channel);
    for(size_t i=0;i<m->tx_len;i++)printf("%02x",((const unsigned char*)m->tx_buf)[i]);
    puts("");
    if(transfers++==fail_index)return fail_status;
    /* MSM returns DMA packet bytes: four-byte header plus aligned payload. */
    return (m->tx_len + 4 + 3) & ~(size_t)3;
}
static void usleep_range(unsigned long a,unsigned long b){printf("W %lu %lu\n",a,b);}
static void msleep(unsigned int a){printf("S %u\n",a);}
static struct mipi_dsi_device *nt36532_cmd_dsi(struct nt36532 *c){return c->dsi[1]?c->dsi[1]:c->dsi[0];}
static struct nt36532 *to_nt36532(struct drm_panel *p){return (struct nt36532 *)p;}
static int pipa_init_sequence(struct nt36532 *c){(void)c;puts("LEGACY_INIT");return 0;}
static int nt36532_enable_supplies(struct nt36532 *c){(void)c;puts("POWER_ON");return 0;}
static void nt36532_disable_supplies(struct nt36532 *c){(void)c;puts("POWER_OFF");}
static void reset_sequence(struct nt36532 *c){(void)c;puts("RESET");}
static void gpiod_set_value_cansleep(int g,int v){(void)g;(void)v;puts("RESET_UNWIND");}
static void drm_dsc_pps_payload_pack(struct drm_dsc_picture_parameter_set *p,int *d){(void)d;memset(p,0,sizeof(*p));}
static int mipi_dsi_picture_parameter_set(struct mipi_dsi_device *d,const struct drm_dsc_picture_parameter_set *p){(void)d;(void)p;puts("PPS");return 0;}
static int mipi_dsi_compression_mode(struct mipi_dsi_device *d,bool on){(void)d;(void)on;puts("COMPRESSION");return 0;}
static int mipi_dsi_dcs_set_display_on(struct mipi_dsi_device *d){(void)d;puts("EXTRA_ON");return 0;}
static int mipi_dsi_dcs_set_display_off(struct mipi_dsi_device *d){(void)d;puts("LEGACY_OFF");return 0;}
static int mipi_dsi_dcs_enter_sleep_mode(struct mipi_dsi_device *d){(void)d;puts("LEGACY_SLEEP");return 0;}
'''
    suffix=r'''
int main(int argc,char **argv) {
    if(argc<3)return 2;
    if(!strcmp(argv[1],"select")) {
        const struct liuqin_oem_panel *p=liuqin_oem_from_bootargs(!strcmp(argv[2],"NULL")?NULL:argv[2]);
        puts(p?p->boot_name:"legacy"); return 0;
    }
    const struct mipi_dsi_host_ops ops={.transfer=transfer};
    struct mipi_dsi_host host={.ops=&ops};
    struct mipi_dsi_device dsi={.host=&host,.channel=0};
    struct panel_desc desc={.init_sequence=liuqin_init_sequence,.reset_sequence=reset_sequence};
    struct nt36532 ctx={.dsi={&dsi,&dsi},.desc=&desc};
    ctx.panel.dev=&dsi.dev;
    int variant=atoi(argv[2]);
    if(variant>=0 && variant<2)ctx.oem_panel=&liuqin_oem_panels[variant];
    if(argc>=5){fail_index=atoi(argv[3]);fail_status=atoi(argv[4]);}
    int ret;
    if(!strcmp(argv[1],"on"))ret=liuqin_init_sequence(&ctx);
    else if(!strcmp(argv[1],"off"))ret=nt36532_off(&ctx);
    else if(!strcmp(argv[1],"sequence")) {
        ret=nt36532_prepare(&ctx.panel);
        if(!ret)ret=nt36532_enable(&ctx.panel);
        if(!ret)ret=nt36532_off(&ctx);
    } else if(!strcmp(argv[1],"nohost")) {
        host.ops=NULL;ret=liuqin_init_sequence(&ctx);
    } else if(!strcmp(argv[1],"invalid")) {
        struct liuqin_oem_cmd cmd={.len=18};ret=liuqin_send_oem_cmds(&ctx,&cmd,1);
    } else return 3;
    printf("RESULT %d\n",ret);return 0;
}
'''
    actual=prefix+block+'\n'+ '\n'.join(function(source,n) for n in ['nt36532_off','nt36532_prepare','nt36532_enable'])+suffix
    cases=0
    with tempfile.TemporaryDirectory(prefix='oem-command-tests-') as temp:
        work=Path(temp);(work/'test.c').write_text(actual)
        subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror',str(work/'test.c'),'-o',str(work/'test')],check=True)
        def run(*values):
            nonlocal cases
            cases+=1
            return subprocess.check_output([str(work/'test'),*map(str,values)],text=True).splitlines()
        def expected(commands):
            lines=[]
            for c in commands:
                assert c['channel']==0 and c['flags'] in [0,0x40]
                lines.append(f"T {c['type']} 2 0 {c['payload']}")
                if c['wait_ms']:lines.append(f"W {c['wait_ms']*1000} {c['wait_ms']*1000+10}")
            return lines
        for variant,name in enumerate(['m81_36_02_0a','m81_42_02_0b']):
            for kind in ['on','off']:
                commands=oem[name][kind]['commands'];wire=expected(commands)
                assert run(kind,variant)==wire+['RESULT 0']
                for index in [0,len(commands)//2,len(commands)-1]:
                    before=expected(commands[:index]);c=commands[index]
                    attempted=f"T {c['type']} 2 0 {c['payload']}"
                    for failure,result in [(-110,-110),(1,-5)]:
                        assert run(kind,variant,index,failure)==before+[attempted,f'RESULT {result}']
            sequence=['POWER_ON','S 120','RESET','S 120']+expected(oem[name]['on']['commands'])+['PPS','S 120']+expected(oem[name]['off']['commands'])+['RESULT 0']
            assert run('sequence',variant)==sequence
        key='msm_drm.dsi_display0='
        tm='qcom,mdss_dsi_m81_36_02_0a_dualdsi_dsc_lcd_video'
        csot='qcom,mdss_dsi_m81_42_02_0b_dualdsi_dsc_lcd_video'
        selections=[('NULL','legacy'),('','legacy'),('quiet splash','legacy'),(key+tm+':',tm),(key+csot+':',csot),(key+tm,tm),('quiet\t'+key+tm+':config0\nrootwait',tm),('prefix'+key+tm+':','legacy'),('msm_drm.dsi_display1='+tm+':','legacy'),(key+tm+'wrong:','legacy'),(key+tm+': '+key+csot+':','legacy'),(key+tm+': '+key+tm+':',tm),(key+'unknown '+key+tm+':','legacy'),(key+tm+': '+key+'unknown','legacy')]
        for text,result in selections:assert run('select',text)==[result],text
        assert run('on',-1)==['LEGACY_INIT','RESULT 0']
        assert run('off',-1)==['LEGACY_OFF','S 20','LEGACY_SLEEP','S 120','RESULT 0']
        assert run('nohost',0)==['RESULT -38']
        assert run('invalid',0)==['RESULT -22']
    report={'cases':cases,'wire_payload_type_order_and_waits_match_rom':True,'known_variant_one_on_and_one_pps':True,'short_writes_and_errors_stop_without_further_commands':True,'selector_exact_primary_token_and_conflict_cases':len(selections),'other_board_sequences_and_power_helpers_unchanged':True,'legacy_fallback_preserved':True,'batch_transport_implemented':False,'hardware_validation_required':True}
    args.report.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
