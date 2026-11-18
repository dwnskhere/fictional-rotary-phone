"""Defensive, read-only serialization of WZML task status objects."""

import inspect
import re
from datetime import datetime, timedelta, timezone
from time import time

_SIZE_RE = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([kmgtpe]?i?b)?\s*(?:/s)?\s*$",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", re.I)
_URL_RE = re.compile(r"(?:https?|ftp)://\S+|magnet:\?\S+", re.IGNORECASE)

_STATE_MAP = {
    "Download": "downloading",
    "Upload": "uploading",
    "Clone": "cloning",
    "QueueDl": "queued_download",
    "QueueUp": "queued_upload",
    "Pause": "paused",
    "Archive": "archiving",
    "Extract": "extracting",
    "Split": "splitting",
    "CheckUp": "checking",
    "Seed": "seeding",
    "SamVid": "sample_processing",
    "Convert": "converting",
    "FFmpeg": "ffmpeg_processing",
    "YouTube": "youtube_processing",
    "Metadata": "metadata_processing",
    "QuickCheck": "checking",
    "Verifying": "checking",
    "Repairing": "repairing",
    "Fetching": "fetching",
    "Moving": "moving",
    "Extracting": "extracting",
    "Checking": "checking",
}


def _get(value, name, default=None):
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _first(objects, names):
    for obj in objects:
        for name in names:
            value = _get(obj, name)
            if callable(value):
                try:
                    value = value()
                except Exception:
                    continue
            if value is not None:
                return value
    return None


def parse_bytes(value):
    """Convert numeric byte counts or human-readable sizes to integer bytes."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, timedelta):
        return None
    match = _SIZE_RE.match(str(value).replace(",", ""))
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "B").lower().replace("i", "")
    power = {"b": 0, "kb": 1, "mb": 2, "gb": 3, "tb": 4, "pb": 5, "eb": 6}.get(unit)
    if power is None:
        return None
    return max(0, int(number * (1024**power)))


def parse_seconds(value):
    if value is None or value == "-":
        return None
    if isinstance(value, timedelta):
        return max(0, int(value.total_seconds()))
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().lower()
    if not text or text == "-":
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            parts = [float(x) for x in parts]
            if len(parts) == 3:
                return max(0, int(parts[0] * 3600 + parts[1] * 60 + parts[2]))
            if len(parts) == 2:
                return max(0, int(parts[0] * 60 + parts[1]))
            if len(parts) == 1:
                return max(0, int(parts[0]))
        except ValueError:
            return None
    matches = _TIME_RE.fullmatch(text)
    if matches and any(matches.groups()):
        days, hours, minutes, seconds = (int(x or 0) for x in matches.groups())
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    try:
        return max(0, int(float(text)))
    except ValueError:
        return None


def parse_rate(value):
    return parse_bytes(value)


def _call(obj, name, default=None):
    method = _get(obj, name)
    if not callable(method):
        return method if method is not None else default
    try:
        return method()
    except Exception:
        return default


async def _call_async(obj, name, default=None):
    method = _get(obj, name)
    if not callable(method):
        return method if method is not None else default
    try:
        result = method()
        if inspect.isawaitable(result):
            return await result
        return result
    except Exception:
        return default


def _safe_label(value, max_length=512):
    text = str(value or "Unknown task")
    text = _URL_RE.sub("[redacted-url]", text)
    # Do not expose an absolute host/container path as a task label.
    if text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", text):
        text = re.split(r"[/\\]", text)[-1] or "Unknown task"
    return text[:max_length]


def _iso(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=timezone.utc)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _origin(listener):
    message = _get(listener, "message")
    chat = _get(message, "chat")
    chat_id = _get(chat, "id")
    message_id = _get(message, "id")
    try:
        chat_id = int(chat_id) if chat_id is not None else None
    except (TypeError, ValueError):
        chat_id = None
    try:
        message_id = int(message_id) if message_id is not None else None
    except (TypeError, ValueError):
        message_id = None
    return {"chat_id": chat_id, "message_id": message_id}


def _raw_objects(status_obj):
    listener = _get(status_obj, "listener")
    return [
        status_obj,
        _get(status_obj, "_download"),
        _get(status_obj, "_info"),
        _get(status_obj, "_obj"),
        _get(status_obj, "_task"),
        listener,
    ]


def _progress(status_obj, raw_objects):
    # Wrapper methods normalize engine-specific conventions (for example,
    # qBittorrent's 0..1 fraction becomes a 0..100 percentage).
    raw = _call(status_obj, "progress_raw")
    if raw is None:
        raw = _call(status_obj, "progress")
    if raw is None:
        raw = _first(raw_objects[1:], ("progress_raw", "progress"))
    try:
        result = float(str(raw).strip().rstrip("%"))
        return min(100.0, max(0.0, round(result, 2)))
    except (ValueError, TypeError):
        return None


def _extract_bytes(status_obj, raw_objects, kind):
    if kind == "processed":
        names = (
            "completedLength",
            "downloaded",
            "bytesLoaded",
            "downloaded_bytes",
            "processed_bytes",
            "transferred_size",
        )
        raw = _first(raw_objects[1:], names)
        if raw is None:
            raw = _call(status_obj, "processed_bytes")
    elif kind == "total":
        names = ("totalLength", "size", "bytesTotal", "total_size")
        raw = _first(raw_objects[1:], names)
        if raw is None:
            raw = _call(status_obj, "size")
    elif kind == "uploaded":
        names = ("uploadLength", "uploaded", "uploaded_bytes", "bytesUploaded")
        raw = _first(raw_objects[1:], names)
        if raw is None:
            raw = _call(status_obj, "uploaded_bytes")
    else:
        return None
    return parse_bytes(raw)


def _extract_rate(status_obj, raw_objects, kind):
    if kind == "download":
        names = ("downloadSpeed", "dlspeed", "download_speed", "speed_raw", "speed")
        method_names = ("speed",)
    else:
        names = ("uploadSpeed", "upspeed", "upload_speed", "seed_speed_raw")
        method_names = ("seed_speed", "speed")
    raw = _first(raw_objects[1:], names)
    if raw is None:
        for method in method_names:
            raw = _call(status_obj, method)
            if raw is not None:
                break
    return parse_rate(raw)


def _extract_eta(status_obj, raw_objects):
    raw = _first(raw_objects[1:], ("eta_raw", "eta", "timeleft"))
    if raw is None:
        raw = _call(status_obj, "eta")
    return parse_seconds(raw)


def normalize_state(raw_state):
    if raw_state is None:
        return "other"
    raw = str(raw_state).strip()
    if raw in _STATE_MAP:
        return _STATE_MAP[raw]
    lowered = raw.lower()
    dynamic = {
        "downloading": "downloading",
        "uploading": "uploading",
        "queued": "queued_download",
        "paused": "paused",
        "seeding": "seeding",
        "checking": "checking",
        "verifying": "checking",
        "repairing": "repairing",
        "extracting": "extracting",
        "moving": "moving",
        "fetching": "fetching",
        "stopped": "paused",
    }
    return dynamic.get(lowered, "other")


async def serialize_task(task_key, task_obj, queue_info=None, timeout=8):
    """Serialize one WZML status object. Engine refresh errors stay task-local."""
    import asyncio

    try:
        raw_state = await asyncio.wait_for(_call_async(task_obj, "status"), timeout)
    except Exception:
        raw_state = None
    state = normalize_state(raw_state)
    listener = _get(task_obj, "listener")
    raw_objects = _raw_objects(task_obj)

    name = _call(task_obj, "name") or _get(listener, "name") or "Unknown task"
    try:
        name = _safe_label(name)
    except Exception:
        name = "Unknown task"

    gid = _call(task_obj, "gid")
    if gid is None:
        gid = _get(task_obj, "_gid")
    if gid is None and _get(task_obj, "_info") is not None:
        gid = _get(_get(task_obj, "_info"), "hash")
    gid = str(gid)[:256] if gid is not None else None
    if gid and (_URL_RE.search(gid) or re.match(r"^[a-z][a-z0-9+.-]*:", gid, re.IGNORECASE)):
        gid = None

    listener_message = _get(listener, "message")
    message_date = _get(listener_message, "date")
    start_time = _get(listener, "download_start_time") or _get(task_obj, "start_time")
    if start_time is None and message_date is not None:
        try:
            start_time = message_date.timestamp()
        except Exception:
            start_time = None
    elapsed = max(0, int(time() - float(start_time))) if start_time else None

    total_bytes = _extract_bytes(task_obj, raw_objects, "total")
    processed_bytes = _extract_bytes(task_obj, raw_objects, "processed")
    uploaded_bytes = _extract_bytes(task_obj, raw_objects, "uploaded")
    progress = _progress(task_obj, raw_objects)
    if progress is None and total_bytes and processed_bytes is not None:
        progress = min(100.0, round(processed_bytes * 100 / total_bytes, 2))

    download_speed = _extract_rate(task_obj, raw_objects, "download")
    upload_speed = _extract_rate(task_obj, raw_objects, "upload")
    if state in ("uploading", "seeding") and download_speed is not None:
        # Some wrappers expose a generic speed() for the current upload stage.
        if upload_speed is None:
            upload_speed = download_speed
        download_speed = None
    eta = _extract_eta(task_obj, raw_objects)

    seeders = _call(task_obj, "seeders_num")
    leechers = _call(task_obj, "leechers_num")
    ratio = _call(task_obj, "ratio")
    try:
        seeders = int(seeders) if seeders is not None else None
    except (TypeError, ValueError):
        seeders = None
    try:
        leechers = int(leechers) if leechers is not None else None
    except (TypeError, ValueError):
        leechers = None
    try:
        ratio = float(ratio) if ratio is not None else None
    except (TypeError, ValueError):
        ratio = None

    mode = _get(listener, "mode")
    mode_in = mode[0] if isinstance(mode, (tuple, list)) and len(mode) > 0 else None
    mode_out = mode[1] if isinstance(mode, (tuple, list)) and len(mode) > 1 else None

    aria_info = _get(task_obj, "_download")
    qbit_info = _get(task_obj, "_info")
    is_torrent = bool(
        _get(task_obj, "seeding", False)
        or (isinstance(aria_info, dict) and aria_info.get("infoHash"))
        or (qbit_info is not None and _get(qbit_info, "hash") and _get(qbit_info, "num_seeds") is not None)
        or callable(_get(task_obj, "hash"))
    )

    return {
        "task_key": str(task_key),
        "gid": gid,
        "name": name,
        "state": state,
        "raw_state": str(raw_state) if raw_state is not None else None,
        "progress_percent": progress,
        "processed_bytes": processed_bytes,
        "total_bytes": total_bytes,
        "download_speed_bytes_per_second": download_speed,
        "upload_speed_bytes_per_second": upload_speed,
        "eta_seconds": eta,
        "elapsed_seconds": elapsed,
        "engine": str(_get(task_obj, "engine") or "Unknown"),
        "is_torrent": is_torrent,
        "seeders": seeders,
        "leechers": leechers,
        "uploaded_bytes": uploaded_bytes,
        "ratio": ratio,
        "mode_in": str(mode_in) if mode_in is not None else None,
        "mode_out": str(mode_out) if mode_out is not None else None,
        "started_at": _iso(start_time),
        "origin": _origin(listener),
        "queue": queue_info or None,
    }
