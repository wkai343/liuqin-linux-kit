/* SPDX-License-Identifier: MIT */
#define main recovery_ui_program_main
#include "../device/recovery/ui/main.c"
#undef main
#define CHECK(x) do { if(!(x)){fprintf(stderr,"UI test failed: %s at %d\n",#x,__LINE__);return 1;} } while(0)
int main(void){
    demo=true;lv_init();CHECK(platform_display(true));focus=lv_group_create();lv_group_set_default(focus);platform_inputs(focus);
    /* USB connection captions must remain legible in the bundled subset. */
    const uint32_t usb_glyphs[] = {0x5df2,0x672a,0x8fde,0x63a5,0x4e2d,0x51c6,0x5907,0x72b6,0x6001,0x77e5};
    for(unsigned i=0;i<sizeof(usb_glyphs)/sizeof(usb_glyphs[0]);i++){
        lv_font_glyph_dsc_t glyph;
        CHECK(lv_font_get_glyph_dsc(&ui_font_20,&glyph,usb_glyphs[i],0)&&!glyph.is_placeholder);
    }
    demo_battery = "unavailable";
    render_page(HOME);
    CHECK(!strcmp(lv_label_get_text(battery_label), "电量 --"));
    lv_obj_t *initial_battery_label = battery_label;
    demo_battery = "85%";
    refresh_status(NULL);
    CHECK(current == HOME && battery_label == initial_battery_label);
    CHECK(!strcmp(lv_label_get_text(battery_label), "电量 85%"));
    CHECK(!strcmp(lv_label_get_text(usb_label), "USB 已连接"));
    lv_obj_t *initial_usb_label = usb_label;
    demo_usb_state = "disconnected";
    refresh_status(NULL);
    CHECK(current == HOME && usb_label == initial_usb_label);
    CHECK(!strcmp(lv_label_get_text(usb_label), "USB 未连接"));
    demo_usb_state = "starting";
    refresh_status(NULL);
    CHECK(!strcmp(lv_label_get_text(usb_label), "USB 准备中"));
    demo_usb_state = "unknown";
    refresh_status(NULL);
    CHECK(!strcmp(lv_label_get_text(usb_label), "USB 状态未知"));
    demo_usb_state = "connected";

    render_page(DEVICE);
    demo_battery = "84%";
    refresh_status(NULL);
    CHECK(current == DEVICE && strstr(lv_label_get_text(device_details), "电量：84%"));
    CHECK(!strcmp(lv_label_get_text(battery_label), "电量 84%"));
    demo_battery = "86%";
    for(int i=0;i<30;i++){
        render_page(HOME);CHECK(current==HOME);CHECK(lv_group_get_obj_count(focus)==4);
        lv_obj_send_event(lv_group_get_obj_by_index(focus,2),LV_EVENT_CLICKED,NULL);CHECK(current==TOOLS);
        CHECK(lv_group_get_obj_count(focus)==7);
        lv_obj_send_event(lv_group_get_obj_by_index(focus,1),LV_EVENT_CLICKED,NULL);CHECK(current==DEVICE);
        lv_obj_send_event(lv_group_get_obj_by_index(focus,0),LV_EVENT_CLICKED,NULL);CHECK(current==TOOLS);
        lv_obj_send_event(lv_group_get_obj_by_index(focus,0),LV_EVENT_CLICKED,NULL);CHECK(current==HOME);
        lv_obj_send_event(lv_group_get_obj_by_index(focus,0),LV_EVENT_CLICKED,NULL);CHECK(current==SYSTEMS);
        CHECK(cJSON_GetArraySize(cJSON_GetObjectItem(systems,"systems"))==3);
        lv_obj_send_event(lv_group_get_obj_by_index(focus,2),LV_EVENT_CLICKED,NULL);
        CHECK(modal&&lv_group_get_obj_count(modal_focus)==2);
        CHECK(lv_group_get_focused(modal_focus)==lv_group_get_obj_by_index(modal_focus,0));
        lv_obj_send_event(lv_group_get_focused(modal_focus),LV_EVENT_CLICKED,NULL);
        CHECK(modal==NULL&&confirm_token[0]==0&&demo_default==0);
        lv_obj_send_event(lv_group_get_obj_by_index(focus,0),LV_EVENT_CLICKED,NULL);CHECK(current==HOME);
        render_page(POWER);
        lv_obj_send_event(lv_group_get_obj_by_index(focus,1),LV_EVENT_CLICKED,NULL);
        CHECK(modal!=NULL&&modal_focus!=NULL);CHECK(lv_group_get_obj_count(modal_focus)==2);
        demo_usb_state="disconnected";
        refresh_usb_status(NULL);
        CHECK(modal!=NULL&&!strcmp(lv_label_get_text(usb_label),"USB 未连接"));
        demo_usb_state="connected";
        refresh_usb_status(NULL);
        CHECK(modal!=NULL&&!strcmp(lv_label_get_text(usb_label),"USB 已连接"));
        CHECK(lv_group_get_focused(modal_focus)==lv_group_get_obj_by_index(modal_focus,0));
        lv_group_focus_next(modal_focus);CHECK(lv_group_get_focused(modal_focus)==lv_group_get_obj_by_index(modal_focus,1));
        lv_group_focus_next(modal_focus);CHECK(lv_group_get_focused(modal_focus)==lv_group_get_obj_by_index(modal_focus,0));
        lv_obj_send_event(lv_group_get_focused(modal_focus),LV_EVENT_CLICKED,NULL);CHECK(modal==NULL&&confirm_token[0]==0);
        render_page(PARTITIONS);render_page(LOGS);
    }
    render_page(POWER);lv_obj_send_event(lv_group_get_obj_by_index(focus,1),LV_EVENT_CLICKED,NULL);
    lv_obj_send_event(lv_group_get_obj_by_index(modal_focus,1),LV_EVENT_CLICKED,NULL);
    CHECK(modal&&lv_group_get_obj_count(modal_focus)==1);CHECK(confirm_token[0]==0);
    close_modal(NULL);
    load_systems(NULL);
    lv_obj_send_event(lv_group_get_obj_by_index(focus,2),LV_EVENT_CLICKED,NULL);
    lv_obj_send_event(lv_group_get_obj_by_index(modal_focus,1),LV_EVENT_CLICKED,NULL);
    CHECK(modal&&current==SYSTEMS&&demo_default==1);
    cJSON *row=cJSON_GetArrayItem(cJSON_GetObjectItem(systems,"systems"),1);
    CHECK(yes(row,"is_default"));
    close_modal(NULL);cJSON_Delete(summary);cJSON_Delete(parts);cJSON_Delete(systems);free(log_text);lv_deinit();platform_close();
    puts("PASS: live battery refresh, system list and switch, power, maintenance navigation and default cancel");return 0;
}
