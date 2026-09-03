"""Unit tests for remote rate limiting."""

import unittest

from windows_gui.remote import policy


class RateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.clock = {'now': 1_000.0}
        self.limiter = policy.RateLimiter(now_factory=lambda: self.clock['now'])

    def test_allows_up_to_limit_then_denies(self):
        limit = policy.RateLimit('probe', 3, 60.0)
        for _ in range(3):
            self.assertTrue(self.limiter.allow(limit, 'device-1'))
        self.assertFalse(self.limiter.allow(limit, 'device-1'))

    def test_window_slides_and_recovers(self):
        limit = policy.RateLimit('probe', 2, 60.0)
        self.assertTrue(self.limiter.allow(limit, 'device-1'))
        self.assertTrue(self.limiter.allow(limit, 'device-1'))
        self.assertFalse(self.limiter.allow(limit, 'device-1'))
        self.clock['now'] += 61
        self.assertTrue(self.limiter.allow(limit, 'device-1'))

    def test_keys_are_isolated(self):
        limit = policy.RateLimit('probe', 1, 60.0)
        self.assertTrue(self.limiter.allow(limit, 'device-1'))
        self.assertTrue(self.limiter.allow(limit, 'device-2'))
        self.assertFalse(self.limiter.allow(limit, 'device-1'))

    def test_limits_with_the_same_key_are_isolated(self):
        first = policy.RateLimit('first', 1, 60.0)
        second = policy.RateLimit('second', 1, 60.0)
        self.assertTrue(self.limiter.allow(first, 'device-1'))
        self.assertTrue(self.limiter.allow(second, 'device-1'))
        self.assertFalse(self.limiter.allow(first, 'device-1'))
        self.assertFalse(self.limiter.allow(second, 'device-1'))

    def test_capacity_evicts_oldest_key(self):
        limiter = policy.RateLimiter(capacity=2, now_factory=lambda: self.clock['now'])
        limit = policy.RateLimit('probe', 1, 60.0)
        self.assertTrue(limiter.allow(limit, 'key-1'))
        self.assertTrue(limiter.allow(limit, 'key-2'))
        self.assertTrue(limiter.allow(limit, 'key-3'))
        self.assertTrue(limiter.allow(limit, 'key-1'))

    def test_design_limit_profiles(self):
        self.assertEqual(5, policy.LIMITS['pairing_claim_source'].max_events)
        self.assertEqual(600.0, policy.LIMITS['pairing_claim_source'].window_seconds)
        self.assertEqual(10, policy.LIMITS['mutating_stage_device'].max_events)
        self.assertEqual(3600.0, policy.LIMITS['mutating_stage_device'].window_seconds)
        self.assertEqual(30, policy.LIMITS['health_read_device'].max_events)


if __name__ == '__main__':
    unittest.main()
