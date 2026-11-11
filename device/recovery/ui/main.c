/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include "platform.h"
#include "cJSON.h"
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

extern const lv_font_t ui_font_20,ui_font_26,ui_font_34;
enum page { HOME,SYSTEMS,TOOLS,DEVICE,PARTITIONS,LOGS,POWER };
enum job_kind { NO_JOB,LOAD_SYSTEMS,PLAN_SWITCH,APPLY_SWITCH };
enum { WIDTH = 900, HEIGHT = 1440, HEADER_HEIGHT = 198, SYSTEMS_PER_PAGE = 5 };
static enum page current=HOME;
static bool demo;
static volatile sig_atomic_t running=1;
static lv_group_t *focus,*modal_focus;
static lv_obj_t *screen,*body,*badge,*modal,*resume_focus,*brightness_label;
static lv_obj_t *battery_label, *usb_label, *device_details;
static cJSON *summary,*parts,*systems;
static char *log_text;
static unsigned part_page,log_page,system_page;
static const char *log_kind="kernel";
static char confirm_token[80];
static struct {
    enum job_kind kind;
    pthread_t thread;
    atomic_bool ready;
    cJSON *request, *reply;
    char error[1600];
} job;
static const char *demo_names[] = {"LineageOS", "Fedora", "Linux Btrfs"};
static const char *demo_partitions[] = {"userdata", "lqr_eb245f9b622e", "lqr_617ebe70c532"};
static const char *demo_ids[] = {"android", "fedora", "linux-btrfs"};
static unsigned demo_default, demo_target;
static const char *demo_battery = "86%";
static const char *demo_usb_state = "connected";
static const uint32_t INK=0x183340,MUTED=0x627b87,TEAL=0x007f7b,BG=0xf0f4f5;

static const char *str(const cJSON *j,const char *key){const cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key);return cJSON_IsString(v)?v->valuestring:"";}
static double num(const cJSON *j,const char *key){const cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key);return cJSON_IsNumber(v)?v->valuedouble:0;}
static bool yes(const cJSON *j,const char *key){return cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j,key));}
static void stop(int sig){(void)sig;running=0;}
static void render_page(enum page page);
static void message(const char *title,const char *text);
static void close_modal(lv_event_t *e);
static void poll_job(lv_timer_t *timer);

static cJSON *fake_rpc(const char *op,const cJSON *request){
    if (!strcmp(op, "status")) {
        cJSON *value = cJSON_Parse("{\"device\":\"liuqin\",\"serial\":\"DEMO-LIUQIN\",\"slot\":\"a\",\"kernel\":\"7.3.4-liuqin-recovery\",\"build_id\":\"offline-preview\",\"brightness\":70,\"partition_count\":132,\"uptime_seconds\":420,\"memory_available_kib\":6200000,\"sealed\":true,\"phase\":\"complete\",\"installer_version\":1,\"usb_transport\":\"adb\",\"tablet_boot_switch\":true}");
        cJSON_AddStringToObject(value, "battery", demo_battery);
        cJSON_AddStringToObject(value, "usb_state", demo_usb_state);
        return value;
    }
    if (!strcmp(op, "systems")) {
        cJSON *r = cJSON_CreateObject();
        cJSON *rows = cJSON_AddArrayToObject(r, "systems");
        cJSON_AddBoolToObject(r, "can_switch", true);
        for (unsigned i = 0; i < 3; ++i) {
            cJSON *row = cJSON_CreateObject();
            cJSON_AddStringToObject(row, "id", demo_ids[i]);
            cJSON_AddStringToObject(row, "name", demo_names[i]);
            cJSON_AddStringToObject(row, "type", i ? "linux" : "android");
            cJSON_AddStringToObject(row, "partition", demo_partitions[i]);
            cJSON_AddStringToObject(row, "filesystem", i == 1 ? "ext4" : i == 2 ? "btrfs" : "");
            cJSON_AddBoolToObject(row, "is_default", i == demo_default);
            cJSON_AddItemToArray(rows, row);
        }
        return r;
    }
    if (!strcmp(op, "prepare_switch")) {
        for (unsigned i = 0; i < 3; ++i) {
            if (i == demo_default || strcmp(str(request, "system_id"), demo_ids[i])) continue;
            demo_target = i;
            cJSON *r = cJSON_CreateObject();
            cJSON_AddStringToObject(r, "token", "demo-switch");
            cJSON_AddStringToObject(r, "previous_name", demo_names[demo_default]);
            cJSON_AddStringToObject(r, "previous_partition", demo_partitions[demo_default]);
            cJSON_AddStringToObject(r, "target_name", demo_names[i]);
            cJSON_AddStringToObject(r, "target_partition", demo_partitions[i]);
            return r;
        }
        return NULL;
    }
    if (!strcmp(op, "execute_switch") && !strcmp(str(request, "token"), "demo-switch")) {
        demo_default = demo_target;
        cJSON *r = cJSON_CreateObject();
        cJSON_AddStringToObject(r, "default_id", demo_ids[demo_default]);
        cJSON_AddStringToObject(r, "target_name", demo_names[demo_default]);
        return r;
    }
    if(!strcmp(op,"partitions")){
        cJSON *r=cJSON_CreateObject(),*a=cJSON_AddArrayToObject(r,"partitions");
        const char *names[]={"boot_a","boot_b","recovery_a","recovery_b","super","userdata","metadata","persist","vbmeta_a","vbmeta_b","dtbo_a","dtbo_b","vendor_boot_a","vendor_boot_b"};
        for(unsigned i=0;i<14;i++){cJSON *p=cJSON_CreateObject();cJSON_AddStringToObject(p,"name",names[i]);cJSON_AddNumberToObject(p,"bytes",i==5?241556238336.0:i==4?9126805504.0:201326592);cJSON_AddBoolToObject(p,"readonly",true);cJSON_AddItemToArray(a,p);}return r;
    }
    if(!strcmp(op,"logs"))return cJSON_Parse("{\"text\":\"[  0.0] Linux kernel started\\n[  0.9] simplefb registered: 1800 x 2880\\n[ 15.4] Internal storage is read-only\\n[ 17.0] USB ADB is ready\\n[ 17.1] Liuqin Recovery service ready\\n[ 18.0] Partition inspection complete\"}");
    if(!strcmp(op,"prepare_exit"))return cJSON_Parse("{\"token\":\"demo-token\"}");
    if(!strcmp(op,"execute_exit"))return cJSON_Parse("{\"message\":\"演示模式：没有执行电源操作。\"}");
    if(!strcmp(op,"export_logs"))return cJSON_Parse("{\"message\":\"日志已在内存中打包。请在电脑安装器中导出。\"}");
    if(!strcmp(op,"brightness"))return cJSON_Duplicate(request,true);
    return NULL;
}

static cJSON *rpc_wait(cJSON *request,char *error,size_t cap,int seconds){
    if(demo){
        cJSON *r=fake_rpc(str(request,"op"),request);
        cJSON_Delete(request);
        if (!r) snprintf(error, cap, "操作未完成，请刷新后重试。");
        return r;
    }
    int fd=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);char *wire=cJSON_PrintUnformatted(request);cJSON_Delete(request);
    if(fd<0||!wire){if(fd>=0)close(fd);free(wire);snprintf(error,cap,"无法连接维护服务。");return NULL;}
    struct timeval timeout={seconds,0};setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
    struct sockaddr_un addr={.sun_family=AF_UNIX};snprintf(addr.sun_path,sizeof(addr.sun_path),"/run/liuqin-recovery-ui.sock");
    size_t n=strlen(wire);char *line=malloc(n+2);if(!line){close(fd);free(wire);return NULL;}
    memcpy(line,wire,n);line[n]='\n';line[n+1]=0;free(wire);
    bool ok=connect(fd,(struct sockaddr *)&addr,sizeof(addr))==0;size_t offset=0;
    while(ok&&offset<n+1){ssize_t sent=send(fd,line+offset,n+1-offset,MSG_NOSIGNAL);if(sent<=0){ok=false;break;}offset+=(size_t)sent;}
    free(line);char *reply=calloc(1,262145);size_t used=0;
    while(ok&&reply&&used<262144){ssize_t got=recv(fd,reply+used,262144-used,0);if(got<=0){ok=false;break;}used+=(size_t)got;if(reply[used-1]=='\n')break;}
    close(fd);
    cJSON *envelope=(ok&&reply&&used&&reply[used-1]=='\n')?cJSON_Parse(reply):NULL;free(reply);
    if(!envelope||!yes(envelope,"ok")){snprintf(error,cap,"%s",envelope?str(envelope,"error"):"维护服务暂未响应，请稍后刷新。");cJSON_Delete(envelope);return NULL;}
    cJSON *result=cJSON_DetachItemFromObjectCaseSensitive(envelope,"result");cJSON_Delete(envelope);return result;
}
static cJSON *rpc(cJSON *request,char *error,size_t cap){
    return rpc_wait(request,error,cap,12);
}
static cJSON *operation(const char *op){cJSON *r=cJSON_CreateObject();cJSON_AddStringToObject(r,"op",op);return r;}
static cJSON *call(const char *op){char error[600]={0};cJSON *r=rpc(operation(op),error,sizeof(error));if(!r)message("操作未完成",error);return r;}

static lv_obj_t *label(lv_obj_t *parent,const char *text,int x,int y,int width,const lv_font_t *font,uint32_t color){
    lv_obj_t *o=lv_label_create(parent);lv_label_set_text(o,text);lv_obj_set_pos(o,x,y);lv_obj_set_width(o,width);
    lv_obj_set_style_text_font(o,font,0);lv_obj_set_style_text_color(o,lv_color_hex(color),0);lv_obj_set_style_text_line_space(o,8,0);return o;
}
static lv_obj_t *panel(lv_obj_t *parent,int x,int y,int w,int h,uint32_t color){
    lv_obj_t *o=lv_obj_create(parent);lv_obj_set_pos(o,x,y);lv_obj_set_size(o,w,h);lv_obj_set_scrollable(o,false);
    lv_obj_set_style_radius(o,18,0);lv_obj_set_style_pad_all(o,0,0);lv_obj_set_style_bg_color(o,lv_color_hex(color),0);
    lv_obj_set_style_border_width(o,1,0);lv_obj_set_style_border_color(o,lv_color_hex(0xdce5e8),0);return o;
}
static enum page parent_page(enum page page) {
    return page == DEVICE || page == PARTITIONS || page == LOGS ? TOOLS : HOME;
}
static void key_escape(lv_event_t *e) {
    if (lv_event_get_key(e) != LV_KEY_ESC || job.kind != NO_JOB) return;
    if (modal) close_modal(NULL);
    else if (current != HOME) render_page(parent_page(current));
}
static void primary(lv_obj_t *button) {
    lv_obj_set_style_bg_color(button, lv_color_hex(TEAL), 0);
    lv_obj_set_style_bg_color(button, lv_color_hex(0x006c69), LV_STATE_FOCUSED);
    lv_obj_set_style_border_width(button, 0, 0);
    for (unsigned i = 0; i < lv_obj_get_child_count(button); ++i)
        lv_obj_set_style_text_color(lv_obj_get_child(button, i), lv_color_hex(0xffffff), 0);
}
static lv_obj_t *button(lv_obj_t *parent,const char *title,const char *detail,int x,int y,int w,int h,lv_event_cb_t cb,void *data){
    lv_obj_t *b=lv_button_create(parent);lv_obj_set_pos(b,x,y);lv_obj_set_size(b,w,h);lv_obj_set_style_radius(b,18,0);
    lv_obj_set_style_bg_color(b,lv_color_hex(0xffffff),0);lv_obj_set_style_shadow_width(b,0,0);
    lv_obj_set_style_border_width(b,1,0);lv_obj_set_style_border_color(b,lv_color_hex(0xdce5e8),0);
    lv_obj_set_style_outline_width(b,4,LV_STATE_FOCUS_KEY);lv_obj_set_style_outline_color(b,lv_color_hex(TEAL),LV_STATE_FOCUS_KEY);
    lv_obj_set_style_bg_color(b,lv_color_hex(0xe3f3ef),LV_STATE_FOCUSED);lv_obj_set_style_pad_all(b,0,0);
    if(title)label(b,title,22,detail?24:(h-32)/2,w-44,&ui_font_26,INK);
    if(detail)label(b,detail,22,72,w-44,&ui_font_20,MUTED);
    lv_obj_add_event_cb(b,cb,LV_EVENT_CLICKED,data);lv_obj_add_event_cb(b,key_escape,LV_EVENT_KEY,NULL);lv_group_add_obj(modal?modal_focus:focus,b);return b;
}
static void close_modal(lv_event_t *e){
    if(!modal)return;
    platform_focus(focus);lv_obj_delete(modal);modal=NULL;lv_group_delete(modal_focus);modal_focus=NULL;
    if(resume_focus&&lv_obj_is_valid(resume_focus))lv_group_focus_obj(resume_focus);
    confirm_token[0]=0;
}
static lv_obj_t *modal_box(int y, int height) {
    if(modal)close_modal(NULL);
    resume_focus=lv_group_get_focused(focus);
    modal_focus = lv_group_create();
    modal = panel(lv_layer_top(), 0, 0, WIDTH, HEIGHT, 0x102b38);
    lv_obj_set_style_bg_opa(modal, LV_OPA_70, 0);
    lv_obj_set_style_radius(modal, 0, 0);
    platform_focus(modal_focus);
    return panel(modal, 70, y, 760, height, 0xffffff);
}
static void message(const char *title,const char *text){
    lv_obj_t *box = modal_box(404, 540);
    label(box, title, 38, 38, 684, &ui_font_34, INK);
    lv_obj_t *content = panel(box, 38, 112, 684, 280, BG);
    lv_obj_set_scroll_dir(content, LV_DIR_VER);
    lv_obj_set_scrollable(content, true);
    label(content, text, 18, 15, 644, &ui_font_26, INK);
    lv_obj_t *b = button(box, "返回", NULL, 38, 420, 684, 80, close_modal, NULL);
    lv_group_focus_obj(b);
}
static void navigate(lv_event_t *e){render_page((enum page)(uintptr_t)lv_event_get_user_data(e));}
static void load_systems(lv_event_t *e);
static void refresh(lv_event_t *e) {
    if (current == SYSTEMS) load_systems(e);
    else render_page(current);
}
static void export_logs(lv_event_t *e){cJSON *r=call("export_logs");if(r){message("日志已准备",str(r,"message"));cJSON_Delete(r);}}
static void brightness(lv_event_t *e){
    int delta=(int)(intptr_t)lv_event_get_user_data(e),level=(int)num(summary,"brightness");level+=delta;if(level<10)level=10;if(level>100)level=100;
    cJSON *q=operation("brightness");cJSON_AddNumberToObject(q,"percent",level);char err[600]={0};cJSON *r=rpc(q,err,sizeof(err));
    if(!r){message("亮度未调整",err);return;}cJSON_Delete(r);cJSON_ReplaceItemInObject(summary,"brightness",cJSON_CreateNumber(level));
    if(brightness_label)lv_label_set_text_fmt(brightness_label,"%d%%",level);
}
static const char *exit_title(const char *action){return !strcmp(action,"system")?"重启到默认系统":!strcmp(action,"fastboot")?"重启到 Fastboot":"关机";}
static void execute_exit(lv_event_t *e){
    cJSON *q=operation("execute_exit");cJSON_AddStringToObject(q,"token",confirm_token);char err[600]={0};cJSON *r=rpc(q,err,sizeof(err));
    confirm_token[0]=0;if(r){message("操作已提交",str(r,"message"));cJSON_Delete(r);}else message("退出状态",err);
}
static void prepare_exit(lv_event_t *e){
    const char *action=lv_event_get_user_data(e);cJSON *q=operation("prepare_exit");cJSON_AddStringToObject(q,"action",action);
    char err[600]={0};cJSON *r=rpc(q,err,sizeof(err));if(!r){message("暂时不能退出",err);return;}
    lv_obj_t *box = modal_box(450, 400);
    snprintf(confirm_token, sizeof(confirm_token), "%s", str(r, "token"));
    cJSON_Delete(r);
    label(box,exit_title(action),38,40,680,&ui_font_34,INK);
    label(box,"请确认电脑端操作已完成。确认后将退出 Recovery。",38,125,680,&ui_font_26,MUTED);
    lv_obj_t *cancel=button(box,"取消",NULL,38,282,324,80,close_modal,NULL);
    primary(button(box,"确认",NULL,398,282,324,80,execute_exit,NULL));
    lv_group_focus_obj(cancel);
}

static void *run_job(void *unused) {
    job.reply = rpc_wait(job.request, job.error, sizeof(job.error), 600);
    atomic_store_explicit(&job.ready, true, memory_order_release);
    return NULL;
}

static void start_job(enum job_kind kind, cJSON *request) {
    if (job.kind != NO_JOB) {
        cJSON_Delete(request);
        return;
    }
    job.kind = kind;
    job.request = request;
    job.error[0] = 0;
    atomic_store(&job.ready, false);
    lv_obj_t *box = modal_box(514, 300);
    const char *title = kind == LOAD_SYSTEMS ? "正在读取系统" :
                        kind == PLAN_SWITCH ? "正在核对引导" : "正在切换默认系统";
    label(box, title, 38, 40, 684, &ui_font_34, INK);
    label(box, kind == APPLY_SWITCH ? "正在写入和回读，请保持平板开启。" : "正在检查系统登记和存储状态。",
          38, 122, 684, &ui_font_26, MUTED);
    lv_obj_t *spinner = lv_spinner_create(box);
    lv_obj_set_size(spinner, 62, 62);
    lv_obj_align(spinner, LV_ALIGN_BOTTOM_MID, 0, -30);
    lv_obj_set_style_arc_color(spinner, lv_color_hex(TEAL), LV_PART_INDICATOR);
    if (demo) {
        run_job(NULL);
        poll_job(NULL);
    } else if (pthread_create(&job.thread, NULL, run_job, NULL) != 0) {
        job.kind = NO_JOB;
        cJSON_Delete(request);
        message("操作未完成", "无法开始检查，请稍后重试。");
    }
}

static void load_systems(lv_event_t *e) {
    if (current != SYSTEMS) {
        system_page = 0;
        cJSON_Delete(systems);
        systems = NULL;
        render_page(SYSTEMS);
    }
    start_job(LOAD_SYSTEMS, operation("systems"));
}

static void prepare_switch(lv_event_t *e) {
    cJSON *q = operation("prepare_switch");
    cJSON_AddStringToObject(q, "system_id", lv_event_get_user_data(e));
    start_job(PLAN_SWITCH, q);
}

static void execute_switch(lv_event_t *e) {
    cJSON *q = operation("execute_switch");
    cJSON_AddStringToObject(q, "token", confirm_token);
    start_job(APPLY_SWITCH, q);
}

static lv_obj_t *one_line(lv_obj_t *parent, const char *text, int x, int y, int width,
                          const lv_font_t *font, uint32_t color) {
    lv_obj_t *line = label(parent, text, x, y, width, font, color);
    lv_obj_set_height(line, lv_font_get_line_height(font));
    lv_label_set_long_mode(line, LV_LABEL_LONG_MODE_DOTS);
    return line;
}

static void switch_confirmation(const cJSON *plan) {
    lv_obj_t *box = modal_box(404, 632);
    snprintf(confirm_token, sizeof(confirm_token), "%s", str(plan, "token"));
    label(box, "切换默认系统", 38, 35, 684, &ui_font_34, INK);
    lv_obj_t *before = panel(box, 38, 108, 684, 132, BG);
    label(before, "当前默认", 20, 16, 140, &ui_font_20, MUTED);
    one_line(before, str(plan, "previous_name"), 20, 48, 644, &ui_font_26, INK);
    label(before, str(plan, "previous_partition"), 20, 89, 644, &ui_font_20, MUTED);
    lv_obj_t *after = panel(box, 38, 254, 684, 132, 0xe4f4ef);
    label(after, "改为默认", 20, 16, 140, &ui_font_20, TEAL);
    one_line(after, str(plan, "target_name"), 20, 48, 644, &ui_font_26, INK);
    label(after, str(plan, "target_partition"), 20, 89, 644, &ui_font_20, MUTED);
    label(box, "确认后写入并回读 A 槽引导。\n切换完成后可启动新默认系统。",
          38, 415, 684, &ui_font_26, MUTED);
    lv_obj_t *cancel = button(box, "取消", NULL, 38, 510, 324, 80, close_modal, NULL);
    primary(button(box, "确认切换", NULL, 398, 510, 324, 80, execute_switch, NULL));
    lv_group_focus_obj(cancel);
}

static void poll_job(lv_timer_t *timer) {
    if (job.kind == NO_JOB || !atomic_load_explicit(&job.ready, memory_order_acquire)) return;
    if (!demo) pthread_join(job.thread, NULL);
    enum job_kind kind = job.kind;
    cJSON *reply = job.reply;
    job.kind = NO_JOB;
    close_modal(NULL);
    if (!reply) {
        if (kind != PLAN_SWITCH) {
            cJSON_Delete(systems);
            systems = NULL;
            render_page(SYSTEMS);
        }
        const char *error = job.error[0] ? job.error : "维护服务暂未响应。";
        message("操作未完成", error);
    } else if (kind == LOAD_SYSTEMS) {
        cJSON_Delete(systems);
        systems = reply;
        render_page(SYSTEMS);
        return;
    } else if (kind == PLAN_SWITCH) {
        switch_confirmation(reply);
    } else {
        cJSON *row;
        cJSON_ArrayForEach(row, cJSON_GetObjectItem(systems, "systems")) {
            bool selected = !strcmp(str(row, "id"), str(reply, "default_id"));
            cJSON_ReplaceItemInObject(row, "is_default", cJSON_CreateBool(selected));
        }
        render_page(SYSTEMS);
        char text[480];
        snprintf(text, sizeof(text), "%s 已设为默认系统。\n可在首页启动。", str(reply, "target_name"));
        message("切换完成", text);
    }
    cJSON_Delete(reply);
}
static void change_part_page(lv_event_t *e){int d=(int)(intptr_t)lv_event_get_user_data(e);if(d<0&&part_page)part_page--;else if(d>0&&(part_page+1)*12<(unsigned)cJSON_GetArraySize(parts))part_page++;render_page(PARTITIONS);}
static void change_log_page(lv_event_t *e){int d=(int)(intptr_t)lv_event_get_user_data(e);if(d<0&&log_page)log_page--;else if(d>0)log_page++;render_page(LOGS);}
static void switch_logs(lv_event_t *e){log_kind=!strcmp(log_kind,"kernel")?"ui":"kernel";log_page=0;render_page(LOGS);}

static const char *status_text(const cJSON *value) {
    if (!value) return "正在连接维护服务";
    const char *phase = str(value, "phase");
    if (!strcmp(phase, "installing")) return "电脑正在安装 · 请保持平板开启";
    if (!strcmp(phase, "write-failed")) return "写入未完成 · 请查看日志";
    if (!yes(value, "sealed")) return "存储保护未确认";
    if (!strcmp(phase, "reading") || !strcmp(phase, "connected")) return "电脑正在检查设备";
    return "存储只读 · 维护就绪";
}

static const char *usb_text(const char *state) {
    if (!strcmp(state, "connected") || !strcmp(state, "suspended")) return "USB 已连接";
    if (!strcmp(state, "disconnected")) return "USB 未连接";
    if (!strcmp(state, "connecting")) return "USB 连接中";
    if (!strcmp(state, "starting")) return "USB 准备中";
    return "USB 状态未知";
}

static void refresh_usb_status(lv_timer_t *timer) {
    lv_label_set_text(usb_label, usb_text(demo ? demo_usb_state : platform_usb_state()));
}

static void update_status_labels(void) {
    const char *battery = str(summary, "battery");
    if (!strchr(battery, '%')) battery = "--";
    lv_label_set_text(badge, status_text(summary));
    lv_label_set_text_fmt(battery_label, "电量 %s", battery);
    refresh_usb_status(NULL);
    if (device_details) {
        lv_label_set_text_fmt(device_details,
            "设备：小米平板 6 Pro\n\n序列号：%s\n\n当前槽位：%s\n\n内核：%s\n\n"
            "构建：%.16s\n\n电量：%s\n\n运行时间：%d 分钟\n\n可用内存：%.1f GiB\n\n存储保护：%s",
            str(summary, "serial"), str(summary, "slot"), str(summary, "kernel"), str(summary, "build_id"),
            battery, (int)(num(summary, "uptime_seconds")/60), num(summary, "memory_available_kib")/1048576.0,
            yes(summary, "sealed") ? "所有块设备只读" : "未确认");
    }
}

static void header(const char *title) {
    lv_obj_t *bar = panel(screen, 0, 0, WIDTH, HEADER_HEIGHT, 0x102b38);
    lv_obj_set_style_radius(bar, 0, 0);
    lv_obj_set_style_border_width(bar, 0, 0);
    label(bar, "LIUQIN   /   RECOVERY", 38, 25, 460, &ui_font_20, 0x81d9ce);
    usb_label = label(bar, "", 654, 25, 210, &ui_font_20, 0x9bebd7);
    lv_obj_set_style_text_align(usb_label, LV_TEXT_ALIGN_RIGHT, 0);
    label(bar, title, 38, 67, 610, &ui_font_34, 0xffffff);
    badge = label(bar, "", 38, 143, 610, &ui_font_20, 0x9bebd7);
    lv_obj_t *info = panel(bar, 670, 51, 194, 91, 0x1e4651);
    lv_obj_set_style_border_width(info, 0, 0);
    label(info, num(summary, "fixed_installer_version") == 1 ? "固定 Recovery" : "临时 Recovery",
          14, 11, 170, &ui_font_20, 0xd5e8ea);
    battery_label = label(info, "", 14, 46, 170, &ui_font_20, 0x9bebd7);
}

static void render_home(void) {
    label(body, "系统启动", 36, 35, 828, &ui_font_26, INK);
    lv_obj_t *launch = panel(body, 36, 93, 828, 340, 0xe4f4ef);
    primary(button(launch, "系统列表", yes(summary, "tablet_boot_switch")
                       ? "查看已安装系统，选择默认启动项" : "查看平板端切换支持状态",
                   20, 20, 786, 138, load_systems, NULL));
    button(launch, "启动默认系统", "退出 Recovery 并启动当前默认系统",
           20, 178, 786, 138, prepare_exit, "system");

    label(body, "设备维护", 36, 483, 828, &ui_font_26, INK);
    button(body, "维护工具", "设备信息、分区、日志和亮度", 36, 541, 398, 154,
           navigate, (void *)(uintptr_t)TOOLS);
    button(body, "重启与关机", "进入 Fastboot 或关闭平板", 466, 541, 398, 154,
           navigate, (void *)(uintptr_t)POWER);

}

static void render_tools(void) {
    button(body, "设备信息", "电量、内核与启动信息", 36, 152, 398, 146,
           navigate, (void *)(uintptr_t)DEVICE);
    button(body, "分区查看", "查看容量和只读状态", 466, 152, 398, 146,
           navigate, (void *)(uintptr_t)PARTITIONS);
    button(body, "运行日志", "查看内核与界面日志", 36, 318, 398, 146,
           navigate, (void *)(uintptr_t)LOGS);
    button(body, "导出日志", "准备诊断包，由电脑保存", 466, 318, 398, 146, export_logs, NULL);
    lv_obj_t *card = panel(body, 36, 512, 828, 138, 0xffffff);
    label(card, "屏幕亮度", 26, 24, 260, &ui_font_26, INK);
    brightness_label = label(card, "--", 26, 72, 200, &ui_font_26, TEAL);
    if (cJSON_IsNumber(cJSON_GetObjectItem(summary, "brightness")))
        lv_label_set_text_fmt(brightness_label, "%d%%", (int)num(summary, "brightness"));
    button(card, "降低", NULL, 394, 28, 182, 80, brightness, (void *)(intptr_t)-10);
    button(card, "提高", NULL, 606, 28, 182, 80, brightness, (void *)(intptr_t)10);
}

static void change_system_page(lv_event_t *e) {
    int next = (int)system_page + (int)(intptr_t)lv_event_get_user_data(e);
    if (next < 0 || next * SYSTEMS_PER_PAGE >= cJSON_GetArraySize(cJSON_GetObjectItem(systems, "systems")))
        return;
    system_page = (unsigned)next;
    render_page(SYSTEMS);
}

static void render_systems(void) {
    const cJSON *rows = cJSON_GetObjectItemCaseSensitive(systems, "systems");
    unsigned total = (unsigned)cJSON_GetArraySize(rows);
    bool can_switch = yes(systems, "can_switch");
    if (!total || system_page * SYSTEMS_PER_PAGE >= total) system_page = 0;
    label(body, "已安装系统", 36, 145, 828, &ui_font_26, INK);
    if (!total) {
        lv_obj_t *empty = panel(body, 36, 211, 828, 192, 0xffffff);
        label(empty, systems ? "暂无可列出的系统" : "系统列表待刷新", 26, 32, 776, &ui_font_26, INK);
        label(empty, systems ? str(systems, "message") : "点按刷新，读取系统登记。",
              26, 96, 776, &ui_font_20, MUTED);
        return;
    }
    label(body, can_switch ? "选择系统，核对引导后可将它设为默认。" : str(systems, "message"),
          36, 197, 828, &ui_font_20, MUTED);
    unsigned start = system_page * SYSTEMS_PER_PAGE;
    for (unsigned i = start; i < total && i < start + SYSTEMS_PER_PAGE; ++i) {
        const cJSON *system = cJSON_GetArrayItem(rows, (int)i);
        bool selected = yes(system, "is_default");
        int y = 256 + (int)(i - start) * 145;
        lv_obj_t *row = selected || !can_switch
            ? panel(body, 36, y, 828, 126, selected ? 0xe4f4ef : 0xffffff)
            : button(body, NULL, NULL, 36, y, 828, 126, prepare_switch, (void *)str(system, "id"));
        one_line(row, str(system, "name"), 24, 23, 580, &ui_font_26, INK);
        const char *filesystem = str(system, "filesystem");
        lv_obj_t *details = label(row, "", 24, 77, 605, &ui_font_20, MUTED);
        lv_label_set_text_fmt(details, "%s   ·   %s%s%s",
                              !strcmp(str(system, "type"), "android") ? "Android" : "Linux",
                              str(system, "partition"), *filesystem ? "   ·   " : "", filesystem);
        if (selected || can_switch) {
            lv_obj_t *tag = panel(row, 637, 35, 164, 54, selected ? 0xc7eae0 : 0xf0f4f5);
            lv_obj_set_clickable(tag, false);
            lv_obj_t *caption = label(tag, selected ? "当前默认" : "设为默认", 0, 12, 164, &ui_font_20, TEAL);
            lv_obj_set_style_text_align(caption, LV_TEXT_ALIGN_CENTER, 0);
        }
    }
    unsigned pages = (total + SYSTEMS_PER_PAGE - 1) / SYSTEMS_PER_PAGE;
    if (pages > 1) {
        lv_obj_t *count = label(body, "", 330, 1034, 240, &ui_font_20, MUTED);
        lv_obj_set_style_text_align(count, LV_TEXT_ALIGN_CENTER, 0);
        lv_label_set_text_fmt(count, "第 %u / %u 页", system_page+1, pages);
        if (system_page)
            button(body, "上一页", NULL, 36, 1007, 260, 80, change_system_page, (void *)(intptr_t)-1);
        if (system_page+1 < pages)
            button(body, "下一页", NULL, 604, 1007, 260, 80, change_system_page, (void *)(intptr_t)1);
    }
}
static void render_page(enum page page){
    if(modal)close_modal(NULL);
    current=page;lv_group_remove_all_objs(focus);brightness_label=NULL;
    device_details = NULL;
    if(screen)lv_obj_clean(screen);else screen=lv_screen_active();
    lv_obj_set_style_bg_color(screen,lv_color_hex(BG),0);lv_obj_set_style_text_font(screen,&ui_font_20,0);lv_obj_set_scrollable(screen,false);
    char error[600]={0};cJSON *s=rpc(operation("status"),error,sizeof(error));if(s){cJSON_Delete(summary);summary=s;}
    static const char *titles[] = {"恢复与维护", "系统列表", "维护工具", "设备信息",
                                   "分区查看", "运行日志", "重启与关机"};
    header(titles[page]);
    body=lv_obj_create(screen);lv_obj_set_pos(body,0,HEADER_HEIGHT);lv_obj_set_size(body,WIDTH,1150);lv_obj_set_style_pad_all(body,0,0);
    lv_obj_set_style_bg_opa(body,LV_OPA_TRANSP,0);lv_obj_set_style_border_width(body,0,0);lv_obj_set_scrollable(body,false);
    if(page==HOME){
        render_home();
    }else{
        enum page back = parent_page(page);
        button(body,back==HOME?"返回首页":"返回维护工具",NULL,36,28,245,76,navigate,(void *)(uintptr_t)back);
        if (page != TOOLS && page != POWER)
            button(body,"刷新",NULL,654,28,210,76,refresh,NULL);
        if (page == SYSTEMS) {
            render_systems();
        } else if (page == TOOLS) {
            render_tools();
        } else if(page==DEVICE){
            lv_obj_t *card = panel(body, 36, 142, 828, 800, 0xffffff);
            device_details = label(card, "", 30, 30, 768, &ui_font_26, INK);
        }else if(page==PARTITIONS){
            cJSON *r=rpc(operation("partitions"),error,sizeof(error));if(r){cJSON_Delete(parts);parts=cJSON_DetachItemFromObjectCaseSensitive(r,"partitions");cJSON_Delete(r);}
            unsigned total=(unsigned)cJSON_GetArraySize(parts);if(part_page*12>=total)part_page=0;
            lv_obj_t *card=panel(body,36,142,828,780,0xffffff);
            label(card,"名称",24,22,390,&ui_font_20,MUTED);label(card,"容量",430,22,220,&ui_font_20,MUTED);label(card,"状态",686,22,110,&ui_font_20,MUTED);
            for(unsigned i=part_page*12;i<total&&i<(part_page+1)*12;i++){
                cJSON *p=cJSON_GetArrayItem(parts,(int)i);int y=74+(int)(i%12)*55;char size[48];snprintf(size,sizeof(size),"%.2f GiB",num(p,"bytes")/1073741824.0);
                label(card,str(p,"name"),24,y,390,&ui_font_20,INK);label(card,size,430,y,220,&ui_font_20,MUTED);label(card,yes(p,"readonly")?"只读":"可写",686,y,110,&ui_font_20,yes(p,"readonly")?TEAL:0xb34e35);
            }
            char page_text[80];snprintf(page_text,sizeof(page_text),"第 %u / %u 页 · 共 %u 个分区",part_page+1,total?(total+11)/12:1,total);label(body,page_text,40,946,820,&ui_font_20,MUTED);
            button(body,"上一页",NULL,36,1000,396,80,change_part_page,(void *)(intptr_t)-1);button(body,"下一页",NULL,468,1000,396,80,change_part_page,(void *)(intptr_t)1);
        }else if(page==LOGS){
            cJSON *q=operation("logs");cJSON_AddStringToObject(q,"kind",log_kind);cJSON *r=rpc(q,error,sizeof(error));if(r){free(log_text);log_text=strdup(str(r,"text"));cJSON_Delete(r);}
            lv_obj_t *card=panel(body,36,142,828,760,0xffffff);char shown[10000]={0};size_t used=0;unsigned lines=0;
            const char *p=log_text?log_text:"";while(*p){const char *end=strchr(p,'\n');size_t n=end?(size_t)(end-p):strlen(p);if(lines/22==log_page&&used+n+2<sizeof(shown)){memcpy(shown+used,p,n);used+=n;shown[used++]='\n';}lines++;if(!end)break;p=end+1;}
            if(log_page&&log_page*22>=lines){log_page=0;render_page(LOGS);return;}
            lv_obj_set_scroll_dir(card, LV_DIR_VER);
            lv_obj_set_scrollable(card, true);
            label(card,shown,24,22,780,&ui_font_20,INK);
            button(body,"上一页",NULL,36,930,250,76,change_log_page,(void *)(intptr_t)-1);button(body,"下一页",NULL,325,930,250,76,change_log_page,(void *)(intptr_t)1);
            button(body,!strcmp(log_kind,"kernel")?"界面日志":"内核日志",NULL,614,930,250,76,switch_logs,NULL);
            button(body,"导出日志",NULL,36,1034,828,76,export_logs,NULL);
        }else{
            label(body,"请选择退出方式。",40,150,820,&ui_font_20,MUTED);
            button(body,"重启到默认系统","启动当前已经设置的 Android 或 Linux",36,228,828,158,prepare_exit,"system");
            button(body,"重启到 Fastboot","回到 Bootloader，继续电脑端维护",36,420,828,158,prepare_exit,"fastboot");
            button(body,"关机","结束维护并关闭平板",36,612,828,158,prepare_exit,"poweroff");
            label(body,"电脑正在检查设备时，退出操作会被阻止。",40,824,820,&ui_font_20,MUTED);
        }
    }
    update_status_labels();
    label(screen,platform_input_label(),36,1380,828,&ui_font_20,MUTED);
    lv_group_focus_obj(lv_group_get_obj_by_index(focus,0));
    if(error[0])message("连接状态",error);
}
static void refresh_status(lv_timer_t *timer){
    if(modal)return;
    char err[600]={0};cJSON *r=rpc(operation("status"),err,sizeof(err));if(!r){lv_label_set_text(badge,"维护服务暂未连接");return;}
    cJSON_Delete(summary);summary=r;
    update_status_labels();
}
int main(int argc,char **argv){
    const char *preview=NULL,*page="home";
    for(int i=1;i<argc;i++){
        if(!strcmp(argv[i],"--demo"))demo=true;
        else if(!strcmp(argv[i],"--preview")&&i+1<argc)preview=argv[++i];
        else if(!strcmp(argv[i],"--page")&&i+1<argc)page=argv[++i];
        else {fprintf(stderr,"usage: [--demo --preview FILE --page home|systems|switch|tools|device|partitions|logs|power|confirm]\n");return 64;}
    }
    if((preview&&!demo)||(demo&&!preview))return 64;
    signal(SIGINT,stop);signal(SIGTERM,stop);signal(SIGPIPE,SIG_IGN);
    lv_init();if(!platform_display(demo)){platform_close();return 2;}
    focus=lv_group_create();lv_group_set_default(focus);platform_inputs(focus);
    enum page first = HOME;
    if (!strcmp(page, "device")) first = DEVICE;
    else if (!strcmp(page, "partitions")) first = PARTITIONS;
    else if (!strcmp(page, "logs")) first = LOGS;
    else if (!strcmp(page, "power") || !strcmp(page, "confirm")) first = POWER;
    else if (!strcmp(page, "tools")) first = TOOLS;
    if (preview && (!strcmp(page, "systems") || !strcmp(page, "switch"))) load_systems(NULL);
    else render_page(first);
    lv_timer_create(refresh_status,2000,NULL);
    lv_timer_create(refresh_usb_status,500,NULL);
    lv_timer_create(poll_job,100,NULL);
    if(preview){
        if(!strcmp(page,"confirm")){
            /* Use the same event path as a physical click, but demo RPC has no side effects. */
            lv_obj_t *target=lv_group_get_obj_by_index(focus,1);lv_obj_send_event(target,LV_EVENT_CLICKED,NULL);
        }
        if (!strcmp(page, "switch"))
            lv_obj_send_event(lv_group_get_obj_by_index(focus, 2), LV_EVENT_CLICKED, NULL);
        for(unsigned i=0;i<8;i++){lv_timer_handler();usleep(40000);}
        bool ok=platform_save(preview);cJSON_Delete(summary);cJSON_Delete(parts);cJSON_Delete(systems);free(log_text);lv_deinit();platform_close();return ok?0:3;
    }
    while(running){platform_poll();lv_timer_handler();usleep(15000);}
    cJSON_Delete(summary);cJSON_Delete(parts);cJSON_Delete(systems);free(log_text);lv_deinit();platform_close();return 0;
}
