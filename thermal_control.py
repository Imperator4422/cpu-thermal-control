#!/usr/bin/env python3
"""Maintain a CPU temperature by varying the amount of CPU work."""

import argparse
from dataclasses import dataclass
import math
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time
import tomllib


@dataclass(frozen=True)
class Config:
    target_c: float
    gain: float
    sample_interval: float
    load_period: float
    workers: int
    sensor_path: str | None


def available_cpus():
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def load_config(path):
    with open(path, "rb") as file:
        data = tomllib.load(file)
    allowed = {"target_c", "gain", "sample_interval", "load_period", "workers", "sensor_path"}
    unknown = data.keys() - allowed
    if unknown:
        raise ValueError(f"unknown configuration keys: {', '.join(sorted(unknown))}")

    def number(key, default, minimum, maximum=None):
        value = data.get(key, default)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= minimum
                or (maximum is not None and value > maximum)):
            raise ValueError(f"invalid {key}: {value!r}")
        return float(value)

    target = number("target_c", 75.0, 0, 120)
    gain = number("gain", 0.1, 0)
    sample_interval = number("sample_interval", 1.0, 0)
    load_period = number("load_period", 0.25, 0)
    workers = data.get("workers", available_cpus())
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError(f"invalid workers: {workers!r}")
    sensor_path = data.get("sensor_path")
    if sensor_path is not None and (not isinstance(sensor_path, str) or not sensor_path):
        raise ValueError("sensor_path must be a non-empty string")
    return Config(target, gain, sample_interval, load_period, workers, sensor_path)


def _read_text(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def discover_sensors(sys_root=Path("/sys/class")):
    sensors = []
    for hwmon in sorted((sys_root / "hwmon").glob("hwmon*")):
        name = _read_text(hwmon / "name").lower()
        if name not in {"coretemp", "k10temp", "zenpower", "cpu_thermal"}:
            continue
        inputs = sorted(hwmon.glob("temp[0-9]*_input"))
        labels = {path: _read_text(path.with_name(path.name.replace("_input", "_label"))).lower()
                  for path in inputs}
        if name == "coretemp":
            selected = [path for path in inputs if labels[path].startswith("package id")]
            if not selected:
                selected = [path for path in inputs if labels[path].startswith("core ")]
        else:
            selected = [path for path in inputs if labels[path] == "tdie"]
            if not selected:
                selected = [path for path in inputs if labels[path] == "tctl"]
            if not selected:
                selected = [path for path in inputs if not labels[path]]
        sensors.extend(selected)
    if sensors:
        return sensors

    for zone in sorted((sys_root / "thermal").glob("thermal_zone*")):
        zone_type = _read_text(zone / "type").lower()
        if zone_type in {"x86_pkg_temp", "cpu-thermal", "cpu_thermal"}:
            path = zone / "temp"
            if path.exists():
                sensors.append(path)
    if not sensors:
        raise ValueError("no CPU temperature sensor found; set sensor_path in the config")
    return sensors


def read_temperature(sensors):
    temperatures = []
    for path in sensors:
        try:
            temperature = int(path.read_text().strip()) / 1000.0
        except (OSError, ValueError) as error:
            raise ValueError(f"cannot read CPU temperature from {path}: {error}") from error
        if not -40 <= temperature <= 200:
            raise ValueError(f"invalid CPU temperature from {path}: {temperature} C")
        temperatures.append(temperature)
    return max(temperatures)


def proportional_load(target_c, temperature_c, gain):
    return max(0.0, min(1.0, gain * (target_c - temperature_c)))


def load_worker(duty, stop, period, parent_pid):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while not stop.is_set() and os.getppid() == parent_pid:
        with duty.get_lock():
            fraction = duty.value
        start = time.monotonic()
        now = start
        while now < start + fraction * period and not stop.is_set() and os.getppid() == parent_pid:
            refresh = now + 0.05
            while time.monotonic() < min(refresh, start + fraction * period):
                pass
            with duty.get_lock():
                fraction = duty.value
            now = time.monotonic()
        while now < start + period and os.getppid() == parent_pid:
            if stop.wait(min(0.1, start + period - now)):
                return
            now = time.monotonic()


def run(config):
    sensors = [Path(config.sensor_path)] if config.sensor_path else discover_sensors()
    print("CPU sensor(s): " + ", ".join(map(str, sensors)), flush=True)
    duty = multiprocessing.Value("d", 0.0)
    stop = multiprocessing.Event()
    workers = []
    try:
        for _ in range(config.workers):
            worker = multiprocessing.Process(
                target=load_worker, args=(duty, stop, config.load_period, os.getpid()))
            worker.start()
            workers.append(worker)
        while True:
            if any(not worker.is_alive() for worker in workers):
                raise RuntimeError("CPU load worker exited unexpectedly")
            temperature = read_temperature(sensors)
            fraction = proportional_load(config.target_c, temperature, config.gain)
            with duty.get_lock():
                duty.value = fraction
            print(f"CPU: {temperature:.1f} C  target: {config.target_c:.1f} C  load: {fraction:.0%}",
                  flush=True)
            time.sleep(config.sample_interval)
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=2)
            if worker.is_alive():
                worker.terminate()
                worker.join()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default="config.toml", help="TOML configuration file")
    args = parser.parse_args(argv)
    try:
        run(load_config(args.config))
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 0
    except (OSError, ValueError, RuntimeError, tomllib.TOMLDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
