"""Public monitoring API route and authentication contract tests."""

import os
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from web.monitoring_routes import router


class MonitoringApiRouteTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://monitoring.test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_api_disabled_returns_404(self):
        with patch.dict(os.environ, {"MONITOR_API_ENABLED": "false"}, clear=False):
            response = await self.client.get("/monitor/api/v1/health")
        self.assertEqual(response.status_code, 404)

    async def test_missing_token_returns_401(self):
        with patch.dict(
            os.environ,
            {
                "MONITOR_API_ENABLED": "true",
                "MONITOR_API_TOKEN": "A" * 48,
            },
            clear=False,
        ):
            response = await self.client.get("/monitor/api/v1/health")
        self.assertEqual(response.status_code, 401)

    async def test_invalid_token_returns_401(self):
        with patch.dict(
            os.environ,
            {
                "MONITOR_API_ENABLED": "true",
                "MONITOR_API_TOKEN": "A" * 48,
            },
            clear=False,
        ):
            response = await self.client.get(
                "/monitor/api/v1/health",
                headers={"Authorization": "Bearer invalid"},
            )
        self.assertEqual(response.status_code, 401)

    def test_only_get_routes_are_registered(self):
        routes = [
            route for route in self.app.routes
            if getattr(route, "path", "").startswith("/monitor/api/v1")
        ]
        self.assertEqual(len(routes), 9)
        self.assertTrue(all(route.methods == {"GET"} for route in routes))


if __name__ == "__main__":
    import unittest

    unittest.main()
