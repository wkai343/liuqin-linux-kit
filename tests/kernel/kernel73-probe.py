#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute migrated GPIO metadata and CAMCC domain selection against kernel contracts."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile


def function(text, name):
    match = re.search(r'^(?:static )?(?:bool|void)\s+' + name + r'\([^;{]*\)\s*\{', text, re.M)
    if not match:
        raise ValueError('function missing: ' + name)
    end, depth = match.end(), 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[match.start():end]


def run_c(root, name, code):
    source = root / (name + '.c')
    source.write_text(code)
    executable = root / name
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=undefined', '-fno-sanitize-recover=all',
                    str(source), '-o', str(executable)], check=True)
    return int(subprocess.check_output([str(executable)], text=True, timeout=10))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, help='Optional first-trial source snapshot for negative controls')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    source = args.source
    common = '#include <assert.h>\n#include <stdbool.h>\n#include <stddef.h>\n#include <stdio.h>\n#define BIT(n) (1UL << (n))\n#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))\n'
    header = (source / 'include/linux/pinctrl/pinctrl.h').read_text()
    gpio = common + header[header.index('#define PINFUNCTION_FLAG_GPIO'):header.index('#if IS_ENABLED(CONFIG_OF) && IS_ENABLED(CONFIG_PINCTRL)', header.index('#define PINFUNCTION_FLAG_GPIO'))]
    msm = (source / 'drivers/pinctrl/qcom/pinctrl-msm.h').read_text()
    gpio += msm[msm.index('#define MSM_PIN_FUNCTION'):msm.index('#define QCA_PIN_FUNCTION')]
    gpio += 'enum {msm_mux_gpio}; static const char * const gpio_groups[]={"gpio0"};\n'
    pinmux = (source / 'drivers/pinctrl/pinmux.c').read_text()
    gpio += '''struct function_desc {const struct pinfunction *func;};
struct pinctrl_dev {struct function_desc *pin_function_tree;};
#define radix_tree_lookup(tree,n) ((n) == 0 ? *(tree) : NULL)
'''
    gpio += function(pinmux, 'pinmux_generic_function_is_gpio')
    def gpio_case(tree, fixed):
        text = (tree / 'drivers/pinctrl/qcom/pinctrl-sm8475.c').read_text()
        entry = re.search(r'^\s*MSM_(?:GPIO_)?PIN_FUNCTION\(gpio\),', text, re.M)[0]
        return gpio + 'static const struct pinfunction functions[]={' + entry + '};\n' + '''int main(void){
 struct function_desc f={.func=&functions[0]};struct pinctrl_dev p={.pin_function_tree=&f};
 assert(pinmux_generic_function_is_gpio(&p,0)==EXPECTED);
 assert(!pinmux_generic_function_is_gpio(&p,1));
 printf("2\\n");return 0;}
'''.replace('EXPECTED', 'true' if fixed else 'false')
    cam_relative = Path('drivers/clk/qcom/camcc-sm8450.c')
    cam = (source / cam_relative).read_text()
    declarations = cam[cam.index('static struct gdsc titan_top_gdsc;'):cam.index('static void cam_cc_sm8475_select_gdscs')]
    domains = common + '''struct generic_pm_domain {const char *name;};
struct gdsc {unsigned int gdscr,flags,pwrsts,en_rest_wait_val,en_few_wait_val,clk_dis_wait_val;
struct generic_pm_domain pd;struct generic_pm_domain *parent;};
#define HW_CTRL 1
#define POLL_CFG_GDSCR 2
#define RETAIN_FF_ENABLE 4
#define PWRSTS_OFF_ON 3
'''
    bindings = (source / 'include/dt-bindings/clock/qcom,sm8450-camcc.h').read_text()
    domains += '\n'.join(re.findall(r'^#define\s+\w+_GDSC\s+\d+', bindings, re.M)) + '\n' + declarations
    domains += '''static unsigned int orphan_count(void){
 unsigned int n=0;
 for(size_t i=0;i<ARRAY_SIZE(cam_cc_sm8450_gdscs);i++){
  struct gdsc *d=cam_cc_sm8450_gdscs[i];bool found=false;
  if(!d || !d->parent)continue;
  for(size_t j=0;j<ARRAY_SIZE(cam_cc_sm8450_gdscs);j++)
   if(cam_cc_sm8450_gdscs[j] && d->parent==&cam_cc_sm8450_gdscs[j]->pd)found=true;
  if(!found)n++;
 }
 return n;
}
'''
    positive = domains + function(cam, 'cam_cc_sm8475_select_gdscs') + '''
int main(void){
 unsigned int cases=0;
 assert(!orphan_count());cases++;
 assert(cam_cc_sm8450_gdscs[TITAN_TOP_GDSC]==&titan_top_gdsc);cases++;
 cam_cc_sm8475_select_gdscs();assert(!orphan_count());cases++;
 assert(cam_cc_sm8450_gdscs[TITAN_TOP_GDSC]==&sm8475_titan_top_gdsc);cases++;
 assert(cam_cc_sm8450_gdscs[IFE_0_GDSC]==&sm8475_ife_0_gdsc);cases++;
 for(size_t i=0;i<ARRAY_SIZE(cam_cc_sm8450_gdscs);i++){
  struct gdsc *d=cam_cc_sm8450_gdscs[i];
  if(i==TITAN_TOP_GDSC)assert(!d->parent);
  else assert(d->parent==&sm8475_titan_top_gdsc.pd);
  cases++;
 }
 cam_cc_sm8475_select_gdscs();assert(!orphan_count());cases++;
 cam_cc_sm8450_gdscs[BPS_GDSC]=NULL;
 cam_cc_sm8475_select_gdscs();assert(!orphan_count());cases++;
 printf("%u\\n",cases);return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='kernel73-probe-') as tmp:
        root = Path(tmp)
        results = {'gpio_metadata': run_c(root, 'gpio', gpio_case(source, True)),
                   'camcc_domain_closure': run_c(root, 'camcc', positive)}
        if args.baseline:
            results['old_gpio_flag_failure_reproduced'] = run_c(root, 'old-gpio', gpio_case(args.baseline, False))
            old = (args.baseline / cam_relative).read_text()
            old_select = old[old.index('\t\tcam_cc_sm8450_gdscs[IFE_0_GDSC] ='):old.index('\t\t/* Update CAMCC PLL0 */')]
            negative = domains + 'int main(void){\n' + old_select + 'assert(orphan_count()==3);printf("3\\n");return 0;}\n'
            results['old_unregistered_parents_reproduced'] = run_c(root, 'old-camcc', negative)
    report = {'all_passed': True, 'checks': results, 'actual_source_executed': True, 'hardware_test_performed': False}
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
