# cpu-thermal-control

Use CPU load to approach a target temperature with proportional control. Requires
Linux and Python 3.11 or newer; there are no external dependencies.

Edit `config.toml` and run:

```sh
python3 thermal_control.py
# Or use a different configuration:
python3 thermal_control.py /path/to/config.toml
```

Stop with Ctrl+C. The program stops its load workers if a sensor cannot be read;
workers also exit if the controller process disappears.
It uses the hottest discovered CPU sensor, preferring CPU hwmon sensors
(`coretemp`, `k10temp`, `zenpower`, or `cpu_thermal`) and falling back to
CPU thermal zones (`x86_pkg_temp`, `cpu-thermal`, or `cpu_thermal`). If your
system uses a different sensor, set `sensor_path` to its sysfs temperature
file, which must report millidegrees Celsius. Ensure that the file is readable.

Configuration keys:

| Key | Default | Description |
| --- | --- | --- |
| `target_c` | 75.0 | Target temperature in Celsius (above 0 and at most 120). |
| `gain` | 0.1 | Fractional load per degree below target, greater than zero. |
| `sample_interval` | 1.0 | Seconds between temperature readings. |
| `load_period` | 0.25 | Seconds per worker's load duty cycle. |
| `workers` | Available CPUs | Number of load processes; defaults to the process's CPU affinity count. |
| `sensor_path` | Automatic | Optional path to one CPU temperature sensor. |

Load is `clamp(gain * (target_c - measured_c), 0, 1)` per worker. The controller
can add heat but cannot cool the CPU below its idle temperature. It may settle
below the target because proportional control needs an error to produce load.
Other workloads, cooling, frequency scaling, and thermal throttling affect the
result. Monitor the machine and choose a safe target for your hardware; this is
not a replacement for hardware thermal protection.