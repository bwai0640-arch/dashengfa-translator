from __future__ import annotations

import ast
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desktop_app import DesktopTranslatorApp


class FakeRoot:
    def __init__(self) -> None:
        self.next_id = 0
        self.callbacks: dict[str, tuple[int, object]] = {}
        self.cancelled: set[str] = set()
        self.withdraw = mock.Mock()

    def after(self, delay: int, callback: object) -> str:
        self.next_id += 1
        after_id = f"after-{self.next_id}"
        self.callbacks[after_id] = (delay, callback)
        return after_id

    def after_cancel(self, after_id: str) -> None:
        self.cancelled.add(after_id)

    @staticmethod
    def after_idle(callback: object, *args: object) -> None:
        callback(*args)  # type: ignore[operator]

    def fire(self, after_id: str) -> None:
        _delay, callback = self.callbacks.pop(after_id)
        if after_id not in self.cancelled:
            callback()  # type: ignore[operator]

    def fire_all(self) -> None:
        for after_id in list(self.callbacks):
            self.fire(after_id)


class FakeButton:
    def __init__(self) -> None:
        self.command: object | None = None

    def invoke(self) -> None:
        assert self.command is not None
        self.command()  # type: ignore[operator]


class FakeVariable:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


class PronunciationClickTests(unittest.TestCase):
    @staticmethod
    def make_app(
        text: str = "integration",
        preference: str = "speed",
    ) -> DesktopTranslatorApp:
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.root = FakeRoot()
        app.store = SimpleNamespace(
            get=lambda key, default=None: (
                preference if key == "auto_speech_preference" else default
            )
        )
        app.speech = SimpleNamespace(
            speak=mock.Mock(),
            prefetch=mock.Mock(),
            cancel=mock.Mock(),
            cancel_prefetch=mock.Mock(),
            record_timing_event=mock.Mock(),
        )
        app.selection_token = 7
        app.active_source = text
        app.current_result = None
        app.pending_auto_speak_token = None
        app.pending_auto_speak_accent = None
        app.pending_auto_speak_mode = None
        app.auto_speak_lock = threading.Lock()
        app.status_text = FakeVariable()
        app.speech_progress_text = FakeVariable("正在并行预热 AI 发音…")
        app.quitting = False
        app.pronunciation_click_delay_ms = 720
        app._pending_pronunciation_after_id = None
        app._pronunciation_click_epoch = 0
        app._speech_status_epoch = 0
        app._pronunciation_mouse_button = None
        return app

    @staticmethod
    def mouse_click(
        app: DesktopTranslatorApp,
        button: FakeButton,
        accent: str,
    ) -> str:
        event = SimpleNamespace(widget=button)
        app._on_pronunciation_button_press(event, accent)
        app._activate_pronunciation_button(button, accent)  # type: ignore[arg-type]
        assert app._pending_pronunciation_after_id is not None
        return app._pending_pronunciation_after_id

    def test_speed_single_prefetches_then_resolves_to_system(self) -> None:
        app = self.make_app()
        button = FakeButton()

        after_id = self.mouse_click(app, button, "us")

        app.speech.cancel.assert_called_once_with()
        app.speech.prefetch.assert_called_once_with("integration", "us")
        app.speech.speak.assert_not_called()
        self.assertEqual(
            app.speech_progress_text.get(),
            "已收到，正在准备可取消的 US AI 候选…",
        )
        self.assertEqual(app.root.callbacks[after_id][0], 720)

        app.root.fire(after_id)

        app.speech.cancel_prefetch.assert_called_once_with(
            reason="gesture_resolved_to_system"
        )
        app.speech.speak.assert_called_once_with(
            "integration", "us", mode="system"
        )

    def test_speed_double_cancels_single_and_promotes_ai_once(self) -> None:
        app = self.make_app()
        button = FakeButton()
        after_id = self.mouse_click(app, button, "uk")

        result = app._on_pronunciation_button_double_press(
            SimpleNamespace(widget=button),
            "uk",
        )
        app.root.fire(after_id)

        self.assertEqual(result, "break")
        self.assertIn(after_id, app.root.cancelled)
        app.speech.speak.assert_called_once_with("integration", "uk")
        app.speech.cancel_prefetch.assert_not_called()

    def test_rapid_us_then_uk_keeps_only_latest_mouse_single(self) -> None:
        app = self.make_app()
        us_button = FakeButton()
        uk_button = FakeButton()
        us_after = self.mouse_click(app, us_button, "us")
        uk_after = self.mouse_click(app, uk_button, "uk")

        app.root.fire(us_after)
        app.root.fire(uk_after)

        self.assertIn(us_after, app.root.cancelled)
        app.speech.speak.assert_called_once_with(
            "integration", "uk", mode="system"
        )

    def test_pending_us_then_uk_double_never_leaks_the_old_ai_request(self) -> None:
        app = self.make_app()
        us_after = self.mouse_click(app, FakeButton(), "us")

        app._on_pronunciation_button_double_press(
            SimpleNamespace(widget=FakeButton()),
            "uk",
        )
        app.root.fire(us_after)

        self.assertIn(us_after, app.root.cancelled)
        app.speech.speak.assert_called_once_with("integration", "uk")

    def test_keyboard_invoke_uses_single_mapping_without_mouse_delay(self) -> None:
        app = self.make_app(preference="natural")
        mouse_button = FakeButton()
        keyboard_button = FakeButton()
        old_after = self.mouse_click(app, mouse_button, "us")
        keyboard_button.command = lambda: app._activate_pronunciation_button(
            keyboard_button, "uk"  # type: ignore[arg-type]
        )

        result = app._invoke_pronunciation_button_from_keyboard(  # type: ignore[arg-type]
            keyboard_button
        )
        app.root.fire(old_after)

        self.assertEqual(result, "break")
        self.assertIn(old_after, app.root.cancelled)
        app.speech.speak.assert_called_once_with("integration", "uk")
        self.assertEqual(
            app.speech_progress_text.get(),
            "已收到，正在准备 UK AI 发音…",
        )

    def test_delayed_single_cannot_speak_a_new_selection(self) -> None:
        app = self.make_app("old")
        after_id = self.mouse_click(app, FakeButton(), "us")
        app.selection_token += 1
        app.active_source = "new"

        app.root.fire(after_id)

        app.speech.speak.assert_not_called()

    def test_chinese_pending_pronunciation_preserves_system_mode(self) -> None:
        app = self.make_app("你好")

        app.speak("uk", mode="system", expected_token=7)

        self.assertEqual(app.pending_auto_speak_token, 7)
        self.assertEqual(app.pending_auto_speak_accent, "uk")
        self.assertEqual(app.pending_auto_speak_mode, "system")
        app.speech.speak.assert_not_called()

        app._play_pending_auto_speak(7, "hello")

        app.speech.speak.assert_called_once_with("hello", "uk", mode="system")
        self.assertIsNone(app.pending_auto_speak_mode)

    def test_natural_preference_single_uses_ai_and_double_uses_system(self) -> None:
        for text in ("integration", "systems work together"):
            for accent in ("us", "uk"):
                with self.subTest(text=text, accent=accent, gesture="single"):
                    app = self.make_app(text, preference="natural")
                    after_id = self.mouse_click(app, FakeButton(), accent)
                    app.root.fire(after_id)
                    app.speech.speak.assert_called_once_with(text, accent)
                    app.speech.cancel_prefetch.assert_not_called()
                with self.subTest(text=text, accent=accent, gesture="double"):
                    app = self.make_app(text, preference="natural")
                    button = FakeButton()
                    after_id = self.mouse_click(app, button, accent)
                    app._on_pronunciation_button_double_press(
                        SimpleNamespace(widget=button), accent
                    )
                    app.root.fire(after_id)
                    app.speech.cancel_prefetch.assert_called_once_with(
                        reason="gesture_resolved_to_system"
                    )
                    app.speech.speak.assert_called_once_with(
                        text, accent, mode="system"
                    )

    def test_speed_preference_maps_words_and_phrases_for_both_accents(self) -> None:
        for text in ("integration", "systems work together"):
            for accent in ("us", "uk"):
                with self.subTest(text=text, accent=accent, gesture="single"):
                    app = self.make_app(text, preference="speed")
                    after_id = self.mouse_click(app, FakeButton(), accent)
                    app.root.fire(after_id)
                    app.speech.speak.assert_called_once_with(
                        text, accent, mode="system"
                    )
                with self.subTest(text=text, accent=accent, gesture="double"):
                    app = self.make_app(text, preference="speed")
                    button = FakeButton()
                    after_id = self.mouse_click(app, button, accent)
                    app._on_pronunciation_button_double_press(
                        SimpleNamespace(widget=button), accent
                    )
                    app.root.fire(after_id)
                    app.speech.speak.assert_called_once_with(text, accent)

    def test_speech_core_status_is_marshaled_to_the_ui_setter(self) -> None:
        app = self.make_app()
        app._post_ui = mock.Mock()

        app.set_speech_status("AI 发音已全部就绪")

        app._post_ui.assert_called_once_with(
            app._set_speech_status_if_current,
            0,
            "AI 发音已全部就绪",
        )

    def test_queued_old_core_status_cannot_overwrite_a_new_click(self) -> None:
        app = self.make_app()
        queued: list[tuple[object, tuple[object, ...]]] = []
        app._post_ui = lambda callback, *args: queued.append((callback, args))
        app.set_speech_status("正在播放旧 AI 发音")

        self.mouse_click(app, FakeButton(), "uk")
        for callback, args in queued:
            callback(*args)  # type: ignore[operator]

        self.assertEqual(
            app.speech_progress_text.get(),
            "已收到，正在准备可取消的 UK AI 候选…",
        )

    def test_hiding_or_invalidating_cancels_an_unheard_click(self) -> None:
        app = self.make_app()
        first_after = self.mouse_click(app, FakeButton(), "us")

        app.hide_panel()
        app.root.fire(first_after)

        self.assertIn(first_after, app.root.cancelled)
        app.speech.speak.assert_not_called()
        app.root.withdraw.assert_called_once_with()

        second_after = self.mouse_click(app, FakeButton(), "uk")
        app._invalidate_active_request()
        app.root.fire(second_after)

        self.assertIn(second_after, app.root.cancelled)
        app.speech.speak.assert_not_called()


class PronunciationUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source_path = Path(__file__).resolve().parents[1] / "desktop_app.py"
        cls.tree = ast.parse(source_path.read_text(encoding="utf-8"))
        cls.app_class = next(
            node
            for node in cls.tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DesktopTranslatorApp"
        )

    @classmethod
    def method(cls, name: str) -> ast.FunctionDef:
        return next(
            node
            for node in cls.app_class.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )

    @staticmethod
    def called_methods(method: ast.FunctionDef) -> list[str]:
        return [
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ]

    def test_both_windows_build_us_and_uk_through_shared_binding_helper(self) -> None:
        for method_name in ("_build_panel", "_build_mini"):
            calls = self.called_methods(self.method(method_name))
            self.assertEqual(calls.count("_make_pronunciation_button"), 2)

    def test_destructive_lifecycle_paths_cancel_unheard_clicks(self) -> None:
        for method_name in (
            "_handle_selection",
            "_handle_capture_started",
            "translate_manual",
            "_invalidate_active_request",
            "hide_panel",
            "hide_mini",
            "quit",
        ):
            with self.subTest(method=method_name):
                calls = self.called_methods(self.method(method_name))
                self.assertIn("cancel_pending_pronunciation_click", calls)

    def test_selection_paths_do_not_start_old_selection_ai_prefetch(self) -> None:
        for method_name in ("_handle_selection", "translate_manual", "show_result"):
            with self.subTest(method=method_name):
                calls = self.called_methods(self.method(method_name))
                self.assertNotIn("_prefetch_natural_accents", calls)


if __name__ == "__main__":
    unittest.main()
