"""Read-only hardware inventory. No input capture, serial numbers or writes."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import subprocess


def read_value(path):
    try:
        return {"value": path.read_text(encoding="utf-8").strip(), "error": None}
    except OSError as exc:
        return {"value": None, "error": {"errno": exc.errno, "message": exc.strerror}}


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=8)
        return {"stdout": result.stdout.strip(), "stderr": result.stderr.strip(),
                "returncode": result.returncode}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}


def number(fields, key):
    try:
        return float(fields.get(key, {}).get("value"))
    except (TypeError, ValueError):
        return None


def battery_metrics(fields):
    """Never divide energy by charge; sysfs uses micro-units for both."""
    metrics = {}
    for unit, suffix in (("energy", "wh"), ("charge", "ah")):
        for kind in ("now", "full", "full_design"):
            value = number(fields, f"{unit}_{kind}")
            if value is not None:
                metrics[f"{unit}_{kind}_{suffix}"] = value / 1_000_000
        full = number(fields, f"{unit}_full")
        design = number(fields, f"{unit}_full_design")
        if "estimated_health_percent" not in metrics and full is not None and full >= 0 and design is not None and design > 0:
            metrics["estimated_health_percent"] = round(full / design * 100, 2)
            metrics["health_basis"] = unit
    power = number(fields, "power_now")
    if power is not None:
        metrics["battery_power_w"] = power / 1_000_000
    voltage = number(fields, "voltage_now")
    if voltage is not None:
        metrics["voltage_v"] = voltage / 1_000_000
        current = number(fields, "current_now")
        if power is None and current is not None and voltage > 0:
            metrics["battery_power_w"] = round(abs(current) * voltage / 1e12, 3)
    energy = number(fields, "energy_now")
    if fields.get("status", {}).get("value") == "Discharging" and energy is not None and energy >= 0 and power is not None and power > 0:
        metrics["instantaneous_hours_remaining"] = round(energy / power, 2)
    return metrics


BATTERY_FIELDS = (
    "present", "status", "capacity", "capacity_level", "technology", "manufacturer",
    "model_name", "cycle_count", "energy_now", "energy_full", "energy_full_design",
    "charge_now", "charge_full", "charge_full_design", "power_now", "current_now",
    "voltage_now", "temp", "time_to_full_now", "time_to_empty_now", "charge_control_start_threshold",
    "charge_control_end_threshold", "charge_behaviour",
)


def collect(root=Path("/"), live=True):
    sys = root / "sys"
    batteries = []
    for device in sorted((sys / "class/power_supply").glob("*")):
        if read_value(device / "type")["value"] != "Battery":
            continue
        fields = {key: read_value(device / key) for key in BATTERY_FIELDS}
        batteries.append({"name": device.name, "fields": fields, "metrics": battery_metrics(fields)})
    eco = []
    # These are ACPI IDs handled by upstream panasonic-laptop; do not search all sysfs.
    for acpi_id in ("MAT0012", "MAT0013", "MAT0018", "MAT0019"):
        for dev in sorted((sys / "bus/acpi/devices").glob(acpi_id + ":*")):
            path = dev / "eco_mode"
            if path.exists():
                result = read_value(path)
                eco.append({"path": str(path), **result,
                            "state": "readable-unverified" if result["error"] is None else "read-error"})
    inputs = []
    for event in sorted((sys / "class/input").glob("event*")):
        dev = event / "device"
        inputs.append({"event": event.name, "name": read_value(dev / "name"),
                       "id": {key: read_value(dev / "id" / key) for key in ("bustype", "vendor", "product", "version")},
                       "properties": read_value(dev / "properties")})
    report = {
        "schema_version": 1, "read_only": True,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "dmi": {key: read_value(sys / "class/dmi/id" / key) for key in
                ("sys_vendor", "product_name", "product_version", "bios_version")},
        "os_release": read_value(root / "etc/os-release"),
        "batteries": batteries, "panasonic_eco": eco, "inputs": inputs,
        "mem_sleep": read_value(sys / "power/mem_sleep"),
        "notes": ["Readable ECO state does not prove charging control works.",
                  "Battery power on AC does not measure whole-system consumption.",
                  "cycle_count=0 is a firmware report, not proof of a new battery.",
                  "Missing or failing sensor reads remain explicit errors."],
    }
    if live:
        report["kernel"] = platform.release()
        report["packages"] = command(["dpkg-query", "-W", "libinput10", "gnome-shell", "power-profiles-daemon", "upower", "tlp"])
        from .power_profiles import get_active, ProfileError
        try:
            report["power_profile"] = {"stdout": get_active(), "stderr": "", "returncode": 0}
        except ProfileError as exc:
            report["power_profile"] = {"error": str(exc)}
        report["power_services"] = {name: command(["systemctl", "is-active", name]) for name in
                                    ("power-profiles-daemon", "tlp", "tuned", "upower")}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["doctor"])
    args = parser.parse_args()
    if args.action == "doctor":
        print(json.dumps(collect(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
