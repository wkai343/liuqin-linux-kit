/* SPDX-License-Identifier: MIT */
#define _POSIX_C_SOURCE 200809L
#include "battery.h"
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static atomic_int percent=-1;
static pthread_t worker;
static pthread_mutex_t lock=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed;
static bool started,stopping;

int recovery_battery_read(const char *directory){
    char path[512],text[32],*end;
    if(snprintf(path,sizeof(path),"%s/capacity",directory)>=(int)sizeof(path))return -1;
    FILE *f=fopen(path,"r");if(!f)return -1;
    size_t n=fread(text,1,sizeof(text)-1,f);
    bool ok=!ferror(f)&&feof(f);fclose(f);text[n]=0;
    if(!ok||!n||text[0]<'0'||text[0]>'9')return -1;
    errno=0;long value=strtol(text,&end,10);
    if(errno||value<0||value>100)return -1;
    if(*end=='\n')end++;
    return *end?-1:(int)value;
}
unsigned recovery_battery_interval(int value){return value>=0&&value<=100?2000:200;}
static void *sample(void *unused){
    (void)unused;bool first=true;
    for(;;){
        int value=recovery_battery_read("/sys/class/power_supply/qcom-battmgr-bat");
        atomic_store(&percent,value);
        struct timespec now;clock_gettime(CLOCK_MONOTONIC,&now);
        if(first&&value>=0){
            fprintf(stderr,"Recovery battery first valid: %d at %lld.%03ld\n",value,(long long)now.tv_sec,now.tv_nsec/1000000);
            first=false;
        }
        unsigned ms=recovery_battery_interval(value);
        now.tv_sec+=ms/1000;now.tv_nsec+=(ms%1000)*1000000L;
        if(now.tv_nsec>=1000000000L){now.tv_sec++;now.tv_nsec-=1000000000L;}
        pthread_mutex_lock(&lock);
        while(!stopping){
            int ret=pthread_cond_timedwait(&changed,&lock,&now);
            if(ret==ETIMEDOUT)break;
            if(ret) {stopping=true;break;}
        }
        bool stop=stopping;pthread_mutex_unlock(&lock);
        if(stop)break;
    }
    return NULL;
}
void recovery_battery_start(void){
    if(started)return;
    pthread_condattr_t attr;
    if(pthread_condattr_init(&attr))return;
    int ret=pthread_condattr_setclock(&attr,CLOCK_MONOTONIC);
    if(!ret)ret=pthread_cond_init(&changed,&attr);
    pthread_condattr_destroy(&attr);if(ret)return;
    stopping=false;atomic_store(&percent,-1);
    ret=pthread_create(&worker,NULL,sample,NULL);
    if(ret){pthread_cond_destroy(&changed);return;}
    started=true;
}
int recovery_battery_percent(void){return atomic_load(&percent);}
void recovery_battery_stop(void){
    if(!started)return;
    pthread_mutex_lock(&lock);stopping=true;pthread_cond_signal(&changed);pthread_mutex_unlock(&lock);
    pthread_join(worker,NULL);pthread_cond_destroy(&changed);started=false;
}
