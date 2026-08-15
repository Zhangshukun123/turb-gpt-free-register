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


if __name__ == "__main__":
    unittest.main()
