/* SPDX-License-Identifier: MIT */
#include "touch_input.h"
#include <stdio.h>
#include <stdlib.h>
#define CHECK(x) do {if(!(x)){fprintf(stderr,"touch input failure at line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
static struct touch_input t;static struct touch_sample s;
static enum touch_result event(int type,int code,int value){struct input_event e={.type=type,.code=code,.value=value};return touch_feed(&t,&e,&s);}
static void finger(int slot,int id,int x,int y){event(EV_ABS,ABS_MT_SLOT,slot);event(EV_ABS,ABS_MT_TRACKING_ID,id);event(EV_ABS,ABS_MT_POSITION_X,x);event(EV_ABS,ABS_MT_POSITION_Y,y);}
static enum touch_result frame(void){return event(EV_SYN,SYN_REPORT,0);}
int main(void){
 touch_init(&t,true);finger(3,11,120,240);CHECK(frame()==TOUCH_SAMPLE&&s.down&&s.x==120&&s.y==240);
 event(EV_ABS,ABS_MT_TRACKING_ID,-1);CHECK(frame()==TOUCH_SAMPLE&&!s.down&&s.x==120);
 CHECK(frame()==TOUCH_NONE);
 finger(7,12,400,500);CHECK(frame()==TOUCH_SAMPLE&&s.down&&s.x==400);
 finger(1,13,700,800);CHECK(frame()==TOUCH_NONE);
 event(EV_ABS,ABS_MT_SLOT,7);event(EV_ABS,ABS_MT_TRACKING_ID,-1);CHECK(frame()==TOUCH_SAMPLE&&!s.down);
 event(EV_ABS,ABS_MT_SLOT,1);event(EV_ABS,ABS_MT_POSITION_X,750);CHECK(frame()==TOUCH_NONE);
 event(EV_ABS,ABS_MT_TRACKING_ID,-1);CHECK(frame()==TOUCH_NONE&&!t.blocked);
 finger(1,14,300,600);CHECK(frame()==TOUCH_SAMPLE&&s.down);
 CHECK(event(EV_SYN,SYN_DROPPED,0)==TOUCH_CANCEL);
 CHECK(event(EV_ABS,ABS_MT_TRACKING_ID,-1)==TOUCH_NONE);CHECK(frame()==TOUCH_RESYNC);
 touch_init(&t,true);t.blocked=true;finger(2,21,800,900);CHECK(frame()==TOUCH_NONE);
 event(EV_ABS,ABS_MT_TRACKING_ID,-1);CHECK(frame()==TOUCH_NONE&&!t.blocked);
 finger(2,22,400,700);CHECK(frame()==TOUCH_SAMPLE&&s.down);
 finger(2,23,600,900);CHECK(frame()==TOUCH_SAMPLE&&!s.down&&t.blocked);
 touch_init(&t,false);event(EV_ABS,ABS_X,100);event(EV_ABS,ABS_Y,200);CHECK(frame()==TOUCH_NONE);
 event(EV_KEY,BTN_TOUCH,1);CHECK(frame()==TOUCH_SAMPLE&&s.down&&s.x==100&&s.y==200);
 event(EV_KEY,BTN_TOUCH,0);CHECK(frame()==TOUCH_SAMPLE&&!s.down);
 puts("PASS: quick taps, arbitrary MT slots, second-finger isolation, event loss, held-contact suppression and pen hover");
 return 0;
}
