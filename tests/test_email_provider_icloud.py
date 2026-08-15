# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import email_provider, icloud_api_client


class EmailProviderICloudTests(unittest.TestCase):
    def setUp(self):
        icloud_api_client._CONTEXT_CACHE.clear()

    def test_parse_email_sources_includes_icloud(self):
        self.assertEqual(email_provider.parse_email_sources("icloud,outlook"), ["icloud", "outlook"])

    @patch("core.icloud_api_client.pick_account")
    def test_pick_from_source_icloud(self, pick_account):
        pick_account.return_value = icloud_api_client.ICloudAccount("box@icloud.com", "lease")
        self.assertEqual(email_provider._pick_from_source("icloud"), "box@icloud.com")

    @patch("core.icloud_api_client.release_account")
    def test_release_routes_to_icloud(self, release):
        icloud_api_client._CONTEXT_CACHE["box@icloud.com"] = icloud_api_client.ICloudAccount(
            "box@icloud.com", "lease"
        )
        self.assertEqual(email_provider.release_email("box@icloud.com", status="failed"), "icloud")
        release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
