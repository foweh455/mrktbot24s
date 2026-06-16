from datetime import datetime, timedelta, timezone
import unittest

from scanner import analyze_trade_lock


def _z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class TradeLockFilterTest(unittest.TestCase):
    def test_long_trade_lock_is_always_blocked(self) -> None:
        now = datetime(2026, 4, 7, 12, 0, 0, tzinfo=timezone.utc)
        gift = {
            "nextTransferDate": _z(now + timedelta(days=10)),
            "isLocked": True,
        }
        blocked, until_ts, lock_seconds, source = analyze_trade_lock(
            gift,
            discount_percent=99.0,
            now_ts=int(now.timestamp()),
        )
        self.assertTrue(blocked)
        self.assertIsNotNone(until_ts)
        self.assertGreaterEqual(lock_seconds, 10 * 24 * 3600)
        self.assertEqual(source, "nextTransferDate")

    def test_short_trade_lock_under_3_days_is_allowed(self) -> None:
        now = datetime(2026, 4, 7, 12, 0, 0, tzinfo=timezone.utc)
        gift = {
            "nextResaleDate": _z(now + timedelta(days=2)),
        }
        blocked, until_ts, lock_seconds, source = analyze_trade_lock(
            gift,
            discount_percent=-5.0,
            now_ts=int(now.timestamp()),
        )
        self.assertFalse(blocked)
        self.assertIsNotNone(until_ts)
        self.assertGreater(lock_seconds, 0)
        self.assertEqual(source, "nextResaleDate")

    def test_mid_trade_lock_requires_13_percent_discount(self) -> None:
        now = datetime(2026, 4, 7, 12, 0, 0, tzinfo=timezone.utc)
        gift = {
            "nextResaleDate": _z(now + timedelta(days=5)),
        }
        blocked, _, _, _ = analyze_trade_lock(
            gift,
            discount_percent=12.99,
            now_ts=int(now.timestamp()),
        )
        self.assertTrue(blocked)

        blocked, _, _, _ = analyze_trade_lock(
            gift,
            discount_percent=13.0,
            now_ts=int(now.timestamp()),
        )
        self.assertFalse(blocked)

    def test_no_future_lock(self) -> None:
        now = datetime(2026, 4, 7, 12, 0, 0, tzinfo=timezone.utc)
        gift = {
            "nextResaleDate": _z(now - timedelta(minutes=1)),
            "nextTransferDate": "0001-01-01T00:00:00",
        }
        blocked, until_ts, lock_seconds, source = analyze_trade_lock(
            gift,
            now_ts=int(now.timestamp()),
        )
        self.assertFalse(blocked)
        self.assertIsNone(until_ts)
        self.assertEqual(lock_seconds, 0)
        self.assertEqual(source, "")


if __name__ == "__main__":
    unittest.main()
