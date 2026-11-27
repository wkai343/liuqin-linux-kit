# SPDX-License-Identifier: MIT
"""Read the two pinned CHI still pipelines, including variant-specific port routing."""
import hashlib
import struct

from elftools.elf.elffile import ELFFile

SELECTOR_SHA = '53ad9cd84d3e484f001a4929944bdabfe2f409613b5fc401d33682c72d7ea7f5'
IPE, EVA = 65538, 65543


def snapshot_graphs(vendor):
    path = vendor / 'lib64/com.qti.chiusecaseselector.so'
    if hashlib.sha256(path.read_bytes()).hexdigest() != SELECTOR_SHA:
        raise ValueError('unsupported CHI usecase selector')
    with path.open('rb') as f:
        elf = ELFFile(f)
        data = elf.get_section_by_name('.data')
        base, data = data['sh_addr'], data.data()
        rodata = elf.get_section_by_name('.rodata')
        robase, rodata = rodata['sh_addr'], rodata.data()

    def read(layout, address):
        offset = address - base
        if offset < 0 or offset + struct.calcsize(layout) > len(data):
            raise ValueError('CHI pointer outside descriptor section')
        return struct.unpack_from(layout, data, offset)

    def string(address):
        index = address - robase
        if not 0 <= index < len(rodata):
            raise ValueError('CHI string outside rodata')
        return rodata[index:rodata.index(0, index)].decode('ascii')

    def descriptor(name):
        ptr = robase + rodata.index(name.encode() + b'\0')
        key = struct.pack('<Q', ptr)
        offset = data.index(key)
        if offset % 8 or data.find(key, offset + 8) != -1:
            raise ValueError(f'ambiguous pipeline descriptor for {name}')
        return base + offset

    def links(desc):
        count, table = read('<I4xQ', desc + 24)
        graph = {}
        for i in range(count):
            link = table + 80 * i
            source = read('<3I', link)
            size, dest, fmt = read('<I4xQI', link + 32)
            graph[source] = (fmt, [read('<3I', dest + 32 * j) for j in range(size)])
        return graph

    def targets(desc, offset):
        count, table = read('<I4xQ', desc + offset)
        result = {}
        for i in range(count):
            name, target, length, nodes = read('<QQI4xQ', table + 32 * i)
            formats, ptr = read('<IQ', target + 20)
            assert length == 1 and formats <= 8
            result[string(name)] = (read('<3I', nodes), read(f'<{formats}I', ptr))
        return result

    # Trace the matching named short/long streams to the same virtual ports.
    # Both CHI relay layers in this route preserve their selected port number.
    rt = descriptor('MFHDRRealTimeFeatureZSLFusion')
    rt_inputs, rt_links = targets(rt, 168), links(rt)
    for role, full, ds4 in [('SHORT', 10050, 10053), ('LONG', 10051, 10054)]:
        for suffix, port in [('FULL_UBWCTP10', full), ('DS4_PD10', ds4)]:
            start, _ = rt_inputs[f'TARGET_BUFFER_IFE_{role}_{suffix}_IN']
            relay = next(dest for dest in rt_links[start][1] if dest[:2] == (255, 5))
            assert (IPE, 4, port) in rt_links[relay][1]

    plans = {}
    for count, prefix in [(2, 'Two'), (3, 'Three')]:
        name = f'MFHDR{prefix}FrameSnapshotFusion'
        desc = descriptor(name)
        inputs, outputs = targets(desc, 168), targets(desc, 152)
        graph = links(desc)
        nnodes, nodes = read('<IQ', desc + 12)
        # CHI variants are indexed from zero in the node descriptors.
        variant = count - 1
        assert string(read('<Q', desc + 64 + variant * 8)[0]) == f'ModeMFHDR{count}Exposure'
        fusion = next(nodes + i * 128 for i in range(nnodes)
                      if read('<2I', nodes + i * 128 + 8) == (IPE, count))
        properties = {}
        for i in range(nnodes):
            node = nodes + i * 128
            kind, instance = read('<2I', node + 8)
            if kind != IPE:
                continue
            table, = read('<Q', node)
            nprops, = read('<I', node + 80)
            props = {}
            for j in range(nprops):
                key, value = read('<I4xQ', table + j * 16)
                props[key] = int(string(value))
            properties[instance] = (props[2], props.get(4, 0))
        assert properties[count] == (9, 9) and properties[count + 1] == (0, 10)
        assert all(properties[inst] == (3, 0) for inst in range(5, 5 + count))
        actual = {}
        for number, array, stride, mode_offset in [(16, 40, 40, 12), (48, 72, 64, 32)]:
            length = read('<I', fusion + number)[0]
            ptr = read('<Q', fusion + array)[0]
            for i in range(length):
                port = ptr + stride * i
                virtual = read('<I', port)[0]
                n, *modes = read('<4I', port + mode_offset)
                assert 1 <= n <= 3 and variant in modes[:n]
                selectors = read('<Q', port + stride - 8)[0]
                pair = read('<Q', selectors + 8 * modes[:n].index(variant))[0]
                src, dest = read('<QQ', pair)
                src_id, src_label = read('<I4xQ', src)
                dest_id, dest_label = read('<I4xQ', dest)
                assert src_id == virtual == int(string(src_label))
                assert dest_id == int(string(dest_label))
                actual[virtual] = dest_id
        sources = {}
        for target, (node, formats) in inputs.items():
            assert formats == (3,) and node[0] == IPE and node[2] == 0
            pair = []
            for output_port, expected_format in [(10, 12), (11, 18)]:
                fmt, dests = graph[IPE, node[1], output_port]
                virtual, = [p for kind, inst, p in dests if (kind, inst) == (IPE, count)]
                assert fmt == expected_format
                pair.append(actual[virtual])
            sources[target] = pair
        expected = [[0, 1], [4, 5]] if count == 2 else [[0, 1], [14, 15], [4, 5]]
        assert [sources[f'TARGET_BUFFER_YUV{i}'] for i in range(count)] == expected
        for virtual, fmt, dest_port in [(10068, 12, 0), (10069, 18, 1)]:
            assert graph[IPE, count, virtual] == (fmt, [(IPE, count + 1, dest_port)])
            assert actual[virtual] == 10 + dest_port
        assert outputs['TARGET_BUFFER_YUV_OUT'] == ((IPE, count + 1, 8), (3,))
        display_ports = sorted(node[2] for node, formats in outputs.values()
                               if node[:2] == (IPE, count + 1) and formats == (3,))
        assert display_ports == ([8, 9] if count == 2 else [8])
        eva_instances = {instance for (kind, instance, _) in graph if kind == EVA}
        assert len(eva_instances) == count - 1
        plans[count] = dict(pipeline=name, inputs=sources, fusion_output=[10, 11],
                            post_outputs=display_ports, profile_processing=properties,
                            registration_instances=sorted(eva_instances))
    return plans
