# WZML-HK Monitoring API — Implementation Report

**Source baseline:** `WZMLHK-X-wzv3.1.3-stable.zip`  
**Baseline version:** `v3.1.3-x`  
**Baseline SHA-256:** `ee19484f87ba20cb07626cef93001611e421c43f931611779ff1b0936b9483c5`  
**Implementation scope:** WZML-side, read-only REST API (Option B)  
**Status:** Implemented in a separate source copy; not deployed to the user's WZML instance  
**Graberr changes:** Not included in this milestone

---

## 1. What was implemented

A dedicated monitoring API has been added to WZML-HK. It reads WZML's in-memory tasks, queues and system counters and exposes structured JSON to authenticated external clients.

The architecture uses:

1. A private HTTP bridge running inside the WZML bot process on loopback (`127.0.0.1:8765` by default).
2. Authenticated public routes registered with the existing `web/wserver.py` FastAPI app.
3. A proxy from the public web process to the private bridge.
4. A cached background collector that follows WZML's `STATUS_UPDATE_INTERVAL` dynamically.
5. Optional lifecycle history persistence through the existing WZML MongoDB connection.

The existing public web port is reused. The private bridge does not bind to a public interface and no second public port is required.

## 2. Public API endpoints

Base path: `/monitor/api/v1`

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | API/bridge status, WZML version, snapshot freshness and history mode |
| GET | `/summary` | Task counts, queue counts, transfer totals and headline system metrics |
| GET | `/tasks` | Paginated active-task records |
| GET | `/tasks/{task_key}` | One task from the current snapshot |
| GET | `/queues` | Download/upload queue positions and active task keys |
| GET | `/system` | CPU, RAM, swap, disk, network, disk I/O, process and runtime metrics |
| GET | `/transfer` | WZML task rates, host/container network rates and cumulative counters |
| GET | `/history` | Paginated lifecycle events |
| GET | `/history/{task_id}` | One task's lifecycle timeline |

All v1 routes are GET-only. There are no HTTP actions to start, stop, cancel, pause, resume, reorder or otherwise control a task.

Successful responses use a `success`, `data`, `meta` envelope. Metrics are numeric where raw values are available; unsupported values are `null` rather than invented.

## 3. Task identity and correlation

Each task record exposes:

- `task_key`: WZML's actual in-memory task key for this task lifecycle.
- `origin.chat_id` and `origin.message_id`: original Telegram command reference.
- `gid`: engine GID where available.

These are kept separate because auto-leech can create derived WZML task keys from one originating message. Graberr should correlate using the origin fields plus `task_key`, and should not assume GID is always present or universally unique.

The API does not return Telegram user IDs, usernames, message contents, raw source URLs, cookies, credentials or absolute local paths. URL-like names and URI-like GIDs are redacted/omitted.

## 4. System metrics and semantics

`/system` reports CPU, RAM, swap, disk usage for the download directory and root filesystem, disk I/O counters, network counters/rates, WZML process RSS/CPU, bot uptime and host uptime where supported.

`/transfer` distinguishes aggregate task transfer rates from `psutil` network counters. Network counters are host/container-level and may include traffic from other processes; they must not be presented as WZML-only network totals.

The monitoring collector reads the existing monthly bandwidth counters without calling the side-effecting `get_bandwidth_string()` helper.

## 5. Collection cadence

The monitoring collector follows WZML's `STATUS_UPDATE_INTERVAL` setting rather than maintaining a separate independent poll interval. It reads the setting before every cycle so runtime changes to the WZML status interval are picked up on the next cycle.

The default interval is 15 seconds. The stale threshold defaults to 30 seconds and is always at least two configured WZML status cycles, so a longer WZML interval does not make a normally refreshed snapshot appear stale.

Task objects are copied under the existing task lock; engine status refresh and serialization occur after releasing locks and are bounded to eight concurrent tasks. HTTP reads use the cached snapshot instead of triggering a fresh engine call per request.

Raw engine counters are preferred over formatted wrapper strings to preserve numeric precision. The serializer then falls back to WZML status methods when raw values are unavailable.

## 6. History

Task listener hooks enqueue lifecycle events without waiting for MongoDB. Events include task start, download-stage completion, successful completion/upload completion, failure and cancellation where the listener provides reliable evidence.

`on_download_complete` is treated as a stage transition, not final completion. A task continuing to seed is represented as seeding rather than terminal success.

When `DATABASE_URL` is configured and the existing WZML database connection is available, events are persisted asynchronously in `monitoring_history`, scoped by bot ID, with a 30-day TTL index.

If MongoDB is not configured or history is disabled, the API uses a bounded in-process history buffer of 1,000 events. It is lost when the process restarts. If MongoDB is configured but unavailable, the API reports `process_memory_fallback`. If a connection exists but no read/write has succeeded yet, it reports `mongodb_pending`.

A bounded in-memory event queue protects task callbacks from waiting on MongoDB. Events can be dropped from the persistence queue if that queue fills; the API reports a dropped-event counter.

## 7. Configuration required before enabling

Set these values in WZML's `config.py` or deployment environment:

```python
MONITOR_API_ENABLED = True
MONITOR_API_TOKEN = "YOUR_LONG_RANDOM_TOKEN"
MONITOR_INTERNAL_SECRET = ""  # Optional; blank uses MONITOR_API_TOKEN
MONITOR_INTERNAL_HOST = "127.0.0.1"
MONITOR_INTERNAL_PORT = 8765
MONITOR_SNAPSHOT_MAX_AGE = 30
MONITOR_HISTORY_ENABLED = True
MONITOR_HISTORY_RETENTION_DAYS = 30
```

Generate a token locally:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The token must be at least 32 characters. Configure the same `MONITOR_API_TOKEN` in Graberr when the separate Graberr integration phase begins. Do not commit or share the token.

Public requests must use:

```http
Authorization: Bearer YOUR_MONITOR_API_TOKEN
```

The API remains disabled by default. The private bridge secret defaults to the public token if `MONITOR_INTERNAL_SECRET` is blank; a separate random internal secret may be configured in the same WZML config/environment.

## 8. Files changed

### Added

| File | Purpose |
|---|---|
| `bot/monitoring/__init__.py` | Monitoring package |
| `bot/monitoring/config.py` | Monitoring settings and token helpers |
| `bot/monitoring/serializer.py` | Task status normalization and JSON-safe task serialization |
| `bot/monitoring/system_collector.py` | CPU, memory, disk, network and runtime statistics |
| `bot/monitoring/history.py` | Non-blocking lifecycle event history and optional MongoDB persistence |
| `bot/monitoring/service.py` | Snapshot collector and read-only monitoring service |
| `bot/monitoring/internal_server.py` | Private loopback bridge inside the bot process |
| `web/monitoring_routes.py` | Authenticated public REST routes and private bridge proxy |
| `docs/WZML_MONITORING_API.md` | API and deployment documentation |
| `tests/test_monitoring_serializer.py` | Serializer and raw qBittorrent-shaped metric tests |
| `tests/test_monitoring_api_routes.py` | Public route/authentication contract tests |

### Modified

| File | Change |
|---|---|
| `bot/__main__.py` | Starts the optional private monitoring service after WZML initialization |
| `bot/core/config_manager.py` | Adds monitoring configuration defaults |
| `bot/helper/listeners/task_listener.py` | Adds best-effort lifecycle event recording at listener hooks |
| `config_sample.py` | Documents the new configuration keys and token generation |
| `web/wserver.py` | Registers the public monitoring router in the existing FastAPI app |

No existing download engine, upload engine, queue manager, Telegram command, task-control route or dependency requirement was intentionally changed. No new production dependency was added.

## 9. Validation performed

| Check | Result |
|---|---|
| `python -m compileall -q bot web tests` | Passed |
| `python -m unittest discover -s tests -p 'test_monitoring_*.py' -v` | Passed — 10 tests |
| Public route registration check | Passed — 9 GET-only endpoints |
| Local private-bridge → public-proxy smoke test with stubbed WZML runtime | Passed |
| Bearer-token success/invalid-token behavior in local smoke test | Passed |
| `task_key` and origin chat/message correlation in local smoke test | Passed |
| Task history endpoint using in-memory fallback in local smoke test | Passed |

The local end-to-end check used a stubbed WZML runtime because this environment does not contain the user's Telegram credentials, active Aria2/qBittorrent/SABnzbd services or MongoDB connection.

## 10. Important deployment limitations / remaining verification

This is a source implementation, not a deployment to the user's live WZML instance.

The following must still be verified in the target environment:

1. Start WZML with monitoring enabled and a real configured token.
2. Confirm the existing public Gunicorn/FastAPI process and the bot process run in the same host/container network namespace, as required by the loopback bridge. The source starts the public web process as a subprocess from WZML startup when its public port is configured; a deployment that moves it to a separate dyno/container would need a different private transport.
3. Exercise actual Aria2, qBittorrent, direct, SABnzbd, JDownloader, cloud, Telegram and post-processing task classes. The serializer uses engine raw attributes and WZML wrapper fallbacks, but live engine integration was not run here.
4. Verify MongoDB insert/query/index creation and TTL retention against the real WZML `DATABASE_URL`.
5. Review graceful process shutdown in the target deployment. `stop_monitoring()` supports a bounded collector/history stop, but the supplied WZML source has no existing async shutdown hook or signal-managed shutdown path to attach it to safely. An abrupt process kill can therefore discard events still waiting in the history writer queue.
6. Validate API response load with the expected maximum number of simultaneous tasks.

If the private bridge is unavailable, public monitoring routes return `503`; a live task snapshot is not fabricated. If a snapshot becomes too old, the response marks `meta.stale = true`.

## 11. Not included in this milestone

- Graberr API client, polling or MongoDB integration.
- Graberr reservation or completion reconciliation changes.
- Any task-control or torrent-selection control API.
- A monitoring dashboard UI.
- A live production deployment or configured token.
- Changes to unrelated WZML architecture.

The next step is to install this source package in a staging copy of WZML, configure the public token, verify the real private/public process topology, and run the API against actual tasks before connecting Graberr.
