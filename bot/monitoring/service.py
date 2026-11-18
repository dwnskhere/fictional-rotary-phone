"""Cached monitoring snapshots built from WZML's authoritative runtime state."""

import asyncio
from datetime import datetime, timezone
from time import time

from .. import (
    LOGGER,
    non_queued_dl,
    non_queued_up,
    queue_dict_lock,
    queued_dl,
    queued_up,
    task_dict,
    task_dict_lock,
)
from ..core.config_manager import Config
from .history import HistoryStore, set_history_store
from .serializer import serialize_task
from .system_collector import SystemCollector


class MonitoringService:
    def __init__(self, interval=15, max_age=30, history_retention_days=30):
        self.interval = interval
        self.max_age = max_age
        self.system_collector = SystemCollector()
        self.history = HistoryStore(retention_days=history_retention_days)
        self.snapshot = None
        self.last_successful_snapshot = None
        self.collector_task = None
        self.stopping = False
        self._refresh_lock = asyncio.Lock()
        self.last_collection_error = None

    async def start(self):
        set_history_store(self.history)
        self.history.start()
        await self.refresh()
        self.collector_task = asyncio.create_task(self._run(), name="wzml-monitor-collector")

    async def _run(self):
        while not self.stopping:
            # STATUS_UPDATE_INTERVAL can be changed through WZML settings at
            # runtime. Read it before every cycle so monitoring stays aligned.
            self.interval = max(1, int(getattr(Config, "STATUS_UPDATE_INTERVAL", self.interval) or self.interval))
            configured_max_age = max(0, int(getattr(Config, "MONITOR_SNAPSHOT_MAX_AGE", self.max_age) or 0))
            self.max_age = max(self.interval * 2, configured_max_age)
            await asyncio.sleep(self.interval)
            try:
                await self.refresh()
            except Exception as exc:
                self.last_collection_error = type(exc).__name__
                LOGGER.exception("WZML monitoring snapshot failed")

    async def stop(self):
        self.stopping = True
        if self.collector_task:
            self.collector_task.cancel()
            try:
                await self.collector_task
            except asyncio.CancelledError:
                pass
        await self.history.stop()
        set_history_store(None)

    async def refresh(self):
        async with self._refresh_lock:
            async with task_dict_lock:
                task_refs = list(task_dict.items())
            async with queue_dict_lock:
                dl_queue_keys = list(queued_dl.keys())
                up_queue_keys = list(queued_up.keys())
                dl_active_keys = list(non_queued_dl)
                up_active_keys = list(non_queued_up)

            dl_key_set = set(dl_queue_keys)
            up_key_set = set(up_queue_keys)
            dl_positions = {str(key): index + 1 for index, key in enumerate(dl_queue_keys)}
            up_positions = {str(key): index + 1 for index, key in enumerate(up_queue_keys)}
            semaphore = asyncio.Semaphore(8)

            async def serialize_one(key, obj):
                async with semaphore:
                    key_str = str(key)
                    qinfo = None
                    if key in dl_key_set:
                        qinfo = {"type": "download", "position": dl_positions.get(key_str)}
                    elif key in up_key_set:
                        qinfo = {"type": "upload", "position": up_positions.get(key_str)}
                    try:
                        return await serialize_task(key, obj, qinfo, timeout=8)
                    except Exception as exc:
                        return {
                            "task_key": key_str,
                            "gid": None,
                            "name": "Unknown task",
                            "state": "other",
                            "raw_state": None,
                            "progress_percent": None,
                            "processed_bytes": None,
                            "total_bytes": None,
                            "download_speed_bytes_per_second": None,
                            "upload_speed_bytes_per_second": None,
                            "eta_seconds": None,
                            "elapsed_seconds": None,
                            "engine": str(getattr(obj, "engine", "Unknown")),
                            "is_torrent": False,
                            "seeders": None,
                            "leechers": None,
                            "uploaded_bytes": None,
                            "ratio": None,
                            "mode_in": None,
                            "mode_out": None,
                            "started_at": None,
                            "origin": {"chat_id": None, "message_id": None},
                            "queue": qinfo,
                            "serialization_error": type(exc).__name__,
                        }

            tasks = await asyncio.gather(
                *(serialize_one(key, obj) for key, obj in task_refs)
            ) if task_refs else []
            tasks.sort(key=lambda item: (item.get("state") in ("queued_download", "queued_upload"), item.get("name", "").lower()))

            by_key = {task["task_key"]: task for task in tasks}
            queues = {
                "download": {
                    "queued": [
                        {"task_key": str(key), "position": index + 1, "name": by_key.get(str(key), {}).get("name")}
                        for index, key in enumerate(dl_queue_keys)
                    ],
                    "active_task_keys": [str(key) for key in dl_active_keys],
                    "queued_count": len(dl_queue_keys),
                    "active_count": len(dl_active_keys),
                    "limit": int(getattr(Config, "QUEUE_DOWNLOAD", 0) or 0),
                },
                "upload": {
                    "queued": [
                        {"task_key": str(key), "position": index + 1, "name": by_key.get(str(key), {}).get("name")}
                        for index, key in enumerate(up_queue_keys)
                    ],
                    "active_task_keys": [str(key) for key in up_active_keys],
                    "queued_count": len(up_queue_keys),
                    "active_count": len(up_active_keys),
                    "limit": int(getattr(Config, "QUEUE_UPLOAD", 0) or 0),
                },
                "all_limit": int(getattr(Config, "QUEUE_ALL", 0) or 0),
                "total_queued": len(dl_queue_keys) + len(up_queue_keys),
            }

            system = self.system_collector.collect()
            counts = {}
            for task in tasks:
                state = task.get("state", "other")
                counts[state] = counts.get(state, 0) + 1
            counts.update(
                {
                    "total": len(tasks),
                    "queued_download": len(dl_queue_keys),
                    "queued_upload": len(up_queue_keys),
                    "active_download": len(dl_active_keys),
                    "active_upload": len(up_active_keys),
                }
            )
            speeds = {
                "download_bytes_per_second": sum(task.get("download_speed_bytes_per_second") or 0 for task in tasks),
                "upload_bytes_per_second": sum(task.get("upload_speed_bytes_per_second") or 0 for task in tasks),
            }
            now = datetime.now(timezone.utc)
            snapshot = {
                "generated_at": now.isoformat(),
                "generated_at_epoch": time(),
                "tasks": tasks,
                "task_counts": counts,
                "queues": queues,
                "transfer": {
                    **speeds,
                    "network_sent_bytes_per_second": system.get("network", {}).get("sent_bytes_per_second"),
                    "network_received_bytes_per_second": system.get("network", {}).get("received_bytes_per_second"),
                    "network_bytes_sent": system.get("network", {}).get("bytes_sent"),
                    "network_bytes_received": system.get("network", {}).get("bytes_recv"),
                    "network_scope": "host_or_container",
                    "wzml_bandwidth_accounting": system.get("bandwidth_accounting", {}),
                },
                "system": system,
                "collector": {
                    "interval_seconds": self.interval,
                    "task_count": len(tasks),
                    "warnings": [task["serialization_error"] for task in tasks if task.get("serialization_error")],
                },
            }
            self.snapshot = snapshot
            self.last_successful_snapshot = now
            self.last_collection_error = None
            return snapshot

    def _meta(self):
        now = datetime.now(timezone.utc)
        generated_epoch = self.snapshot.get("generated_at_epoch") if self.snapshot else None
        age = max(0, now.timestamp() - generated_epoch) if generated_epoch else None
        stale = self.snapshot is None or age is None or age > self.max_age
        return {
            "generated_at": self.snapshot.get("generated_at") if self.snapshot else None,
            "last_successful_snapshot_at": self.last_successful_snapshot.isoformat() if self.last_successful_snapshot else None,
            "snapshot_age_seconds": round(age, 2) if age is not None else None,
            "stale": stale,
            "collector_error": self.last_collection_error,
        }

    def health(self, bridge_ready=True):
        from ..version import get_version

        return {
            "source": "WZML-HK",
            "wzml_version": get_version(),
            "api": "online" if bridge_ready else "offline",
            "bridge": "online" if bridge_ready else "offline",
            "snapshot": self._meta(),
            "history_storage": self.history.storage_mode,
            "history_dropped_events": self.history.dropped_events,
            "api_version": "v1",
        }

    def summary(self):
        snap = self.snapshot or {}
        system = snap.get("system", {})
        return {
            "task_counts": snap.get("task_counts", {"total": 0}),
            "transfer": snap.get("transfer", {}),
            "system": {
                "cpu_percent": system.get("cpu", {}).get("percent"),
                "ram_percent": system.get("memory", {}).get("percent"),
                "disk_download": system.get("disk", {}).get("download", {}),
                "bot_uptime_seconds": system.get("runtime", {}).get("bot_uptime_seconds"),
            },
            "queues": snap.get("queues", {}),
        }

    def tasks(self, offset=0, limit=50):
        tasks = (self.snapshot or {}).get("tasks", [])
        offset = max(0, int(offset))
        limit = min(max(1, int(limit)), 200)
        return {"total": len(tasks), "offset": offset, "limit": limit, "tasks": tasks[offset : offset + limit]}

    def get_task(self, task_key):
        for task in (self.snapshot or {}).get("tasks", []):
            if task.get("task_key") == str(task_key):
                return task
        return None

    def queues(self):
        return (self.snapshot or {}).get("queues", {})

    def system(self):
        return (self.snapshot or {}).get("system", {})

    def transfer(self):
        return (self.snapshot or {}).get("transfer", {})


monitoring_service = None


async def start_monitoring(settings):
    global monitoring_service
    if monitoring_service is not None:
        return monitoring_service
    monitoring_service = MonitoringService(
        interval=settings["snapshot_interval"],
        max_age=settings["snapshot_max_age"],
        history_retention_days=settings["history_retention_days"],
    )
    await monitoring_service.start()
    return monitoring_service


async def stop_monitoring():
    global monitoring_service
    if monitoring_service is not None:
        await monitoring_service.stop()
        monitoring_service = None
