import unittest
from pathlib import Path
import tempfile
import os

from autosell import (
    allowed_price_change_limit,
    compute_stop_loss_price_ton,
    is_rare_model_protected,
    should_trigger_stuck_prompt,
)
from autosell_store import AutoSellStore


class AutoSellRulesTest(unittest.TestCase):
    def test_stop_loss_cap_minus_4_percent(self) -> None:
        self.assertAlmostEqual(compute_stop_loss_price_ton(10.0, 4.0), 9.6, places=6)
        self.assertAlmostEqual(compute_stop_loss_price_ton(4.5, 4.0), 4.32, places=6)

    def test_rare_model_protect_le_1_percent(self) -> None:
        self.assertTrue(is_rare_model_protected(1.0, 1.0))
        self.assertTrue(is_rare_model_protected(0.7, 1.0))
        self.assertFalse(is_rare_model_protected(1.1, 1.0))
        self.assertFalse(is_rare_model_protected(None, 1.0))

    def test_stuck_branch_with_price_ge_4_ton(self) -> None:
        self.assertTrue(
            should_trigger_stuck_prompt(
                age_seconds=2 * 3600,
                current_price_ton=4.0,
                prompt_price_threshold_ton=4.0,
                stuck_seconds=2 * 3600,
            )
        )

    def test_stuck_branch_with_price_lt_4_ton(self) -> None:
        self.assertFalse(
            should_trigger_stuck_prompt(
                age_seconds=2 * 3600,
                current_price_ton=3.99,
                prompt_price_threshold_ton=4.0,
                stuck_seconds=2 * 3600,
            )
        )

    def test_price_change_limit_and_manual_plus_5(self) -> None:
        self.assertEqual(allowed_price_change_limit(0), 4)
        self.assertEqual(allowed_price_change_limit(5), 9)
        self.assertEqual(allowed_price_change_limit(10), 14)


class AutoSellStorePromptTest(unittest.TestCase):
    def test_prompt_timeout_default_hold_flow(self) -> None:
        db_fd, db_name = tempfile.mkstemp(prefix="autosell_test_", suffix=".sqlite3")
        try:
            os.close(db_fd)
            Path(db_name).unlink(missing_ok=True)
            store = AutoSellStore(db_path=Path(db_name))
            store.record_purchase(
                gift_id="gift-1",
                buy_price_ton=10.0,
                collection_name="Test",
                collection_title="Test",
                model_name="M",
                model_rarity_percent=2.0,
            )
            prompt_id = store.create_critical_prompt(
                gift_id="gift-1",
                reason="stuck_2h_no_profit",
                deadline_ts=1,
                options=["hold", "sell_now"],
                default_on_timeout="hold",
            )
            prompt = store.get_prompt(prompt_id)
            self.assertIsNotNone(prompt)
            self.assertEqual(prompt["status"], "OPEN")

            expired = store.list_expired_open_prompts(now_ts=2)
            self.assertEqual(len(expired), 1)
            store.resolve_prompt(prompt_id, action="hold", source="AUTO_TIMEOUT")
            resolved = store.get_prompt(prompt_id)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved["status"], "CLOSED")
            self.assertEqual(resolved["resolved_action"], "hold")
            self.assertEqual(resolved["resolved_by"], "AUTO_TIMEOUT")
        finally:
            try:
                Path(db_name).unlink(missing_ok=True)
                Path(f"{db_name}-wal").unlink(missing_ok=True)
                Path(f"{db_name}-shm").unlink(missing_ok=True)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
