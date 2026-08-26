from __future__ import annotations

import unittest

from capture_runtime import (
    MAX_ADAPTIVE_TIMEOUT_SECONDS,
    MIN_ADAPTIVE_TIMEOUT_SECONDS,
    AdaptiveWaitState,
    adaptive_wait_from_mapping,
    learn_adaptive_wait,
)


class AdaptiveWaitTests(unittest.TestCase):
    def test_invalid_saved_state_falls_back_to_bounded_base(self) -> None:
        state = adaptive_wait_from_mapping({"timeout_seconds": "broken"}, 0.9)
        self.assertEqual(state.timeout_seconds, 0.9)

        non_finite = adaptive_wait_from_mapping({"timeout_seconds": float("nan")}, 0.9)
        self.assertEqual(non_finite.timeout_seconds, 0.9)

        legacy_too_short = adaptive_wait_from_mapping({"timeout_seconds": 0.35}, 0.9)
        self.assertEqual(legacy_too_short.timeout_seconds, 0.9)

    def test_success_never_learns_below_an_app_safety_window(self) -> None:
        state = AdaptiveWaitState(timeout_seconds=0.9)

        learned = learn_adaptive_wait(
            state,
            reason="captured",
            elapsed_ms=125,
            minimum_timeout_seconds=0.9,
        )

        self.assertEqual(learned.timeout_seconds, 0.9)

    def test_success_learns_a_shorter_wait_and_resets_timeout_streak(self) -> None:
        state = AdaptiveWaitState(timeout_seconds=0.9, timeout_streak=3)

        learned = learn_adaptive_wait(state, reason="captured", elapsed_ms=125)

        self.assertGreaterEqual(learned.timeout_seconds, MIN_ADAPTIVE_TIMEOUT_SECONDS)
        self.assertLess(learned.timeout_seconds, 0.9)
        self.assertEqual(learned.samples, 1)
        self.assertEqual(learned.timeout_streak, 0)

    def test_only_second_real_timeout_increases_wait(self) -> None:
        state = AdaptiveWaitState(timeout_seconds=0.7)

        first = learn_adaptive_wait(state, reason="no_clipboard_change", elapsed_ms=700)
        second = learn_adaptive_wait(first, reason="no_clipboard_change", elapsed_ms=700)

        self.assertEqual(first.timeout_seconds, 0.7)
        self.assertAlmostEqual(second.timeout_seconds, 0.8)

    def test_wechat_base_timeout_can_grow_after_repeated_real_timeouts(self) -> None:
        state = adaptive_wait_from_mapping({}, 1.5)

        first = learn_adaptive_wait(
            state,
            reason="no_clipboard_change",
            elapsed_ms=1500,
        )
        second = learn_adaptive_wait(
            first,
            reason="no_clipboard_change",
            elapsed_ms=1500,
        )

        self.assertEqual(first.timeout_seconds, 1.5)
        self.assertAlmostEqual(second.timeout_seconds, 1.6)
        self.assertGreater(MAX_ADAPTIVE_TIMEOUT_SECONDS, 1.6)

    def test_untrusted_failures_do_not_train_and_timeout_is_bounded(self) -> None:
        state = AdaptiveWaitState(timeout_seconds=MAX_ADAPTIVE_TIMEOUT_SECONDS)
        unchanged = learn_adaptive_wait(state, reason="focus_changed", elapsed_ms=5000)
        increased = learn_adaptive_wait(
            AdaptiveWaitState(timeout_seconds=MAX_ADAPTIVE_TIMEOUT_SECONDS, timeout_streak=1),
            reason="no_clipboard_change",
            elapsed_ms=5000,
        )

        self.assertEqual(unchanged, state)
        self.assertEqual(increased.timeout_seconds, MAX_ADAPTIVE_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
