#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute board quirks, audio lifetimes and the published ARM64 load entry."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'build/lib'))
import arm64_alignment as alignment
parser=argparse.ArgumentParser()
parser.add_argument('--source',type=Path)
args,remaining=parser.parse_known_args()
SOURCE=args.source
CC=next((shutil.which(prefix+'gcc') for prefix in ('aarch64-unknown-linux-musl-','aarch64-linux-gnu-')
         if shutil.which(prefix+'gcc')),None)
OBJCOPY=CC[:-3]+'objcopy' if CC else None
try:
    from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM,UC_HOOK_CODE
    from unicorn.arm64_const import UC_ARM64_REG_X0,UC_ARM64_REG_PC,UC_ARM64_REG_PSTATE
    EMULATOR=True
except ImportError:
    EMULATOR=False


def function(text,name):
    match=re.search(r'^(?:static\s+)?(?:inline\s+)?(?:bool|int|int32_t|void)\s+'+name+r'\([^;{]*\)\s*\{',text,re.M)
    if not match:raise ValueError(name)
    end=match.end();depth=1
    while depth:
        depth+=(text[end]=='{')-(text[end]=='}');end+=1
    return text[match.start():end]


def execute(code, expected_null_failure=False):
    with tempfile.TemporaryDirectory(prefix='liuqin-common-fixes-') as work:
        source=Path(work)/'fixture.c';source.write_text(code)
        binary=source.with_suffix('')
        subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
                        '-Wno-unused-function','-fsanitize=undefined','-fno-sanitize-recover=all',
                        str(source),'-o',str(binary)],check=True)
        result=subprocess.run([str(binary)],check=not expected_null_failure,timeout=15,
                              capture_output=expected_null_failure,text=expected_null_failure)
        if expected_null_failure and (not result.returncode or 'null pointer' not in result.stderr):
            raise AssertionError('The clock fixture did not reproduce the expected NULL access: '+str(result))


@unittest.skipUnless(CC and OBJCOPY,'an ARM64 toolchain is required')
class AlignmentTests(unittest.TestCase):
    def image(self,size=4*1024**2):
        data=bytearray(bytes(range(256))*(size//256))
        struct.pack_into('<QQQ',data,8,0,len(data)+4096,0xa);data[56:60]=b'ARM\x64'
        return bytes(data)

    def test_publication_is_idempotent_and_authenticates_the_entire_entry(self):
        with tempfile.TemporaryDirectory() as work:
            image=Path(work)/'Image';original=self.image(16384);image.write_bytes(original)
            first=alignment.publish(image,CC,OBJCOPY)
            self.assertEqual(first,alignment.publish(image,CC,OBJCOPY))
            self.assertEqual((image.with_name('Image.raw')).read_bytes(),original)
            altered=bytearray(image.read_bytes());altered[80]^=1;image.write_bytes(altered)
            with self.assertRaisesRegex(ValueError,'exact relocation entry'):
                alignment.publish(image,CC,OBJCOPY)

    def test_invalid_geometry_is_rejected_before_linking(self):
        for offset,size,flags in ((0x80000,20480,10),(0,16,10),(0,64*1024**2,10),(0,20480,11)):
            data=bytearray(self.image(16384));struct.pack_into('<QQQ',data,8,offset,size,flags)
            with self.assertRaises(ValueError):alignment.wrap(bytes(data),CC,OBJCOPY)

    @unittest.skipUnless(EMULATOR,'Unicorn is required for relocation execution')
    def test_real_entry_relocates_overlapping_payload_at_legacy_load_offsets(self):
        raw=self.image();wrapped=alignment.wrap(raw,CC,OBJCOPY)
        for base in (0x10000000,0x10008000,0x10080000,0x101ff000):
            target=(base+4096+0x1fffff)&~0x1fffff
            machine=Uc(UC_ARCH_ARM64,UC_MODE_ARM);machine.mem_map(0x10000000,16*1024**2)
            machine.mem_write(base,wrapped);machine.reg_write(UC_ARM64_REG_PSTATE,0x3c5)
            machine.reg_write(UC_ARM64_REG_X0,0x400000)
            for i in (1,2,3):machine.reg_write(UC_ARM64_REG_X0+i,0xdeadbeef)
            machine.hook_add(UC_HOOK_CODE,lambda emulator,*_:emulator.emu_stop(),begin=target,end=target)
            machine.emu_start(base,0x11000000,count=4000000)
            self.assertEqual(machine.reg_read(UC_ARM64_REG_PC),target)
            self.assertEqual(machine.reg_read(UC_ARM64_REG_X0),0x400000)
            self.assertEqual(bytes(machine.mem_read(target,len(raw))),raw)
            for i in (1,2,3):self.assertEqual(machine.reg_read(UC_ARM64_REG_X0+i),0)
            self.assertGreaterEqual(base+struct.unpack_from('<Q',wrapped,16)[0],target+len(raw)+4096)


COMMON=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>
typedef uint64_t u64;
typedef uint32_t u32;
#define BIT_ULL(n) (1ULL<<(n))
#define GENMASK_ULL(h,l) ((~0ULL<<(l))&(~0ULL>>(63-(h))))
'''
BOARD=r'''
static bool compatible=true,model=true,ids_present=true;
static u32 board_ids[2]={16,0};
static void *of_root;
static bool of_machine_is_compatible(const char *s){return compatible;}
static int of_property_match_string(void *p,const char *key,const char *val){return model?0:-1;}
static int of_property_read_u32_array(void *p,const char *key,u32 *out,int n){memcpy(out,board_ids,sizeof(board_ids));return ids_present?0:-1;}
'''


@unittest.skipUnless(SOURCE,'--source enables actual compiled-kernel function checks')
class PlatformTests(unittest.TestCase):
    def board(self):return BOARD+function((SOURCE/'include/linux/platform_data/liuqin.h').read_text(),'liuqin_board_quirks')

    def test_power_notification_startup_is_serialized_and_publishes_only_after_ack(self):
        source=(SOURCE/'drivers/power/supply/qcom_battmgr.c').read_text()
        code=COMMON+r'''
#define PMIC_GLINK_OWNER_BATTMGR 32778
#define PMIC_GLINK_NOTIFY 2
#define BATTMGR_REQUEST_NOTIFICATION 4
#define cpu_to_le32(x) (x)
struct work_struct {int unused;};
struct pmic_glink_hdr {u32 owner,type,opcode;};
struct qcom_battmgr_enable_request {struct pmic_glink_hdr hdr;u32 battery_id,power_state,low_capacity,high_capacity;};
struct qcom_battmgr {int lock;void *dev,*bat_psy,*usb_psy,*ac_psy,*wls_psy;};
static struct qcom_battmgr b;
#define container_of(p,t,m) (&b)
static int depth,requests,notifications,errors,result;
static void mutex_lock(int *l){assert(!depth);depth++;}
static void mutex_unlock(int *l){assert(depth==1);depth--;}
static int qcom_battmgr_request(struct qcom_battmgr *p,void *data,size_t size){
 struct qcom_battmgr_enable_request *r=data;
 assert(depth==1&&size==sizeof(*r));assert(r->hdr.owner==32778&&r->hdr.type==2&&r->hdr.opcode==4);
 requests++;return result;
}
static void power_supply_changed(void *p){assert(p&&!depth);notifications++;}
#define dev_err(...) do{errors++;}while(0)
'''+function(source,'qcom_battmgr_enable_worker')+r'''
int main(void){
 struct work_struct work;
 b.bat_psy=(void *)1;b.usb_psy=(void *)2;b.wls_psy=(void *)3;
 qcom_battmgr_enable_worker(&work);assert(requests==1&&notifications==3&&!errors&&!depth);
 notifications=0;result=-ETIMEDOUT;qcom_battmgr_enable_worker(&work);
 assert(requests==2&&!notifications&&errors==1&&!depth);
 b=(struct qcom_battmgr){0};result=0;qcom_battmgr_enable_worker(&work);
 assert(requests==3&&!notifications&&errors==1&&!depth);
 return 0;
}
'''
        execute(code)



    def test_eusb2_wake_retries_are_bounded_and_never_publish_failed_reads(self):
        code=COMMON+r'''
typedef uint8_t u8;
struct i2c_eusb2_repeater {void *regmap,*dev;};
static int attempts,waits,errors,failures,code=-ENXIO;
static int regmap_read(void *map,unsigned int reg,unsigned int *out){
 attempts++;if(attempts<=failures)return code;*out=0x42;return 0;
}
static void usleep_range(int low,int high){assert(low==1000&&high==2000);waits++;}
#define dev_err(...) do{errors++;}while(0)
#define dev_dbg(...) ((void)0)
'''+function((SOURCE/'drivers/phy/qualcomm/phy-qcom-i2c-eusb2-repeater.c').read_text(),'eusb2_i2c_read_reg')+r'''
int main(void){
 struct i2c_eusb2_repeater r={0};u8 value=0xa5;
 failures=2;assert(!eusb2_i2c_read_reg(&r,0x13,&value));
 assert(attempts==3&&waits==2&&errors==0&&value==0x42);
 attempts=waits=errors=0;failures=20;value=0xa5;
 assert(eusb2_i2c_read_reg(&r,0x13,&value)==-ENXIO);
 assert(attempts==5&&waits==4&&errors==1&&value==0xa5);
 attempts=waits=errors=0;code=-EINVAL;
 assert(eusb2_i2c_read_reg(&r,0x13,&value)==-EINVAL);
 assert(attempts==1&&!waits&&errors==1&&value==0xa5);
 return 0;
}
'''
        execute(code)

    def test_only_the_marked_initial_abl_subtree_is_omitted(self):
        code=COMMON+r"""
#define CONFIG_OF_KOBJ 1
#define IS_ENABLED(x) (x)
#define WARN_ON_ONCE(x) (x)
#define FDT_ERR_NOTFOUND 1
#define pr_err(...) ((void)0)
struct device_node {struct device_node *parent;};
struct test_blob {int marker_len;};
static struct test_blob initial={0}, overlay={0};
static const void *initial_boot_params=&initial;
static struct device_node nodes[10];
static unsigned int visited;
static int fdt_path_offset(const void *blob,const char *path){
 assert(!strcmp(path,"/liuqin-abl-overlay-sink"));return 5;
}
static const void *fdt_getprop(const void *blob,int offset,const char *prop,int *len){
 assert(offset==5&&!strcmp(prop,"xiaomi,abl-overlay-sink"));
 *len=((const struct test_blob *)blob)->marker_len;return *len<0?NULL:blob;
}
static int fdt_next_node(const void *blob,int offset,int *depth){
 static const int depths[]={0,1,2,2,3,1,2,3,1,2};
 if(++offset==10)return -FDT_ERR_NOTFOUND;
 *depth=depths[offset];return offset;
}
static bool of_fdt_device_is_available(const void *blob,int offset){return true;}
static int populate_node(const void *blob,int offset,void **mem,struct device_node *parent,
                         struct device_node **result,bool dryrun){
 visited|=1u<<offset;nodes[offset].parent=parent;*result=&nodes[offset];return 0;
}
static void reverse_nodes(struct device_node *root){assert(root==&nodes[0]);}
"""+function((SOURCE/'drivers/of/fdt.c').read_text(),'unflatten_dt_nodes')+r"""
int main(void){
 char memory[16];struct device_node *root=NULL;
 assert(!unflatten_dt_nodes(&initial,memory,NULL,&root));
 assert(root==&nodes[0]&&visited==(0x3ffu&~(7u<<5)));
 assert(nodes[4].parent==&nodes[3]&&nodes[8].parent==root&&nodes[9].parent==&nodes[8]);
 visited=0;assert(!unflatten_dt_nodes(&overlay,memory,NULL,&root)&&visited==0x3ffu);
 initial.marker_len=-1;visited=0;
 assert(!unflatten_dt_nodes(&initial,memory,NULL,&root)&&visited==0x3ffu);
 initial.marker_len=4;visited=0;
 assert(!unflatten_dt_nodes(&initial,memory,NULL,&root)&&visited==0x3ffu);
 return 0;
}
"""
        execute(code)

    def test_cpu_exception_is_board_specific_and_never_changes_the_strict_mask(self):
        code=COMMON+self.board()+r'''
#define SYS_ID_AA64MMFR1_EL1 7
#define ID_AA64MMFR1_EL1_SpecSEI_SHIFT 24
struct arm64_ftr_reg {u64 strict_mask;};
static struct arm64_ftr_reg reg={~0ULL};
static int updates,warnings;
static struct arm64_ftr_reg *get_arm64_ftr_reg(u32 id){return id?&reg:NULL;}
static void update_cpu_ftr_reg(struct arm64_ftr_reg *r,u64 v){updates++;}
#define pr_warn(...) do{warnings++;}while(0)
'''+function((SOURCE/'arch/arm64/kernel/cpufeature.c').read_text(),'check_update_ftr_reg')+r'''
int main(void){
 const u64 boot=0x11212122,other=0x10212122;
 assert(!check_update_ftr_reg(7,4,other,boot));
 assert(!check_update_ftr_reg(7,0,boot,other));
 assert(reg.strict_mask==~0ULL&&updates==2&&!warnings);
 assert(check_update_ftr_reg(7,4,other^2,boot));
 assert(check_update_ftr_reg(7,4,0x12212122,0x10212122));
 assert(check_update_ftr_reg(6,4,other,boot));
 compatible=false;model=false;assert(check_update_ftr_reg(7,4,other,boot));
 model=true;assert(!check_update_ftr_reg(7,4,other,boot));
 board_ids[0]=17;assert(check_update_ftr_reg(7,4,other,boot));
 board_ids[0]=16;ids_present=false;assert(check_update_ftr_reg(7,4,other,boot));
 assert(reg.strict_mask==~0ULL);int before=updates;
 assert(!check_update_ftr_reg(0,4,other,boot)&&updates==before);
 return 0;
}
'''
        execute(code)

    def test_factory_gpt_link_requires_the_measured_luns_and_identical_metadata(self):
        code=COMMON+self.board()+r'''
typedef uint8_t gpt_entry[128];
typedef struct {u64 my_lba,alternate_lba,first_usable_lba,last_usable_lba;
 u32 num_partition_entries,sizeof_partition_entry,partition_entry_array_crc32;
 uint8_t disk_guid[16];} gpt_header;
struct device_node {bool ufs;};
struct device {struct device *parent;struct device_node *of_node;};
struct disk {const char *disk_name;struct device dev;};struct parsed_partitions {struct disk *disk;};
#define disk_to_dev(d) (&(d)->dev)
static bool of_device_is_compatible(struct device_node *n,const char *s){return n->ufs&&!strcmp(s,"qcom,sm8450-ufshc");}
#define le64_to_cpu(x) (x)
#define le32_to_cpu(x) (x)
#define efi_guidcmp(a,b) memcmp((a),(b),16)
'''+function((SOURCE/'block/partitions/efi.c').read_text(),'liuqin_factory_disk')+'\n'+function((SOURCE/'block/partitions/efi.c').read_text(),'liuqin_factory_backup')+r'''
int main(void){
 struct device_node ufs_node={true},usb_node={false};
 struct device ufs={.of_node=&ufs_node},usb={.of_node=&usb_node},scsi={.parent=&ufs};
 struct disk disk={.disk_name="sdd",.dev={.parent=&scsi}};struct parsed_partitions state={&disk};
 gpt_header a={.my_lba=1,.alternate_lba=8191,.first_usable_lba=6,.last_usable_lba=8186,
 .num_partition_entries=4,.sizeof_partition_entry=128,.partition_entry_array_crc32=0x12345678};
 gpt_header b=a;b.my_lba=8191;gpt_entry entries[4]={{1}},mirror[4];memcpy(mirror,entries,sizeof(entries));
#define GOOD() liuqin_factory_backup(&state,&a,&b,entries,mirror,8191)
 assert(GOOD());disk.dev.parent=&usb;assert(!GOOD());disk.dev.parent=NULL;assert(!GOOD());disk.dev.parent=&scsi;
 disk.disk_name="sdf";assert(GOOD());disk.disk_name="sda";assert(!GOOD());disk.disk_name="sdd";
 compatible=false;model=false;assert(!GOOD());compatible=true;
 b.alternate_lba=1;assert(!GOOD());b.alternate_lba=8191;
 b.disk_guid[0]=1;assert(!GOOD());b.disk_guid[0]=0;
 b.first_usable_lba++;assert(!GOOD());b.first_usable_lba--;
 b.partition_entry_array_crc32++;assert(!GOOD());b.partition_entry_array_crc32--;
 mirror[3][10]=1;assert(!GOOD());mirror[3][10]=0;assert(GOOD());
 assert(!liuqin_factory_backup(&state,&a,&b,entries,mirror,16383));
 assert(!liuqin_factory_backup(&state,&a,NULL,entries,mirror,8191));return 0;
}
'''
        execute(code)
        source=(SOURCE/'block/partitions/efi.c').read_text()
        self.assertRegex(source,r'good_pgpt && good_agpt &&\s+liuqin_factory_backup')

    def test_touch_polling_reports_only_terminal_failure(self):
        code=COMMON+r'''
typedef uint8_t uint8_t;typedef int RST_COMPLETE_STATE;
#define RESET_STATE_INIT 0xa0
#define RESET_STATE_MAX 0xaf
#define EVENT_MAP_RESET_COMPLETE 0x60
#define unlikely(x) (x)
static int reads,ready_after,errors;
static struct {struct {int EVENT_BUF_ADDR;} *mmap;void *client;} device;
static typeof(device) *ts=&device;
#define nvt_set_page(x) ((void)(x))
#define CTP_SPI_READ(client,buf,size) do{reads++;(buf)[1]=reads>=ready_after?0xa1:0;}while(0)
#define NVT_ERR(...) do{errors++;}while(0)
#define usleep_range(a,b) ((void)0)
'''+function((SOURCE/'drivers/input/touchscreen/nt36523/nt36xxx.c').read_text(),'nvt_check_fw_reset_state')+r'''
int main(void){typeof(*device.mmap) map={0};device.mmap=&map;
 ready_after=4;assert(!nvt_check_fw_reset_state(0xa1));assert(reads==4&&!errors);
 reads=0;ready_after=1000;assert(nvt_check_fw_reset_state(0xa1)<0);assert(reads==51&&errors==1);return 0;}
'''
        execute(code)

    def test_keyboard_retries_nack_but_never_delays_a_successful_read(self):
        code=COMMON+r'''
#define NANOSIC_I2C_DATA_READ 1
#define NANOSIC_I2C_RETRY_US 25000
struct client {int dev;};struct nanosic {int lock;struct client *client;unsigned char rxbuf[64];};
static int calls,sleeps,parsed,failures,error_code=-ENXIO;
#define lockdep_assert_held(p) ((void)(p))
#define dev_err_ratelimited(...) ((void)0)
static int nanosic_read_frame(struct nanosic *n,int op){calls++;return calls<=failures?error_code:0;}
static void usleep_range(int a,int b){assert(a==25000&&b==25500);sleeps++;}
static void nanosic_parse_frame(struct nanosic *n,unsigned char *buf,int len){parsed++;}
'''+function((SOURCE/'drivers/hid/hid-nanosic.c').read_text(),'nanosic_receive_frame')+r'''
int main(void){struct client c={0};struct nanosic n={.client=&c};
 failures=0;nanosic_receive_frame(&n);assert(calls==1&&!sleeps&&parsed==1);
 calls=sleeps=parsed=0;failures=3;nanosic_receive_frame(&n);assert(calls==4&&sleeps==3&&parsed==1);
 calls=sleeps=parsed=0;failures=20;nanosic_receive_frame(&n);assert(calls==4&&sleeps==3&&!parsed);
 calls=sleeps=parsed=0;error_code=-EIO;nanosic_receive_frame(&n);assert(calls==2&&sleeps==1&&!parsed);return 0;}
'''
        execute(code)


@unittest.skipUnless(SOURCE,'--source enables actual audio function checks')
class AudioTests(unittest.TestCase):
    def clock_registration_fixture(self, driver):
        header=(SOURCE/'sound/soc/qcom/qdsp6/q6dsp-lpass-clocks.h').read_text()
        driver=re.sub(r'^#include.*\n','',driver,flags=re.M)
        fixture=(PROJECT/'tests/q6-clock-registration-mock.c').read_text()
        return fixture.replace('/* Q6DSP_PUBLIC_HEADER */',header).replace('/* Q6DSP_DRIVER */',driver)

    def test_clock_probe_can_prepare_new_parents_during_registration(self):
        driver=(SOURCE/'sound/soc/qcom/qdsp6/q6dsp-lpass-clocks.c').read_text()
        execute(self.clock_registration_fixture(driver))

    def test_clock_fixture_detects_the_original_late_drvdata_bug(self):
        driver=(SOURCE/'sound/soc/qcom/qdsp6/q6dsp-lpass-clocks.c').read_text()
        self.assertEqual(driver.count('\tdev_set_drvdata(dev, cc);'),1)
        driver=driver.replace('\tdev_set_drvdata(dev, cc);\n','')
        anchor='\treturn 0;\n}\nEXPORT_SYMBOL_GPL(q6dsp_clock_dev_probe);'
        self.assertEqual(driver.count(anchor),1)
        driver=driver.replace(anchor,'\tdev_set_drvdata(dev, cc);\n\n'+anchor)
        execute(self.clock_registration_fixture(driver),expected_null_failure=True)

    def test_only_the_observed_liuqin_spf_status_is_retryable(self):
        text=(SOURCE/'sound/soc/qcom/qdsp6/audioreach.c').read_text()
        code=COMMON+r"""
#define APM_CMD_GET_SPF_STATE 0x01001021
#define ADSP_EFAILED 1
#define HZ 100
struct device {int unused;};
struct mutex {int unused;};
typedef int gpr_device_t;
typedef int gpr_port_t;
typedef int wait_queue_head_t;
struct gpr_hdr {u32 opcode;};
struct gpr_pkt {struct gpr_hdr hdr;};
struct gpr_ibasic_rsp_result_t {u32 opcode,status;};
static struct gpr_ibasic_rsp_result_t response;
static unsigned int status=ADSP_EFAILED,errors;
static bool liuqin=true;
#define mutex_lock(m) ((void)(m))
#define mutex_unlock(m) ((void)(m))
#define dev_err(...) do{errors++;}while(0)
#define wait_event_timeout(wait,condition,timeout) (condition)
static bool liuqin_board_quirks(void){return liuqin;}
static int gpr_send_pkt(gpr_device_t *gdev,const struct gpr_pkt *pkt){
 response.opcode=pkt->hdr.opcode;response.status=status;return 0;
}
static int gpr_send_port_pkt(gpr_port_t *port,const struct gpr_pkt *pkt){return gpr_send_pkt(NULL,pkt);}
"""+function(text,'audioreach_send_cmd_sync')+r"""
int main(void){
 gpr_device_t dev=0;struct gpr_pkt pkt={{APM_CMD_GET_SPF_STATE}};
#define SEND() audioreach_send_cmd_sync(NULL,&dev,&response,NULL,NULL,NULL,&pkt,0)
 assert(SEND()==-EAGAIN&&!errors);
 liuqin=false;assert(SEND()==-EINVAL&&errors==1);
 liuqin=true;pkt.hdr.opcode++;assert(SEND()==-EINVAL&&errors==2);
 pkt.hdr.opcode=APM_CMD_GET_SPF_STATE;status=2;assert(SEND()==-EINVAL&&errors==3);
 status=0;assert(!SEND()&&errors==3);return 0;
}
"""
        execute(code)

    def test_probe_never_queries_before_callback_and_errors_never_mean_ready(self):
        text=(SOURCE/'sound/soc/qcom/qdsp6/q6apm.c').read_text()
        code=COMMON+r"""
#define APM_CMD_GET_SPF_STATE 0x01001021
#define APM_CMD_RSP_GET_SPF_STATE 0x02001007
#define GPR_BASIC_RSP_RESULT 0x02001005
#define APM_SPF_STATE_NOT_READY 0
#define APM_SPF_STATE_READY 1
#define GFP_KERNEL 0
#define __free(x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define READ_ONCE(x) (x)
#define IS_ERR(p) ((uintptr_t)(p)>=(uintptr_t)-4095)
#define PTR_ERR(p) ((int)(intptr_t)(p))
#define dev_err(...) ((void)0)
static int warnings,wakes,retry_errors,sleeps,transient_failures;
static bool retry_board=true;
#define dev_warn_ratelimited(...) do{warnings++;}while(0)
#define dev_err_ratelimited(...) do{retry_errors++;}while(0)
struct device {void *driver_data,*of_node;};
struct gpr_hdr {u32 opcode;};
struct gpr_pkt {struct gpr_hdr hdr;int payload_size;};
struct gpr_resp_pkt {struct gpr_hdr hdr;void *payload;int payload_size;};
struct gpr_ibasic_rsp_result_t {u32 opcode,status;};
typedef struct {struct device dev;struct {void *callback;} svc;} gpr_device_t;
struct q6apm {struct device *dev;gpr_device_t *gdev;int lock,wait;u32 state;
 struct gpr_ibasic_rsp_result_t result;};
static struct q6apm storage,*g_apm;
static int apm_ready_lock,q6apm_audio_component;
static int allocation_error,send_error,reply_state=1,queries,registration_error,populate_error,unregistered;
static void mutex_lock(int *p){assert(!*p);*p=1;}
static void mutex_unlock(int *p){assert(*p);*p=0;}
static bool liuqin_board_quirks(void){return retry_board;}
static void msleep(unsigned int delay){assert(delay==25);sleeps++;}
#define mutex_init(p) (*(p)=0)
#define init_waitqueue_head(p) (*(p)=0)
#define INIT_LIST_HEAD(p) ((void)0)
#define idr_init(p) ((void)0)
#define dev_set_drvdata(d,p) ((d)->driver_data=(p))
#define dev_get_drvdata(d) ((d)->driver_data)
static void *devm_kzalloc(struct device *d,int size,int flags){memset(&storage,0,sizeof(storage));return &storage;}
static int snd_soc_register_component(struct device *d,void *c,void *dai,int n){return registration_error;}
static void snd_soc_unregister_component(struct device *d){unregistered++;}
static int of_platform_populate(void *a,void *b,void *c,struct device *d){return populate_error;}
static void of_platform_depopulate(struct device *d){}
static struct gpr_pkt *audioreach_alloc_apm_cmd_pkt(int size,u32 opcode,u32 token){
 static struct gpr_pkt pkt;assert(size==0&&token==0);pkt.hdr.opcode=opcode;pkt.payload_size=16;
 return allocation_error?(void*)(intptr_t)-ENOMEM:&pkt;
}
static int q6apm_send_cmd_sync(struct q6apm *a,const struct gpr_pkt *p,u32 response){
 assert(p->hdr.opcode==APM_CMD_GET_SPF_STATE&&response==APM_CMD_RSP_GET_SPF_STATE&&p->payload_size==16);
 queries++;
 if(transient_failures){transient_failures--;return -EAGAIN;}
 if(!send_error)a->state=reply_state;
 return send_error;
}
static u32 get_unaligned_le32(const void *p){u32 v;memcpy(&v,p,4);return v;}
static void wake_up(int *p){wakes++;}
"""
        for name in ('q6apm_get_apm_state','q6apm_is_adsp_ready','apm_probe','apm_remove','q6apm_state_response'):
            code+=function(text,name)+'\n'
        code+=r"""
int main(void){
 gpr_device_t dev={0};
 registration_error=-EIO;assert(apm_probe(&dev)==-EIO&&!g_apm&&!queries&&!unregistered);
 registration_error=0;populate_error=-ENODEV;assert(apm_probe(&dev)==-ENODEV&&!g_apm&&!queries&&unregistered==1);
 populate_error=0;assert(!apm_probe(&dev)&&g_apm==&storage&&!queries);
 assert(!q6apm_is_adsp_ready()&&!queries);dev.svc.callback=(void*)1;
 assert(q6apm_is_adsp_ready()&&queries==1);
 transient_failures=1;assert(q6apm_is_adsp_ready()&&queries==3&&sleeps==1&&!retry_errors);
 transient_failures=4;assert(!q6apm_is_adsp_ready()&&queries==7&&sleeps==4&&retry_errors==1);
 retry_board=false;transient_failures=1;
 assert(!q6apm_is_adsp_ready()&&queries==8&&sleeps==4&&retry_errors==1);
 retry_board=true;
 send_error=-ETIMEDOUT;storage.state=1;assert(!q6apm_is_adsp_ready());
 send_error=0;reply_state=2;assert(!q6apm_is_adsp_ready());
 allocation_error=1;assert(!q6apm_is_adsp_ready());allocation_error=0;
 u32 state=1;struct gpr_resp_pkt response={.hdr={APM_CMD_RSP_GET_SPF_STATE},.payload=&state,.payload_size=4};
 assert(q6apm_state_response(&storage,&response)&&storage.state==1&&wakes==1);
 storage.state=0;response.payload_size=3;
 assert(q6apm_state_response(&storage,&response)&&!storage.state&&wakes==1&&warnings==1);
 u32 basic[2]={APM_CMD_GET_SPF_STATE,2};response.hdr.opcode=GPR_BASIC_RSP_RESULT;response.payload=basic;response.payload_size=8;
 storage.state=1;assert(q6apm_state_response(&storage,&response)&&!storage.state&&storage.result.status==2&&wakes==2);
 basic[1]=0;storage.state=0;assert(q6apm_state_response(&storage,&response)&&!storage.state&&wakes==2);
 response.hdr.opcode=APM_CMD_RSP_GET_SPF_STATE;response.payload=&state;response.payload_size=4;
 assert(q6apm_state_response(&storage,&response)&&storage.state==1&&wakes==3);
 response.hdr.opcode=GPR_BASIC_RSP_RESULT;response.payload=basic;response.payload_size=8;
 assert(q6apm_state_response(&storage,&response)&&storage.state==1&&wakes==3);
 basic[0]=123;assert(!q6apm_state_response(&storage,&response)&&wakes==3);
 apm_remove(&dev);assert(!g_apm&&!apm_ready_lock&&!q6apm_is_adsp_ready());return 0;
}
"""
        execute(code)
        apr=(SOURCE/'drivers/soc/qcom/apr.c').read_text()
        probe=function(apr,'apr_device_probe')
        self.assertLess(probe.index('adrv->probe(adev)'),probe.index('adev->svc.callback'))

    def test_jackless_flag_preserves_legacy_jack_calls_on_other_boards(self):
        code=COMMON+r"""
struct device {void *private;};
struct snd_soc_component {struct device *dev;};struct snd_soc_jack {int dummy;};
struct wcd938x_priv {bool mbhc_disabled;void *wcd_mbhc;int mbhc_cfg;};
static int starts,stops;
#define dev_get_drvdata(d) ((d)->private)
static int wcd_mbhc_start(void *p,int *cfg,struct snd_soc_jack *j){starts++;return 7;}
static void wcd_mbhc_stop(void *p){stops++;}
"""+function((SOURCE/'sound/soc/codecs/wcd938x.c').read_text(),'wcd938x_codec_set_jack')+r"""
int main(void){struct wcd938x_priv w={.mbhc_disabled=true};struct device d={&w};struct snd_soc_component c={&d};struct snd_soc_jack j;
 assert(!wcd938x_codec_set_jack(&c,&j,NULL));assert(!wcd938x_codec_set_jack(&c,NULL,NULL));assert(!starts&&!stops);
 w.mbhc_disabled=false;assert(wcd938x_codec_set_jack(&c,&j,NULL)==7);assert(starts==1&&!stops);
 assert(!wcd938x_codec_set_jack(&c,NULL,NULL)&&stops==1);return 0;}
"""
        execute(code)


if __name__=='__main__':unittest.main(argv=[__file__,*remaining],verbosity=2)
