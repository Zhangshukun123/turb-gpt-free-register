# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from unittest.mock import patch

from core.account_export import save_account_data


class AccountExportICloudTests(unittest.TestCase):
    @patch("core.icloud_api_client.release_account")
    @patch("core.account_export._append_batch_archive", return_value=Path("batch"))
    @patch("core.db.insert_account", return_value=42)
    def test_saved_icloud_account_marks_server_lease_used(self, insert_account, archive, release_account):
        row_id = save_account_data(
            "box@icloud.com",
            "access-token",
            email_source="icloud",
            auto_plan_check=False,
        )
        self.assertEqual(row_id, 42)
        release_account.assert_called_once_with(
            "box@icloud.com",
            status="used",
            note="注册成功并已保存，account_id=42",
        )


if __name__ == "__main__":
    unittest.main()
