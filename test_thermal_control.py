import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
            for content in ('workers = 0', 'gain = nan', 'sample_interval = 0',
                            'target_c = 121', 'sensor_path = ""', 'unknown = 1'):
                with self.subTest(content=content):
                    path.write_text(content)
                    with self.assertRaises(ValueError):
                        control.load_config(path)


class WorkerLifecycleTests(unittest.TestCase):
    def test_parent_death_signal_is_installed_before_parent_check(self):
        prctl = Mock(return_value=0)
        with patch("thermal_control.ctypes.CDLL") as cdll, \
                patch("thermal_control.os.getppid", return_value=123):
            cdll.return_value.prctl = prctl
            self.assertTrue(control._set_parent_death_signal(123))
        prctl.assert_called_once_with(control._PR_SET_PDEATHSIG, control.signal.SIGTERM, 0, 0, 0)


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
