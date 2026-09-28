from __future__ import annotations

import time
import unittest

from server import price_cache


class PriceCacheKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        price_cache._cache.clear()
        price_cache._build_locks.clear()

    def test_since_is_clamped_to_max_age(self) -> None:
        now_ms = int(time.time() * 1000)
        oldest = price_cache.since_ms_for_load(full_history=False, since_ms=now_ms - price_cache.MAX_SINCE_AGE_MS)
        self.assertEqual(price_cache.since_ms_for_load(full_history=False, since_ms=0), oldest)
        self.assertEqual(price_cache.since_ms_for_load(full_history=False, since_ms=-10**15), oldest)

    def test_future_since_is_clamped_to_now(self) -> None:
        now_ms = int(time.time() * 1000)
        bucket_now = (now_ms // price_cache.BUCKET_MS) * price_cache.BUCKET_MS
        self.assertEqual(price_cache.since_ms_for_load(full_history=False, since_ms=10**18), bucket_now)

    def test_key_and_load_since_share_bucket(self) -> None:
        since = int(time.time() * 1000) - 7 * 86_400_000
        key = price_cache.prices_cache_key(full_history=False, since_ms=since)
        load = price_cache.since_ms_for_load(full_history=False, since_ms=since)
        self.assertEqual(key, f"since:{load}")
        self.assertIsNone(price_cache.since_ms_for_load(full_history=True, since_ms=since))

    def test_build_locks_do_not_accumulate(self) -> None:
        now_ms = int(time.time() * 1000)
        for i in range(200):
            since = now_ms - (i + 1) * price_cache.BUCKET_MS
            price_cache.get_cached_prices_body(full_history=False, since_ms=since, build=lambda: b"{}")
        self.assertLessEqual(len(price_cache._cache), price_cache._MAX_ENTRIES)
        self.assertLessEqual(len(price_cache._build_locks), price_cache._MAX_ENTRIES + 1)


if __name__ == "__main__":
    unittest.main()
