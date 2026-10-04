from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from poller import deploy_restart
from poller.deploy_restart import consume_restart_request, restart_flag_path


class ConsumeRestartRequestTests(unittest.TestCase):
    def test_missing_flag_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(consume_restart_request(Path(tmp) / "flag"))

    def test_flag_is_read_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "flag"
            flag.write_text("abc1234\n", encoding="utf-8")
            self.assertEqual(consume_restart_request(flag), "abc1234")
            self.assertFalse(flag.exists())
            self.assertIsNone(consume_restart_request(flag))

    def test_empty_flag_still_counts_as_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "flag"
            flag.touch()
            self.assertEqual(consume_restart_request(flag), "")

    def test_undeletable_flag_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "flag"
            flag.write_text("abc", encoding="utf-8")
            with mock.patch.object(Path, "unlink", side_effect=PermissionError):
                self.assertIsNone(consume_restart_request(flag))

    def test_env_overrides_default_path(self) -> None:
        with mock.patch.dict(os.environ, {deploy_restart.RESTART_FLAG_ENV: "/tmp/x-flag"}):
            self.assertEqual(restart_flag_path(), Path("/tmp/x-flag"))
        with mock.patch.dict(os.environ, {deploy_restart.RESTART_FLAG_ENV: ""}):
            self.assertEqual(restart_flag_path(), Path(deploy_restart.DEFAULT_RESTART_FLAG_PATH))


if __name__ == "__main__":
    unittest.main()
