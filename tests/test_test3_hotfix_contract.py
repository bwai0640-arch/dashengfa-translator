from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import desktop_app
from selection_capture import capture_selected_text_with_clipboard
from desktop_app import (
    DesktopSelectionWatcher,
    DesktopTranslatorApp,
    SettingsStore,
    SettingsWindow,
    UNSUPPORTED_APPS,
    app_info_from_hwnd,
)


class Test3HotfixContractTests(unittest.TestCase):
    def test_global_hooks_explicitly_never_suppress_user_input(self) -> None:
        store = SimpleNamespace()
        watcher = DesktopSelectionWatcher(
            store,
            lambda *_args: None,
            lambda _status: None,
            lambda _x, _y: None,
        )
        worker = mock.Mock()
        mouse_listener = mock.Mock()
        keyboard_listener = mock.Mock()

        with mock.patch.object(
            desktop_app.threading, "Thread", return_value=worker
        ), mock.patch.object(
            desktop_app.mouse, "Listener", return_value=mouse_listener
        ) as mouse_factory, mock.patch.object(
            desktop_app.keyboard, "Listener", return_value=keyboard_listener
        ) as keyboard_factory:
            watcher.start()

        self.assertFalse(mouse_factory.call_args.kwargs["suppress"])
        self.assertFalse(keyboard_factory.call_args.kwargs["suppress"])
        worker.start.assert_called_once_with()
        mouse_listener.start.assert_called_once_with()
        keyboard_listener.start.assert_called_once_with()

    def test_wechat_is_disabled_even_when_an_old_settings_file_enabled_it(self) -> None:
        store = SettingsStore.__new__(SettingsStore)
        store.lock = threading.RLock()
        store.data = {
            "enabled_apps": {
                "wechat.exe": True,
                "weixin.exe": True,
                "winword.exe": True,
            },
            "custom_common_apps": ["wechat.exe", "winword.exe"],
        }

        self.assertEqual(UNSUPPORTED_APPS, {"wechat.exe", "weixin.exe"})
        self.assertEqual(store.enabled_apps(), {"winword.exe": True})
        self.assertFalse(store.is_app_enabled("WECHAT.EXE"))
        self.assertFalse(store.is_app_enabled("weixin.exe"))
        self.assertEqual(store.custom_common(), ["winword.exe"])
        self.assertFalse(store.set_app("wechat.exe", True))
        self.assertFalse(store.set_common("weixin.exe", True))

    def test_wechat_foreground_window_is_never_returned_as_a_capture_target(self) -> None:
        store = mock.Mock()
        with mock.patch.object(
            desktop_app.win32process,
            "GetWindowThreadProcessId",
            return_value=(1, 99),
        ), mock.patch.object(
            desktop_app, "process_path", return_value=r"C:\Program Files\WeChat\WeChat.exe"
        ), mock.patch.object(desktop_app.win32gui, "GetWindowText") as get_title:
            result = app_info_from_hwnd(123, store)

        self.assertIsNone(result)
        get_title.assert_not_called()
        store.app_name.assert_not_called()

    def test_wechat_cannot_be_readded_manually(self) -> None:
        settings = SettingsWindow.__new__(SettingsWindow)
        settings.window = mock.Mock()
        settings.store = mock.Mock()
        settings.app = mock.Mock()

        with mock.patch.object(
            desktop_app.filedialog,
            "askopenfilename",
            return_value=r"C:\Program Files\WeChat\WeChat.exe",
        ), mock.patch.object(desktop_app.messagebox, "showinfo") as showinfo:
            settings.add_application()

        showinfo.assert_called_once()
        settings.store.set_app.assert_not_called()
        settings.store.set_common.assert_not_called()
        settings.app.refresh_app_state.assert_not_called()

    def test_copy_translation_writes_the_current_translation(self) -> None:
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.current_result = SimpleNamespace(translated="translation result")
        app.root = mock.Mock()
        app.status_text = mock.Mock()

        app.copy_translation()

        app.root.clipboard_clear.assert_called_once_with()
        app.root.clipboard_append.assert_called_once_with("translation result")
        app.status_text.set.assert_called_once_with("译文已复制")
        app.root.after.assert_called_once_with(1200, app._restore_status_after_copy)

    def test_copy_intent_arriving_after_read_still_wins_before_restore(self) -> None:
        now = 0.0
        intent_checks = 0
        snapshot = mock.Mock()

        def monotonic() -> float:
            return now

        def sleep(seconds: float) -> None:
            nonlocal now
            now += seconds

        def sequence_number() -> int:
            return 2 if now >= 0.05 else 1

        def external_copy_intent_detected() -> bool:
            nonlocal intent_checks
            intent_checks += 1
            return intent_checks >= 2

        result = capture_selected_text_with_clipboard(
            old_text="old",
            old_formats=[2, 15],
            old_state_known=True,
            old_sequence=1,
            snapshot_factory=lambda: snapshot,
            sequence_number=sequence_number,
            send_copy=lambda: None,
            read_text=lambda: "selected",
            restore_plain_text=lambda _value, _expected: True,
            timeout_seconds=0.2,
            clipboard_change_is_ours=lambda: True,
            external_copy_intent_detected=external_copy_intent_detected,
            monotonic=monotonic,
            sleep=sleep,
        )

        self.assertEqual(result.text, "selected")
        self.assertEqual(result.reason, "external_change_preserved")
        self.assertGreaterEqual(intent_checks, 2)
        snapshot.restore.assert_not_called()
        snapshot.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
