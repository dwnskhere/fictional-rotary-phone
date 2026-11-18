"""Private loopback-only HTTP bridge hosted in the WZML bot process."""

import asyncio
import hmac
from contextlib import contextmanager, suppress

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from uvicorn import Config as UvicornConfig, Server

from .. import LOGGER
from . import service as monitoring


internal_app = FastAPI(title="WZML Internal Monitoring Bridge", docs_url=None, redoc_url=None, openapi_url=None)


@internal_app.middleware("http")
async def _private_access_guard(request: Request, call_next):
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1"):
        return JSONResponse({"success": False, "error": {"code": "LOCAL_ONLY"}}, status_code=403)
    from . import config as monitor_config

    expected = monitor_config.internal_token()
    supplied = request.headers.get("x-wzml-internal-token", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        return JSONResponse({"success": False, "error": {"code": "UNAUTHORIZED"}}, status_code=401)
    return await call_next(request)


def _service():
    if monitoring.monitoring_service is None:
        raise HTTPException(status_code=503, detail="Monitoring service is not ready")
    return monitoring.monitoring_service


def _response(data):
    service = _service()
    return {"success": True, "data": data, "meta": service._meta()}


@internal_app.get("/internal/v1/health")
async def health():
    return _response(_service().health())


@internal_app.get("/internal/v1/summary")
async def summary():
    return _response(_service().summary())


@internal_app.get("/internal/v1/tasks")
async def tasks(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    return _response(_service().tasks(offset, limit))


@internal_app.get("/internal/v1/tasks/{task_key}")
async def task_detail(task_key: str):
    result = _service().get_task(task_key)
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found in current snapshot")
    return _response(result)


@internal_app.get("/internal/v1/queues")
async def queues():
    return _response(_service().queues())


@internal_app.get("/internal/v1/system")
async def system():
    return _response(_service().system())


@internal_app.get("/internal/v1/transfer")
async def transfer():
    return _response(_service().transfer())


@internal_app.get("/internal/v1/history")
async def history(limit: int = Query(50, ge=1, le=200), before: str | None = None):
    before_dt = None
    if before:
        from datetime import datetime, timezone

        try:
            before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
            if before_dt.tzinfo is None:
                before_dt = before_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid before timestamp")
    events, storage = await _service().history.query(limit=limit, before=before_dt)
    return _response({"events": events, "storage": storage})


@internal_app.get("/internal/v1/history/{task_id}")
async def task_history(task_id: str, limit: int = Query(50, ge=1, le=200)):
    events, storage = await _service().history.query(task_id=task_id, limit=limit)
    return _response({"task_id": task_id, "events": events, "storage": storage})


class EmbeddedUvicornServer(Server):
    """Uvicorn server that leaves process signal handling to the WZML bot."""

    @contextmanager
    def capture_signals(self):
        yield


class PrivateMonitoringServer:
    def __init__(self):
        self.server = None
        self.task = None

    async def start(self, host="127.0.0.1", port=8765):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("The monitoring bridge must bind to loopback only")
        if self.task and not self.task.done():
            return
        config = UvicornConfig(
            internal_app,
            host=host,
            port=port,
            loop="asyncio",
            lifespan="off",
            access_log=False,
            log_level="warning",
        )
        self.server = EmbeddedUvicornServer(config)
        self.task = asyncio.create_task(self.server.serve(), name="wzml-monitor-private-http")
        for _ in range(100):
            if self.server.started:
                LOGGER.info(f"WZML private monitoring bridge listening on {host}:{port}")
                return
            if self.task.done():
                exception = self.task.exception()
                if exception:
                    raise exception
                raise RuntimeError("Private monitoring bridge stopped during startup")
            await asyncio.sleep(0.05)
        await self.stop()
        raise TimeoutError("Timed out starting private monitoring bridge")

    async def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.task:
            with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self.task, timeout=5)
        self.server = None
        self.task = None


private_server = PrivateMonitoringServer()
