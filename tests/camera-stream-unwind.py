#!/usr/bin/env python3
"""Execute real CAMSS STREAMON rollback with a failure at each pipeline node.

No hardware access. Mock subdevices detect double-stop, leaked streams and PM
references; the old driver leaves enabled nodes behind after a sensor failure.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kernel', type=Path, required=True)
    a = p.parse_args()
    source = (a.kernel / 'drivers/media/platform/qcom/camss/camss-video.c').read_text()
    bodies = []
    for signature in ('static void video_stop_started_subdevs(', 'static int video_start_streaming('):
        if signature in source:
            bodies.append(source[source.index(signature):].split('\n}\n', 1)[0] + '\n}\n')
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#define MEDIA_PAD_FL_SINK 1
#define ENOIOCTLCMD 515
#define VB2_BUF_STATE_QUEUED 2
#define dev_err(...) ((void)0)
#define v4l2_subdev_call(sd,type,op,on) stream(sd,on)
struct media_entity;
struct media_pad {unsigned flags;struct media_entity *entity;struct media_pad *remote;};
struct v4l2_subdev {const char *name;int id,active;};
struct media_entity {struct media_pad pads[1];struct v4l2_subdev *sd;};
struct video_device {struct media_entity entity;};
struct camss_video;
struct video_ops {void (*flush_buffers)(struct camss_video *,int);};
struct camss_video {struct video_device vdev;const struct video_ops *ops;};
struct vb2_queue {struct camss_video *video;};
static int fail_node,skip_node,fail_pipeline,fail_format,pipe_active,flushed,starts[4],stops[4];
static struct camss_video *vb2_get_drv_priv(struct vb2_queue *q){return q->video;}
static struct media_pad *media_pad_remote_pad_first(struct media_pad *p){return p->remote;}
static bool is_media_entity_v4l2_subdev(struct media_entity *e){return e->sd!=NULL;}
static struct v4l2_subdev *media_entity_to_v4l2_subdev(struct media_entity *e){return e->sd;}
static int video_device_pipeline_alloc_start(struct video_device *v){(void)v;if(fail_pipeline)return -11;assert(!pipe_active);pipe_active=1;return 0;}
static void video_device_pipeline_stop(struct video_device *v){(void)v;assert(pipe_active);pipe_active=0;}
static int video_check_format(struct camss_video *v){(void)v;return fail_format?-22:0;}
static void flush(struct camss_video *v,int state){(void)v;assert(state==VB2_BUF_STATE_QUEUED);flushed++;}
static int stream(struct v4l2_subdev *s,int on){
 if(s->id==skip_node)return -ENOIOCTLCMD;
 if(on){assert(!s->active);starts[s->id]++;if(s->id==fail_node)return -110;s->active=1;}
 else{assert(s->active);s->active=0;stops[s->id]++;}
 return 0;
}
''' + '\n'.join(bodies) + r'''
int main(void){
 const struct video_ops ops={flush};struct camss_video v={.ops=&ops};struct vb2_queue q={&v};
 struct media_entity nodes[4]={0};struct v4l2_subdev sd[4]={0};
 v.vdev.entity.pads[0].flags=MEDIA_PAD_FL_SINK;
 v.vdev.entity.pads[0].remote=&nodes[0].pads[0];
 for(int i=0;i<4;i++){
  sd[i].name="mock";sd[i].id=i;nodes[i].sd=&sd[i];nodes[i].pads[0].entity=&nodes[i];
  if(i<3){nodes[i].pads[0].flags=MEDIA_PAD_FL_SINK;nodes[i].pads[0].remote=&nodes[i+1].pads[0];}
 }
 for(int skip=-1;skip<4;skip++)for(int fail=0;fail<4;fail++){
  if(fail==skip)continue;
  skip_node=skip;fail_node=fail;flushed=0;memset(starts,0,sizeof(starts));memset(stops,0,sizeof(stops));
  assert(video_start_streaming(&q,4)==-110);assert(!pipe_active && flushed==1);
  for(int i=0;i<4;i++){
   assert(!sd[i].active);
   assert(starts[i]==(i<=fail && i!=skip));
   assert(stops[i]==(i<fail && i!=skip));
  }
 }
 fail_node=skip_node=-1;fail_pipeline=1;flushed=0;
 assert(video_start_streaming(&q,4)==-11 && !pipe_active && flushed==1);
 fail_pipeline=0;fail_format=1;flushed=0;
 assert(video_start_streaming(&q,4)==-22 && !pipe_active && flushed==1);
 fail_format=0;flushed=0;
 assert(video_start_streaming(&q,4)==0 && pipe_active && !flushed);
 for(int i=0;i<4;i++)assert(sd[i].active);
}
'''
    with tempfile.TemporaryDirectory() as directory:
        c, exe = Path(directory) / 'stream.c', Path(directory) / 'stream'
        c.write_text(code)
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter', str(c), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
    print('PASS: every STREAMON failure unwinds exactly the accepted subdevices; original error and queued buffers preserved')


if __name__ == '__main__':
    main()
