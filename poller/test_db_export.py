from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from poller.db_export import _snapshot_sqlite_db


class DbExportScrubTests(unittest.TestCase):
    def test_snapshot_drops_visitor_data_but_keeps_live_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "market.db"
            dst = Path(tmp) / "snapshot.db"
            con = sqlite3.connect(src)
            con.executescript(
                """
                CREATE TABLE visits (id INTEGER PRIMARY KEY, ip TEXT, path TEXT);
                CREATE TABLE ip_geo_cache (ip TEXT PRIMARY KEY, lat REAL, lon REAL);
                CREATE TABLE sales (id INTEGER PRIMARY KEY, seller TEXT);
                INSERT INTO visits (ip, path) VALUES ('203.0.113.77', '/');
                INSERT INTO ip_geo_cache VALUES ('203.0.113.77', 52.1, 5.1);
                INSERT INTO sales (seller) VALUES ('A');
                """
            )
            con.commit()
            con.close()

            _snapshot_sqlite_db(src, dst)

            snap = sqlite3.connect(dst)
            try:
                self.assertEqual(snap.execute("SELECT COUNT(*) FROM visits").fetchone()[0], 0)
                self.assertEqual(snap.execute("SELECT COUNT(*) FROM ip_geo_cache").fetchone()[0], 0)
                self.assertEqual(snap.execute("SELECT COUNT(*) FROM sales").fetchone()[0], 1)
            finally:
                snap.close()
            # Deleted rows must not survive in free pages of the exported file.
            self.assertNotIn(b"203.0.113.77", dst.read_bytes())

            live = sqlite3.connect(src)
            try:
                self.assertEqual(live.execute("SELECT COUNT(*) FROM visits").fetchone()[0], 1)
            finally:
                live.close()


if __name__ == "__main__":
    unittest.main()
