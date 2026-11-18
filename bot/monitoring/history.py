"""Non-blocking task lifecycle history with optional MongoDB persistence."""

import asyncio
from collections import deque
from datetime import datetime, timezone
from time import time

from .. import LOGGER, task_dict
from ..core.config_manager import Config
from ..core.tg_client import TgClient
from ..helper.ext_utils.db_handler import database


class HistoryStore:
    def __init__(self, retention_days=30, max_memory_events=1000, queue_size=1000):
        self.retention_days = retention_days
        self.memory_events = deque(maxlen=max_memory_events)
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.writer_task = None
        self._db_available = False
        self._db_failed = False
        self._indexes_ready = False
        self._stopping = False
        self.dropped_events = 0

    @property
    def storage_mode(self):
        if not Config.MONITOR_HISTORY_ENABLED or not getattr(Config, "DATABASE_URL", ""):
            return "process_memory"
        if database.db is None:
            return "process_memory_fallback"
        if self._db_available:
            return "mongodb"
        if self._db_failed:
            return "process_memory_fallback"
        return "mongodb_pending"

    def start(self):
        if self.writer_task is None or self.writer_task.done():
            self.writer_task = asyncio.create_task(self._writer(), name="monitor-history-writer")

    def enqueue(self, event):
        """Fast synchronous enqueue: never waits for MongoDB or queue capacity."""
        self.memory_events.appendleft(event)
        try:
            self.queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self.dropped_events += 1
            return False

    async def _ensure_indexes(self):
        if self._indexes_ready or database.db is None:
            return
        collection = database.db["monitoring_history"]
        index_specs = (
            ([("bot_id", 1), ("task_id", 1), ("event_at", -1)], {}),
            ([("bot_id", 1), ("gid", 1)], {}),
            ([("bot_id", 1), ("event_type", 1), ("event_at", -1)], {}),
            ("event_at", {
                "expireAfterSeconds": self.retention_days * 86400,
                "name": "monitoring_history_ttl",
            }),
        )
        for keys, options in index_specs:
            try:
                await collection.create_index(keys, **options)
            except Exception as exc:
                # Index conflicts (for example, an existing TTL value) must not
                # prevent history events from being persisted.
                LOGGER.warning(f"Monitoring history index creation skipped: {type(exc).__name__}")
        self._indexes_ready = True

    async def _writer(self):
        while not self._stopping or not self.queue.empty():
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                if not Config.MONITOR_HISTORY_ENABLED or not getattr(Config, "DATABASE_URL", ""):
                    self._db_available = False
                    self._db_failed = False
                    continue
                if database.db is None:
                    self._db_available = False
                    self._db_failed = True
                    continue
                await self._ensure_indexes()
                document = {
                    key: value
                    for key, value in event.items()
                    if key not in ("_event_dt", "event_at_iso")
                }
                await database.db["monitoring_history"].insert_one(document)
                self._db_available = True
                self._db_failed = False
            except Exception as exc:
                self._db_available = False
                self._db_failed = True
                LOGGER.warning(f"Monitoring history persistence unavailable: {type(exc).__name__}")
            finally:
                self.queue.task_done()

    async def stop(self, timeout=5):
        self._stopping = True
        if self.writer_task:
            try:
                await asyncio.wait_for(self.writer_task, timeout=timeout)
            except asyncio.TimeoutError:
                self.writer_task.cancel()

    async def query(self, task_id=None, limit=50, before=None):
        limit = min(max(int(limit), 1), 200)
        bot_id = str(TgClient.ID)
        if Config.MONITOR_HISTORY_ENABLED and database.db is not None:
            try:
                query = {"bot_id": bot_id}
                if task_id is not None:
                    query["task_id"] = str(task_id)
                if before is not None:
                    query["event_at"] = {"$lt": before}
                cursor = (
                    database.db["monitoring_history"]
                    .find(query)
                    .sort("event_at", -1)
                    .limit(limit)
                )
                docs = await cursor.to_list(length=limit)
                self._db_available = True
                self._db_failed = False
                for doc in docs:
                    doc["id"] = str(doc.pop("_id", ""))
                    if isinstance(doc.get("event_at"), datetime):
                        event_at = doc["event_at"]
                        if event_at.tzinfo is None:
                            event_at = event_at.replace(tzinfo=timezone.utc)
                        doc["event_at"] = event_at.isoformat()
                return docs, "mongodb"
            except Exception as exc:
                self._db_available = False
                self._db_failed = True
                LOGGER.warning(f"Monitoring history query failed: {type(exc).__name__}")
        events = [
            event for event in self.memory_events
            if (task_id is None or event.get("task_id") == str(task_id))
            and (before is None or event.get("_event_dt") < before)
        ]
        docs = []
        for event in events[:limit]:
            clean = dict(event)
            clean.pop("_event_dt", None)
            clean["id"] = clean.get("event_id")
            docs.append(clean)
        return docs, self.storage_mode


def _task_gid(task_key):
    task = task_dict.get(task_key)
    if task is None:
        return None
    try:
        gid = task.gid()
    except Exception:
        gid = getattr(task, "_gid", None)
    if gid is None:
        return None
    import re

    gid = str(gid)[:256]
    return None if re.match(r"^[a-z][a-z0-9+.-]*:", gid, re.IGNORECASE) else gid


def record_task_event(listener, event_type, state, error_category=None):
    """Record an event if the monitoring history service is active."""
    store = _history_store
    if store is None:
        return
    now = datetime.now(timezone.utc)
    task_key = getattr(listener, "mid", None)
    if task_key is None:
        return
    message = getattr(listener, "message", None)
    chat = getattr(message, "chat", None)
    start = getattr(listener, "download_start_time", None)
    if start is None and message is not None:
        try:
            start = message.date.timestamp()
        except Exception:
            start = None
    duration = max(0, int(time() - start)) if start else None
    size_bytes = getattr(listener, "size", None)
    try:
        size_bytes = int(size_bytes) if size_bytes is not None else None
    except (TypeError, ValueError):
        size_bytes = None
    task_name = str(getattr(listener, "name", None) or "Unknown task")
    import re

    task_name = re.sub(r"(?:https?|ftp)://\S+|magnet:\?\S+", "[redacted-url]", task_name, flags=re.IGNORECASE)
    if task_name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", task_name):
        task_name = re.split(r"[/\\]", task_name)[-1] or "Unknown task"
    event = {
        "event_id": f"{task_key}:{event_type}:{int(now.timestamp() * 1000)}",
        "bot_id": str(TgClient.ID),
        "task_id": str(task_key),
        "task_key": str(task_key),
        "gid": _task_gid(task_key),
        "name": task_name[:512],
        "event_type": str(event_type),
        "state": str(state),
        "error_category": str(error_category)[:100] if error_category else None,
        "event_at": now,
        "event_at_iso": now.isoformat(),
        "duration_seconds": duration,
        "size_bytes": size_bytes,
        "origin": {
            "chat_id": getattr(chat, "id", None),
            "message_id": getattr(message, "id", None),
        },
        "_event_dt": now,
    }
    store.enqueue(event)


_history_store = None


def set_history_store(store):
    global _history_store
    _history_store = store
