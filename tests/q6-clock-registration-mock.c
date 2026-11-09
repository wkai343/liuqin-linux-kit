/* Test the actual clock driver with synchronous CCF orphan reparenting. */
#pragma GCC diagnostic ignored "-Wsign-compare"

#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <errno.h>
#include <stdio.h>
#define GFP_KERNEL 0
#define LPASS_CLK_ATTRIBUTE_COUPLE_DIVISOR 4
#define EXPORT_SYMBOL_GPL(x)
#define container_of(ptr,type,member) ((type *)((char *)(ptr)-offsetof(type,member)))
#define ERR_PTR(n) ((void *)(intptr_t)(n))
#define dev_err(...) ((void)0)
struct device { void *drvdata; const void *match; };
struct platform_device { struct device dev; };
struct clk_init_data;
struct clk_hw { const struct clk_init_data *init; };
struct clk_rate_request { int ignored; };
struct clk_ops {
 int (*prepare)(struct clk_hw *);
 void (*unprepare)(struct clk_hw *);
 int (*set_rate)(struct clk_hw *,unsigned long,unsigned long);
 int (*determine_rate)(struct clk_hw *,struct clk_rate_request *);
 unsigned long (*recalc_rate)(struct clk_hw *,unsigned long);
};
struct clk_init_data { const char *name; const struct clk_ops *ops; };
struct of_phandle_args { unsigned int args[2]; };
static void *allocated[256];static int nalloc,registrations,fail_registration,provider_error,calls;
static void *devm_kzalloc(struct device *dev,size_t size,int flags) {
 void *p=calloc(1,size);assert(p);allocated[nalloc++]=p;return p;
}
static const void *of_device_get_match_data(struct device *dev) { return dev->match; }
static void *dev_get_drvdata(struct device *dev) { return dev->drvdata; }
static void dev_set_drvdata(struct device *dev,void *data) { dev->drvdata=data; }
static const char *clk_hw_get_name(struct clk_hw *hw) { return hw->init->name; }
static int devm_clk_hw_register(struct device *dev,struct clk_hw *hw) {
 registrations++;
 if (registrations==fail_registration) return -EIO;
 /* CCF can immediately prepare a newly reparented orphan's parent. */
 int rc=hw->init->ops->prepare(hw);
 assert(rc==0);hw->init->ops->unprepare(hw);return 0;
}
static int devm_of_clk_add_hw_provider(struct device *dev,
 struct clk_hw *(*get)(struct of_phandle_args *,void *),void *data) {
 assert(data==dev_get_drvdata(dev));return provider_error;
}

/* Q6DSP_PUBLIC_HEADER */
/* Q6DSP_DRIVER */

static int set_clk(struct device *dev,int id,int attr,int root,unsigned int freq) {
 assert(dev_get_drvdata(dev));calls++;return 0;
}
static int vote_clk(struct device *dev,uint32_t hid,const char *name,uint32_t *handle) {
 assert(dev_get_drvdata(dev));*handle=12;calls++;return 0;
}
static int unvote_clk(struct device *dev,uint32_t hid,uint32_t handle) {
 assert(dev_get_drvdata(dev));assert(handle==12);calls++;return 0;
}
static void reset(void) {
 for(int i=0;i<nalloc;i++)free(allocated[i]);
 nalloc=registrations=fail_registration=provider_error=calls=0;
}
int main(void) {
 struct q6dsp_clk_init clocks[]={
  {.clk_id=1,.q6dsp_clk_id=100,.name="rate",.rate=19200000},
  {.clk_id=2,.q6dsp_clk_id=101,.name="vote",.rate=0}};
 struct q6dsp_clk_desc desc={.clks=clocks,.num_clks=2,
  .lpass_set_clk=set_clk,.lpass_vote_clk=vote_clk,.lpass_unvote_clk=unvote_clk};
 struct platform_device p={.dev={.match=&desc}};
 assert(q6dsp_clock_dev_probe(&p)==0);assert(calls==4);reset();
 p.dev.drvdata=NULL;fail_registration=2;
 assert(q6dsp_clock_dev_probe(&p)==-EIO);assert(calls==2);reset();
 p.dev.drvdata=NULL;provider_error=-ENOMEM;
 assert(q6dsp_clock_dev_probe(&p)==-ENOMEM);assert(calls==4);reset();
 p.dev.drvdata=NULL;p.dev.match=NULL;
 assert(q6dsp_clock_dev_probe(&p)==-EINVAL);assert(calls==0);reset();
 return 0;
}
