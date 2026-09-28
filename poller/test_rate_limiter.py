from __future__ import annotations

import os
import unittest
from collections import deque
from unittest import mock

from requests.structures import CaseInsensitiveDict

from poller import poll_item_prices as pip

# (max_requests, window_seconds, penalty_seconds) per GGG policy, shaped like live trade headers.
SEARCH_RULES = [(5, 10, 60), (15, 60, 300), (30, 300, 1800), (600, 21600, 3600)]
FETCH_RULES = [(12, 4, 60), (16, 12, 300)]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class FakeTradeApi:
    """Sliding-window rate limiter that answers with GGG-style headers (and 429s on overflow)."""

    def __init__(self, clock: FakeClock, policies: dict[str, list[tuple[int, int, int]]]) -> None:
        self.clock = clock
        self.policies = policies
        self.history: dict[str, deque[float]] = {name: deque() for name in policies}
        self.rejected = 0

    def request(self, policy: str) -> tuple[CaseInsensitiveDict, int]:
        rules = self.policies[policy]
        hist = self.history[policy]
        now = self.clock.now
        longest = max(w for _, w, _ in rules)
        while hist and hist[0] <= now - longest:
            hist.popleft()

        def used_in(window: int) -> int:
            return sum(1 for t in hist if t > now - window)

        over = any(used_in(w) + 1 > m for m, w, _ in rules)
        status = 429 if over else 200
        if over:
            self.rejected += 1
        else:
            hist.append(now)
        states = []
        for m, w, p in rules:
            used = used_in(w)
            states.append(f"{used}:{w}:{p if used >= m else 0}")
        headers = CaseInsensitiveDict(
            {
                "x-rate-limit-policy": policy,
                "x-rate-limit-ip": ",".join(f"{m}:{w}:{p}" for m, w, p in rules),
                "x-rate-limit-ip-state": ",".join(states),
            }
        )
        return headers, status

    def used(self, policy: str, window: int) -> int:
        now = self.clock.now
        return sum(1 for t in self.history[policy] if t > now - window)


class RateLimiterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        patches = [
            mock.patch.object(pip.time, "monotonic", self.clock.monotonic),
            mock.patch.object(pip.time, "sleep", self.clock.sleep),
            mock.patch.object(pip, "log_line", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.api = FakeTradeApi(self.clock, {"trade-search-request-limit": SEARCH_RULES, "trade-fetch-request-limit": FETCH_RULES})

    def call(self, limiter: pip.AdaptiveRateLimiter, bucket: str, policy: str, latency: float = 0.3) -> int:
        limiter.wait_before_request(bucket)
        self.clock.sleep(latency)
        headers, status = self.api.request(policy)
        limiter.update_from_response(headers, status, request_label=bucket, bucket=bucket)
        return status


class SteadyStateTests(RateLimiterTestCase):
    def test_continuous_search_uses_most_of_the_budget_without_429(self) -> None:
        limiter = pip.AdaptiveRateLimiter(reserve_ratio=0.0)
        for _ in range(2500):  # ~24h of continuous searching
            self.call(limiter, "search", "trade-search-request-limit")
        self.assertEqual(self.api.rejected, 0)
        utilization = self.api.used("trade-search-request-limit", 21600) / 600
        # The old usage-scaled pacing settled at ~58%; the fixed pace stays near the limit
        # while the headroom slowdowns keep it safely below 100%.
        self.assertGreaterEqual(utilization, 0.80)
        self.assertLess(utilization, 0.95)

    def test_reserve_ratio_keeps_extra_headroom(self) -> None:
        limiter = pip.AdaptiveRateLimiter(reserve_ratio=0.20)
        for _ in range(2500):
            self.call(limiter, "search", "trade-search-request-limit")
        self.assertEqual(self.api.rejected, 0)
        utilization = self.api.used("trade-search-request-limit", 21600) / 600
        self.assertLessEqual(utilization, 0.82)

    def test_item_loop_search_plus_fetches_never_exceeds_limits(self) -> None:
        limiter = pip.AdaptiveRateLimiter(reserve_ratio=0.0)
        for _ in range(600):
            self.call(limiter, "search", "trade-search-request-limit")
            for _ in range(10):
                self.call(limiter, "fetch", "trade-fetch-request-limit", latency=0.2)
        self.assertEqual(self.api.rejected, 0)


class PerPolicyTests(RateLimiterTestCase):
    def test_fetch_does_not_wait_for_search_pacing(self) -> None:
        limiter = pip.AdaptiveRateLimiter(reserve_ratio=0.0)
        self.call(limiter, "search", "trade-search-request-limit")
        before = self.clock.now
        limiter.wait_before_request("fetch")
        self.assertEqual(self.clock.now, before)
        limiter.wait_before_request("search")
        # 21600 / 600 * 1.1 safety = 39.6s since the search response.
        self.assertAlmostEqual(self.clock.now - before, 39.6, places=6)

    def test_buckets_reporting_the_same_policy_share_pacing(self) -> None:
        limiter = pip.AdaptiveRateLimiter(reserve_ratio=0.0)
        self.call(limiter, "search", "trade-search-request-limit")
        self.call(limiter, "search_account", "trade-search-request-limit")
        before = self.clock.now
        limiter.wait_before_request("search_account")
        self.assertGreater(self.clock.now - before, 30.0)

    def test_429_honours_retry_after(self) -> None:
        limiter = pip.AdaptiveRateLimiter(reserve_ratio=0.0)
        headers = CaseInsensitiveDict(
            {
                "x-rate-limit-policy": "trade-search-request-limit",
                "x-rate-limit-ip": "5:10:60",
                "x-rate-limit-ip-state": "6:10:60",
                "retry-after": "60",
            }
        )
        limiter.update_from_response(headers, 429, bucket="search")
        before = self.clock.now
        limiter.wait_before_request("search")
        self.assertGreaterEqual(self.clock.now - before, 60.0)


class ReserveRatioConfigTests(unittest.TestCase):
    def test_env_parsing(self) -> None:
        cases = {"": pip.RESERVE_RATIO, "0": 0.0, "0.1": 0.1, "abc": pip.RESERVE_RATIO, "9": 0.5, "-1": 0.0, "nan": pip.RESERVE_RATIO}
        for raw, expected in cases.items():
            with mock.patch.dict(os.environ, {"POE_RATE_LIMIT_RESERVE_RATIO": raw}):
                self.assertEqual(pip.load_rate_limit_reserve_ratio(), expected, raw)


if __name__ == "__main__":
    unittest.main()
