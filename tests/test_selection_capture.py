from __future__ import annotations

import unittest
from types import SimpleNamespace

from selection_capture import (
    ClipboardCaptureResult,
    capture_selected_text_with_clipboard,
    read_powerpoint_selected_text,
    read_uia_selected_text,
    timing_for_app,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeSnapshot:
    def __init__(self, restore_result: bool = True) -> None:
        self.restore_result = restore_result
        self.restore_calls = 0
        self.close_calls = 0

    def restore(self) -> bool:
        self.restore_calls += 1
        return self.restore_result

    def close(self) -> None:
        self.close_calls += 1


class PowerPointCaptureTests(unittest.TestCase):
    def test_reads_only_a_real_text_selection(self) -> None:
        selection = SimpleNamespace(Type=3, TextRange=SimpleNamespace(Text="Duration"))
        powerpoint = SimpleNamespace(
            ActiveWindow=SimpleNamespace(Selection=selection, Caption="Deck")
        )

        value = read_powerpoint_selected_text(
            lambda _name: powerpoint, 3000, "Deck - PowerPoint"
        )

        self.assertEqual(value, "Duration")

    def test_does_not_read_an_entire_selected_shape(self) -> None:
        selection = SimpleNamespace(Type=2, TextRange=SimpleNamespace(Text="whole shape"))
        powerpoint = SimpleNamespace(
            ActiveWindow=SimpleNamespace(Selection=selection, Caption="Deck")
        )

        value = read_powerpoint_selected_text(lambda _name: powerpoint, 3000)

        self.assertEqual(value, "")

    def test_rejects_a_powerpoint_instance_whose_window_is_not_foreground(self) -> None:
        selection = SimpleNamespace(Type=3, TextRange=SimpleNamespace(Text="wrong deck"))
        powerpoint = SimpleNamespace(
            ActiveWindow=SimpleNamespace(Selection=selection, Caption="Other deck")
        )

        value = read_powerpoint_selected_text(
            lambda _name: powerpoint, 3000, "Wanted deck - PowerPoint"
        )

        self.assertEqual(value, "")

    def test_powerpoint_and_wechat_get_longer_capture_windows(self) -> None:
        self.assertGreater(timing_for_app("POWERPNT.EXE").clipboard_timeout_seconds, 1.0)
        self.assertGreater(timing_for_app("weixin.exe").clipboard_timeout_seconds, 0.8)


class FakeTextRange:
    def __init__(self, text: str) -> None:
        self.text = text

    def GetText(self, _max_length: int) -> str:
        return self.text


class FakePattern:
    def __init__(self, text: str) -> None:
        self.text = text

    def GetSelection(self) -> list[FakeTextRange]:
        return [FakeTextRange(self.text)]


class FakeControl:
    def __init__(
        self,
        patterns: dict[object, object] | None = None,
        *,
        password: bool = False,
        parent: FakeControl | None = None,
    ) -> None:
        self.patterns = patterns or {}
        self.IsPassword = password
        self.parent = parent

    @property
    def BoundingRectangle(self) -> object:
        raise AssertionError("selection probing must not depend on BoundingRectangle")

    def GetPattern(self, pattern_id: object) -> object:
        value = self.patterns.get(pattern_id)
        if isinstance(value, Exception):
            raise value
        return value

    def GetParentControl(self) -> FakeControl | None:
        return self.parent


class UiaCaptureTests(unittest.TestCase):
    def test_rectangle_failure_does_not_hide_a_valid_text_pattern(self) -> None:
        control = FakeControl({"text2": FakePattern("Duration")})

        value, protected = read_uia_selected_text(
            [control], ["text2", "text"], 3000, lambda text: text.strip()
        )

        self.assertEqual(value, "Duration")
        self.assertFalse(protected)

    def test_one_broken_pattern_does_not_skip_the_next_pattern(self) -> None:
        control = FakeControl(
            {"text2": RuntimeError("unsupported"), "text": FakePattern("WeChat selection")}
        )

        value, protected = read_uia_selected_text(
            [control], ["text2", "text"], 3000, lambda text: text.strip()
        )

        self.assertEqual(value, "WeChat selection")
        self.assertFalse(protected)

    def test_password_control_stops_before_any_text_read(self) -> None:
        control = FakeControl({"text": FakePattern("secret")}, password=True)

        value, protected = read_uia_selected_text(
            [control], ["text"], 3000, lambda text: text.strip()
        )

        self.assertEqual(value, "")
        self.assertTrue(protected)

    def test_password_parent_is_checked_before_child_text_pattern(self) -> None:
        parent = FakeControl(password=True)
        child = FakeControl({"text": FakePattern("secret")}, parent=parent)

        value, protected = read_uia_selected_text(
            [child], ["text"], 3000, lambda text: text.strip()
        )

        self.assertEqual(value, "")
        self.assertTrue(protected)


class ClipboardCaptureTests(unittest.TestCase):
    def run_capture(self, **overrides: object) -> tuple[ClipboardCaptureResult, FakeSnapshot, list[str | None]]:
        clock = FakeClock()
        snapshot = FakeSnapshot()
        restored_text: list[str | None] = []
        values: dict[str, object] = {
            "old_text": "old",
            "old_formats": [2, 15, 49324],
            "old_state_known": True,
            "old_sequence": 1,
            "snapshot_factory": lambda: snapshot,
            "sequence_number": lambda: 2 if clock.now >= 0.65 else 1,
            "send_copy": lambda: None,
            "read_text": lambda: "selected",
            "restore_plain_text": lambda value: restored_text.append(value) is None,
            "timeout_seconds": 0.9,
            "monotonic": clock.monotonic,
            "sleep": clock.sleep,
        }
        values.update(overrides)
        result = capture_selected_text_with_clipboard(**values)
        return result, snapshot, restored_text

    def test_rich_or_image_clipboard_no_longer_blocks_safe_capture(self) -> None:
        result, snapshot, restored_text = self.run_capture()

        self.assertEqual(result.text, "selected")
        self.assertEqual(result.reason, "captured")
        self.assertTrue(result.restored)
        self.assertEqual(snapshot.restore_calls, 1)
        self.assertEqual(snapshot.close_calls, 1)
        self.assertEqual(restored_text, [])

    def test_does_not_send_copy_when_rich_clipboard_cannot_be_snapshotted(self) -> None:
        copy_calls: list[bool] = []
        result, _snapshot, _restored = self.run_capture(
            snapshot_factory=lambda: None,
            send_copy=lambda: copy_calls.append(True),
        )

        self.assertEqual(result.reason, "snapshot_unavailable")
        self.assertFalse(result.attempted)
        self.assertEqual(copy_calls, [])

    def test_plain_text_clipboard_still_works_without_ole_snapshot(self) -> None:
        result, _snapshot, restored_text = self.run_capture(
            old_formats=[1, 7, 13, 16],
            snapshot_factory=lambda: None,
        )

        self.assertEqual(result.text, "selected")
        self.assertTrue(result.restored)
        self.assertEqual(restored_text, ["old"])

    def test_ansi_only_clipboard_is_not_cleared_without_a_snapshot(self) -> None:
        copy_calls: list[bool] = []
        result, _snapshot, restored_text = self.run_capture(
            old_text=None,
            old_formats=[1, 7, 16],
            snapshot_factory=lambda: None,
            send_copy=lambda: copy_calls.append(True),
        )

        self.assertEqual(result.reason, "snapshot_unavailable")
        self.assertEqual(copy_calls, [])
        self.assertEqual(restored_text, [])

    def test_unknown_clipboard_state_never_allows_an_unprotected_copy(self) -> None:
        copy_calls: list[bool] = []
        result, _snapshot, _restored = self.run_capture(
            old_formats=[],
            old_state_known=False,
            snapshot_factory=lambda: None,
            send_copy=lambda: copy_calls.append(True),
        )

        self.assertEqual(result.reason, "snapshot_unavailable")
        self.assertEqual(copy_calls, [])

    def test_clipboard_change_during_snapshot_cancels_before_copy(self) -> None:
        copy_calls: list[bool] = []
        result, snapshot, _restored = self.run_capture(
            old_sequence=1,
            sequence_number=lambda: 2,
            send_copy=lambda: copy_calls.append(True),
        )

        self.assertEqual(result.reason, "concurrent_change")
        self.assertFalse(result.attempted)
        self.assertEqual(copy_calls, [])
        self.assertEqual(snapshot.restore_calls, 0)
        self.assertEqual(snapshot.close_calls, 1)

    def test_waits_long_enough_for_a_slow_wechat_copy(self) -> None:
        result, _snapshot, _restored = self.run_capture()

        self.assertEqual(result.text, "selected")

    def test_partial_copy_exception_is_still_observed_and_restored(self) -> None:
        def fail_copy() -> None:
            raise RuntimeError("keyboard unavailable")

        result, snapshot, _restored = self.run_capture(send_copy=fail_copy)

        self.assertEqual(result.text, "selected")
        self.assertEqual(result.reason, "captured")
        self.assertEqual(snapshot.restore_calls, 1)
        self.assertEqual(snapshot.close_calls, 1)

    def test_copy_exception_without_clipboard_change_never_rewrites_it(self) -> None:
        def fail_copy() -> None:
            raise RuntimeError("keyboard unavailable")

        result, snapshot, _restored = self.run_capture(
            send_copy=fail_copy,
            sequence_number=lambda: 1,
            timeout_seconds=0.1,
        )

        self.assertEqual(result.reason, "copy_failed")
        self.assertEqual(snapshot.restore_calls, 0)
        self.assertEqual(snapshot.close_calls, 1)

    def test_cancels_before_copy_if_focus_has_changed(self) -> None:
        copy_calls: list[bool] = []
        result, snapshot, _restored = self.run_capture(
            focus_is_current=lambda: False,
            send_copy=lambda: copy_calls.append(True),
        )

        self.assertEqual(result.reason, "focus_changed")
        self.assertFalse(result.attempted)
        self.assertEqual(copy_calls, [])
        self.assertEqual(snapshot.restore_calls, 0)

    def test_focus_change_after_copy_restores_only_our_clipboard_change(self) -> None:
        clock = FakeClock()
        snapshot = FakeSnapshot()

        result = capture_selected_text_with_clipboard(
            old_text="old",
            old_formats=[2, 15],
            old_state_known=True,
            old_sequence=1,
            snapshot_factory=lambda: snapshot,
            sequence_number=lambda: 2 if clock.now >= 0.05 else 1,
            send_copy=lambda: None,
            read_text=lambda: "must not be read",
            restore_plain_text=lambda _value: True,
            timeout_seconds=0.9,
            focus_is_current=lambda: clock.now < 0.05,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "focus_changed")
        self.assertEqual(snapshot.restore_calls, 1)

    def test_focus_change_before_delayed_copy_waits_and_restores_target_change(self) -> None:
        clock = FakeClock()
        snapshot = FakeSnapshot()

        result = capture_selected_text_with_clipboard(
            old_text="old",
            old_formats=[2, 15],
            old_state_known=True,
            old_sequence=1,
            snapshot_factory=lambda: snapshot,
            sequence_number=lambda: 2 if clock.now >= 0.1 else 1,
            send_copy=lambda: None,
            read_text=lambda: "must not be read after cancellation",
            restore_plain_text=lambda _value: True,
            timeout_seconds=0.3,
            focus_is_current=lambda: clock.now < 0.05,
            clipboard_change_is_ours=lambda: True,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "focus_changed")
        self.assertEqual(snapshot.restore_calls, 1)

    def test_focus_change_preserves_clipboard_owned_by_another_process(self) -> None:
        clock = FakeClock()
        snapshot = FakeSnapshot()

        result = capture_selected_text_with_clipboard(
            old_text="old",
            old_formats=[2, 15],
            old_state_known=True,
            old_sequence=1,
            snapshot_factory=lambda: snapshot,
            sequence_number=lambda: 2 if clock.now >= 0.1 else 1,
            send_copy=lambda: None,
            read_text=lambda: "external",
            restore_plain_text=lambda _value: True,
            timeout_seconds=0.3,
            focus_is_current=lambda: clock.now < 0.05,
            clipboard_change_is_ours=lambda: False,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "concurrent_change")
        self.assertEqual(snapshot.restore_calls, 0)

    def test_first_change_from_another_owner_is_never_read_or_overwritten(self) -> None:
        read_calls: list[bool] = []
        result, snapshot, _restored = self.run_capture(
            clipboard_change_is_ours=lambda: False,
            read_text=lambda: read_calls.append(True) or "external text",
        )

        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "concurrent_change")
        self.assertEqual(read_calls, [])
        self.assertEqual(snapshot.restore_calls, 0)

    def test_no_clipboard_change_does_not_rewrite_clipboard(self) -> None:
        result, snapshot, restored_text = self.run_capture(
            sequence_number=lambda: 1,
            timeout_seconds=0.1,
        )

        self.assertEqual(result.reason, "no_clipboard_change")
        self.assertEqual(snapshot.restore_calls, 0)
        self.assertEqual(snapshot.close_calls, 1)
        self.assertEqual(restored_text, [])

    def test_newer_external_clipboard_change_is_preserved(self) -> None:
        clock = FakeClock()
        reads = 0

        def sequence_number() -> int:
            nonlocal reads
            reads += 1
            if reads >= 4:
                return 3
            return 2 if clock.now >= 0.05 else 1

        result, snapshot, restored_text = self.run_capture(
            sequence_number=sequence_number,
            read_text=lambda: "selected",
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(result.text, "selected")
        self.assertEqual(result.reason, "external_change_preserved")
        self.assertEqual(snapshot.restore_calls, 0)
        self.assertEqual(restored_text, [])

    def test_rich_clipboard_restore_failure_never_downgrades_to_plain_text(self) -> None:
        snapshot = FakeSnapshot(restore_result=False)
        restored_text: list[str | None] = []
        result, _unused, _restored = self.run_capture(
            snapshot_factory=lambda: snapshot,
            restore_plain_text=lambda value: restored_text.append(value) is None,
        )

        self.assertEqual(result.reason, "restore_failed")
        self.assertFalse(result.restored)
        self.assertEqual(restored_text, [])


if __name__ == "__main__":
    unittest.main()
