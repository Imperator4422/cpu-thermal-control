import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import thermal_control as control


class ProportionalControlTests(unittest.TestCase):
    def test_load_is_proportional_and_clamped(self):
        self.assertEqual(control.proportional_load(75, 80, 0.1), 0)
        self.assertEqual(control.proportional_load(75, 75, 0.1), 0)
        self.assertAlmostEqual(control.proportional_load(75, 70, 0.1), 0.5)
        self.assertEqual(control.proportional_load(75, 60, 0.1), 1)

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('target_c = 70\nworkers = 2\nsensor_path = "/tmp/cpu"\n')
            config = control.load_config(path)
            self.assertEqual((config.target_c, config.workers, config.sensor_path), (70, 2, "/tmp/cpu"))
            self.assertEqual(config.integral_gain, 0.001)
            self.assertTrue(config.log_to_console)
            path.write_text('integral_gain = 0\n')
            self.assertEqual(control.load_config(path).integral_gain, 0)
            path.write_text('log_to_console = false\n')
            self.assertFalse(control.load_config(path).log_to_console)
            for content in ('workers = 0', 'gain = nan', 'sample_interval = 0',
                            'target_c = 121', 'sensor_path = ""', 'unknown = 1',
                            'log_to_console = 1', 'log_to_console = "false"'):
                with self.subTest(content=content):
                    path.write_text(content)
                    with self.assertRaises(ValueError):
                        control.load_config(path)
            for content in ('integral_gain = -0.1', 'integral_gain = nan',
                            'integral_gain = true'):
                with self.subTest(content=content):
                    path.write_text(content)
                    with self.assertRaises(ValueError):
                        control.load_config(path)


class PIControllerTests(unittest.TestCase):
    def test_integral_term_accumulates_with_elapsed_time(self):
        controller = control.PIController(0.05, 0.01)
        self.assertAlmostEqual(controller.update(75, 65, 2), 0.7)
        self.assertAlmostEqual(controller.update(75, 65, 2), 0.9)

    def test_integral_term_does_not_wind_up_at_load_limit(self):
        controller = control.PIController(0.2, 0.01)
        self.assertEqual(controller.update(75, 65, 10), 1)
        self.assertEqual(controller.integral_error, 0)
        self.assertEqual(controller.update(75, 65, 10), 1)
        self.assertEqual(controller.integral_error, 0)

    def test_zero_integral_gain_preserves_proportional_control(self):
        controller = control.PIController(0.1, 0)
        self.assertAlmostEqual(controller.update(75, 70, 10), 0.5)


class WorkerLifecycleTests(unittest.TestCase):
    def test_parent_death_signal_is_installed_before_parent_check(self):
        prctl = Mock(return_value=0)
        with patch("thermal_control.ctypes.CDLL") as cdll, \
                patch("thermal_control.os.getppid", return_value=123):
            cdll.return_value.prctl = prctl
            self.assertTrue(control._set_parent_death_signal(123))
        prctl.assert_called_once_with(control._PR_SET_PDEATHSIG, control.signal.SIGTERM, 0, 0, 0)


class ConsoleLoggingTests(unittest.TestCase):
    def test_status_reports_pi_contributions_and_can_be_disabled(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                config = control.Config(75, 0.05, 0.01, 1, 0.25, 1, "/tmp/cpu", enabled)
                context = MagicMock()
                context.Process.return_value.is_alive.return_value = True
                output = io.StringIO()
                with patch("thermal_control.multiprocessing.get_context", return_value=context), \
                        patch("thermal_control.read_temperature", side_effect=[65, 85]), \
                        patch("thermal_control.time.monotonic", side_effect=[0, 2, 4]), \
                        patch("thermal_control.time.sleep", side_effect=[None, KeyboardInterrupt]), \
                        patch("sys.stdout", output):
                    with self.assertRaises(KeyboardInterrupt):
                        control.run(config)
                self.assertEqual(context.Value.return_value.value, 0)
                context.Event.return_value.set.assert_called_once()
                if enabled:
                    self.assertIn("CPU sensor(s): /tmp/cpu", output.getvalue())
                    self.assertIn("load: 70%  proportional: 50.0%  integral: 20.0%",
                                  output.getvalue())
                    self.assertIn("load: 0%  proportional: -50.0%  integral: 20.0%",
                                  output.getvalue())
                else:
                    self.assertEqual(output.getvalue(), "")


class SensorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def test_discovers_packages_and_uses_hottest(self):
        self.write("hwmon/hwmon0/name", "coretemp")
        package = self.write("hwmon/hwmon0/temp1_input", "65000")
        self.write("hwmon/hwmon0/temp1_label", "Package id 0")
        self.write("hwmon/hwmon0/temp2_input", "99000")
        self.write("hwmon/hwmon0/temp2_label", "Core 0")
        self.write("hwmon/hwmon1/name", "k10temp")
        self.write("hwmon/hwmon1/temp1_input", "90000")
        self.write("hwmon/hwmon1/temp1_label", "Tctl")
        die = self.write("hwmon/hwmon1/temp2_input", "70000")
        self.write("hwmon/hwmon1/temp2_label", "Tdie")
        self.assertEqual(control.discover_sensors(self.root), [package, die])
        self.assertEqual(control.read_temperature([package, die]), 70)

    def test_thermal_zone_fallback_and_invalid_readings(self):
        self.write("thermal/thermal_zone0/type", "battery")
        self.write("thermal/thermal_zone0/temp", "30000")
        self.write("thermal/thermal_zone1/type", "cpu-thermal")
        sensor = self.write("thermal/thermal_zone1/temp", "42000")
        self.assertEqual(control.discover_sensors(self.root), [sensor])
        self.assertEqual(control.read_temperature([sensor]), 42)
        for value in ("not a temperature", "250000"):
            sensor.write_text(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                control.read_temperature([sensor])
        sensor.unlink()
        with self.assertRaises(ValueError):
            control.read_temperature([sensor])

    def test_missing_sensor_fails(self):
        with self.assertRaisesRegex(ValueError, "no CPU temperature sensor"):
            control.discover_sensors(self.root)


if __name__ == "__main__":
    unittest.main()
