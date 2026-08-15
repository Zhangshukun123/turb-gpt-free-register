# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock, patch

from icloud_gateway.app import create_app, fetch_latest_otp_once


class ICloudGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "accounts.json"
        self.app = create_app(
            {
                "TESTING": True,
                "ICLOUD_GATEWAY_API_KEY": "gateway-secret",
                "ICLOUD_ACCOUNTS_FILE": str(self.path),
            }
        )
        self.client = self.app.test_client()
        self.headers = {"X-API-Key": "gateway-secret"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_auth_import_acquire_and_release(self):
        self.assertEqual(self.client.get("/api/v1/accounts/summary").status_code, 401)
        imported = self.client.post(
            "/api/v1/accounts/import",
            headers=self.headers,
            json={"text": "alias@icloud.com----login-name----xxxx-xxxx-xxxx-xxxx"},
        )
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.get_json()["inserted"], 1)

        acquired = self.client.post("/api/v1/mailboxes/acquire", headers=self.headers, json={})
        payload = acquired.get_json()
        self.assertEqual(acquired.status_code, 200)
        self.assertEqual(payload["email"], "alias@icloud.com")
        self.assertNotIn("app_password", payload)

        released = self.client.post(
            "/api/v1/mailboxes/release",
            headers=self.headers,
            json={"email": payload["email"], "lease_id": payload["lease_id"], "status": "available"},
        )
        self.assertEqual(released.status_code, 200)
        state = json.loads(self.path.read_text(encoding="utf-8"))["accounts"][0]
        self.assertEqual(state["status"], "available")
        self.assertEqual(state["lease_id"], "")

    def test_used_mailbox_can_be_released_for_retry_by_email(self):
        self.client.post(
            "/api/v1/accounts/import",
            headers=self.headers,
            json={"text": "box@icloud.com----app-pass"},
        )
        first = self.client.post("/api/v1/mailboxes/acquire", headers=self.headers, json={}).get_json()
        second = self.client.post(
            "/api/v1/mailboxes/acquire",
            headers=self.headers,
            json={"email": "box@icloud.com", "reuse": True},
        )
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first["lease_id"], second.get_json()["lease_id"])

    @patch("icloud_gateway.app.fetch_latest_otp_once", return_value="123456")
    def test_otp_endpoint_returns_code_for_valid_lease(self, fetch):
        self.client.post(
            "/api/v1/accounts/import",
            headers=self.headers,
            json={"records": [{"email": "box@icloud.com", "username": "box", "app_password": "app-pass"}]},
        )
        lease = self.client.post("/api/v1/mailboxes/acquire", headers=self.headers, json={}).get_json()
        response = self.client.post(
            "/api/v1/mailboxes/otp",
            headers=self.headers,
            json={"email": lease["email"], "lease_id": lease["lease_id"], "after_ts": 100},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["code"], "123456")
        self.assertEqual(fetch.call_args.kwargs["after_ts"], 100.0)

    @patch("icloud_gateway.app.fetch_latest_otp_once", return_value=None)
    def test_otp_endpoint_returns_pending(self, _fetch):
        self.client.post(
            "/api/v1/accounts/import",
            headers=self.headers,
            json={"text": "box@icloud.com----app-pass"},
        )
        lease = self.client.post("/api/v1/mailboxes/acquire", headers=self.headers, json={}).get_json()
        response = self.client.post(
            "/api/v1/mailboxes/otp",
            headers=self.headers,
            json={"email": lease["email"], "lease_id": lease["lease_id"], "after_ts": 0},
        )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["pending"])

    @patch("icloud_gateway.app.imaplib.IMAP4_SSL")
    def test_imap_fetch_filters_recipient_and_extracts_otp(self, imap_ssl):
        message = EmailMessage()
        message["From"] = "OpenAI <noreply@openai.com>"
        message["To"] = "alias@icloud.com"
        message["Subject"] = "Your verification code is 246810"
        message["Date"] = "Sat, 15 Aug 2026 01:00:00 +0000"
        message.set_content("Use verification code 246810 to continue.")

        mail = Mock()
        mail.select.return_value = ("OK", [b"1"])
        mail.uid.side_effect = [
            ("OK", [b"10"]),
            ("OK", [(b"10 (BODY[] {1})", message.as_bytes())]),
        ]
        imap_ssl.return_value = mail

        code = fetch_latest_otp_once(
            {"email": "alias@icloud.com", "username": "login-name", "app_password": "app-pass"},
            after_ts=0,
        )
        self.assertEqual(code, "246810")
        mail.login.assert_called_once_with("login-name", "app-pass")
        mail.select.assert_called_once_with("INBOX", readonly=True)


if __name__ == "__main__":
    unittest.main()
