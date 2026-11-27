#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the migrated first-frame path with Linux 7.3 follower callbacks."""
from pathlib import Path
import argparse
import ast
import json
import re
import subprocess
import tempfile


def function(text, name):
    match = re.search(r'^(?:static )?(?:unsigned int|int|void|bool)\s+' + name + r'\([^;{]*\)\s*\{', text, re.M)
    if not match:
        raise ValueError('function missing: '+name)
    end=match.end();depth=1
    while depth:
        depth+=(text[end]=='{')-(text[end]=='}');end+=1
    return text[match.start():end]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    fixtures={}
    tree=ast.parse(Path(__file__).with_name('display-first-frame.py').read_text())
    for node in ast.walk(tree):
        if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name) and node.targets[0].id in ('stubs','cases'):
            fixtures[node.targets[0].id]=ast.literal_eval(node.value)
    panel=(args.source/'drivers/gpu/drm/drm_panel.c').read_text()
    bridge=(args.source/'drivers/gpu/drm/bridge/panel.c').read_text()
    kms=(args.source/'drivers/gpu/drm/msm/disp/dpu1/dpu_kms.c').read_text()
    helpers='\n'.join(function(panel,n) for n in ('drm_panel_enable_backlight','drm_panel_enable','drm_panel_disable','drm_panel_get_first_frame_cookie','drm_panel_complete_first_frame'))
    helpers+='\n'+'\n'.join(function(bridge,n) for n in ('drm_bridge_is_panel','drm_panel_bridge_get_first_frame_cookie','drm_panel_bridge_complete_first_frame'))
    helpers+='\n'+function(kms,'dpu_kms_complete_panel_first_frame')
    stubs=fixtures['stubs'].replace('#define dev_info(dev,...)', '#define list_for_each_entry(p,head,member) for((p)=*(head);(p);(p)=(p)->next)\n#define dev_info(dev,...)')
    stubs=stubs.replace('struct drm_panel;','''struct drm_panel;
struct drm_panel_follower;
struct drm_panel_follower_funcs {
    int (*panel_enabled)(struct drm_panel_follower *);
    int (*panel_disabling)(struct drm_panel_follower *);
};
struct drm_panel_follower {
    const struct drm_panel_follower_funcs *funcs;
    struct drm_panel_follower *next;
};''')
    stubs=stubs.replace('int (*disable)(struct drm_panel *);','int (*disable)(struct drm_panel *);int (*post_backlight_enable)(struct drm_panel *);')
    stubs=stubs.replace('unsigned int backlight_cookie;struct mutex backlight_lock;',
        'unsigned int backlight_cookie;struct mutex backlight_lock, follower_lock;struct drm_panel_follower *followers;')
    stubs=stubs.replace('funcs={driver_enable,driver_disable}','funcs={.enable=driver_enable,.disable=driver_disable}')
    stubs=stubs.replace('for((b)=(e)->first;(b);(b)=(b)->next)', 'for(struct drm_bridge *b=(e)->first;b;b=b->next)')
    cases=fixtures['cases'].replace('assert(!pthread_mutex_init(&p.backlight_lock.value,NULL));',
        'assert(!pthread_mutex_init(&p.backlight_lock.value,NULL));assert(!pthread_mutex_init(&p.follower_lock.value,NULL));')
    cases=cases.replace('assert(!pthread_mutex_destroy(&p.backlight_lock.value));',
        'assert(!pthread_mutex_destroy(&p.backlight_lock.value));assert(!pthread_mutex_destroy(&p.follower_lock.value));')
    cases=cases.replace('int main(void){','''static int follower_enabled, follower_disabling;
static int on_enabled(struct drm_panel_follower *f) { (void)f; follower_enabled++; return 0; }
static int on_disabling(struct drm_panel_follower *f) { (void)f; follower_disabling++; return 0; }
static const struct drm_panel_follower_funcs follower_ops={on_enabled,on_disabling};
static struct drm_panel_follower follower={.funcs=&follower_ops};
int main(void){''')
    cases=cases.replace('printf("%d\\n",cases);','''setup(true);p.followers=&follower;drm_panel_enable(&p);
    assert(follower_enabled==1 && !bl.on && p.backlight_pending);
    assert(!pthread_mutex_trylock(&p.follower_lock.value));assert(!pthread_mutex_unlock(&p.follower_lock.value));
    dpu_kms_complete_panel_first_frame(&encoder);assert(bl.on && follower_enabled==1);
    drm_panel_disable(&p);assert(follower_disabling==1 && !bl.on);finish();
    setup(true);p.followers=&follower;enable_result=-EIO;drm_panel_enable(&p);
    assert(follower_enabled==1 && !p.enabled);
    assert(!pthread_mutex_trylock(&p.follower_lock.value));assert(!pthread_mutex_unlock(&p.follower_lock.value));finish();
    printf("%d\\n",cases);''')
    with tempfile.TemporaryDirectory(prefix='kernel73-first-frame-') as tmp:
        root=Path(tmp);source=root/'test.c';source.write_text(stubs+helpers+cases)
        subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-pthread',str(source),'-o',str(root/'test')],check=True)
        count=int(subprocess.check_output([str(root/'test')],text=True,timeout=20))
    result={'cases':count,'actual_migrated_functions_executed':True,'follower_callbacks_preserved':True,
            'deferred_and_failed_enable_release_follower_lock':True,'cancellation_and_concurrent_disable_passed':True}
    if args.report:args.report.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
