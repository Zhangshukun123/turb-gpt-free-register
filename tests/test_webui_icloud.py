# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class ICloudWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_icloud_without_gateway_config(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "icloud"
        ), patch.object(email_config, "ICLOUD_API_BASE", ""), patch.object(email_config, "ICLOUD_API_KEY", ""):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn("iCloud 网关地址", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_icloud_gateway_skips_local_pool(self, submit_registration, outlook_pool_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "icloud"
        ), patch.object(email_config, "ICLOUD_API_BASE", "https://icloud.example"), patch.object(
            email_config, "ICLOUD_API_KEY", "secret"
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(count=1, workers=1)

    @patch("core.icloud_api_client.list_inventory_emails")
    def test_selecting_icloud_source_pulls_read_only_server_pool(self, list_inventory_emails):
        list_inventory_emails.return_value = {
            "ok": True,
            "items": [{
                "email": "box@icloud.com",
                "source": "icloud",
                "status": "available",
                "copy_line": "box@icloud.com",
                "readonly": True,
                "otp_source": "服务器取码",
            }],
            "total": 1,
        }
        response = self.client.get("/api/outlook?source=icloud&paged=1&page=1&page_size=20")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["source"], "icloud")
        self.assertTrue(payload["items"][0]["readonly"])
        list_inventory_emails.assert_called_once_with(status=None, q="", page=1, page_size=5000)

    @patch("core.icloud_api_client.inventory_pool_summary")
    def test_summary_includes_icloud_server_pool(self, inventory_pool_summary):
        inventory_pool_summary.return_value = {
            "total": 99,
            "available": 21,
            "used": 70,
            "failed": 0,
        }
        with patch.object(email_config, "EMAIL_SOURCE", "icloud"):
            response = self.client.get("/api/summary")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["outlook_total"], 99)
        self.assertEqual(payload["outlook_available"], 21)
        self.assertEqual(payload["outlook_used"], 70)

    def test_icloud_server_pool_rejects_local_mutation(self):
        response = self.client.post(
            "/api/outlook/status",
            json={"email": "box@icloud.com", "source": "icloud", "status": "used"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("只读", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
