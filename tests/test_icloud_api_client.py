# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import icloud_api_client


class ICloudAPIClientTests(unittest.TestCase):
    def setUp(self):
        icloud_api_client._CONTEXT_CACHE.clear()

    @patch("core.icloud_api_client.requests.request")
    def test_pick_and_fetch_otp(self, request):
        acquire = Mock(status_code=200, text="")
        acquire.json.return_value = {"ok": True, "email": "box@icloud.com", "lease_id": "lease-1"}
        pending = Mock(status_code=202, text="")
        pending.json.return_value = {"ok": True, "pending": True}
        success = Mock(status_code=200, text="")
        success.json.return_value = {"ok": True, "pending": False, "code": "654321"}
        request.side_effect = [acquire, pending, success]

        with patch.object(icloud_api_client._email_cfg, "ICLOUD_API_BASE", "https://icloud.example", create=True), patch.object(
            icloud_api_client._email_cfg, "ICLOUD_API_KEY", "secret", create=True
        ), patch.object(icloud_api_client.time, "sleep"):
            account = icloud_api_client.pick_account()
            code = icloud_api_client.fetch_latest_otp(account.email, after_ts=123, max_wait=3, poll_interval=1)

        self.assertEqual(account.lease_id, "lease-1")
        self.assertEqual(code, "654321")
        self.assertEqual(request.call_args.kwargs["json"]["after_ts"], 123.0)

    @patch("core.icloud_api_client.requests.request")
    def test_release_uses_lease_and_clears_context(self, request):
        response = Mock(status_code=200, text="")
        response.json.return_value = {"ok": True}
        request.return_value = response
        icloud_api_client._CONTEXT_CACHE["box@icloud.com"] = icloud_api_client.ICloudAccount(
            email="box@icloud.com", lease_id="lease-1"
        )
        with patch.object(icloud_api_client._email_cfg, "ICLOUD_API_BASE", "https://icloud.example", create=True), patch.object(
            icloud_api_client._email_cfg, "ICLOUD_API_KEY", "secret", create=True
        ):
            icloud_api_client.release_account("box@icloud.com", status="failed", note="bad")
        self.assertIsNone(icloud_api_client.get_account_context("box@icloud.com"))
        self.assertEqual(request.call_args.kwargs["json"]["lease_id"], "lease-1")

    @patch("core.icloud_api_client.requests.request")
    def test_fetch_reacquires_existing_mailbox_when_context_is_missing(self, request):
        acquire = Mock(status_code=200, text="")
        acquire.json.return_value = {"ok": True, "email": "box@icloud.com", "lease_id": "retry-lease"}
        otp = Mock(status_code=200, text="")
        otp.json.return_value = {"ok": True, "code": "123456"}
        request.side_effect = [acquire, otp]
        with patch.object(icloud_api_client._email_cfg, "ICLOUD_API_BASE", "https://icloud.example", create=True), patch.object(
            icloud_api_client._email_cfg, "ICLOUD_API_KEY", "secret", create=True
        ):
            code = icloud_api_client.fetch_latest_otp("box@icloud.com", after_ts=0, max_wait=1)
        self.assertEqual(code, "123456")
        self.assertEqual(request.call_args_list[0].kwargs["json"], {"email": "box@icloud.com", "reuse": True})


if __name__ == "__main__":
    unittest.main()
