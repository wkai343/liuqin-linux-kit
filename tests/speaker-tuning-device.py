#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Read the active CS35L41 profile on a liuqin playing music or a speakerphone call."""
import argparse
import hashlib
import os
from pathlib import Path
import re

AMPS = ('TL', 'TR', 'BL', 'BR')
FIRMWARE_SHA = '1bb7ea70dd6b8e01ca5563cb051c025e1c8f361c57c28780ac559c0fc6b55f87'
# Observed in the working X memory of this specific Protection image.
ACTIVE = {'gain': 0x02801f78, **{f'eq{i}': 0x02801e98 + 4*i for i in range(5)}}
GAIN = {'music': 0x47cf26, 'voice': 0x400000}
# Block 23 is stored with the first two biquad coefficients negated in working memory.
EQ = (0x7f2ee2, 0x810955, 0x7fd941, 0x80d11e, 0x7f1d69)
PCM_REGS = {'AMP_DIG_VOL_CTRL': 0x6000, 'AMP_GAIN_CTRL': 0x6c04}
DEBUG = Path('/sys/kernel/debug')


def read_registers(regmap, addresses):
    result = {}
    index = 0
    with (regmap/'registers').open('rb', buffering=0) as stream:
        fd = stream.fileno()
        width = len(os.pread(fd, 64, 0).splitlines(keepends=True)[0])
        for line in (regmap/'range').read_text().splitlines():
            start, end = (int(v, 16) for v in line.split('-'))
            for name, addr in addresses.items():
                if start <= addr <= end:
                    offset = width * (index + (addr - start)//4)
                    key, value = os.pread(fd, width, offset).split(b':')
                    if int(key, 16) != addr or b'X' in value:
                        raise ValueError(f'Cannot read {name} at {addr:#x}. Keep speaker playback active.')
                    result[name] = int(value, 16)
            index += (end - start)//4 + 1
    if result.keys() != addresses.keys():
        raise ValueError(f'Missing registers in {regmap}')
    return result


def check(mode):
    release = os.uname().release
    lookup = [Path('/usr/lib/firmware')/sub/'cirrus/cs35l41-dsp1-spk-prot.wmfw'
              for sub in (f'updates/{release}', 'updates', release, '')]
    image = next((p for p in lookup if p.is_file()), None)
    if image is None or hashlib.sha256(image.read_bytes()).hexdigest() != FIRMWARE_SHA:
        raise ValueError('The working-memory offsets require the stock OS2.0.6.0 Protection program.')
    found = set()
    for prefix in sorted(Path('/sys/bus/i2c/devices').glob('*/of_node/sound-name-prefix')):
        amp = prefix.read_bytes().rstrip(b'\0').decode()
        if amp not in AMPS:
            continue
        dev = prefix.parents[1].name
        controls = DEBUG/'asoc/Xiaomi-Pad-6-Pro'/('cs35l41.' + dev)/'DSP1/controls'
        names = ('CSPL_COMMAND', 'CSPL_STATE', 'CSPL_ERRORNO')
        symbols = dict(re.findall(r'^\s*(\w+):\s+0x\w+ XM:\w+ (0x\w+) Protection\s+0xcd\b',
                                  controls.read_text(), re.MULTILINE))
        addresses = {**ACTIVE, **PCM_REGS, **{n: int(symbols[n], 16) for n in names}}
        values = read_registers(DEBUG/'regmap'/dev, addresses)
        eq = tuple(values[f'eq{i}'] for i in range(5))
        pcm_field = (values['AMP_DIG_VOL_CTRL'] >> 3) & 0x7ff
        analog_step = (values['AMP_GAIN_CTRL'] >> 5) & 0x1f
        print(f"{amp} ({dev})  gain={values['gain']:06x}  EQ={' '.join(f'{v:06x}' for v in eq)}  "
              f"AMP_PCM_VOL={pcm_field:#x}  analog_step={analog_step}  "
              f"command={values[names[0]]}  state={values[names[1]]}  error={values[names[2]]}")
        if pcm_field != 0 or analog_step != 18:
            raise ValueError(f'{amp} does not have unity PCM gain and analog step 18')
        if amp in found or values['gain'] != GAIN[mode] or eq != EQ or any(values[n] for n in names):
            raise ValueError(f'{amp} does not confirm the {mode} profile')
        found.add(amp)
    if found != set(AMPS):
        raise ValueError('Missing amplifiers: ' + ', '.join(sorted(set(AMPS) - found)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=GAIN)
    try:
        check(parser.parse_args().mode)
    except (KeyError, OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')
