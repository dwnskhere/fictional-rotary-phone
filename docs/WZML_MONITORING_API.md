# WZML-HK Monitoring API

Version: `v1`  
Base path: `/monitor/api/v1`  
Default snapshot interval: 15 seconds  
Access model: authenticated, read-only HTTP API

## 1. Overview

The WZML Monitoring API exposes structured task, queue, system, transfer and lifecycle-history information for authorized external clients such as Graberr.

The monitoring API does not control tasks. It has no HTTP endpoint to start, pause, resume, cancel, delete, reorder or force-run a WZML task.

### Process topology

The WZML bot and its public FastAPI web server run in separate processes. The bot's task dictionaries and queue state are in bot-process memory, so the public web process cannot read them directly.

The implementation therefore uses:

1. A private monitoring HTTP service inside the bot process, bound only to `127.0.0.1` by default.
2. Authenticated public routes registered in the existing `web/wserver.py` FastAPI application.
3. A loopback-only proxy from the public web process to the private service.
4. A background collector that publishes cached snapshots at WZML's configured `STATUS_UPDATE_INTERVAL` (15 seconds by default).

The private service must never be exposed on a public interface. The existing public web port is reused; no second public port is required.

## 2. Configuration

Enable the API in `config.py` or through environment variables:

```python
MONITOR_API_ENABLED = True
MONITOR_API_TOKEN = "REPLACE_WITH_A_LONG_RANDOM_TOKEN"
MONITOR_INTERNAL_SECRET = ""  # Optional separate secret; defaults to public token
MONITOR_INTERNAL_HOST = "127.0.0.1"
MONITOR_INTERNAL_PORT = 8765
MONITOR_SNAPSHOT_MAX_AGE = 30
MONITOR_HISTORY_ENABLED = True
MONITOR_HISTORY_RETENTION_DAYS = 30
```

Generate a public token locally:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Use the same `MONITOR_API_TOKEN` in WZML and the Graberr client configuration. Keep it private. Do not paste it into logs, issue trackers, source control, screenshots or public chat.

`MONITOR_INTERNAL_SECRET` is optional. When blank, the private bridge uses the public token over loopback. For stricter secret separation, configure a different random secret in WZML's config/environment; the public web process reads the same setting for its private request.

The API will not start unless:
- `MONITOR_API_ENABLED` is true;
- `MONITOR_API_TOKEN` is at least 32 characters;
- the private bridge host is loopback-only;
- the internal secret (or token fallback) is at least 32 characters.

The public routes are disabled with `404` when `MONITOR_API_ENABLED` is false.

## 3. Authentication

Every public API request requires:

```http
Authorization: Bearer YOUR_MONITOR_API_TOKEN
```

Responses:
- `401`: token is missing or invalid.
- `404`: API is disabled.
- `429`: per-client-IP request limit exceeded (120 requests per 60-second window).
- `502`: private bridge returned an invalid response.
- `503`: private bridge unavailable or secure configuration missing.

Use HTTPS when connecting from outside the WZML host. The private bridge binds to loopback and uses a separate internal request header; it is not intended for direct external access.

## 4. Endpoints

All endpoints are `GET` and read-only.

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Public API/bridge readiness, snapshot freshness and history storage mode. |
| GET | `/summary` | Task counts, transfer totals, queue counts and key system metrics. |
| GET | `/tasks` | Paginated active-task snapshot. |
| GET | `/tasks/{task_key}` | One task from the latest snapshot. |
| GET | `/queues` | Download/upload queue positions and active task keys. |
| GET | `/system` | CPU, memory, swap, disk, network, disk I/O, process and runtime metrics. |
| GET | `/transfer` | WZML task transfer rates and host/container network counters/rates. |
| GET | `/history` | Paginated lifecycle events. |
| GET | `/history/{task_id}` | One task's recent lifecycle timeline. |

Task/history pagination:
- `offset`: zero-based offset for `/tasks`.
- `limit`: 1–200; defaults to 50.
- `before`: ISO-8601 timestamp for history pagination.

## 5. Response envelope

Successful responses use:

```json
{
  "success": true,
  "data": {},
  "meta": {
    "generated_at": "2026-11-14T12:00:00+00:00",
    "last_successful_snapshot_at": "2026-11-14T12:00:00+00:00",
    "snapshot_age_seconds": 1.2,
    "stale": false,
    "collector_error": null
  }
}
```

The `meta.stale` flag is authoritative for freshness. A last-known snapshot can be returned after collector trouble, but must not be treated as live when `stale` is true. The health data also identifies the WZML-HK source and source version.

## 6. Task records

Task records include, where supported:

- `task_key`: WZML's actual in-memory task key for this lifecycle instance.
- `gid`: engine GID when available.
- `origin.chat_id` and `origin.message_id`: originating Telegram command reference for external correlation.
- `name`, normalized `state`, and original `raw_state`.
- `progress_percent`, `processed_bytes`, `total_bytes`.
- `download_speed_bytes_per_second`, `upload_speed_bytes_per_second`, `eta_seconds`, `elapsed_seconds`.
- `engine`, `is_torrent`, `seeders`, `leechers`, `uploaded_bytes`, `ratio`.
- `mode_in`, `mode_out`, `started_at`, and queue information where applicable.

The `task_key` and origin message identifiers are distinct. This matters for auto-leech, where a single incoming Telegram message can produce multiple WZML task keys.

Unsupported fields are `null`; the API does not invent values. The API does not return the user's Telegram ID, username, message text, raw source URL, cookie, credential or absolute local file path. URL-like names are redacted, and URL-like GIDs are omitted.

### Example task record

```json
{
  "task_key": "456789",
  "gid": "a1b2c3d4",
  "name": "Example.Movie.2026.1080p",
  "state": "downloading",
  "raw_state": "Download",
  "progress_percent": 64.5,
  "processed_bytes": 1350000000,
  "total_bytes": 2100000000,
  "download_speed_bytes_per_second": 5200000,
  "upload_speed_bytes_per_second": null,
  "eta_seconds": 180,
  "elapsed_seconds": 120,
  "engine": "qBittorrent",
  "is_torrent": true,
  "seeders": 12,
  "leechers": 3,
  "uploaded_bytes": 24000000,
  "ratio": 0.018,
  "mode_in": "Torrent",
  "mode_out": "Leech",
  "started_at": "2026-11-14T11:58:00+00:00",
  "origin": {
    "chat_id": -1001234567890,
    "message_id": 987
  },
  "queue": null
}
```

The example is illustrative, not live WZML output.

## 7. System and transfer metric semantics

`/system` includes CPU, RAM, swap, download-directory and root-filesystem disk usage, disk I/O counters, host/container network counters and rates, bot uptime, host uptime and WZML process RSS/CPU where supported.

`/transfer` distinguishes:
- aggregate speeds calculated from active WZML task records;
- host/container network receive/transmit speeds derived from `psutil` counter deltas;
- cumulative host/container network bytes;
- WZML's existing monthly bandwidth accounting and configured limit.

Host/container network counters include traffic beyond WZML, such as other processes or services in the same network namespace. They must not be presented as WZML-only transfer totals.

The API reads the existing WZML bandwidth counters without calling the side-effecting `get_bandwidth_string()` helper, so polling the API does not schedule extra database writes or trigger bandwidth alerts.

## 8. Queue semantics

The source tracks tasks in `task_dict`, while queue positions and active-slot admission are represented separately in `queued_dl`, `queued_up`, `non_queued_dl` and `non_queued_up`.

The API builds one canonical task list from `task_dict`, then enriches it from queue structures. It does not add the lengths of those structures together as though they were separate task lists. Queue positions are one-based and are snapshots; they may change after collection.

## 9. History semantics

When `DATABASE_URL` is configured and the existing WZML database connection is available, lifecycle events are written asynchronously to the `monitoring_history` collection, scoped by `bot_id`.

When MongoDB is not configured or history persistence is disabled, live monitoring continues and history is retained in process memory only (bounded to the most recent 1,000 events). That history is lost when the bot process restarts. If MongoDB is configured but the connection is unavailable, history reports `process_memory_fallback`; if the connection exists but no read/write has yet succeeded, it reports `mongodb_pending`.

History events include start, download-stage completion, upload completion, successful completion, failure and cancellation where listener hooks provide reliable evidence. `on_download_complete` is a stage transition, not necessarily final job completion. A task still seeding after upload is represented as seeding rather than terminal success.

Raw error messages are not stored; only a broad error category is recorded. MongoDB TTL cleanup is asynchronous and may not happen exactly at the retention boundary.

## 10. Collection and performance

- Snapshot interval follows WZML's `STATUS_UPDATE_INTERVAL` setting (15 seconds by default). If the WZML setting is changed at runtime, the monitoring collector follows the updated value on its next cycle. There is no separate monitoring interval setting.
- Maximum accepted snapshot age: 30 seconds by default, and never less than two WZML status-refresh cycles. It follows an increased `STATUS_UPDATE_INTERVAL` so a normal slower interval is not incorrectly reported as stale.
- Task serialization is bounded to a limited number of concurrent engine refreshes.
- The collector copies task references under WZML's existing task lock, then performs engine refresh/serialization after releasing the lock.
- Public HTTP requests read cached snapshots and do not trigger an engine request for every task.
- One task's serialization failure does not discard the full snapshot.

Some engines expose fewer raw numeric metrics than others. In those cases, the API returns `null` rather than parsing unreliable display text or inventing values.

## 11. Example requests

```bash
curl -H "Authorization: Bearer $WZML_MONITOR_API_TOKEN" \
  https://YOUR-WZML-HOST/monitor/api/v1/health

curl -H "Authorization: Bearer $WZML_MONITOR_API_TOKEN" \
  "https://YOUR-WZML-HOST/monitor/api/v1/tasks?limit=50"

curl -H "Authorization: Bearer $WZML_MONITOR_API_TOKEN" \
  https://YOUR-WZML-HOST/monitor/api/v1/system
```

## 12. Graberr integration guidance

Graberr should:
1. Poll `/health` to check API/bridge availability and freshness.
2. Poll `/summary` and `/tasks` at a cadence no faster than the WZML collection interval unless a concrete requirement justifies it.
3. Match tasks using `origin.chat_id`, `origin.message_id`, and `task_key`; do not assume GID is universally unique or always available.
4. Use `/history` to reconcile terminal outcomes after a task disappears from `/tasks`.
5. Never release a Graberr reservation solely because a task is absent from one snapshot.
6. Treat stale data or unavailable bridge as unknown monitoring state, not as a completed or failed task.

Graberr integration is a separate phase after independent WZML API verification.
