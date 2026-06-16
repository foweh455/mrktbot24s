import unittest

from sniper_metrics import (
    get_balance_miss_bucket,
    is_already_bought_response,
    should_count_balance_miss,
)


class SniperMetricsTest(unittest.TestCase):
    def test_is_already_bought_response_list(self) -> None:
        self.assertTrue(is_already_bought_response([]))
        self.assertFalse(is_already_bought_response([{"ok": True}]))

    def test_is_already_bought_response_dict_markers(self) -> None:
        self.assertTrue(
            is_already_bought_response({"error": "Already sold"})
        )
        self.assertTrue(
            is_already_bought_response({"message": "Лот уже продан"})
        )
        self.assertFalse(
            is_already_bought_response({"error": "Insufficient balance"})
        )

    def test_should_count_balance_miss_positive(self) -> None:
        self.assertTrue(
            should_count_balance_miss(
                price_ton=8.01,
                discount_percent=3.0,
                balance_limit_ton=7.5,
            )
        )

    def test_should_count_balance_miss_negative_cases(self) -> None:
        self.assertFalse(
            should_count_balance_miss(
                price_ton=8.0,  # must be strictly above 8
                discount_percent=5.0,
                balance_limit_ton=7.5,
            )
        )
        self.assertFalse(
            should_count_balance_miss(
                price_ton=10.0,
                discount_percent=2.99,  # below required 3%
                balance_limit_ton=7.5,
            )
        )
        self.assertFalse(
            should_count_balance_miss(
                price_ton=10.0,
                discount_percent=5.0,
                balance_limit_ton=12.0,  # enough balance
            )
        )

    def test_balance_miss_bucket_ranges(self) -> None:
        self.assertEqual(get_balance_miss_bucket(8.01), "8_20")
        self.assertEqual(get_balance_miss_bucket(19.99), "8_20")
        self.assertEqual(get_balance_miss_bucket(20.0), "20_50")
        self.assertEqual(get_balance_miss_bucket(49.99), "20_50")
        self.assertEqual(get_balance_miss_bucket(50.0), "50_plus")
        self.assertEqual(get_balance_miss_bucket(120.0), "50_plus")


if __name__ == "__main__":
    unittest.main()
