"""System info tool: report Dragon server status."""

import logging
import os
import time

from dragon_voice.tools.base import Tool

logger = logging.getLogger(__name__)

# Server start time (set at import; close enough for uptime calc)
_import_time = time.time()


def _read_proc_file(path: str) -> str:
    """Read a /proc file, return empty string on failure."""
    try:
        with open(path, "r") as f:
            return f.read()
    except (OSError, PermissionError):
        return ""


class SystemInfoTool(Tool):
    """Get information about the Dragon server (memory, CPU, uptime, load)."""

    @property
    def name(self) -> str:
        return "system_info"

    @property
    def description(self) -> str:
        return "Get information about the Dragon server (memory, CPU, uptime, connections)"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, args: dict) -> dict:
        result = {}

        # Memory from /proc/meminfo
        meminfo = _read_proc_file("/proc/meminfo")
        if meminfo:
            mem = {}
            for line in meminfo.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    try:
                        mem[key] = int(parts[1])  # in kB
                    except ValueError:
                        pass
            total_kb = mem.get("MemTotal", 0)
            avail_kb = mem.get("MemAvailable", 0)
            used_kb = total_kb - avail_kb
            result["ram_total_mb"] = round(total_kb / 1024)
            result["ram_used_mb"] = round(used_kb / 1024)
            result["ram_available_mb"] = round(avail_kb / 1024)
            if total_kb > 0:
                result["ram_percent"] = round(used_kb / total_kb * 100, 1)

        # CPU load from /proc/loadavg
        loadavg = _read_proc_file("/proc/loadavg")
        if loadavg:
            parts = loadavg.split()
            if len(parts) >= 3:
                result["load_1m"] = float(parts[0])
                result["load_5m"] = float(parts[1])
                result["load_15m"] = float(parts[2])
            # Estimate CPU percent from 1m load / number of cores
            try:
                ncpu = os.cpu_count() or 1
                result["cpu_cores"] = ncpu
                result["cpu_percent"] = round(float(parts[0]) / ncpu * 100, 1)
            except (ValueError, IndexError):
                pass

        # Uptime from /proc/uptime
        uptime_str = _read_proc_file("/proc/uptime")
        if uptime_str:
            try:
                uptime_secs = float(uptime_str.split()[0])
                result["system_uptime_hours"] = round(uptime_secs / 3600, 1)
            except (ValueError, IndexError):
                pass

        # Server process uptime
        result["server_uptime_hours"] = round((time.time() - _import_time) / 3600, 1)

        # Disk usage for root partition
        try:
            statvfs = os.statvfs("/")
            total_gb = (statvfs.f_frsize * statvfs.f_blocks) / (1024 ** 3)
            free_gb = (statvfs.f_frsize * statvfs.f_bavail) / (1024 ** 3)
            result["disk_total_gb"] = round(total_gb, 1)
            result["disk_free_gb"] = round(free_gb, 1)
        except OSError:
            pass

        # Python process info
        try:
            result["pid"] = os.getpid()
        except Exception:
            pass

        return result
