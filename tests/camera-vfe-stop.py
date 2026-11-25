#!/usr/bin/env python3
"""Exercise late CAMSS completion between output stop and WM-map release.

Compiles the actual queue/completion/stop bodies with mocked MMIO and vb2. This
reproduces active_num underflow and the next-session negative buffer index.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kernel', type=Path, required=True)
    a = p.parse_args()
    source = (a.kernel / 'drivers/media/platform/qcom/camss/camss-vfe.c').read_text()
    functions = []
    for signature in ('void vfe_buf_done(', 'static void vfe_disable_output(', 'int vfe_queue_buffer_v2('):
        body = source[source.index(signature):].split('\n}\n', 1)[0] + '\n}\n'
        functions.append(body)
    harness = r'''
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#define VFE_LINE_NONE -1
#define VFE_OUTPUT_OFF 0
#define VFE_OUTPUT_ON 2
#define VB2_BUF_STATE_DONE 1
#define CAMSS_INIT_BUF_COUNT 2
#define container_of(ptr,type,member) ((type *)((char *)(ptr)-offsetof(type,member)))
#define spin_lock_irqsave(lock,flags) ((void)(lock), (flags)=0)
#define spin_unlock_irqrestore(lock,flags) ((void)(lock), (void)(flags))
#define dev_err_ratelimited(...) ((void)0)
typedef uint32_t u32;
typedef uint64_t u64;
struct vb2_buffer { u64 timestamp; };
struct camss_buffer { struct { struct vb2_buffer vb2_buf; unsigned sequence; } vb; u32 addr[1]; };
struct camss_video { int unused; };
struct vfe_device;
struct vfe_output { unsigned wm_num; int wm_idx[2]; struct camss_buffer *buf[2]; struct {int active_num;} gen2; int state; unsigned sequence; };
struct vfe_line { struct vfe_output output; struct camss_video video_out; struct vfe_device *vfe; int id; };
struct vfe_hw_ops { void (*vfe_wm_update)(struct vfe_device*, unsigned char,u32,struct vfe_line*); void (*reg_update)(struct vfe_device*,int); void (*vfe_wm_stop)(struct vfe_device*,unsigned char); };
struct resource {const struct vfe_hw_ops *hw_ops;};
struct vfe_device {struct vfe_line line[4]; int wm_output_map[4],output_lock; struct resource *res;};
static int done, updated, stopped, pending;
static struct vfe_device *to_vfe(struct vfe_line *line) {return line->vfe;}
static u64 ktime_get_ns(void) {return 1234;}
static struct camss_buffer *vfe_buf_get_pending(struct vfe_output *o) {(void)o;return NULL;}
static void vfe_buf_add_pending(struct vfe_output *o,struct camss_buffer *b) {(void)o;(void)b;pending++;}
static void vb2_buffer_done(struct vb2_buffer *b,int state) {(void)b;assert(state==VB2_BUF_STATE_DONE);done++;}
static void update(struct vfe_device *v,unsigned char w,u32 a,struct vfe_line *l) {(void)v;(void)a;(void)l;assert(w==3);updated++;}
static void reg_update(struct vfe_device *v,int id) {(void)v;assert(id==3);}
static void wm_stop(struct vfe_device *v,unsigned char w) {(void)v;assert(w==3);stopped++;}
'''
    harness += '\n'.join(functions)
    harness += r'''
int main(void) {
 const struct vfe_hw_ops ops={update,reg_update,wm_stop};
 struct resource res={&ops};struct vfe_device v={.res=&res};
 struct vfe_line *l=&v.line[3]; l->vfe=&v;l->id=3;
 struct vfe_output *o=&l->output;struct camss_buffer b={.addr={0x12340000}};
 o->wm_num=1;o->wm_idx[0]=3;v.wm_output_map[3]=3;
 o->state=VFE_OUTPUT_ON;o->gen2.active_num=1;o->buf[0]=&b;
 vfe_buf_done(&v,3);assert(done==1 && o->gen2.active_num==0);
 // A pending IRQ after output stop, before release of the write-master map.
 o->buf[0]=&b;o->gen2.active_num=1;
 vfe_disable_output(l);assert(stopped==1 && o->gen2.active_num==0);
 vfe_buf_done(&v,3);assert(done==1 && o->gen2.active_num==0);
 // Queued buffers after stop stay pending and never reach stopped MMIO.
 vfe_queue_buffer_v2(&l->video_out,&b);assert(pending==1 && updated==0);
 v.wm_output_map[3]=VFE_LINE_NONE;vfe_buf_done(&v,3);assert(done==1);
 // Restart with no queued buffers (the converter's start order).
 v.wm_output_map[3]=3;o->state=VFE_OUTPUT_ON;o->buf[0]=NULL;
 vfe_queue_buffer_v2(&l->video_out,&b);
 assert(o->buf[0]==&b && o->gen2.active_num==1 && o->wm_idx[0]==3 && updated==1);
 vfe_buf_done(&v,3);assert(done==2 && o->gen2.active_num==0);
 // Defensive check: an invalid count must never be used as buf[-1].
 o->gen2.active_num=-1;vfe_queue_buffer_v2(&l->video_out,&b);
 assert(pending==2 && updated==1 && o->wm_idx[0]==3);
}
'''
    with tempfile.TemporaryDirectory() as tmp:
        c, exe = Path(tmp) / 'vfe.c', Path(tmp) / 'vfe'
        c.write_text(harness)
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', str(c), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
    print('PASS: late IRQ during stop, queue after stop, unmapped WM, empty restart, negative-count protection')


if __name__ == '__main__':
    main()
