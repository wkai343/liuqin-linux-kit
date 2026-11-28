#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compare the camera in Kamoso's open settings page with its active PipeWire stream."""
import argparse
import json
import subprocess

import gi

gi.require_version('Atspi', '2.0')
from gi.repository import Atspi


def children(item):
    for index in range(item.get_child_count()):
        child = item.get_child_at_index(index)
        if child is not None:
            yield child


def selectors(item):
    if item.get_role() == Atspi.Role.COMBO_BOX and item.get_state_set().contains(Atspi.StateType.SHOWING):
        yield item.get_name()
    for child in children(item):
        yield from selectors(child)


def active_camera(pid):
    graph = json.loads(subprocess.check_output(['pw-dump'], timeout=5))
    clients = {item['id'] for item in graph if item['type'] == 'PipeWire:Interface:Client'
               and str(item['info']['props'].get('application.process.id')) == str(pid)}
    nodes = {item['id']: item['info']['props'] for item in graph if item['type'] == 'PipeWire:Interface:Node'}
    sources = set()
    for item in graph:
        if item['type'] != 'PipeWire:Interface:Link' or item['info']['state'] != 'active':
            continue
        link = item['info']
        source = nodes.get(link['output-node-id'], {})
        if nodes.get(link['input-node-id'], {}).get('client.id') in clients and source.get('api.libcamera.path'):
            sources.add(link['output-node-id'])
    if len(sources) != 1:
        raise SystemExit(f'Expected one active libcamera source for Kamoso PID {pid}, found {len(sources)}')
    return nodes[sources.pop()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, help='select one Kamoso process when multiple windows are open')
    args = parser.parse_args()
    choices = []
    for app in children(Atspi.get_desktop(0)):
        if (app.get_name() or '').lower() == 'kamoso':
            pid = app.get_process_id()
            if args.pid is None or args.pid == pid:
                choices.extend((pid, label) for label in selectors(app))
    if len(choices) != 1:
        raise SystemExit('Open Configure Kamoso with both cameras available and select a single window')
    pid, label = choices[0]
    source = active_camera(pid)
    print(json.dumps({'pid': pid, 'selector': label, 'stream': source['node.description'],
                      'device': source['api.libcamera.path']}, ensure_ascii=False))
    if label != source['node.description']:
        raise SystemExit('The camera label differs from the active preview')


if __name__ == '__main__':
    main()
