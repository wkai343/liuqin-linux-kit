/* SPDX-License-Identifier: MIT */
#include "touch_input.h"
#include <string.h>
void touch_init(struct touch_input *t,bool mt){
    memset(t,0,sizeof(*t));t->mt=mt;t->primary=-1;
    for(int i=0;i<TOUCH_SLOTS;i++)t->slots[i].id=-1;
}
enum touch_result touch_feed(struct touch_input *t,const struct input_event *e,struct touch_sample *s){
    if(e->type==EV_SYN&&e->code==SYN_DROPPED){t->dropped=true;return TOUCH_CANCEL;}
    if(t->dropped)return e->type==EV_SYN&&e->code==SYN_REPORT?TOUCH_RESYNC:TOUCH_NONE;
    if(e->type==EV_ABS){
        if(t->mt&&e->code==ABS_MT_SLOT)t->slot=e->value;
        int slot=t->mt?t->slot:0;
        if(slot>=0&&slot<TOUCH_SLOTS){
            struct touch_slot *p=&t->slots[slot];
            if(t->mt&&e->code==ABS_MT_TRACKING_ID)p->id=e->value;
            if(e->code==(t->mt?ABS_MT_POSITION_X:ABS_X))p->x=e->value;
            if(e->code==(t->mt?ABS_MT_POSITION_Y:ABS_Y))p->y=e->value;
        }
    }
    if(e->type==EV_KEY&&e->code==BTN_TOUCH&&!t->mt)t->slots[0].id=e->value?0:-1;
    if(e->type!=EV_SYN||e->code!=SYN_REPORT)return TOUCH_NONE;
    int first=-1;
    for(int i=0;i<TOUCH_SLOTS;i++)if(t->slots[i].id>=0){first=i;break;}
    if(t->blocked){if(first<0)t->blocked=false;return TOUCH_NONE;}
    bool was_down=t->down;int old_x=t->x,old_y=t->y;
    if(t->primary>=0&&t->slots[t->primary].id!=t->tracking_id){
        t->primary=-1;t->down=false;t->blocked=first>=0;
        /* Do not jump to a second finger or reuse a slot within one gesture. */
    }else if(t->primary<0&&first>=0){
        t->primary=first;t->tracking_id=t->slots[first].id;t->down=true;
    }
    if(t->down){t->x=t->slots[t->primary].x;t->y=t->slots[t->primary].y;}
    if(t->down!=was_down||(t->down&&(t->x!=old_x||t->y!=old_y))){
        *s=(struct touch_sample){t->x,t->y,t->down};return TOUCH_SAMPLE;
    }
    return TOUCH_NONE;
}
