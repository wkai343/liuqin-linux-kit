#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute Iris capture-response functions against empty-frame lifecycle cases."""
import argparse
from pathlib import Path

from kernel_test import function, run_c

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, required=True)
args = parser.parse_args()
source = (args.source / 'drivers/media/platform/qcom/iris/iris_hfi_gen2_response.c').read_text()
code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
#include <stdio.h>
typedef uint32_t u32;
typedef uint64_t u64;
enum { DECODER, ENCODER };
enum { IRIS_INST_STREAMING, IRIS_INST_OUTPUT_STREAMING, IRIS_INST_ERROR, STOPPED };
enum { BUF_ATTR_QUEUED=1, BUF_ATTR_DEQUEUED=2, BUF_ATTR_DEFERRED=4 };
enum { V4L2_BUF_FLAG_ERROR=1, V4L2_BUF_FLAG_LAST=2, V4L2_BUF_FLAG_KEYFRAME=4,
       V4L2_BUF_FLAG_PFRAME=8, V4L2_BUF_FLAG_BFRAME=16 };
enum { HFI_GEN2_PICTURE_IDR=1, HFI_GEN2_PICTURE_I=2, HFI_GEN2_PICTURE_CRA=4,
       HFI_GEN2_PICTURE_BLA=8, HFI_GEN2_PICTURE_P=16, HFI_GEN2_PICTURE_B=32,
       HFI_GEN2_PICTURE_NOSHOW=64 };
enum { HFI_BUF_FW_FLAG_LAST=1, HFI_BUF_FW_FLAG_PSC_LAST=2 };
struct iris_buffer { u32 index, data_offset, data_size, attr, flags;
                     u64 device_addr, timestamp; };
struct v4l2_m2m_buffer { struct iris_buffer vb; };
struct v4l2_m2m_ctx { struct v4l2_m2m_buffer buffer; };
struct iris_inst_hfi_gen2 { bool output_stopping;
    struct { u32 picture_type, data_corrupt, overflow; } hfi_frame_info; };
struct iris_inst { int domain, state; struct v4l2_m2m_ctx *m2m_ctx;
                   struct iris_inst_hfi_gen2 gen2; };
struct iris_hfi_buffer { u32 index, data_offset, data_size, flags;
                        u64 base_address, timestamp; };
#define to_iris_inst_hfi_gen2(i) (&(i)->gen2)
#define to_iris_buffer(b) (b)
#define v4l2_m2m_for_each_dst_buf_safe(c,b,n) \
    for ((b)=&(c)->buffer, (n)=NULL; (b); (b)=(n))
static int queued, queue_error, drain_last, drc_last;
static int iris_queue_buffer(struct iris_inst *inst, struct iris_buffer *buf)
{
    (void)inst;
    queued++;
    if (queue_error) return queue_error;
    buf->attr &= ~BUF_ATTR_DEFERRED;
    buf->attr |= BUF_ATTR_QUEUED;
    return 0;
}
static int iris_inst_sub_state_change_drain_last(struct iris_inst *inst)
{ (void)inst; drain_last++; return 0; }
static int iris_inst_sub_state_change_drc_last(struct iris_inst *inst)
{ (void)inst; drc_last++; return 0; }
'''
for name in ('iris_hfi_gen2_get_driver_buffer_flags',
             'iris_hfi_gen2_requeue_output_buffer',
             'iris_hfi_gen2_handle_output_buffer'):
    code += function(source, name) + '\n'
code += r'''
static struct v4l2_m2m_ctx ctx;
static struct iris_inst inst;
static struct iris_hfi_buffer response;
static void reset(void)
{
    ctx=(struct v4l2_m2m_ctx){.buffer.vb={.device_addr=0x1000,
        .attr=BUF_ATTR_QUEUED}};
    inst=(struct iris_inst){.domain=DECODER,.state=IRIS_INST_STREAMING,.m2m_ctx=&ctx};
    response=(struct iris_hfi_buffer){.base_address=0x1000,.timestamp=42};
    queued=queue_error=drain_last=drc_last=0;
}
int main(void)
{
    struct iris_buffer *b=&ctx.buffer.vb;
    reset(); /* Unflagged empty return must not consume a visible frame. */
    assert(!iris_hfi_gen2_handle_output_buffer(&inst,&response));
    assert(queued==1 && b->attr==BUF_ATTR_QUEUED && !b->flags && !b->timestamp);
    response.data_size=4096; /* The actual frame can reuse its timestamp. */
    inst.gen2.hfi_frame_info.picture_type=HFI_GEN2_PICTURE_IDR;
    assert(!iris_hfi_gen2_handle_output_buffer(&inst,&response));
    assert(queued==1 && b->attr==BUF_ATTR_DEQUEUED && b->timestamp==42);
    assert(b->data_size==4096 && b->flags==V4L2_BUF_FLAG_KEYFRAME);
    reset();
    inst.gen2.hfi_frame_info.picture_type=HFI_GEN2_PICTURE_NOSHOW;
    response.data_size=4096;
    assert(!iris_hfi_gen2_handle_output_buffer(&inst,&response));
    assert(queued==1 && b->data_size==0 && !b->flags);
    for (int stopping=0; stopping<2; stopping++) {
        reset();
        inst.gen2.output_stopping=stopping;
        if (!stopping) inst.state=STOPPED;
        assert(!iris_hfi_gen2_handle_output_buffer(&inst,&response));
        assert(!queued && b->attr==BUF_ATTR_DEFERRED);
    }
    for (int last=1; last<=2; last++) {
        reset(); response.flags=last;
        assert(!iris_hfi_gen2_handle_output_buffer(&inst,&response));
        assert(!queued && b->attr==BUF_ATTR_DEQUEUED && b->flags==V4L2_BUF_FLAG_LAST);
        assert((last==1 ? drain_last : drc_last)==1);
    }
    reset(); inst.gen2.hfi_frame_info.data_corrupt=1;
    assert(!iris_hfi_gen2_handle_output_buffer(&inst,&response));
    assert(!queued && b->flags==V4L2_BUF_FLAG_ERROR && b->timestamp==42);
    reset(); inst.state=IRIS_INST_ERROR;
    assert(!iris_hfi_gen2_handle_output_buffer(&inst,&response));
    assert(!queued && b->flags==V4L2_BUF_FLAG_ERROR);
    reset(); queue_error=-EIO;
    assert(iris_hfi_gen2_handle_output_buffer(&inst,&response)==-EIO);
    assert(b->attr==BUF_ATTR_DEQUEUED && b->flags==V4L2_BUF_FLAG_ERROR && b->timestamp==42);
    reset(); inst.domain=ENCODER;
    assert(!iris_hfi_gen2_handle_output_buffer(&inst,&response));
    assert(!queued && b->flags==V4L2_BUF_FLAG_ERROR);
    reset(); response.base_address=0x2000;
    assert(iris_hfi_gen2_handle_output_buffer(&inst,&response)==-EINVAL);
    assert(!queued && b->attr==BUF_ATTR_QUEUED);
    puts("PASS: empty/hidden frames, visible-frame identity, STOP, EOS, PSC, errors and invalid buffers");
}
'''
print(run_c(code), end='')
