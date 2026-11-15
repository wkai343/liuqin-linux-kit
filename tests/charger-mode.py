#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fault-inject stage-1 charging without touching real power/display devices."""
import os
import hashlib
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / 'device/charger-mode'


class ChargerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('battery', 'usb', 'backlight', 'run', 'dev', 'cpu/cpufreq/policy0', 'cpu/cpu1'):
            (self.root / name).mkdir(parents=True)
        for name, value in {'uptime': '100.00 0\n', 'usb/online': '1\n',
                            'cmdline': 'quiet androidboot.mode=charger\n',
                            'backlight/bl_power': '4\n', 'backlight/brightness': '0\n',
                            'cpu/cpufreq/policy0/cpuinfo_min_freq': '300000\n',
                            'cpu/cpufreq/policy0/scaling_max_freq': '1000000\n',
                            'cpu/cpu1/online': '1\n', 'dev/sda': ''}.items():
            (self.root / name).write_text(value)
        self.env = dict(os.environ, FIXTURE=str(self.root),
                        LIUQIN_CHARGER_CMDLINE_FILE=str(self.root / 'cmdline'),
                        LIUQIN_CHARGER_BAT_ROOT=str(self.root / 'battery'),
                        LIUQIN_CHARGER_USB_ROOT=str(self.root / 'usb'),
                        LIUQIN_CHARGER_BACKLIGHT=str(self.root / 'backlight'),
                        LIUQIN_CHARGER_DEV_ROOT=str(self.root / 'dev'),
                        LIUQIN_CHARGER_CPU_ROOT=str(self.root / 'cpu'),
                        LIUQIN_CHARGER_UPTIME_FILE=str(self.root / 'uptime'),
                        LIUQIN_CHARGER_RUN_DIR=str(self.root / 'run'),
                        LIUQIN_CHARGER_LOG_FILE=str(self.root / 'log'),
                        LIUQIN_CHARGER_PMSG_FILE=str(self.root / 'absent-pmsg'),
                        LIUQIN_CHARGER_TEST_ALLOW_REGULAR='1', LIUQIN_CHARGER_MAX_TICKS='1',
                        LIUQIN_CHARGER_TIMEOUT_CMD=shutil.which('timeout'))
        self.env['LIUQIN_CHARGER_EXIT_TOOL'] = str(self.program('exit',
            'printf "%s %s\\n" "$1" "$(cat "$FIXTURE/uptime")" >>"$FIXTURE/exits"\nexit 1\n'))
        self.env['LIUQIN_CHARGER_KEY_TOOL'] = str(self.program('key', 'exit 2\n'))
        self.env['LIUQIN_CHARGER_UI_TOOL'] = str(self.program('ui', ': >"$FIXTURE/ui-started"\n'))
        self.env['LIUQIN_CHARGER_DISPLAY_PREPARE'] = str(self.program('prepare',
            '[ "$1" = charger-prepare ] || exit 99\n: >"$FIXTURE/prepared"\n'))
        self.env['LIUQIN_CHARGER_BLOCKDEV'] = str(self.program('blockdev',
            '[ "${FAIL_STORAGE:-0}" = 0 ] || exit 1\n[ "$1" != --getro ] || echo 1\n'))
        self.functions = (SOURCE / 'liuqin-charger-mode').read_text().split('# Entry point (')[0]

    def program(self, name, content):
        path = self.root / name
        path.write_text('#!/bin/sh\n' + content)
        path.chmod(0o755)
        return path

    def run_functions(self, action):
        script = self.root / 'test-script'
        script.write_text(self.functions + '\n' + action + '\n')
        return subprocess.run(['sh', str(script)], env=self.env, capture_output=True, text=True, timeout=10)

    def monitor(self, values):
        # Each fake sleep advances monotonic time and publishes the next sample.
        steps = []
        for second, online in enumerate(values):
            action = 'rm -f "$FIXTURE/usb/online"' if online is None else \
                'printf "%s\\n" ' + shlex.quote(str(online)) + ' >"$FIXTURE/usb/online"'
            steps.append(f'{second}) printf "{100 + second}.00 0\\n" >"$FIXTURE/uptime"; {action} ;;')
        advance = self.program('advance', '''n=$(cat "$FIXTURE/step" 2>/dev/null || echo 0)
case $n in
''' + '\n'.join(steps) + '''
*) kill -TERM "$PPID"; exit 0 ;;
esac
echo $((n+1)) >"$FIXTURE/step"
''')
        self.env['LIUQIN_CHARGER_SLEEP_CMD'] = str(advance)
        result = self.run_functions('"$sleep_cmd" 1\ntrap "exit 0" TERM\ncharger_monitor_power')
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.root / 'exits'
        return path.read_text().splitlines() if path.exists() else []

    def test_unplug_debounced_and_failed_poweroff_retried(self):
        exits = self.monitor([1] + [0] * 8)
        self.assertEqual(exits, ['poweroff 104.00 0', 'poweroff 108.00 0'])

    def test_replug_and_unknown_break_the_confirmation(self):
        for unknown in (1, None, 'garbage', '', 'NA'):
            with self.subTest(unknown=unknown):
                (self.root / 'step').unlink(missing_ok=True)
                self.assertEqual(self.monitor([1, 0, 0, unknown, 0, 0, 0, 1]), [])

    def test_boot_offline_has_initialization_grace_but_does_not_require_seen_online(self):
        self.assertEqual(self.monitor([0] * 14), ['poweroff 113.00 0'])

    def test_startup_zero_then_attached_never_powers_off(self):
        self.assertEqual(self.monitor([0] * 9 + [1] * 5), [])

    def test_invalid_clock_and_unreadable_power_never_power_off(self):
        self.assertEqual(self.monitor([None] * 15), [])
        (self.root / 'uptime').write_text('bad\n')
        result = self.run_functions('charger_seconds')
        self.assertNotEqual(result.returncode, 0)

    def test_exit_rechecks_cable_and_serializes_requests(self):
        result = self.run_functions('charger_request_exit poweroff')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'exits').exists())
        (self.root / 'usb/online').write_text('0\n')
        (self.root / 'run/exit-lock').mkdir()
        self.run_functions('charger_request_exit normal')
        self.assertFalse((self.root / 'exits').exists())

    def test_main_seals_storage_and_shows_screen_without_starting_a_root(self):
        result = subprocess.run(['sh', str(SOURCE / 'liuqin-charger-mode')], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'ui-started').exists())
        self.assertTrue((self.root / 'prepared').exists())
        self.assertEqual((self.root / 'cpu/cpu1/online').read_text().strip(), '0')
        self.assertEqual((self.root / 'backlight/brightness').read_text().strip(), '0')
        self.assertFalse((self.root / 'exits').exists())

    def test_storage_failure_keeps_display_off(self):
        self.env['FAIL_STORAGE'] = '1'
        result = subprocess.run(['sh', str(SOURCE / 'liuqin-charger-mode')], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'ui-started').exists())

    def test_initial_readiness_retries_without_a_power_key_press(self):
        (self.root / 'cpu/cpufreq/policy0/cpuinfo_min_freq').write_text('NA\n')
        self.env['LIUQIN_CHARGER_MAX_TICKS'] = '3'
        self.program('key', '''printf '%s\\n' "$*" >>"$FIXTURE/key-windows"
n=$(cat "$FIXTURE/key-count" 2>/dev/null || echo 0)
echo $((n+1)) >"$FIXTURE/key-count"
[ "$n" != 1 ] || echo 300000 >"$FIXTURE/cpu/cpufreq/policy0/cpuinfo_min_freq"
exit 2
''')
        result = subprocess.run(['sh', str(SOURCE / 'liuqin-charger-mode')], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'key-windows').read_text().splitlines(), [
            '--arm-after-ms 3000 --timeout-ms 3000',
            '--arm-after-ms 0 --timeout-ms 1000',
            '--arm-after-ms 0 --timeout-ms 30000'])
        self.assertTrue((self.root / 'ui-started').exists())
        self.assertFalse((self.root / 'exits').exists())

    def test_startup_zero_does_not_permanently_suppress_the_screen(self):
        (self.root / 'usb/online').write_text('0\n')
        result = subprocess.run(['sh', str(SOURCE / 'liuqin-charger-mode')], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'ui-started').exists())
        self.assertFalse((self.root / 'exits').exists())

    def test_finished_display_can_be_reopened_repeatedly(self):
        # Offlining the last CPU in a cluster leaves an inactive cpufreq
        # directory whose reads fail. It must not block later screen wakes.
        for cpu in (4, 7):
            policy = self.root / f'cpu/cpufreq/policy{cpu}'
            policy.mkdir()
            (policy / 'cpuinfo_min_freq').write_text('NA\n')
            (self.root / f'cpu/cpu{cpu}').mkdir()
            (self.root / f'cpu/cpu{cpu}/online').write_text('1\n')
        self.program('ui', 'echo frame >>"$FIXTURE/frames"\n')
        result = self.run_functions('''charger_display_pid=
charger_display_started=false
charger_wake_display
wait "$charger_display_pid"
charger_wake_display
wait "$charger_display_pid"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'frames').read_text().splitlines(), ['frame', 'frame'])
        self.assertFalse((self.root / 'exits').exists())

    def test_active_cpu_policy_failure_still_blocks_display(self):
        (self.root / 'cpu/cpufreq/policy0/cpuinfo_min_freq').write_text('NA\n')
        result = self.run_functions('charger_display_pid=\ncharger_wake_display')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'ui-started').exists())

    def test_async_cpu_limit_readback_is_confirmed_before_display(self):
        self.env['LIUQIN_CHARGER_SLEEP_CMD'] = str(self.program('settle', '''[ "$1" = 0.02 ] || exit 99
n=$(cat "$FIXTURE/settle-count" 2>/dev/null || echo 0)
echo $((n+1)) >"$FIXTURE/settle-count"
'''))
        result = self.run_functions('''charger_value() {
case $1 in
*/scaling_max_freq)
    if [ "$(cat "$FIXTURE/settle-count" 2>/dev/null || echo 0)" -lt 3 ]; then
        echo 1000000; return
    fi ;;
esac
cat "$1"
}
charger_display_pid=
charger_display_started=false
charger_wake_display || exit 1
wait "$charger_display_pid"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'settle-count').read_text().strip(), '3')
        self.assertTrue((self.root / 'ui-started').exists())

    def test_cpu_limit_that_never_settles_is_bounded_and_blocks_display(self):
        self.env['LIUQIN_CHARGER_SLEEP_CMD'] = str(self.program('settle',
            'echo "$1" >>"$FIXTURE/settle-waits"\n'))
        result = self.run_functions('''charger_value() {
case $1 in */scaling_max_freq) echo 1000000 ;; *) cat "$1" ;; esac
}
charger_display_pid=
charger_wake_display
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'settle-waits').read_text().splitlines(), ['0.02'] * 10)
        self.assertFalse((self.root / 'ui-started').exists())

    def test_short_key_stays_in_charger_and_long_key_requests_normal_boot(self):
        for code, expected in ((0, None), (10, 'normal')):
            self.program('key', f'exit {code}\n')
            result = subprocess.run(['sh', str(SOURCE / 'liuqin-charger-mode')], env=self.env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            if expected is None:
                self.assertFalse((self.root / 'exits').exists())
            else:
                self.assertEqual((self.root / 'exits').read_text().split()[0], expected)

    def test_power_monitor_is_not_blocked_by_display_or_key_waits(self):
        self.program('key', 'sleep 2\n: >"$FIXTURE/key-done"\nexit 2\n')
        self.program('prepare', 'sleep 2\n: >"$FIXTURE/prepare-done"\n')
        self.program('exit', '''[ "$1" = poweroff ] || exit 99
[ -e "$FIXTURE/key-done" ] || : >"$FIXTURE/poweroff-before-key"
[ -e "$FIXTURE/prepare-done" ] || : >"$FIXTURE/poweroff-before-display"
printf '1\\n' >"$FIXTURE/usb/online"
exit 1
''')
        advance = self.program('advance-fast', '''sleep 0.02
n=$(cat "$FIXTURE/step" 2>/dev/null || echo 0)
echo $((n+1)) >"$FIXTURE/step"
printf '%s.00 0\\n' "$((101+n))" >"$FIXTURE/uptime"
[ "$n" -gt 3 ] || printf '0\\n' >"$FIXTURE/usb/online"
''')
        self.env['LIUQIN_CHARGER_SLEEP_CMD'] = str(advance)
        result = subprocess.run(['sh', str(SOURCE / 'liuqin-charger-mode')], env=self.env,
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'poweroff-before-key').exists())
        self.assertTrue((self.root / 'poweroff-before-display').exists())

    @unittest.skipUnless(shutil.which('busybox'), 'BusyBox required')
    def test_busybox_stage_one_shell(self):
        result = subprocess.run(['busybox', 'sh', str(SOURCE / 'liuqin-charger-mode')],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'ui-started').exists())

    def test_normal_boot_does_not_enter_charger(self):
        for cmdline in ('quiet', 'xandroidboot.mode=charger'):
            (self.root / 'cmdline').write_text(cmdline + '\n')
            result = subprocess.run(['sh', str(SOURCE / 'liuqin-charger-mode')], env=self.env,
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 64)
        self.assertFalse((self.root / 'ui-started').exists())


class HelperTests(unittest.TestCase):
    def test_actual_key_classifier_and_press_across_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); binary = root / 'key'; events = root / 'events'
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror',
                            str(SOURCE / 'liuqin-charger-mode-power-key.c'), '-o', str(binary)], check=True)
            for rows, timeout, expected in [('press 10\nrelease 510\n', 1000, 0),
                                             ('press 10\nrelease 2510\n', 3000, 10),
                                             ('press 150\nrelease 250\n', 200, 0),
                                             ('press 150\nrelease 2650\n', 200, 10),
                                             ('press 250\nrelease 500\n', 200, 2)]:
                events.write_text(rows)
                result = subprocess.run([str(binary), '--arm-after-ms', '0', '--timeout-ms', str(timeout),
                                         '--test-script', str(events)], capture_output=True, timeout=3)
                self.assertEqual(result.returncode, expected, result.stderr)

    @unittest.skipUnless(os.environ.get('SLOT_SUCCESS_GCC'), 'Devbox ARM64 compiler required')
    def test_initramfs_packages_matching_static_arm64_helpers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('etc', 'bin'):
                (root / name).mkdir()
            source = (PROJECT / 'build/lib/build-initramfs.sh').read_text()
            start = source.index('if [ "$charger_mode" = 1 ]; then')
            section = source[start:source.index('\nfi\n', start) + 4]
            env = dict(os.environ, LC_ALL='C', charger_mode='1', staging=str(root),
                       charger_mode_source=str(SOURCE / 'liuqin-charger-mode'),
                       charger_key_source=str(SOURCE / 'liuqin-charger-mode-power-key.c'),
                       charger_exit_source=str(SOURCE / 'liuqin-charger-mode-exit.c'),
                       charger_ui_source=str(SOURCE / 'liuqin-charger-mode-ui.c'),
                       slot_success_gcc=os.environ['SLOT_SUCCESS_GCC'])
            subprocess.run(['sh', '-eu', '-c', section], env=env, check=True, timeout=30)
            marker = (root / 'etc/liuqin-charger-mode').read_bytes()
            self.assertEqual(marker, b'hold-v2\n')
            self.assertIn(hashlib.sha256(marker).hexdigest(), (PROJECT / 'device/boot/initramfs/init').read_text())
            for name in ('power-key', 'exit', 'ui'):
                binary = root / ('bin/liuqin-charger-mode-' + name)
                content = binary.read_bytes()
                self.assertEqual(content[:6], b'\x7fELF\x02\x01')
                self.assertEqual(int.from_bytes(content[18:20], 'little'), 183)
                self.assertNotIn('INTERP', subprocess.check_output(['readelf', '-l', str(binary)], text=True))
            if shutil.which('qemu-aarch64'):
                preview = root / 'preview.ppm'
                subprocess.run(['qemu-aarch64', str(root / 'bin/liuqin-charger-mode-ui'),
                                '--preview', str(preview), '68', 'Charging'], check=True, timeout=10)
                self.assertEqual(len(preview.read_bytes()), len(b'P6\n450 720\n255\n') + 450 * 720 * 3)

    def test_renderer_against_kms_faults(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'ui-test'
            subprocess.run(['cc', '-std=c11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie',
                            str(PROJECT / 'tests/charger-mode-ui.c'), '-o', str(binary),
                            *['-Wl,--wrap=' + name for name in
                              ('open', 'fstat', 'close', 'ioctl', 'mmap', 'munmap',
                               'geteuid', 'access', 'clock_gettime', 'nanosleep', 'fopen')]], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)

    def test_real_exit_helper_dispatch_without_any_real_syscall(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            code = path / 'exit-test.c'
            code.write_text('''#define main charger_exit_main
#include "''' + str(SOURCE / 'liuqin-charger-mode-exit.c') + '''"
#undef main
#include <assert.h>
#include <stdarg.h>
static unsigned int command; static int calls, synced, nonroot;
uid_t __wrap_geteuid(void) { return nonroot ? 1000 : 0; }
void __wrap_sync(void) { synced++; }
long __wrap_syscall(long number, ...) {
    assert(number == SYS_reboot && synced);
    va_list args; va_start(args, number);
    assert(va_arg(args, unsigned int) == LINUX_REBOOT_MAGIC1);
    assert(va_arg(args, unsigned int) == LINUX_REBOOT_MAGIC2);
    command = va_arg(args, unsigned int); char *reason = va_arg(args, char *);
    if (command == LINUX_REBOOT_CMD_POWER_OFF) assert(!reason);
    else assert(command == LINUX_REBOOT_CMD_RESTART2 && reason);
    va_end(args); calls++; errno = EIO; return -1;
}
int main(void) {
    char *args[] = {"exit", "poweroff", NULL};
    assert(charger_exit_main(2,args) == 1 && calls == 1 && command == LINUX_REBOOT_CMD_POWER_OFF);
    args[1] = "normal"; assert(charger_exit_main(2,args) == 1 && command == LINUX_REBOOT_CMD_RESTART2);
    args[1] = "bootloader"; assert(charger_exit_main(2,args) == 1 && calls == 3);
    args[1] = "invalid"; assert(charger_exit_main(2,args) == 64 && calls == 3);
    nonroot = 1; args[1] = "poweroff"; assert(charger_exit_main(2,args) == 2 && calls == 3);
    return 0;
}
''')
            binary = path / 'exit-test'
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', str(code), '-o', str(binary),
                            '-Wl,--wrap=syscall', '-Wl,--wrap=geteuid', '-Wl,--wrap=sync'], check=True)
            subprocess.run([str(binary)], check=True, capture_output=True, timeout=3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
