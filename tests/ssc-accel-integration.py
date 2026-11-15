#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise SSC accelerometer and ambient light through the real D-Bus proxy."""

import importlib.util
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} UPSTREAM_SSC_TEST")

    test_path = pathlib.Path(sys.argv[1])
    spec = importlib.util.spec_from_file_location(
        "iio_sensor_proxy_ssc_test", test_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load upstream test: {test_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    upstream_test = module.Tests.test_ssc_accel
    if not getattr(upstream_test, "__unittest_skip__", False):
        raise RuntimeError("upstream SSC accelerometer test contract changed")

    class LiuqinSSCTests(module.Tests):
        @staticmethod
        def add_liuqin_light(testbed, with_accel=False):
            testbed.add_device(
                "misc", "fastrpc-sdsp", None,
                ["name", "SSC Test Light Sensor"],
                ["DEVNAME", "/dev/fastrpc-sdsp", "IIO_SENSOR_PROXY_TYPE",
                 "ssc-light ssc-accel" if with_accel else "ssc-light"],
            )

        def assert_light_reading(self):
            self.assertEventually(lambda: self.get_dbus_property("LightLevel") == 7)
            self.assertEqual(self.get_dbus_property("LightLevelUnit"), "lux")
            self.assertEqual(self.daemon.poll(), None, "daemon crashed")

        def test_liuqin_ssc_light(self):
            self.add_liuqin_light(self.testbed)
            self.start_daemon()
            # Discovery/open_sync iterates the main context. Bus ownership
            # does not mean asynchronous sensor publication is complete.
            self.assertEventually(lambda: bool(self.get_dbus_property("HasAmbientLight")))
            self.assertEqual(self.get_dbus_property("HasAccelerometer"), False)
            self.proxy.ClaimLight()
            self.assert_light_reading()
            self.proxy.ReleaseLight()
            self.assertEventually(lambda: self.count_text_in_log("Disabling sensor (") >= 2)
            before = self.count_text_in_log("Light level sent by driver")
            self.proxy.ClaimLight()
            self.assertEventually(lambda: self.count_text_in_log("Light level sent by driver") > before)
            self.assert_light_reading()

        def test_liuqin_ssc_light_during_coldplug(self):
            self.add_liuqin_light(self.testbed)
            self.start_daemon_before_coldplug_finishes()
            self.assertEqual(self.get_dbus_property("HasAmbientLight"), False)
            self.proxy.ClaimLight()
            self.resume_mock_server()
            self.assert_light_reading()
            self.assertEqual(self.get_dbus_property("HasAmbientLight"), True)
            self.assertEqual(self.count_text_in_log("Enabling sensor ("), 1)

        def test_liuqin_ssc_light_released_during_coldplug(self):
            self.add_liuqin_light(self.testbed)
            self.start_daemon_before_coldplug_finishes()
            self.proxy.ClaimLight()
            self.proxy.ReleaseLight()
            self.resume_mock_server()
            self.assertEventually(lambda: bool(self.get_dbus_property("HasAmbientLight")))
            self.assertEqual(self.count_text_in_log("Enabling sensor ("), 0)
            self.proxy.ClaimLight()
            self.assert_light_reading()

        def test_liuqin_ssc_light_and_accel(self):
            self.add_liuqin_light(self.testbed, with_accel=True)
            self.start_daemon()
            self.assertEventually(lambda: bool(self.get_dbus_property("HasAmbientLight")))
            self.assertEventually(lambda: bool(self.get_dbus_property("HasAccelerometer")))
            self.proxy.ClaimAccelerometer()
            self.proxy.ClaimLight()
            self.assert_light_reading()
            self.assertEventually(lambda: self.get_dbus_property("AccelerometerOrientation") == "left-up")
            self.proxy.ReleaseLight()
            self.assertEqual(self.get_dbus_property("HasAccelerometer"), True)
            self.assertEqual(self.get_dbus_property("AccelerometerOrientation"), "left-up")
            self.proxy.ClaimLight()
            self.assert_light_reading()
            self.proxy.ReleaseAccelerometer()
            self.assertEqual(self.get_dbus_property("HasAmbientLight"), True)

        def start_daemon_before_coldplug_finishes(self):
            """Own the proxy as soon as its bus name appears, before SSC discovery."""
            # Hold the real mock server's responses while D-Bus dispatch runs
            # inside libssc's synchronous discovery. Do not rely on a slow CPU
            # or QEMU to win a race against discovery on native test hosts.
            self.mock_pid = int(os.environ["LIUQIN_SSC_TEST_SERVER_PID"])
            if self.mock_pid <= 1:
                raise ValueError("invalid dedicated SSC mock server PID")
            os.kill(self.mock_pid, signal.SIGSTOP)
            self.addCleanup(self.resume_mock_server)
            env = os.environ.copy()
            env["G_DEBUG"] = "fatal-criticals"
            env["G_MESSAGES_DEBUG"] = "all"
            env["UMOCKDEV_DEBUG"] = "all"
            env["UMOCKDEV_DIR"] = self.testbed.get_root_dir()
            self.log = tempfile.NamedTemporaryFile()
            self.daemon = subprocess.Popen(
                [self.daemon_path, "-v"],
                env=env,
                stdout=self.log,
                stderr=subprocess.STDOUT,
            )

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                proxy = module.Gio.DBusProxy.new_sync(
                    self.dbus,
                    module.Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                    None,
                    module.SP,
                    module.SP_PATH,
                    module.SP,
                    None,
                )
                if proxy.get_name_owner() is not None:
                    self.proxy = proxy
                    break
                time.sleep(0.02)
            else:
                self.fail("SensorProxy did not own its D-Bus name in 10 seconds")

            self.assertEqual(self.daemon.poll(), None, "daemon crashed")

        def resume_mock_server(self):
            os.kill(self.mock_pid, signal.SIGCONT)

        @staticmethod
        def add_liuqin_accelerometer(testbed):
            testbed.add_device(
                "misc",
                "ssc-accel",
                None,
                ["name", "SSC Test Accelerometer Sensor"],
                [
                    "NAME",
                    '"SSC Accelerometer Sensor"',
                    "DEVNAME",
                    "/dev/fastrpc-sdsp",
                    "IIO_SENSOR_PROXY_TYPE",
                    "ssc-accel",
                ],
            )

        def test_liuqin_ssc_accel(self):
            self.add_liuqin_accelerometer(self.testbed)
            self.start_daemon()
            self.assertEqual(self.get_dbus_property("HasAmbientLight"), False)
            self.assertEventually(
                lambda: bool(self.get_dbus_property("HasAccelerometer"))
            )
            self.assertEqual(self.get_dbus_property("HasProximity"), False)
            self.assertEqual(
                self.get_compass_dbus_property("HasCompass"), False
            )
            self.assertEventually(
                lambda: self.get_dbus_property("AccelerometerOrientation")
                == "undefined"
            )
            self.proxy.ClaimAccelerometer()
            self.assertEventually(
                lambda: self.get_dbus_property("AccelerometerOrientation")
                == "left-up"
            )
            self.stop_daemon()

        def test_liuqin_ssc_claim_during_coldplug(self):
            self.add_liuqin_accelerometer(self.testbed)
            self.start_daemon_before_coldplug_finishes()

            self.assertEqual(self.get_dbus_property("HasAccelerometer"), False)
            self.proxy.ClaimAccelerometer()
            self.resume_mock_server()
            self.assertEventually(
                lambda: self.get_dbus_property("AccelerometerOrientation")
                == "left-up",
                timeout=300,
            )
            self.assertEqual(self.get_dbus_property("HasAccelerometer"), True)

            with open(self.log.name, encoding="utf-8") as daemon_log:
                output = daemon_log.read()
            claim = output.find(
                "Handling driver refcounting method 'ClaimAccelerometer'"
            )
            discovered = output.find("Found device ")
            self.assertGreaterEqual(claim, 0)
            self.assertGreater(discovered, claim)
            self.assertEqual(output.count("Enabling sensor ("), 1)
            self.stop_daemon()

    suite = unittest.TestSuite(
        (
            LiuqinSSCTests("test_liuqin_ssc_accel"),
            LiuqinSSCTests("test_liuqin_ssc_claim_during_coldplug"),
            LiuqinSSCTests("test_liuqin_ssc_light"),
            LiuqinSSCTests("test_liuqin_ssc_light_during_coldplug"),
            LiuqinSSCTests("test_liuqin_ssc_light_and_accel"),
            LiuqinSSCTests("test_liuqin_ssc_light_released_during_coldplug"),
        )
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and result.testsRun == 6 and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
