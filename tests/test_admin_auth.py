from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from base64 import b64encode

from fastapi.testclient import TestClient


class AdminAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        os.environ["GROK_HELPER_ADMIN_USERNAME"] = "admin"
        os.environ["GROK_HELPER_ADMIN_PASSWORD"] = "test-secret"
        os.environ["DATA_DIR"] = os.path.join(self.temp_dir.name, "data")
        os.environ["LOG_DIR"] = os.path.join(self.temp_dir.name, "logs")
        import main

        self.main = importlib.reload(main)
        self.client = TestClient(self.main.create_app())

    def tearDown(self) -> None:
        os.environ.pop("GROK_HELPER_ADMIN_USERNAME", None)
        os.environ.pop("GROK_HELPER_ADMIN_PASSWORD", None)
        os.environ.pop("DATA_DIR", None)
        os.environ.pop("LOG_DIR", None)
        from grok_helper.logger import logger

        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        self.temp_dir.cleanup()

    def _basic_auth(self, username: str, password: str) -> str:
        raw = f"{username}:{password}".encode("utf-8")
        return "Basic " + b64encode(raw).decode("ascii")

    def test_register_page_loads_without_basic_challenge(self) -> None:
        response = self.client.get("/admin/register")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("www-authenticate", response.headers)
        self.assertIn("Grok2API", response.text)
        self.assertIn('class="admin-auth-pending"', response.text)

    def test_register_page_accepts_valid_basic_auth(self) -> None:
        response = self.client.get(
            "/admin/register",
            headers={"Authorization": self._basic_auth("admin", "test-secret")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Grok2API", response.text)

    def test_register_api_requires_basic_auth(self) -> None:
        response = self.client.get("/admin/register/meta")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Basic", response.headers.get("www-authenticate", ""))

    def test_admin_verify_rejects_invalid_credentials_without_basic_challenge(self) -> None:
        response = self.client.get(
            "/admin/register/auth/verify",
            headers={"Authorization": self._basic_auth("admin", "wrong-secret")},
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("www-authenticate", response.headers)
        self.assertEqual(response.json(), {"authenticated": False})

    def test_admin_verify_accepts_valid_credentials(self) -> None:
        response = self.client.get(
            "/admin/register/auth/verify",
            headers={"Authorization": self._basic_auth("admin", "test-secret")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"authenticated": True})

    def test_admin_login_page_exists(self) -> None:
        response = self.client.get("/admin/register/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="admin-login-form"', response.text)
        self.assertIn('/admin/register/static/css/app.css', response.text)
        self.assertIn('/admin/register/static/js/auth.js', response.text)
        self.assertNotIn('/admin/login', response.text)

    def test_legacy_admin_login_redirects_to_register_login(self) -> None:
        response = self.client.get("/admin/login", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers.get("location"), "/admin/register/login")

    def test_register_page_uses_register_scoped_assets_and_login(self) -> None:
        response = self.client.get("/admin/register")
        self.assertEqual(response.status_code, 200)
        self.assertIn('/admin/register/static/css/app.css', response.text)
        self.assertIn('/admin/register/static/js/admin-register.js', response.text)
        self.assertIn('/admin/register/login?next=', response.text)
        self.assertNotIn('href="/static/', response.text)
        self.assertNotIn('src="/static/', response.text)
        self.assertNotIn('/admin/login', response.text)

    def test_register_scoped_static_assets_are_served(self) -> None:
        response = self.client.get("/admin/register/static/js/auth.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/admin/register/auth/verify", response.text)

    def test_health_remains_public(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
