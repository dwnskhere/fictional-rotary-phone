"""Authenticated public REST endpoints proxying to the private WZML bridge."""

import hmac
import os
from importlib import import_module
from time import monotonic

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/monitor/api/v1", tags=["WZML Monitoring API"])
_RATE_WINDOW_SECONDS = 60
_RATE_MAX_REQUESTS = 120
_RATE_BUCKETS = {}


def _setting(name, default=None):
    value = os.getenv(name)
    if value is not None:
        return value
    try:
        config_module = import_module("config")
        return getattr(config_module, name, default)
    except Exception:
        return default


def _enabled():
    value = _setting("MONITOR_API_ENABLED", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _public_token():
    return str(_setting("MONITOR_API_TOKEN", "") or "").strip()


def _internal_token():
    return str(_setting("MONITOR_INTERNAL_SECRET", "") or _public_token()).strip()


def _internal_url(path):
    host = str(_setting("MONITOR_INTERNAL_HOST", "127.0.0.1") or "127.0.0.1")
    port = int(_setting("MONITOR_INTERNAL_PORT", 8765) or 8765)
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(status_code=503, detail="Private bridge host must be loopback")
    return f"http://{host}:{port}/internal/v1/{path.lstrip('/')}"


async def _authorize(request: Request):
    if not _enabled():
        raise HTTPException(status_code=404, detail="Monitoring API is disabled")
    expected = _public_token()
    if len(expected) < 32:
        raise HTTPException(status_code=503, detail="Monitoring API token is not configured securely")
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing Bearer token", headers={"WWW-Authenticate": "Bearer"})
    client_host = request.client.host if request.client else "unknown"
    now = monotonic()
    window_start, count = _RATE_BUCKETS.get(client_host, (now, 0))
    if now - window_start >= _RATE_WINDOW_SECONDS:
        window_start, count = now, 0
    count += 1
    _RATE_BUCKETS[client_host] = (window_start, count)
    if count > _RATE_MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="Monitoring API rate limit exceeded")
    if len(_RATE_BUCKETS) > 2048:
        for host, (start, _) in list(_RATE_BUCKETS.items()):
            if now - start >= _RATE_WINDOW_SECONDS:
                _RATE_BUCKETS.pop(host, None)


async def _proxy(request, path, params=None):
    await _authorize(request)
    token = _internal_token()
    if len(token) < 32:
        raise HTTPException(status_code=503, detail="Private bridge authentication is not configured")
    url = _internal_url(path)
    timeout = httpx.Timeout(5.0, connect=2.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                url,
                params=params,
                headers={"X-WZML-Internal-Token": token},
            )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="WZML monitoring bridge is unavailable")
    try:
        payload = response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Invalid response from monitoring bridge")
    if response.status_code >= 400:
        detail = payload.get("detail") or payload.get("error", {}).get("code") or "Monitoring bridge error"
        raise HTTPException(status_code=response.status_code, detail=detail)
    return JSONResponse(payload, status_code=response.status_code)


@router.get("/health")
async def health(request: Request):
    return await _proxy(request, "health")


@router.get("/summary")
async def summary(request: Request):
    return await _proxy(request, "summary")


@router.get("/tasks")
async def tasks(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    return await _proxy(request, "tasks", {"offset": offset, "limit": limit})


@router.get("/tasks/{task_key}")
async def task_detail(request: Request, task_key: str):
    return await _proxy(request, f"tasks/{task_key}")


@router.get("/queues")
async def queues(request: Request):
    return await _proxy(request, "queues")


@router.get("/system")
async def system(request: Request):
    return await _proxy(request, "system")


@router.get("/transfer")
async def transfer(request: Request):
    return await _proxy(request, "transfer")


@router.get("/history")
async def history(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    before: str | None = None,
):
    params = {"limit": limit}
    if before:
        params["before"] = before
    return await _proxy(request, "history", params)


@router.get("/history/{task_id}")
async def task_history(
    request: Request,
    task_id: str,
    limit: int = Query(50, ge=1, le=200),
):
    return await _proxy(request, f"history/{task_id}", {"limit": limit})
