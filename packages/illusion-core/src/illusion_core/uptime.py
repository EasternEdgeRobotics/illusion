"""Uptime reporting shared by every service.

Durations, never absolute timestamps. The kiosk laptop gets shut down and
carried around, so its clock can drift from the VM's; a duration computed on
the service's own clock is immune to skew between hosts, while an absolute
started_at subtracted against the reader's clock quietly is not.
"""

import platform
import time
from datetime import timedelta


def service_uptime_ms(started_at):
    """Milliseconds since this process started, from its own clock."""
    return round((time.time() - started_at) * 1000)


def system_uptime_ms():
    """None where /proc/uptime does not exist, ie macOS during development."""
    if platform.system() != "Linux":
        return None

    with open("/proc/uptime", "r") as file:
        return round(float(file.readline().split()[0]) * 1000)


def format_duration(uptime_ms):
    if uptime_ms is None:
        return "Unknown"

    td = timedelta(milliseconds=uptime_ms)

    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []

    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts)
