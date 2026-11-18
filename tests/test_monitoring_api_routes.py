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


    async def test_authenticated_request_proxies_with_separate_internal_secret(self):
        expected_payload = {
            "success": True,
            "data": {"source": "WZML-HK"},
            "meta": {"stale": False},
        }
        captured = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return expected_payload

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def get(self, url, params=None, headers=None):
                captured["url"] = url
                captured["headers"] = headers
                return FakeResponse()

        with (
            patch.dict(
                os.environ,
                {
                    "MONITOR_API_ENABLED": "true",
                    "MONITOR_API_TOKEN": "P" * 48,
                    "MONITOR_INTERNAL_SECRET": "I" * 48,
                    "MONITOR_INTERNAL_HOST": "127.0.0.1",
                    "MONITOR_INTERNAL_PORT": "8765",
                },
                clear=False,
            ),
            patch("web.monitoring_routes.httpx.AsyncClient", return_value=FakeClient()),
        ):
            response = await self.client.get(
                "/monitor/api/v1/health",
                headers={"Authorization": "Bearer " + "P" * 48},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected_payload)
        self.assertEqual(
            captured["url"], "http://127.0.0.1:8765/internal/v1/health"
        )
        self.assertEqual(
            captured["headers"], {"X-WZML-Internal-Token": "I" * 48}
        )

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
