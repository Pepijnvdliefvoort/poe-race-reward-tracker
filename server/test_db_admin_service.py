from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.db_admin_service import preview_table, run_query
from storage.db import Database


class RunQueryReadOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        db = Database(root_dir=self.root)
        con = db.connect()
        con.execute("INSERT INTO app_config(key, value_json, updated_at_utc) VALUES ('k', '{}', 'now')")
        con.commit()
        con.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _count(self) -> int:
        res = run_query(root_dir=self.root, sql="SELECT COUNT(*) AS n FROM app_config")
        self.assertTrue(res["ok"], res)
        return int(res["rows"][0]["n"])

    def test_select_works(self) -> None:
        self.assertEqual(self._count(), 1)

    def test_cte_write_is_rejected(self) -> None:
        res = run_query(root_dir=self.root, sql="WITH x AS (SELECT 1) DELETE FROM app_config")
        self.assertFalse(res["ok"])
        self.assertEqual(self._count(), 1)

    def test_non_read_keyword_is_rejected(self) -> None:
        res = run_query(root_dir=self.root, sql="DELETE FROM app_config")
        self.assertFalse(res["ok"])
        self.assertEqual(self._count(), 1)

    def test_persistent_pragma_write_is_rejected(self) -> None:
        res = run_query(root_dir=self.root, sql="PRAGMA user_version = 42")
        after = run_query(root_dir=self.root, sql="PRAGMA user_version")
        self.assertTrue(after["ok"], after)
        self.assertEqual(list(after["rows"][0].values())[0], 0, res)


class PreviewTableTests(unittest.TestCase):
    def test_unknown_table_returns_error_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            res = preview_table(root_dir=Path(tmp), name="does_not_exist")
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"])


if __name__ == "__main__":
    unittest.main()
