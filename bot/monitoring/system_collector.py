"""Host and process metrics collected without blocking the bot event loop."""

import os
from time import time

import psutil

from .. import DOWNLOAD_DIR, bot_start_time


class SystemCollector:
    def __init__(self):
        self._last_sample_at = None
        self._last_net = None
        self._process = None
        try:
            self._process = psutil.Process(os.getpid())
            self._process.cpu_percent(None)
        except Exception:
            self._process = None
        try:
            psutil.cpu_percent(None)
        except Exception:
            pass

    def collect(self):
        now = time()
        result = {
            "sampled_at": now,
            "cpu": {},
            "memory": {},
            "swap": {},
            "disk": {},
            "network": {},
            "runtime": {},
            "process": {},
            "errors": {},
        }

        try:
            cpu_frequency = psutil.cpu_freq()
            result["cpu"] = {
                "percent": psutil.cpu_percent(None),
                "logical_cores": psutil.cpu_count(logical=True),
                "physical_cores": psutil.cpu_count(logical=False),
                "frequency_mhz": cpu_frequency.current if cpu_frequency else None,
                "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            }
        except Exception as exc:
            result["errors"]["cpu"] = type(exc).__name__

        try:
            vm = psutil.virtual_memory()
            result["memory"] = {
                "total_bytes": vm.total,
                "used_bytes": vm.used,
                "available_bytes": vm.available,
                "percent": vm.percent,
            }
        except Exception as exc:
            result["errors"]["memory"] = type(exc).__name__

        try:
            swap = psutil.swap_memory()
            result["swap"] = {
                "total_bytes": swap.total,
                "used_bytes": swap.used,
                "free_bytes": swap.free,
                "percent": swap.percent,
            }
        except Exception as exc:
            result["errors"]["swap"] = type(exc).__name__

        for key, path in (("download", DOWNLOAD_DIR), ("root", "/")):
            try:
                usage = psutil.disk_usage(path)
                result["disk"][key] = {
                    "path_label": key,
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_bytes": usage.free,
                    "percent": usage.percent,
                }
            except Exception as exc:
                result["errors"][f"disk_{key}"] = type(exc).__name__

        try:
            net = psutil.net_io_counters()
            net_now = {
                "bytes_sent": net.bytes_sent,
                "bytes_recv": net.bytes_recv,
                "packets_sent": net.packets_sent,
                "packets_recv": net.packets_recv,
                "errin": net.errin,
                "errout": net.errout,
                "dropin": net.dropin,
                "dropout": net.dropout,
            }
            elapsed = now - self._last_sample_at if self._last_sample_at else 0
            rates = {"sent_bytes_per_second": None, "received_bytes_per_second": None}
            if self._last_net and elapsed > 0:
                sent_delta = net.bytes_sent - self._last_net["bytes_sent"]
                recv_delta = net.bytes_recv - self._last_net["bytes_recv"]
                # Counter decreases indicate reset/restart; establish a new baseline.
                if sent_delta >= 0:
                    rates["sent_bytes_per_second"] = int(sent_delta / elapsed)
                if recv_delta >= 0:
                    rates["received_bytes_per_second"] = int(recv_delta / elapsed)
            result["network"] = {
                **net_now,
                **rates,
                "scope": "host_or_container",
            }
            # Read the existing WZML monthly-bandwidth accounting without calling
            # get_bandwidth_string(), which mutates counters and schedules DB writes.
            try:
                from ..helper.ext_utils import status_utils
                from ..core.config_manager import Config

                raw_total = net.bytes_sent + net.bytes_recv
                historical = int(getattr(status_utils, "_historical_bw", 0) or 0)
                last_raw = int(getattr(status_utils, "_last_raw_bw", 0) or 0)
                effective_raw = last_raw if raw_total < last_raw else raw_total
                used = historical + effective_raw
                limit = int(getattr(Config, "MONTHLY_BANDWIDTH", 0) or 0) * (1024**3)
                result["bandwidth_accounting"] = {
                    "used_bytes": used,
                    "limit_bytes": limit or None,
                    "percent": round((used / limit) * 100, 2) if limit else None,
                    "source": "WZML_existing_counters",
                }
            except Exception as exc:
                result["errors"]["bandwidth_accounting"] = type(exc).__name__
            self._last_net = net_now
        except Exception as exc:
            result["errors"]["network"] = type(exc).__name__

        try:
            disk_io = psutil.disk_io_counters()
            if disk_io:
                result["disk_io"] = {
                    "read_bytes": disk_io.read_bytes,
                    "write_bytes": disk_io.write_bytes,
                    "read_count": disk_io.read_count,
                    "write_count": disk_io.write_count,
                }
        except Exception as exc:
            result["errors"]["disk_io"] = type(exc).__name__

        try:
            result["runtime"] = {
                "bot_uptime_seconds": max(0, int(now - bot_start_time)),
                "host_uptime_seconds": max(0, int(now - psutil.boot_time())),
            }
        except Exception as exc:
            result["errors"]["runtime"] = type(exc).__name__

        if self._process is not None:
            try:
                result["process"] = {
                    "pid": self._process.pid,
                    "rss_bytes": self._process.memory_info().rss,
                    "cpu_percent": self._process.cpu_percent(None),
                }
            except Exception as exc:
                result["errors"]["process"] = type(exc).__name__

        self._last_sample_at = now
        return result
