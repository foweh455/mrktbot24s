"""
Property-based tests for sniper-reliability-improvements.

Covers four correctness properties from design.md:
    Property 1 — effective_cadence clamp to FLOOR_REFRESH_MIN_SECONDS
    Property 3 — is_zero_rarity specification and determinism
    Property 5 — allowed_price_change_limit linearity & monotonicity
    Property 6 — runtime state round-trip for new fields

Uses hypothesis to generate random inputs within realistic bounds.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import config
from autosell import allowed_price_change_limit
from main import effective_cadence
from scanner import Listing, is_zero_rarity


# Feature: sniper-reliability-improvements, Property 3: is_zero_rarity specification
listing_strategy = st.builds(
    Listing,
    gift_id=st.text(min_size=1, max_size=16),
    collection_name=st.text(min_size=1, max_size=16),
    collection_title=st.text(min_size=0, max_size=32),
    listing_price_nano=st.integers(min_value=1, max_value=10**12),
    floor_price_nano=st.integers(min_value=0, max_value=10**12),
    discount_percent=st.floats(
        min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False
    ),
    model_name=st.one_of(
        st.just(""),
        st.just("   "),
        st.text(min_size=1, max_size=32),
    ),
    model_rarity_per_mille=st.one_of(
        st.none(),
        st.integers(min_value=0, max_value=1000),
    ),
    backdrop_name=st.text(max_size=32),
    number=st.one_of(st.none(), st.integers(min_value=0, max_value=10**6)),
)


class ZeroRarityProperties(unittest.TestCase):
    @given(listing=listing_strategy)
    def test_matches_or_rule(self, listing: Listing) -> None:
        per_mille = listing.model_rarity_per_mille
        name_empty = (listing.model_name or "").strip() == ""
        expected = (per_mille is None) or (per_mille == 0) or name_empty
        self.assertIs(is_zero_rarity(listing), expected)

    @given(listing=listing_strategy)
    def test_deterministic(self, listing: Listing) -> None:
        self.assertIs(is_zero_rarity(listing), is_zero_rarity(listing))


class AllowedPriceChangeLimitProperties(unittest.TestCase):
    # Feature: sniper-reliability-improvements, Property 5

    @given(n=st.integers(min_value=-100, max_value=10_000))
    def test_linear(self, n: int) -> None:
        result = allowed_price_change_limit(n)
        self.assertEqual(
            result,
            int(config.AUTO_SELL_PRICE_CHANGE_LIMIT_BASE) + max(0, n),
        )
        self.assertGreaterEqual(result, int(config.AUTO_SELL_PRICE_CHANGE_LIMIT_BASE))

    @given(
        a=st.integers(min_value=-100, max_value=10_000),
        b=st.integers(min_value=-100, max_value=10_000),
    )
    def test_monotonic(self, a: int, b: int) -> None:
        if a > b:
            a, b = b, a
        self.assertLessEqual(
            allowed_price_change_limit(a),
            allowed_price_change_limit(b),
        )


class EffectiveCadenceProperties(unittest.TestCase):
    # Feature: sniper-reliability-improvements, Property 1

    @given(
        v=st.floats(
            min_value=-1e6,
            max_value=1e6,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    def test_clamp(self, v: float) -> None:
        result = effective_cadence(v)
        self.assertGreaterEqual(result, float(config.FLOOR_REFRESH_MIN_SECONDS))
        if v >= config.FLOOR_REFRESH_MIN_SECONDS:
            self.assertEqual(result, float(v))
        else:
            self.assertEqual(result, float(config.FLOOR_REFRESH_MIN_SECONDS))


class RuntimeStateRoundTripProperties(unittest.TestCase):
    # Feature: sniper-reliability-improvements, Property 6

    @given(
        cadence=st.floats(
            min_value=float(config.FLOOR_REFRESH_MIN_SECONDS),
            max_value=600.0,
            allow_nan=False,
            allow_infinity=False,
        ),
        threshold=st.floats(
            min_value=0.0,
            max_value=100.0,
            allow_nan=False,
            allow_infinity=False,
        ),
        enabled=st.booleans(),
    )
    @settings(
        suppress_health_check=[HealthCheck.function_scoped_fixture],
        max_examples=25,
        deadline=None,
    )
    def test_roundtrip_new_fields(
        self,
        cadence: float,
        threshold: float,
        enabled: bool,
    ) -> None:
        from state import AppState  # local import to isolate

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "runtime_state.json"
            payload = {
                "floor_refresh_seconds": cadence,
                "zero_rarity_min_discount": threshold,
                "zero_rarity_gate_enabled": enabled,
            }
            state_file.write_text(
                json.dumps(payload, ensure_ascii=True),
                encoding="utf-8",
            )

            app = AppState.__new__(AppState)
            object.__setattr__(app, "_state_file", state_file)
            object.__setattr__(app, "_autosave_enabled", False)
            for key, value in app._defaults().items():
                object.__setattr__(app, key, value)
            for key, value in app._load().items():
                object.__setattr__(app, key, value)

            self.assertEqual(app.floor_refresh_seconds, cadence)
            self.assertEqual(app.zero_rarity_min_discount, threshold)
            self.assertIs(app.zero_rarity_gate_enabled, enabled)


if __name__ == "__main__":
    unittest.main()
