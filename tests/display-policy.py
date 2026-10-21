#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise user policy selection, persistence and refusal of ambiguous devices."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import tempfile
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
ns = runpy.run_path(str(PROJECT/'device/board-overlay/usr/local/sbin/liuqin-display-policy'))
g = ns['main'].__globals__
cases = 0


def rejects(fn, *args):
    global cases
    try:
        fn(*args)
    except (ValueError, OSError):
        cases += 1
    else:
        raise AssertionError('Expected refusal')


with tempfile.TemporaryDirectory(prefix='display-policy-') as temp:
    root = Path(temp)
    sysfs = root/'devices'
    sysfs.mkdir()
    rejects(ns['panel'], sysfs)
    node = sysfs/'panel0'
    node.mkdir()
    (node/'cabc_mode').write_text('unchanged\n')
    rejects(ns['panel'], sysfs)
    (node/'cabc_status').write_text('requested=unchanged programmed=unknown cycle=0 ready=0 error=0\n')
    assert ns['panel'](sysfs) == node
    before = {p.name:p.read_bytes() for p in node.iterdir()}
    assert ns['status'](node)['requested'] == 'unchanged'
    assert before == {p.name:p.read_bytes() for p in node.iterdir()}
    cases += 1
    rejects(ns['select'], node, 'invalid')
    assert (node/'cabc_mode').read_text() == 'unchanged\n'
    for mode in ns['MODES']:
        assert ns['select'](node, mode)['requested'] == mode
        assert (node/'cabc_mode').read_text() == mode+'\n'
        cases += 1
    second = sysfs/'panel1'
    second.mkdir()
    for name in before:
        (second/name).write_bytes(before[name])
    rejects(ns['panel'], sysfs)
    conf = root/'etc/liuqin/display-policy.json'
    for mode in ns['MODES']:
        ns['save'](mode, conf)
        assert ns['read_config'](conf) == mode
        assert not list(conf.parent.glob('.display-policy-*'))
        cases += 1
    saved = conf.read_bytes()
    rejects(ns['save'], 'auto', conf)
    assert conf.read_bytes() == saved
    for value in [[], {'cabc': 'invalid'}, {'cabc': 'off', 'extra': True}, {'cabc': []}]:
        conf.write_text(json.dumps(value))
        rejects(ns['read_config'], conf)
    # Missing persistent configuration does not try to access or change a panel.
    conf.unlink()
    with mock.patch.dict(g, {'CONFIG': conf, 'panel': mock.Mock(side_effect=AssertionError('unexpected panel access'))}), mock.patch('os.geteuid', return_value=0), mock.patch('sys.argv', ['policy', 'apply']):
        ns['main']()
        cases += 1
    with mock.patch.dict(g, {'panel': mock.Mock(side_effect=AssertionError('unexpected panel access'))}), mock.patch('os.geteuid', return_value=1000), mock.patch('sys.argv', ['policy', 'set', 'ui']), contextlib.redirect_stderr(io.StringIO()):
        try:
            ns['main']()
        except SystemExit as e:
            assert e.code == 1
            cases += 1
        else:
            raise AssertionError('Unprivileged change allowed')
print(json.dumps({'all_passed': True, 'cases': cases}))
