/* SPDX-License-Identifier: MIT */
#ifndef LIUQIN_TOUCH_INPUT_H
#define LIUQIN_TOUCH_INPUT_H
#include <stdbool.h>
#include <linux/input.h>
#define TOUCH_SLOTS 32
struct touch_slot { int id,x,y; };
struct touch_input {
    bool mt,blocked,dropped,down;
    int slot,primary,tracking_id,x,y;
    struct touch_slot slots[TOUCH_SLOTS];
};
struct touch_sample { int x,y;bool down; };
enum touch_result { TOUCH_NONE,TOUCH_SAMPLE,TOUCH_CANCEL,TOUCH_RESYNC };
void touch_init(struct touch_input *t,bool mt);
enum touch_result touch_feed(struct touch_input *t,const struct input_event *e,struct touch_sample *sample);
#endif
