/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include "platform.h"
#include "touch_input.h"
#include "cJSON.h"
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <linux/fb.h>
#include <linux/input.h>
#include <linux/kd.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define W 900
#define H 1440
#define NINPUT 24
#define NQUEUE 128
#define BITS (sizeof(unsigned long)*8)
#define HAS(a,n) (((a)[(n)/BITS]>>((n)%BITS))&1UL)
static int fb=-1,tty=-1,old_kd=KD_TEXT;
static uint8_t *pixels;
static size_t pixels_size;
static unsigned stride,scale=2;
static bool preview_mode;
static uint32_t *draw;
static lv_display_t *display;
static lv_group_t *group;
static lv_indev_t *pointer,*keypad;
static lv_obj_t *cursor;
static int pointer_x=W/2,pointer_y=H/2;
static bool pointer_down;
struct pointer_sample {int x,y;bool down;};
static struct pointer_sample pointer_queue[NQUEUE];
static unsigned phead,ptail;
static int pointer_owner=-1;
struct key { uint32_t value; lv_indev_state_t state; };
static struct key queue[NQUEUE];static unsigned qhead,qtail;
struct input {int fd;bool direct,mouse,keys;int minx,maxx,miny,maxy;struct touch_input touch;};
static struct input inputs[NINPUT];static unsigned ninputs,ndirect,nmouse,nkeys;

static uint32_t tick(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (uint32_t)((uint64_t)t.tv_sec*1000+t.tv_nsec/1000000);}
const char *platform_usb_state(void){
    char body[2049],boot[80];
    FILE *f=fopen("/run/liuqin-native-usb.json","r");if(!f)return "unknown";
    size_t n=fread(body,1,sizeof(body)-1,f);bool ok=!ferror(f)&&feof(f);fclose(f);body[n]=0;
    f=fopen("/proc/sys/kernel/random/boot_id","r");if(!f)return "unknown";
    if(!fgets(boot,sizeof(boot),f))ok=false;
    fclose(f);if(!ok)return "unknown";boot[strcspn(boot,"\r\n")]=0;
    cJSON *doc=cJSON_Parse(body),*state=cJSON_GetObjectItemCaseSensitive(doc,"usb_state"),
          *id=cJSON_GetObjectItemCaseSensitive(doc,"boot_id"),*stamp=cJSON_GetObjectItemCaseSensitive(doc,"monotonic");
    struct timespec now;clock_gettime(CLOCK_MONOTONIC,&now);
    double age=now.tv_sec+now.tv_nsec/1e9-(cJSON_IsNumber(stamp)?stamp->valuedouble:-1);
    const char *result="unknown";
    if(cJSON_IsString(id)&&!strcmp(id->valuestring,boot)&&cJSON_IsString(state)&&age>=0&&age<=5){
        static const char *states[]={"connected","disconnected","suspended","connecting","starting"};
        for(unsigned i=0;i<sizeof(states)/sizeof(states[0]);i++)
            if(!strcmp(state->valuestring,states[i])){result=states[i];break;}
    }
    cJSON_Delete(doc);return result;
}
static void flush(lv_display_t *d,const lv_area_t *a,uint8_t *raw){
    const uint32_t *src=(const uint32_t *)raw;int width=a->x2-a->x1+1;
    if(a->x1<0||a->y1<0||a->x2>=W||a->y2>=H){lv_display_flush_ready(d);return;}
    for(int y=a->y1;y<=a->y2;y++){
        uint32_t *row=(uint32_t *)(pixels+(size_t)y*scale*stride)+(size_t)a->x1*scale;
        for(int x=0;x<width;x++){uint32_t p=*src++|0xff000000U;for(unsigned i=0;i<scale;i++)row[(size_t)x*scale+i]=p;}
        for(unsigned i=1;i<scale;i++)memcpy((uint8_t *)row+i*stride,row,(size_t)width*scale*4);
    }
    lv_display_flush_ready(d);
}

bool platform_display(bool preview){
    preview_mode=preview;
    if(preview){scale=1;stride=W*4;pixels_size=(size_t)stride*H;pixels=calloc(1,pixels_size);}
    else {
#ifndef __aarch64__
        fprintf(stderr,"Live display requires ARM64 Recovery\n");return false;
#endif
        if(geteuid()!=0||access("/etc/liuqin-native-recovery.json",R_OK))return false;
        fb=open("/dev/fb0",O_RDWR|O_CLOEXEC|O_NOFOLLOW);if(fb<0)return false;
        struct fb_var_screeninfo v;struct fb_fix_screeninfo f;struct stat st;
        if(fstat(fb,&st)||!S_ISCHR(st.st_mode)||ioctl(fb,FBIOGET_VSCREENINFO,&v)||ioctl(fb,FBIOGET_FSCREENINFO,&f))return false;
        if(v.xres!=W*2||v.yres!=H*2||v.bits_per_pixel!=32||v.xoffset||v.yoffset||
           v.red.offset!=16||v.green.offset!=8||v.blue.offset!=0||f.line_length<W*8||
           (uint64_t)f.line_length*v.yres>f.smem_len){fprintf(stderr,"Unsupported framebuffer layout\n");return false;}
        stride=f.line_length;pixels_size=(size_t)stride*v.yres;
        pixels=mmap(NULL,pixels_size,PROT_READ|PROT_WRITE,MAP_SHARED,fb,0);if(pixels==MAP_FAILED){pixels=NULL;return false;}
        tty=open("/dev/tty1",O_RDWR|O_CLOEXEC|O_NOFOLLOW);
        if(tty>=0){if(ioctl(tty,KDGETMODE,&old_kd))old_kd=KD_TEXT;ioctl(tty,KDSETMODE,KD_GRAPHICS);}
    }
    if(!pixels)return false;
    lv_tick_set_cb(tick);display=lv_display_create(W,H);
    lv_display_set_color_format(display,LV_COLOR_FORMAT_XRGB8888);
    draw=malloc((size_t)W*120*4);if(!draw)return false;
    lv_display_set_buffers(display,draw,NULL,(size_t)W*120*4,LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(display,flush);return true;
}

static void enqueue(uint32_t key,int state){
    unsigned next=(qtail+1)%NQUEUE;if(next==qhead)return;
    queue[qtail]=(struct key){key,state?LV_INDEV_STATE_PRESSED:LV_INDEV_STATE_RELEASED};qtail=next;
}
void platform_test_key(uint32_t key){enqueue(key,1);enqueue(key,0);}
static void read_keys(lv_indev_t *indev,lv_indev_data_t *data){
    if(qhead==qtail){data->state=LV_INDEV_STATE_RELEASED;return;}
    data->key=queue[qhead].value;data->state=queue[qhead].state;qhead=(qhead+1)%NQUEUE;data->continue_reading=qhead!=qtail;
}
static void read_pointer(lv_indev_t *indev,lv_indev_data_t *data){
    static struct pointer_sample last={W/2,H/2,false};
    if(phead!=ptail){last=pointer_queue[phead];phead=(phead+1)%NQUEUE;}
    data->point.x=last.x;data->point.y=last.y;data->state=last.down?LV_INDEV_STATE_PRESSED:LV_INDEV_STATE_RELEASED;
    data->continue_reading=phead!=ptail;
}
static int clamp(int n,int max){return n<0?0:n>=max?max-1:n;}
static uint32_t keymap(unsigned code){
    switch(code){
        case KEY_VOLUMEUP:case KEY_UP:case KEY_LEFT:return LV_KEY_PREV;
        case KEY_VOLUMEDOWN:case KEY_DOWN:case KEY_RIGHT:case KEY_TAB:return LV_KEY_NEXT;
        case KEY_POWER:case KEY_ENTER:case KEY_KPENTER:case KEY_SPACE:return LV_KEY_ENTER;
        case KEY_ESC:case KEY_BACK:return LV_KEY_ESC;
        default:return 0;
    }
}
static bool touch_ready(void){
    char body[4097],boot[80];
    FILE *f=fopen("/run/liuqin-native-touch.json","r");if(!f)return false;
    size_t n=fread(body,1,sizeof(body)-1,f);bool ok=!ferror(f)&&feof(f);fclose(f);body[n]=0;
    f=fopen("/proc/sys/kernel/random/boot_id","r");if(!f)return false;
    if(!fgets(boot,sizeof(boot),f))ok=false;
    fclose(f);if(!ok)return false;boot[strcspn(boot,"\r\n")]=0;
    cJSON *doc=cJSON_Parse(body),*state=cJSON_GetObjectItemCaseSensitive(doc,"status"),*id=cJSON_GetObjectItemCaseSensitive(doc,"boot_id");
    ok=cJSON_IsString(state)&&!strcmp(state->valuestring,"ready")&&cJSON_IsString(id)&&!strcmp(id->valuestring,boot);
    cJSON_Delete(doc);return ok;
}
/* On open or event loss, query evdev state and ignore held contacts until up. */
static void sync_touch(struct input *in){
    bool mt=in->touch.mt;touch_init(&in->touch,mt);
    if(mt){
        int values[TOUCH_SLOTS+1];
        const int axes[]={ABS_MT_TRACKING_ID,ABS_MT_POSITION_X,ABS_MT_POSITION_Y};
        for(unsigned a=0;a<3;a++){
            for(int j=0;j<=TOUCH_SLOTS;j++)values[j]=a==0?-1:0;
            values[0]=axes[a];
            if(ioctl(in->fd,EVIOCGMTSLOTS(sizeof(values)),values)<0){in->touch.blocked=true;return;}
            for(int j=0;j<TOUCH_SLOTS;j++){
                if(a==0)in->touch.slots[j].id=values[j+1];
                else if(a==1)in->touch.slots[j].x=values[j+1];
                else in->touch.slots[j].y=values[j+1];
            }
        }
        struct input_absinfo slot;if(!ioctl(in->fd,EVIOCGABS(ABS_MT_SLOT),&slot))in->touch.slot=slot.value;
    }else{
        unsigned long keys[(KEY_MAX+BITS)/BITS]={0};struct input_absinfo x,y;
        if(ioctl(in->fd,EVIOCGKEY(sizeof(keys)),keys)<0||ioctl(in->fd,EVIOCGABS(ABS_X),&x)||ioctl(in->fd,EVIOCGABS(ABS_Y),&y)){
            in->touch.blocked=true;return;
        }
        in->touch.slots[0]=(struct touch_slot){HAS(keys,BTN_TOUCH)?0:-1,x.value,y.value};
    }
    for(int j=0;j<TOUCH_SLOTS;j++)if(in->touch.slots[j].id>=0)in->touch.blocked=true;
}
static void cancel_pointer(void){
    phead=ptail=0;pointer_down=false;pointer_owner=-1;
    lv_indev_reset(pointer,NULL);
    pointer_queue[ptail++]=(struct pointer_sample){pointer_x,pointer_y,false};
    for(unsigned j=0;j<ninputs;j++)if(inputs[j].direct)sync_touch(&inputs[j]);
}
static void queue_pointer(unsigned owner,int x,int y,bool down){
    if(pointer_owner>=0&&pointer_owner!=(int)owner)return;
    if(down)pointer_owner=(int)owner;
    unsigned next=(ptail+1)%NQUEUE;
    if(next==phead){cancel_pointer();return;}
    pointer_x=x;pointer_y=y;pointer_down=down;
    pointer_queue[ptail]=(struct pointer_sample){x,y,down};ptail=next;
    if(!down)pointer_owner=-1;
}
void platform_inputs(lv_group_t *g){
    group=g;
    keypad=lv_indev_create();lv_indev_set_type(keypad,LV_INDEV_TYPE_KEYPAD);lv_indev_set_read_cb(keypad,read_keys);lv_indev_set_group(keypad,group);
    pointer=lv_indev_create();lv_indev_set_type(pointer,LV_INDEV_TYPE_POINTER);lv_indev_set_read_cb(pointer,read_pointer);
    cursor=lv_obj_create(lv_layer_top());lv_obj_set_size(cursor,12,12);lv_obj_set_style_radius(cursor,LV_RADIUS_CIRCLE,0);
    lv_obj_set_style_bg_color(cursor,lv_color_hex(0x10b8a6),0);lv_obj_set_style_border_color(cursor,lv_color_hex(0xffffff),0);
    lv_obj_set_style_border_width(cursor,2,0);lv_obj_set_hidden(cursor,true);lv_indev_set_cursor(pointer,cursor);
    if(preview_mode)return;
    bool touch_available=touch_ready();
    glob_t list={0};if(glob("/dev/input/event*",0,NULL,&list))return;
    for(size_t i=0;i<list.gl_pathc&&ninputs<NINPUT;i++){
        int fd=open(list.gl_pathv[i],O_RDONLY|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW);if(fd<0)continue;
        unsigned long ev[4]={0},rel[4]={0},props[4]={0},kb[(KEY_MAX+BITS)/BITS];memset(kb,0,sizeof(kb));
        if(ioctl(fd,EVIOCGBIT(0,sizeof(ev)),ev)<0){close(fd);continue;}
        ioctl(fd,EVIOCGBIT(EV_REL,sizeof(rel)),rel);ioctl(fd,EVIOCGPROP(sizeof(props)),props);ioctl(fd,EVIOCGBIT(EV_KEY,sizeof(kb)),kb);
        bool direct=HAS(ev,EV_ABS)&&HAS(props,INPUT_PROP_DIRECT),mouse=HAS(ev,EV_REL)&&HAS(rel,REL_X)&&HAS(rel,REL_Y);
        if(direct&&!touch_available){close(fd);continue;}
        bool keyboard=HAS(ev,EV_KEY)&&(HAS(kb,KEY_POWER)||HAS(kb,KEY_VOLUMEUP)||HAS(kb,KEY_VOLUMEDOWN)||HAS(kb,KEY_ENTER));
        if(!direct&&!mouse&&!keyboard){close(fd);continue;}
        struct input *in=&inputs[ninputs];memset(in,0,sizeof(*in));in->fd=fd;in->direct=direct;in->mouse=mouse;in->keys=keyboard;
        if(direct){
            struct input_absinfo x,y,slot;bool mt=true;
            if(ioctl(fd,EVIOCGABS(ABS_MT_POSITION_X),&x)||ioctl(fd,EVIOCGABS(ABS_MT_POSITION_Y),&y)||ioctl(fd,EVIOCGABS(ABS_MT_SLOT),&slot)){
                mt=false;
                if(ioctl(fd,EVIOCGABS(ABS_X),&x)||ioctl(fd,EVIOCGABS(ABS_Y),&y)){close(fd);continue;}
            }
            if(x.maximum<=x.minimum||y.maximum<=y.minimum){close(fd);continue;}
            if(mt&&(slot.minimum!=0||slot.maximum>=TOUCH_SLOTS)){close(fd);continue;}
            in->minx=x.minimum;in->maxx=x.maximum;in->miny=y.minimum;in->maxy=y.maximum;
            touch_init(&in->touch,mt);sync_touch(in);ndirect++;
        }
        if(ioctl(fd,EVIOCGRAB,1)<0){if(direct)ndirect--;close(fd);continue;}
        if(mouse)nmouse++;
        if(keyboard)nkeys++;
        ninputs++;
    }
    /* An enumerated mouse interface does not imply an active pointing device. */
    globfree(&list);
}

void platform_poll(void){
    for(unsigned i=0;i<ninputs;i++){
        struct input *in=&inputs[i];struct input_event e;ssize_t count;
        if(in->fd<0)continue;
        while((count=read(in->fd,&e,sizeof(e)))==(ssize_t)sizeof(e)){
            if(in->direct){
                struct touch_sample sample;enum touch_result r=touch_feed(&in->touch,&e,&sample);
                if(r==TOUCH_CANCEL){if(pointer_owner==(int)i)cancel_pointer();in->touch.dropped=true;}
                else if(r==TOUCH_RESYNC)sync_touch(in);
                else if(r==TOUCH_SAMPLE){
                    int x=clamp((int)(((int64_t)sample.x-in->minx)*(W-1)/(in->maxx-in->minx)),W);
                    int y=clamp((int)(((int64_t)sample.y-in->miny)*(H-1)/(in->maxy-in->miny)),H);
                    if(sample.down||pointer_owner==(int)i){lv_obj_set_hidden(cursor,true);queue_pointer(i,x,y,sample.down);}
                }
                continue;
            }
            if(e.type==EV_SYN&&e.code==SYN_DROPPED){cancel_pointer();continue;}
            if(e.type==EV_KEY){
                if(in->mouse&&e.code==BTN_LEFT)queue_pointer(i,pointer_x,pointer_y,e.value!=0);
                else if(in->keys){uint32_t k=keymap(e.code);if(k){lv_obj_set_hidden(cursor,true);enqueue(k,e.value);}}
            }else if(in->mouse&&e.type==EV_REL){
                if(e.value&&(e.code==REL_X||e.code==REL_Y)&&pointer_owner<0){
                    lv_obj_set_hidden(cursor,false);
                    queue_pointer(i,clamp(pointer_x+(e.code==REL_X?e.value:0),W),clamp(pointer_y+(e.code==REL_Y?e.value:0),H),false);
                }else if(pointer_owner==(int)i&&(e.code==REL_X||e.code==REL_Y)){
                    queue_pointer(i,clamp(pointer_x+(e.code==REL_X?e.value:0),W),clamp(pointer_y+(e.code==REL_Y?e.value:0),H),pointer_down);
                }
            }
        }
        if(count==0||(count<0&&errno==ENODEV)){
            if(pointer_owner==(int)i)cancel_pointer();
            close(in->fd);in->fd=-1;
        }
    }
}
const char *platform_input_label(void){
    if(preview_mode)return "离线预览 · 不连接设备";
    if(ndirect)return "触屏可用 · 音量键选择 · 电源键确认";
    if(nmouse)return "键盘 / 触控板可用 · 音量键选择 · 电源键确认";
    return nkeys?"音量键选择 · 电源键确认 · 屏幕触控暂未就绪":"未检测到输入设备，请使用电脑安装器";
}
void platform_focus(lv_group_t *g){group=g;lv_indev_set_group(keypad,g);if(cursor)lv_obj_move_foreground(cursor);}
bool platform_save(const char *path){
    if(!preview_mode)return false;
    FILE *f=fopen(path,"wb");if(!f)return false;
    fprintf(f,"P6\n%d %d\n255\n",W,H);
    for(int y=0;y<H;y++)for(int x=0;x<W;x++){uint32_t p=((uint32_t *)(pixels+(size_t)y*stride))[x];unsigned char rgb[3]={p>>16,p>>8,p};if(fwrite(rgb,1,3,f)!=3){fclose(f);return false;}}
    return fclose(f)==0;
}
void platform_close(void){
    for(unsigned i=0;i<ninputs;i++)if(inputs[i].fd>=0){ioctl(inputs[i].fd,EVIOCGRAB,0);close(inputs[i].fd);}
    if(tty>=0){ioctl(tty,KDSETMODE,old_kd);close(tty);}
    if(pixels){if(preview_mode)free(pixels);else munmap(pixels,pixels_size);}
    if(fb>=0)close(fb);
    free(draw);
}
