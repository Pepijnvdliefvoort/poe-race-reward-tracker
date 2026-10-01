import json
import tempfile
import unittest
from pathlib import Path

from server.admin_service import query_log_entries


def _line(i: int, level: str = "info", msg: str | None = None, **extra) -> str:
    row = {"ts": f"2026-10-01T00:00:{i % 60:02d}+00:00", "level": level, "name": "poller", "msg": msg or f"line {i}"}
    row.update(extra)
    return json.dumps(row)


class LogPagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "poller.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, lines: list[str]) -> None:
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _page_all(self, snapshot: dict, **kw) -> list[str]:
        """Snapshot tail (minus detached entries) plus every older page, oldest first."""
        msgs = [e["msg"] for e in snapshot["entries"][snapshot["detached"] :]]
        before = snapshot["before"]
        while before > 0:
            page = query_log_entries(self.path, before=before, **kw)
            self.assertLess(page["before"], before)
            msgs = [e["msg"] for e in page["entries"]] + msgs
            before = page["before"]
        return msgs

    def test_paging_back_crosses_sessions_without_gaps(self) -> None:
        lines = [_line(i) for i in range(50)]
        lines.append(json.dumps({"level": "info", "msg": "session start", "event": "session_start"}))
        lines += [_line(i) for i in range(50, 120)]
        self._write(lines)

        snap = query_log_entries(self.path, limit=30)
        self.assertEqual([e["msg"] for e in snap["entries"]], [f"line {i}" for i in range(90, 120)])
        self.assertEqual(snap["detached"], 0)

        msgs = self._page_all(snap, limit=7)
        expected = [json.loads(x)["msg"] for x in lines]
        self.assertEqual(msgs, expected)

    def test_detached_warnings_reappear_in_order(self) -> None:
        lines = [_line(i, "warning" if i in (3, 10) else "info") for i in range(40)]
        self._write(lines)

        snap = query_log_entries(self.path, limit=10)
        msgs = [e["msg"] for e in snap["entries"]]
        self.assertEqual(msgs[:2], ["line 3", "line 10"])
        self.assertEqual(snap["detached"], 2)

        self.assertEqual(self._page_all(snap, limit=6), [f"line {i}" for i in range(40)])

    def test_older_pages_apply_filters(self) -> None:
        lines = [_line(i, "error" if i % 9 == 0 else "info") for i in range(100)]
        self._write(lines)

        snap = query_log_entries(self.path, limit=3, level="error")
        msgs = self._page_all(snap, limit=2, level="error")
        self.assertEqual(msgs, [f"line {i}" for i in range(0, 100, 9)])

    def test_small_chunks_still_find_every_line(self) -> None:
        import server.admin_service as svc

        lines = [_line(i, msg=f"line {i} " + "x" * 200) for i in range(60)]
        self._write(lines)
        old = svc.ADMIN_LOG_PAGE_CHUNK_BYTES
        svc.ADMIN_LOG_PAGE_CHUNK_BYTES = 700
        try:
            snap = query_log_entries(self.path, limit=5)
            msgs = self._page_all(snap, limit=4)
        finally:
            svc.ADMIN_LOG_PAGE_CHUNK_BYTES = old
        self.assertEqual(msgs, [json.loads(x)["msg"] for x in lines])


if __name__ == "__main__":
    unittest.main()
