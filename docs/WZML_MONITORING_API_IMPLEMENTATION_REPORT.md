# WZML-HK Monitoring API — Latest Source Integration Report

**Base source:** `WZMLHK-X-wzv3.1.3 (3)(2).zip` (latest user-supplied unmodified source)  
**Source version marker:** `v3.1.3-x`  
**Implementation:** WZML-side read-only Monitoring API, Option B  
**API version:** `v1`  
**Status:** Implemented in source package; local checks passed; live deployment verification remains outstanding.

## 1. What this package is

This build is based on the newest unmodified WZML archive supplied in this conversation. The monitoring API files were ported into that source. The older monitoring-API archive was used only as a reference for the feature implementation; unrelated/stale code from that older archive was not copied over the latest source. In particular, the latest repository's Jackett-related search and configuration code is retained.

The original uploaded ZIP was not modified. The result is a separate source package.

## 2. Monitoring API architecture

The WZML Telegram bot and existing public FastAPI web application run in separate processes. The bot owns the live task dictionaries and queue state, so the public web process cannot directly inspect them.

The implementation uses:

1. A private HTTP bridge running inside the WZML bot process on loopback (`127.0.0.1:8765` by default).
2. Authenticated GET-only endpoints registered in the existing `web/wserver.py` FastAPI app.
3. An HTTP proxy from the public web process to the private bridge.
4. A cached background collector reading WZML runtime state.

No second public port is required. Do not expose port `8765` publicly. The public web process and WZML bot process must run in the same host/container network namespace so loopback requests reach the private bridge.

## 3. Public endpoints

Base path: `/monitor/api/v1`

| Method | Endpoint | Function |
|---|---|---|
| GET | `/health` | API/bridge readiness, source version, snapshot freshness, history storage mode |
| GET | `/summary` | Aggregate task counts, queues, transfer totals and system headline metrics |
| GET | `/tasks` | Paginated active task list |
| GET | `/tasks/{task_key}` | One active task |
| GET | `/queues` | Download/upload queue positions and active keys |
| GET | `/system` | CPU, RAM, swap, disk, network, disk I/O and runtime stats |
| GET | `/transfer` | WZML task speeds and host/container network counters/rates |
| GET | `/history` | Paginated task lifecycle events |
| GET | `/history/{task_id}` | One task's lifecycle timeline |

All monitoring endpoints are GET-only. There are no HTTP controls for pause, resume, cancel, delete, reorder, force-start or torrent selection.

### Task correlation

Task records expose `task_key`, `origin.chat_id`, `origin.message_id` and GID where supported. `task_key` remains separate from the originating Telegram message ID so multiple auto-leech tasks can be mapped to one source message. The API does not expose Telegram user IDs, usernames, message text, cookies, source URLs or absolute local paths. URL-like task labels are redacted.

### Metric semantics

- Numeric bytes, bytes/second, percentages and seconds are returned where raw engine data supports them.
- Missing/unsupported values are `null`; the API does not invent metrics.
- Raw engine values are preferred over formatted status strings.
- Host/container network counters are not WZML-only traffic.
- The API reads WZML's existing bandwidth accounting without invoking the side-effecting display helper that schedules additional writes/alerts.
- `meta.stale` marks snapshots that exceed the freshness threshold.

## 4. Collection and history

The collector follows `Config.STATUS_UPDATE_INTERVAL` dynamically (15 seconds by default). It copies task references while holding the existing task lock, then performs status refresh/serialization outside the lock with bounded concurrency. Public HTTP requests read cached snapshots rather than triggering engine RPCs for every task.

The canonical active-task list comes from `task_dict`. Queue dictionaries and active-slot sets enrich it; they are not added together as if they were separate task lists.

Task listener hooks enqueue start, download-stage completion, upload-stage, success, failure and cancellation events without awaiting MongoDB. `on_download_complete` is a stage transition, not necessarily final job completion; an upload that continues seeding remains a seeding task.

When the existing `DATABASE_URL` connection is available and history is enabled, events are persisted asynchronously to the `monitoring_history` collection with bot-scoped indexes and a 30-day TTL. When MongoDB is absent/unavailable, the API reports an in-process fallback. In-memory history is bounded and is lost at process restart. History persistence failure does not block task callbacks.

## 5. Authentication and configuration

Required public settings:

```python
MONITOR_API_ENABLED = True
MONITOR_API_TOKEN = "YOUR_LONG_RANDOM_TOKEN"
MONITOR_INTERNAL_SECRET = ""  # Blank: a separate internal secret is generated at startup
MONITOR_INTERNAL_HOST = "127.0.0.1"
MONITOR_INTERNAL_PORT = 8765
MONITOR_SNAPSHOT_MAX_AGE = 30
MONITOR_HISTORY_ENABLED = True
MONITOR_HISTORY_RETENTION_DAYS = 30
```

Generate the public token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The public token must be at least 32 characters and is shared between WZML and the future Graberr client. Public requests use:

```http
Authorization: Bearer YOUR_MONITOR_API_TOKEN
```

The private bridge uses a different secret. When `MONITOR_INTERNAL_SECRET` is blank, WZML generates a separate random value at startup and passes it to the public web child process. It is not the public Graberr token. Do not expose the private secret or private bridge port.

The public API remains disabled unless `MONITOR_API_ENABLED` is true. Configure `MONITOR_API_TOKEN` in WZML and use the same public token in Graberr when that integration is developed. The private secret does not need to be set manually.

## 6. Latest-source startup correction: missing `stormtorrent`

The latest unmodified source starts the custom qBittorrent executable `stormtorrent` unconditionally from `bot/__init__.py`. In the supplied Heroku logs, that executable was absent and Python terminated with:

```text
FileNotFoundError: [Errno 2] No such file or directory: 'stormtorrent'
```

The merged package moves qBittorrent startup until after WZML configuration is loaded and checks that the executable is available before starting it. If the executable is missing or the qBittorrent WebUI cannot be reached:

- WZML no longer crashes solely because qBittorrent is unavailable.
- Aria2 initialization remains active.
- qBittorrent-specific tasks, qBittorrent search plugins, qBittorrent settings and qBittorrent web-selector/proxy operations report unavailable.
- The automatic runtime fallback does not overwrite the operator's explicit `DISABLE_QBITTORRENT` setting in MongoDB.

To use qBittorrent, deploy a compatible runtime/base image containing WZML's expected custom executable, or set `QBITTORRENT_BIN` to a compatible executable that accepts `-d --profile=<path>`. This fallback does not emulate qBittorrent; it only prevents qBittorrent absence from taking down Aria2/WZML startup.

`DISABLE_QBITTORRENT=True` disables only qBittorrent. `DISABLE_TORRENTS=True` retains its existing broader WZML meaning and disables torrent functionality.

## 7. Source files changed

### Added

- `bot/monitoring/__init__.py`
- `bot/monitoring/config.py`
- `bot/monitoring/history.py`
- `bot/monitoring/internal_server.py`
- `bot/monitoring/serializer.py`
- `bot/monitoring/service.py`
- `bot/monitoring/system_collector.py`
- `web/monitoring_routes.py`
- `tests/test_monitoring_api_routes.py`
- `tests/test_monitoring_serializer.py`
- `docs/WZML_MONITORING_API.md`
- `docs/WZML_MONITORING_API_IMPLEMENTATION_REPORT.md`

### Modified

- `bot/__init__.py` — removes the unconditional qBittorrent daemon launch and includes new deployment environment keys in config reconciliation.
- `bot/__main__.py` — starts qBittorrent only after config loading, checks executable availability, propagates effective monitoring settings to the web child, generates a separate private bridge secret if needed, and starts the private monitoring bridge.
- `bot/core/config_manager.py` — adds monitoring configuration, `DISABLE_QBITTORRENT`, and `QBITTORRENT_BIN` override.
- `bot/core/startup.py` — skips qBittorrent option operations when the client is unavailable.
- `bot/core/torrent_manager.py` — keeps Aria2 active if qBittorrent is disabled/unavailable or its WebUI connection fails.
- `bot/helper/listeners/task_listener.py` — best-effort lifecycle event recording.
- `bot/helper/mirror_leech_utils/download_utils/qbit_download.py` — gives a controlled unavailable response rather than dereferencing a missing qBittorrent client.
- `bot/modules/bot_settings.py` — guards qBittorrent settings actions when the client is unavailable.
- `bot/modules/search.py` — skips qBittorrent plugin operations if unavailable; latest Jackett search implementation remains intact.
- `config_sample.py` — documents API and qBittorrent configuration.
- `web/wserver.py` — registers public monitoring routes and safely handles unavailable qBittorrent web endpoints.

No new production Python dependency was added. Existing FastAPI, Uvicorn, HTTPX, psutil and PyMongo dependencies are used.

## 8. Validation performed

The following checks were run against this merged source tree:

| Check | Result |
|---|---|
| `python -m compileall -q bot web tests` | Passed |
| `python -m unittest discover -s tests -v` | Passed — 11 tests |
| Serializer state/bytes/time tests | Passed |
| Raw qBittorrent metric precedence | Passed |
| Task key and origin correlation serialization | Passed |
| URL/path redaction tests | Passed |
| API disabled/401 authentication tests | Passed |
| Public proxy uses separate internal bridge secret | Passed with mocked bridge response |
| Read-only route registration | Passed — 9 GET endpoints |

The validation is local unit/static validation. It is not a live Heroku deployment or a live WZML engine/MongoDB integration test. The local environment did not have the full WZML runtime dependencies/services installed, so no production Telegram, Aria2, qBittorrent, SABnzbd or MongoDB session was started.

## 9. Deployment notes

1. Extract the contents of the inner `WZMLHK-X-wzv3.1.3/` directory into the root of the GitHub/Heroku application repository. Do not leave the project one directory below the app root.
2. Keep the existing start command and public `PORT` behavior.
3. Set `MONITOR_API_ENABLED=true`.
4. Set `MONITOR_API_TOKEN` to a newly generated token of at least 32 characters.
5. Leave `MONITOR_INTERNAL_SECRET` unset unless you want to supply a separate private secret. WZML generates it if blank.
6. Do not expose port `8765`; it is loopback-only. Do not change Heroku's public `PORT` to `8765`.
7. Confirm the WZML bot process and Gunicorn web child share the same dyno/container network namespace.
8. Check logs for `WZML private monitoring bridge listening on 127.0.0.1:8765` and query `/monitor/api/v1/health` using the Bearer token.
9. Verify real task metrics and MongoDB history before connecting Graberr.

## 10. Remaining live verification

- Actual engine-specific field availability for Aria2, qBittorrent, direct download, SABnzbd, JDownloader, cloud and processing task wrappers.
- MongoDB collection/index/TTL behavior with the deployed async Mongo client.
- Real Heroku child-process and loopback behavior.
- Load behavior at the deployment's actual maximum concurrent task count.
- Shutdown-time history flushing. The supplied source does not expose a reliable async shutdown hook; abrupt dyno termination can discard events still queued for MongoDB.

Graberr integration is intentionally not included in this package. The WZML API must be deployed and verified independently first.
