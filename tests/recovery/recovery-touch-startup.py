#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compile and exercise the actual Rec kernel firmware qualification helper."""
from pathlib import Path
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[2]


class FirmwareTests(unittest.TestCase):
    def test_bounded_recovery_and_failure_paths(self):
        patch = (PROJECT / 'build/recovery/kernel/patches/linux-7.3/0004-recovery-touch-startup-health.patch').read_text()
        section = patch.split('+++ b/drivers/input/touchscreen/nt36523/nt36xxx_rec.h\n', 1)[1]
        helper = '\n'.join(line[1:] for line in section.splitlines() if line.startswith('+'))
        stub = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
typedef unsigned char u8;
#define POINT_DATA_LEN 65
#define RESET_STATE_REK 0xa1
#define NVT_LOG(...) ((void)printf(__VA_ARGS__))
struct nvt_ts_data { bool rec_health_checked; unsigned rec_wdt_polls,rec_fw_retries; const char *fw_name; };
static unsigned elapsed,polls,reloads,quiesces;
static unsigned fault_at,clear_at,off_at;
static bool persistent,recurring;
static int spi_error,download_error,rek_error;
static bool nvt_panel_is_on(struct nvt_ts_data *data) { (void)data;return elapsed<off_at; }
static int nvt_read_event_bytes(struct nvt_ts_data *data,u8 offset,u8 *buf,size_t len) {
 (void)data;assert(offset==0 && len==66);polls++;
 if(spi_error)return spi_error;
 bool fault=elapsed>=fault_at && elapsed<clear_at && (!reloads || persistent);
 if(recurring)fault=fault && polls%11!=0;
 memset(buf,fault?0xfd:0xff,len);return 0;
}
static void usleep_range(unsigned a,unsigned b) { assert(a==10000 && b==11000);elapsed+=10; }
static void msleep(unsigned ms) { assert(ms==50);elapsed+=ms; }
static void nvt_quiesce_failed_tddi(struct nvt_ts_data *data) { (void)data;quiesces++; }
static int nvt_update_firmware_no_eng_reset(const char *fw) { assert(!strcmp(fw,"test.bin"));reloads++;return download_error; }
static int nvt_check_fw_reset_state(int state) { assert(state==RESET_STATE_REK);return rek_error; }
static struct nvt_ts_data fresh(void) {
 elapsed=polls=reloads=quiesces=0;fault_at=clear_at=off_at=~0u;
 persistent=recurring=false;spi_error=download_error=rek_error=0;
 return (struct nvt_ts_data){.fw_name="test.bin"};
}
'''
        checks = r'''
int main(void) {
 struct nvt_ts_data d=fresh();
 assert(nvt_rec_startup_health(&d)==0 && d.rec_health_checked);
 assert(elapsed==1000 && polls==101 && !reloads && !d.rec_wdt_polls);
 d=fresh();fault_at=300;
 assert(nvt_rec_startup_health(&d)==0 && d.rec_health_checked);
 assert(reloads==1 && quiesces==1 && d.rec_fw_retries==1 && d.rec_wdt_polls==11);
 assert(elapsed==1450);
 d=fresh();fault_at=300;persistent=true;
 assert(nvt_rec_startup_health(&d)==-EUCLEAN && !d.rec_health_checked);
 assert(reloads==1 && quiesces==1 && d.rec_wdt_polls==22);
 d=fresh();fault_at=100;clear_at=130;
 assert(nvt_rec_startup_health(&d)==0 && d.rec_health_checked);
 assert(!reloads && d.rec_wdt_polls==3 && elapsed==1130);
 d=fresh();fault_at=0;persistent=recurring=true;
 assert(nvt_rec_startup_health(&d)==-EUCLEAN && !d.rec_health_checked);
 assert(reloads==1 && polls==300 && elapsed==3050);
 d=fresh();spi_error=-EIO;
 assert(nvt_rec_startup_health(&d)==-EIO && !d.rec_health_checked && !reloads);
 d=fresh();off_at=300;
 assert(nvt_rec_startup_health(&d)==-EHOSTDOWN && !reloads);
 d=fresh();fault_at=0;off_at=125;
 assert(nvt_rec_startup_health(&d)==-EHOSTDOWN && !reloads && quiesces==1);
 d=fresh();fault_at=0;download_error=-EIO;
 assert(nvt_rec_startup_health(&d)==-EIO && reloads==1 && !d.rec_health_checked);
 d=fresh();fault_at=0;rek_error=-ETIMEDOUT;
 assert(nvt_rec_startup_health(&d)==-ETIMEDOUT && reloads==1 && !d.rec_health_checked);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'test.c'; binary = Path(tmp) / 'test'
            source.write_text(stub + helper + checks)
            subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    unittest.main()
