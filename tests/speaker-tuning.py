#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the speakerphone routing policy and CSPL update transaction."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import runpy
import struct
from tempfile import TemporaryDirectory
import unittest
import zlib

PROJECT = Path(__file__).resolve().parents[1]
tuning = runpy.run_path(str(PROJECT/'device/board-overlay/usr/libexec/liuqin-speaker-tuning'))
Node = tuning['Node']
speaker_mode = tuning['speaker_mode']
AMPS = tuning['AMPS']
ZERO = tuning['ZERO']
UPDATE = tuning['UPDATE']


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.nodes = {
            1: Node('Stream/Output/Audio', 'Communication', (), True),
            2: Node('Audio/Sink', '', ('120', 'loopback'), True),
            3: Node('Stream/Output/Audio', 'Loopback', ('120', 'loopback'), True),
            4: Node('Audio/Sink', '', (), True, speaker=True),
            5: Node('Audio/Sink', '', (), True),
        }

    def test_match_alsa_card_and_speaker_pcm_or_profile(self):
        physical = {'media.class': 'Audio/Sink/Internal', 'alsa.id': 'XiaomiPad6Pro',
                    'api.alsa.path': 'liuqin_speaker'}
        self.assertTrue(Node.from_properties(physical, True).speaker)
        ucm = {'media.class': 'Audio/Sink', 'alsa.id': 'XiaomiPad6Pro',
               'device.profile.name': 'HiFi: Speaker: sink'}
        self.assertTrue(Node.from_properties(ucm, True).speaker)
        self.assertFalse(Node.from_properties({**ucm, 'device.profile.name': 'HiFi: HDMI: sink'}, True).speaker)
        self.assertFalse(Node.from_properties({**physical, 'alsa.id': 'USB'}, True).speaker)

    def test_follow_the_active_route_and_last_speaker_call(self):
        self.assertEqual(speaker_mode(self.nodes, [(1, 2), (3, 4)]), 'voice')
        self.assertEqual(speaker_mode(self.nodes, [(1, 2), (3, 5)]), 'music')
        self.assertEqual(speaker_mode(self.nodes, [(1, 2)]), 'music')
        self.nodes[6] = Node('Stream/Output/Audio', 'phone', (), True)
        links = [(1, 2), (3, 5), (6, 4)]
        self.assertEqual(speaker_mode(self.nodes, links), 'voice')
        self.nodes[6] = replace(self.nodes[6], running=False)
        self.assertEqual(speaker_mode(self.nodes, links), 'music')

    def test_idle_loopbacks_capture_and_music_do_not_select_voice(self):
        self.assertEqual(speaker_mode(self.nodes, [(3, 4)]), 'music')
        for changes in ({'media_class': 'Stream/Input/Audio'}, {'role': 'Music'}, {'running': False}):
            nodes = dict(self.nodes)
            nodes[1] = replace(nodes[1], **changes)
            self.assertEqual(speaker_mode(nodes, [(1, 4)]), 'music')
        self.nodes[4] = replace(self.nodes[4], running=False)
        self.assertIsNone(speaker_mode(self.nodes, [(1, 5)]))

    def test_virtual_route_requires_the_running_pair_from_the_same_client(self):
        links = [(1, 2), (3, 4)]
        node = self.nodes[3]
        self.nodes[3] = replace(node, group=('121', 'loopback'))
        self.assertEqual(speaker_mode(self.nodes, links), 'music')
        self.nodes[3] = replace(node, running=False)
        self.assertEqual(speaker_mode(self.nodes, links), 'music')


class TestAmplifiers(tuning['Amplifiers']):
    def __init__(self):
        # Distinct data at each position catches accidental cross-channel copies.
        super().__init__({mode: {a: bytes([i + offset])*72 for i, a in enumerate(AMPS)}
                          for mode, offset in [('music', 10), ('voice', 20)]})
        self.buffers = {a: bytes([100 + i])*400 for i, a in enumerate(AMPS)}
        self.active = dict(self.buffers)
        self.commands = dict.fromkeys(AMPS, ZERO)
        self.writes = []
        self.uncalibrated = None
        self.fail_once = None

    def control(self, amp, name):
        assert name == 'Speaker Calibration Loaded'
        return 'off' if amp == self.uncalibrated else 'on'

    def read(self, amp, name, size):
        if name == '_UPDATE_PARAMS_CONFIG':
            return self.buffers[amp]
        assert name in ('CSPL_STATE', 'CSPL_ERRORNO') and size == 4
        return ZERO

    def write(self, amp, name, data):
        self.writes.append((amp, name, data))
        if name == '_UPDATE_PARAMS_CONFIG':
            self.buffers[amp] = data
            return
        assert name == 'CSPL_COMMAND'
        if data == UPDATE and amp == self.fail_once:
            self.fail_once = None
            raise OSError('lost command')
        if data == UPDATE and data != self.commands[amp]:
            self.active[amp] = self.buffers[amp]
        self.commands[amp] = data


class TransactionTests(unittest.TestCase):
    def test_all_four_are_staged_before_update_and_host_command_is_rearmed(self):
        hw = TestAmplifiers()
        tails = {a: data[72:] for a, data in hw.buffers.items()}
        for mode in ('music', 'voice', 'music'):
            hw.writes.clear()
            hw.apply(mode)
            first = next(i for i, (_, _, data) in enumerate(hw.writes) if data == UPDATE)
            self.assertEqual({a for a, name, _ in hw.writes[:first] if name == '_UPDATE_PARAMS_CONFIG'}, set(AMPS))
            for a in AMPS:
                self.assertEqual(hw.active[a], hw.packets[mode][a] + tails[a])
                self.assertEqual(hw.commands[a], ZERO)

    def test_missing_calibration_prevents_any_write(self):
        hw = TestAmplifiers()
        hw.uncalibrated = 'BR'
        with self.assertRaisesRegex(OSError, 'BR'):
            hw.apply('voice')
        self.assertEqual(hw.writes, [])

    def test_mid_update_failure_restores_music_and_allows_retry(self):
        hw = TestAmplifiers()
        hw.fail_once = 'BL'
        with self.assertRaisesRegex(OSError, 'lost command'):
            hw.apply('voice')
        self.assertEqual({a: p[:72] for a, p in hw.active.items()}, hw.packets['music'])
        self.assertEqual(set(hw.commands.values()), {ZERO})
        hw.apply('voice')
        self.assertEqual({a: p[:72] for a, p in hw.active.items()}, hw.packets['voice'])


class ProfileTests(unittest.TestCase):
    def write_profile(self, root, amp, eq, gain, opcode=3):
        words = [1245, 4, 768, 1236] + [0]*1240
        for start in range(202, 234, 8):
            words[start:start+3] = [opcode, 9, 9]
        words[205:210] = eq
        words[234:239] = [7, 9, 9, gain, 0]
        checksum = zlib.crc32(struct.pack('<1244i', *words))
        meta = json.dumps({'client_tuning_name': f'SPK_A_{amp}_Music_Version3.5',
                           'checksum': str(checksum)}).encode() + b'\0'
        raw = b''.join(n.to_bytes(3, 'little', signed=True) for n in words)
        packed = b''.join(raw[i:i+4][::-1] for i in range(0, len(raw), 4))
        data = bytearray(b'WMDR\x10\x00\x00\x00' + bytes(8))
        for off, kind, value in ((0x134, 0x12, packed), (0, 0xfc00, meta)):
            data.extend(struct.pack('<HHIIII', off, kind, 0xcd, 0x1d500000, 0, len(value)))
            data.extend(value)
            data.extend(bytes(-len(value) % 4))
        path = root/f'cs35l41-dsp1-spk-prot-{amp.lower()}.bin'
        path.write_bytes(data)
        return path

    def test_source_delta_repairs_biquad_and_selects_gain_separately_for_each_position(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root/'sha256.json'
            digests, expected = {}, {}
            for i, a in enumerate(AMPS):
                eq = [-7900000+i, 7000000, 8100000, -7900000+i, 8100100]
                self.write_profile(root, a, eq, 4706086)
                for mode, number in [('music', 4706086), ('voice', 4194304)]:
                    name = f'{a}-{mode}.txt'
                    words = [12, 4, 768, 1236, 0, 0, 0, 1, 23, number, 0]
                    crc = zlib.crc32(struct.pack('<11i', *words))
                    words.append(crc if crc < 2**31 else crc - 2**32)
                    data = ','.join(map(str, words)).encode()
                    (root/name).write_bytes(data)
                    digests[name] = hashlib.sha256(data).hexdigest()
                    expected[mode, a] = [18, 4, 768, 1236, 0, 0, 0, 2, 23, *eq, 27, number, 0]
            manifest.write_text(json.dumps(digests))
            packets = tuning['load_packets'](root, manifest, [root])
            for (mode, amp), body in expected.items():
                *words, crc = struct.unpack('>17iI', packets[mode][amp])
                self.assertEqual(words, body)
                self.assertEqual(crc, zlib.crc32(struct.pack('<17i', *words)))
            (root/'BR-voice.txt').write_bytes((root/'BR-music.txt').read_bytes())
            with self.assertRaisesRegex(ValueError, 'BR-voice.txt'):
                tuning['load_packets'](root, manifest, [root])
            # Even a file matching the package manifest needs a valid Cirrus CRC.
            bad = (root/'BR-voice.txt').read_bytes().rsplit(b',', 1)[0] + b',0'
            (root/'BR-voice.txt').write_bytes(bad)
            digests['BR-voice.txt'] = hashlib.sha256(bad).hexdigest()
            manifest.write_text(json.dumps(digests))
            with self.assertRaisesRegex(ValueError, 'checksum'):
                tuning['load_packets'](root, manifest, [root])

    def test_reject_different_signal_chain_and_damaged_full_tuning(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write_profile(root, 'TL', [1, -2, 3, -4, 5], 4706086, opcode=7)
            with self.assertRaisesRegex(ValueError, 'EQ layout'):
                tuning['v35_blocks'](path, 'TL')
            blob = bytearray(path.read_bytes())
            blob[500] ^= 1
            path.write_bytes(blob)
            with self.assertRaisesRegex(ValueError, 'checksum'):
                tuning['v35_blocks'](path, 'TL')


if __name__ == '__main__':
    unittest.main()
