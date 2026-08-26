from __future__ import annotations

import queue
import sqlite3
import threading
import unittest
import json
import tempfile
import wave
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pynput import keyboard, mouse

import selection_capture
import app as engine
import desktop_app as desktop_module

from desktop_app import (
    AMBER,
    RED,
    AppInfo,
    COMMON_APPS,
    CaptureRequest,
    DesktopSelectionWatcher,
    DesktopTranslatorApp,
    PortableClipboardState,
    SelectionProbeResult,
    SettingsStore,
    SettingsWindow,
    WorkArea,
    clipboard_owner_matches_app,
    restore_clipboard_text_if_unchanged,
    run_packaged_smoke_test,
    windows_mouse_gesture_thresholds,
    acquire_single_instance,
)
from hotkey_service import HotkeyCommand


_ORIGINAL_LOG_PATH = engine.LOG_PATH
_TEST_LOG_DIRECTORY: tempfile.TemporaryDirectory[str] | None = None


def setUpModule() -> None:
    global _TEST_LOG_DIRECTORY
    _TEST_LOG_DIRECTORY = tempfile.TemporaryDirectory(prefix="dashengfa-desktop-tests-")
    engine.LOG_PATH = Path(_TEST_LOG_DIRECTORY.name) / "app.log"


def tearDownModule() -> None:
    engine.LOG_PATH = _ORIGINAL_LOG_PATH
    if _TEST_LOG_DIRECTORY is not None:
        _TEST_LOG_DIRECTORY.cleanup()


class FakeListener:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class FakeWorker:
    def __init__(self) -> None:
        self.join_timeouts: list[float] = []

    def join(self, timeout: float | None = None) -> None:
        if timeout is not None:
            self.join_timeouts.append(timeout)


class FakeWindow:
    def __init__(self) -> None:
        self.destroy_calls = 0
        self.after_callbacks: list[tuple[int, object]] = []

    def after(self, delay: int, callback: object) -> None:
        self.after_callbacks.append((delay, callback))

    def destroy(self) -> None:
        self.destroy_calls += 1


class FakeStore:
    def __init__(self) -> None:
        self.flush_calls = 0

    def flush_pending(self) -> None:
        self.flush_calls += 1


class FakeVariable:
    def __init__(self, value: bool) -> None:
        self.value = value

    def set(self, value: bool) -> None:
        self.value = value

    def get(self) -> bool:
        return self.value


class WatcherLifecycleTests(unittest.TestCase):
    @staticmethod
    def make_selection_app(
        *,
        auto_translate: bool = False,
        speech_preference: str = "speed",
    ) -> DesktopTranslatorApp:
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.store = SimpleNamespace(
            get=lambda key, default=True: (
                speech_preference
                if key == "auto_speech_preference"
                else default
            ),
            is_app_enabled=lambda _exe: True,
        )
        app.selection_token = 0
        app.mini_dismissed_token = None
        app.pending_auto_speak_token = None
        app.pending_auto_speak_accent = None
        app.pending_auto_speak_mode = None
        app.auto_speak_lock = threading.Lock()
        app.latest_capture_identity = None
        app.last_invalidated_capture_identity = None
        app.current_app = None
        app.active_source = ""
        app.current_result = None
        app.selection_point = (0, 0)
        app.app_text = FakeVariable("")
        app.status_text = FakeVariable("")
        app.mini_app_text = FakeVariable("")
        app.direction_text = FakeVariable("")
        app.phonetic_text = FakeVariable("")
        app.meta_text = FakeVariable("")
        app.engine_text = FakeVariable("")
        app.mini_source = FakeVariable("")
        app.mini_phonetic = FakeVariable("")
        app.mini_translation = FakeVariable("")
        app.source_text = mock.Mock()
        app.result_text = mock.Mock()
        app.display_mode = "panel"
        app.root = SimpleNamespace(
            deiconify=mock.Mock(),
            lift=mock.Mock(),
            withdraw=mock.Mock(),
            winfo_containing=mock.Mock(return_value=None),
        )
        app.mini = SimpleNamespace(
            winfo_exists=mock.Mock(return_value=True),
            winfo_viewable=mock.Mock(return_value=False),
            withdraw=mock.Mock(),
            winfo_rootx=mock.Mock(return_value=100),
            winfo_rooty=mock.Mock(return_value=100),
            winfo_width=mock.Mock(return_value=260),
            winfo_height=mock.Mock(return_value=150),
        )
        app.settings_window = SimpleNamespace(window=None)
        app.show_panel_no_activate = mock.Mock()
        app.refresh_app_state = mock.Mock()
        app.auto_translate = auto_translate
        app.enqueue = mock.Mock()
        app.request_queue = queue.Queue(maxsize=1)
        app.ui_tasks = queue.SimpleQueue()
        app.quitting = False
        app.speech = SimpleNamespace(
            speak=mock.Mock(),
            cancel=mock.Mock(),
            cancel_prefetch=mock.Mock(),
            record_timing_event=mock.Mock(),
        )
        return app

    @staticmethod
    def make_double_alt_watcher(*, enabled: bool = True) -> DesktopSelectionWatcher:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.state_lock = threading.Lock()
        watcher.events = queue.SimpleQueue()
        watcher.interaction_id = 0
        watcher.input_generation = 0
        watcher.keys_down = set()
        watcher.copy_intent_generation = 0
        watcher.context_menu_armed_until = 0.0
        watcher.press = None
        watcher.last_release = None
        watcher.outside_click_callback = lambda _x, _y: None
        watcher.set_double_alt_enabled(enabled)
        return watcher

    def test_packaged_smoke_test_checks_resources_and_both_directions_without_ui(self) -> None:
        required = (
            "ecdict.db",
            "app_icon.png",
            "app_icon.ico",
            "models/translate-en_zh-1_9/model",
            "models/translate-en_zh-1_9/sentencepiece.model",
            "models/translate-zh_en-1_9/model",
            "models/translate-zh_en-1_9/sentencepiece.model",
            "models/kokoro/kokoro-v1.0.int8.onnx",
            "models/kokoro/voices-v1.0.bin",
            "models/piper/en_US-lessac-high.onnx",
            "models/piper/en_US-lessac-high.onnx.json",
            "models/piper/en_US-lessac-high.MODEL_CARD.md",
            "models/piper/en_GB-cori-high.onnx",
            "models/piper/en_GB-cori-high.onnx.json",
            "models/piper/en_GB-cori-high.MODEL_CARD.md",
            "models/piper/PIPER_GPL-3.0.txt",
            "models/piper/README.md",
            "uiautomation/bin/UIAutomationClient_VC140_X64.dll",
            "uiautomation/bin/UIAutomationClient_VC140_X86.dll",
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in required:
                path = root / name
                if name.endswith("/model"):
                    path.mkdir(parents=True)
                elif name == "ecdict.db":
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with closing(sqlite3.connect(path)) as db:
                        db.execute(
                            "CREATE TABLE entries "
                            "(word TEXT, phonetic TEXT, translation TEXT, definition TEXT)"
                        )
                        db.execute(
                            "INSERT INTO entries VALUES (?, ?, ?, ?)",
                            ("hello", "həˈləʊ", "你好", "greeting"),
                        )
                        db.commit()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"test")
            report = root / "smoke.json"
            translator = mock.Mock()
            translator.dictionary.lookup.return_value = (
                "həˈləʊ",
                "你好",
                "greeting",
            )
            translator.translate.side_effect = [
                SimpleNamespace(translated="集成"),
                SimpleNamespace(translated="hello"),
            ]
            piper_wav = root / "piper.wav"
            kokoro_wav = root / "kokoro.wav"
            for audio_path in (piper_wav, kokoro_wav):
                with wave.open(str(audio_path), "wb") as stream:
                    stream.setnchannels(1)
                    stream.setsampwidth(2)
                    stream.setframerate(22_050)
                    stream.writeframes(b"\x00\x00" * 240)

            class FixedDigest:
                def __init__(self, value: str) -> None:
                    self.value = value

                def update(self, _chunk: bytes) -> None:
                    pass

                def hexdigest(self) -> str:
                    return self.value

            digest_builders = [
                FixedDigest(value)
                for value in desktop_module.PIPER_RESOURCE_SHA256.values()
            ]

            with mock.patch(
                "desktop_app.engine.resource_path",
                side_effect=lambda name: root / name,
            ), mock.patch(
                "desktop_app.hashlib.sha256", side_effect=digest_builders
            ), mock.patch(
                "desktop_app.engine.LocalTranslator", return_value=translator
            ), mock.patch.object(
                engine.PiperSpeechBackend,
                "synthesize",
                return_value=(piper_wav, 0.01),
            ) as piper_synthesize, mock.patch.object(
                engine.PiperSpeechBackend, "discard"
            ), mock.patch.object(
                engine.KokoroSpeechBackend,
                "synthesize",
                return_value=(kokoro_wav, 0.01),
            ) as kokoro_synthesize, mock.patch.object(
                engine.KokoroSpeechBackend, "discard"
            ), mock.patch(
                "desktop_app.DesktopTranslatorApp",
                side_effect=AssertionError("smoke test must not construct the UI"),
            ):
                exit_code = run_packaged_smoke_test(report)

            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0, payload)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["product"], "大声发划词翻译")
            self.assertEqual(payload["author"], "眼泪斷了线")
            self.assertEqual(payload["dictionary"], "ok:hello")
            self.assertEqual(payload["piper_speech"], "ok:us,uk")
            self.assertEqual(payload["kokoro_speech"], "ok:us,uk")
            self.assertEqual(
                [call.args[1] for call in piper_synthesize.call_args_list],
                ["us", "uk"],
            )
            self.assertEqual(
                [call.args[1] for call in kokoro_synthesize.call_args_list],
                ["us", "uk"],
            )
            translator.load.assert_called_once_with()
            translator.dictionary.lookup.assert_called_once_with("hello")
            self.assertEqual(
                [call.args[0] for call in translator.translate.call_args_list],
                ["integration smoke sentence", "你好"],
            )

    def test_packaged_smoke_test_rejects_a_corrupt_dictionary(self) -> None:
        required = (
            "ecdict.db",
            "app_icon.png",
            "app_icon.ico",
            "models/translate-en_zh-1_9/model",
            "models/translate-en_zh-1_9/sentencepiece.model",
            "models/translate-zh_en-1_9/model",
            "models/translate-zh_en-1_9/sentencepiece.model",
            "models/kokoro/kokoro-v1.0.int8.onnx",
            "models/kokoro/voices-v1.0.bin",
            "models/piper/en_US-lessac-high.onnx",
            "models/piper/en_US-lessac-high.onnx.json",
            "models/piper/en_US-lessac-high.MODEL_CARD.md",
            "models/piper/en_GB-cori-high.onnx",
            "models/piper/en_GB-cori-high.onnx.json",
            "models/piper/en_GB-cori-high.MODEL_CARD.md",
            "models/piper/PIPER_GPL-3.0.txt",
            "models/piper/README.md",
            "uiautomation/bin/UIAutomationClient_VC140_X64.dll",
            "uiautomation/bin/UIAutomationClient_VC140_X86.dll",
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in required:
                path = root / name
                if name.endswith("/model"):
                    path.mkdir(parents=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"not a sqlite database")
            report = root / "smoke.json"

            with mock.patch(
                "desktop_app.engine.resource_path",
                side_effect=lambda name: root / name,
            ), mock.patch(
                "desktop_app.engine.LocalTranslator"
            ) as translator_class:
                exit_code = run_packaged_smoke_test(report)

            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error_type"], "DatabaseError")
            translator_class.assert_not_called()

    def test_single_instance_uses_named_mutex_without_a_network_port(self) -> None:
        with mock.patch.object(
            desktop_module, "_CREATE_MUTEX", return_value=321
        ) as create, mock.patch.object(
            desktop_module.ctypes, "get_last_error", return_value=0
        ), mock.patch.object(desktop_module, "_CLOSE_HANDLE") as close:
            guard = acquire_single_instance()
            self.assertIsNotNone(guard)
            assert guard is not None
            guard.close()

        create.assert_called_once_with(
            None, False, desktop_module.SINGLE_INSTANCE_MUTEX
        )
        close.assert_called_once_with(321)

    def test_existing_named_mutex_reports_the_real_app_as_running(self) -> None:
        with mock.patch.object(
            desktop_module, "_CREATE_MUTEX", return_value=654
        ), mock.patch.object(
            desktop_module.ctypes, "get_last_error", return_value=183
        ), mock.patch.object(desktop_module, "_CLOSE_HANDLE") as close:
            guard = acquire_single_instance()

        self.assertIsNone(guard)
        close.assert_called_once_with(654)

    def test_main_reports_mutex_creation_failure_without_constructing_the_app(self) -> None:
        root = SimpleNamespace(withdraw=mock.Mock(), destroy=mock.Mock())
        with mock.patch.object(
            desktop_module, "set_dpi_awareness"
        ), mock.patch.object(
            desktop_module,
            "acquire_single_instance",
            side_effect=OSError("access denied"),
        ), mock.patch.object(
            desktop_module.tk, "Tk", return_value=root
        ), mock.patch.object(
            desktop_module.messagebox, "showerror"
        ) as showerror, mock.patch.object(
            desktop_module, "DesktopTranslatorApp"
        ) as app_class:
            exit_code = desktop_module.main()

        self.assertEqual(exit_code, 1)
        root.withdraw.assert_called_once_with()
        root.destroy.assert_called_once_with()
        showerror.assert_called_once()
        app_class.assert_not_called()

    def test_tray_failure_keeps_the_panel_discoverable(self) -> None:
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.quitting = False
        app.tray_failure_reported = False
        app.status_text = FakeVariable("")
        app.root = SimpleNamespace(deiconify=mock.Mock(), lift=mock.Mock())
        app._position_panel = mock.Mock()

        app._handle_tray_failure("backend failed")
        app._handle_tray_failure("duplicate report")

        self.assertTrue(app.tray_failure_reported)
        self.assertIn("大窗口", app.status_text.get())
        app.root.deiconify.assert_called_once_with()
        app.root.lift.assert_called_once_with()
        app._position_panel.assert_called_once_with()

    def test_new_clipboard_text_is_size_checked_before_materialising(self) -> None:
        with mock.patch(
            "desktop_app.win32clipboard.OpenClipboard"
        ), mock.patch(
            "desktop_app.win32clipboard.CloseClipboard"
        ), mock.patch(
            "desktop_app.win32clipboard.EnumClipboardFormats",
            side_effect=[13, 0],
        ), mock.patch(
            "desktop_app.win32clipboard.IsClipboardFormatAvailable",
            return_value=True,
        ), mock.patch(
            "desktop_app.win32clipboard.GetClipboardDataHandle",
            return_value=123,
        ), mock.patch(
            "desktop_app._GLOBAL_SIZE",
            return_value=(3000 * 4 + 65),
        ), mock.patch(
            "desktop_app.win32clipboard.GetClipboardData",
            side_effect=AssertionError("oversized text must not be materialised"),
        ):
            from desktop_app import read_clipboard_text

            text, formats, known = read_clipboard_text()

        self.assertIsNone(text)
        self.assertEqual(formats, [13])
        self.assertTrue(known)

    def test_clipboard_preflight_allows_3000_non_bmp_characters(self) -> None:
        value = "😀" * 3000
        with mock.patch(
            "desktop_app.win32clipboard.OpenClipboard"
        ), mock.patch(
            "desktop_app.win32clipboard.CloseClipboard"
        ), mock.patch(
            "desktop_app.win32clipboard.EnumClipboardFormats",
            side_effect=[13, 0],
        ), mock.patch(
            "desktop_app.win32clipboard.IsClipboardFormatAvailable",
            return_value=True,
        ), mock.patch(
            "desktop_app.win32clipboard.GetClipboardDataHandle",
            return_value=123,
        ), mock.patch(
            "desktop_app._GLOBAL_SIZE",
            return_value=len(value.encode("utf-16-le")) + 2,
        ), mock.patch(
            "desktop_app.win32clipboard.GetClipboardData",
            return_value=value,
        ):
            from desktop_app import read_clipboard_text

            text, _formats, known = read_clipboard_text()

        self.assertEqual(text, value)
        self.assertTrue(known)

    def test_mini_height_grows_past_the_old_238_pixel_cutoff(self) -> None:
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.mini = SimpleNamespace(
            update_idletasks=mock.Mock(),
            winfo_reqheight=lambda: 310,
            geometry=mock.Mock(),
        )
        app.root = SimpleNamespace(
            winfo_screenwidth=lambda: 1920,
            winfo_screenheight=lambda: 1080,
        )

        with mock.patch(
            "desktop_app.monitor_work_area_at",
            return_value=WorkArea(0, 0, 1920, 1080),
        ):
            _x, _y, _width, height = app.position_mini(100, 100)

        self.assertEqual(height, 313)
        app.mini.geometry.assert_called_once_with("440x313+114+118")

    def test_stop_invalidates_pending_capture_and_replaces_queue_with_sentinel(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.stopped_event = threading.Event()
        watcher.interaction_id = 7
        watcher.events = queue.SimpleQueue()
        watcher.state_lock = threading.Lock()
        watcher.events.put_nowait(CaptureRequest(1, 2, 7))
        watcher.listener = FakeListener()
        watcher.keyboard_listener = FakeListener()
        watcher.worker = FakeWorker()

        watcher.stop()

        self.assertTrue(watcher.stop_event.is_set())
        self.assertEqual(watcher.interaction_id, 8)
        self.assertEqual(watcher.listener.stop_calls, 1)
        self.assertEqual(watcher.keyboard_listener.stop_calls, 1)
        self.assertIsInstance(watcher.events.get_nowait(), CaptureRequest)
        self.assertIsNone(watcher.events.get_nowait())
        self.assertEqual(watcher.worker.join_timeouts, [])

    def test_mouse_hook_only_updates_memory_and_enqueues(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.state_lock = threading.Lock()
        watcher.events = queue.SimpleQueue()
        watcher.interaction_id = 0
        watcher.input_generation = 0
        watcher.press = None
        watcher.last_release = None
        outside: list[tuple[int, int]] = []
        watcher.outside_click_callback = lambda x, y: outside.append((x, y))

        with mock.patch("desktop_app.foreground_app", side_effect=AssertionError("hook queried foreground app")):
            watcher._on_click(10, 10, mouse.Button.left, True)
            watcher._on_click(30, 10, mouse.Button.left, False)

        self.assertEqual(outside, [(10, 10)])
        event = watcher.events.get_nowait()
        self.assertEqual((event.x, event.y, event.origin), (30, 10, "mouse"))

    def test_windows_mouse_thresholds_follow_accessibility_settings(self) -> None:
        user32 = SimpleNamespace(
            GetDoubleClickTime=mock.Mock(return_value=720),
            GetSystemMetrics=mock.Mock(
                side_effect=lambda metric: {36: 20, 37: 12, 68: 14, 69: 8}[metric]
            ),
        )

        with mock.patch(
            "desktop_app.ctypes.windll", SimpleNamespace(user32=user32)
        ):
            thresholds = windows_mouse_gesture_thresholds()

        self.assertEqual(thresholds, (0.72, 10, 6, 7, 4))

    def test_drag_release_keeps_test3_double_click_seed(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.state_lock = threading.Lock()
        watcher.events = queue.SimpleQueue()
        watcher.interaction_id = 0
        watcher.input_generation = 0
        watcher.keys_down = set()
        watcher.copy_intent_generation = 0
        watcher.context_menu_armed_until = 0.0
        watcher.press = None
        watcher.last_release = None
        watcher.drag_threshold_x = 4
        watcher.drag_threshold_y = 4
        watcher.double_click_interval_seconds = 0.8
        watcher.double_click_radius_x = 10
        watcher.double_click_radius_y = 10
        watcher.outside_click_callback = lambda _x, _y: None

        with mock.patch(
            "desktop_app.time.monotonic",
            side_effect=[1.0, 1.1, 1.2, 1.3],
        ):
            watcher._on_click(0, 0, mouse.Button.left, True)
            watcher._on_click(20, 0, mouse.Button.left, False)
            watcher._on_click(20, 0, mouse.Button.left, True)
            watcher._on_click(20, 0, mouse.Button.left, False)

        self.assertEqual(watcher.events.get_nowait().origin, "mouse")
        self.assertEqual(watcher.events.get_nowait().origin, "mouse")
        with self.assertRaises(queue.Empty):
            watcher.events.get_nowait()

    def test_capture_queue_coalesces_a_burst_to_the_latest_request(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.events = queue.SimpleQueue()

        for index in range(5000):
            watcher._queue_capture(CaptureRequest(index, index, index))

        first = watcher.events.get_nowait()
        latest = watcher._latest_queued_request(first)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual((latest.x, latest.interaction_id), (4999, 4999))

    @unittest.skip("Superseded by the requested test3 capture-context rollback")
    def test_new_capture_context_invalidates_pending_auto_speech(self) -> None:
        app = self.make_selection_app()
        app.selection_token = 4
        app.pending_auto_speak_token = 3
        app.pending_auto_speak_accent = "us"
        app.active_source = "old"

        app.on_capture_started((8, 5), 900, 700, "mouse")

        # Hook threads only enqueue work; token mutation is serialized with
        # show_result on the UI queue.
        self.assertEqual(app.selection_token, 4)
        callback, args = app.ui_tasks.get_nowait()
        callback(*args)

        self.assertEqual(app.latest_capture_identity, (8, 5))
        self.assertEqual(app.selection_token, 5)
        self.assertIsNone(app.pending_auto_speak_token)
        self.assertIsNone(app.pending_auto_speak_accent)
        self.assertEqual(app.active_source, "")

        app._handle_capture_started((8, 5), 900, 700, "mouse")
        self.assertEqual(app.selection_token, 5)

    def test_click_inside_app_does_not_invalidate_inflight_translation(self) -> None:
        app = self.make_selection_app(auto_translate=True)
        app.selection_token = 7
        app.active_source = "integration"
        app.root.winfo_containing.return_value = object()

        app._handle_capture_started((9, 2), 120, 80, "mouse")

        self.assertEqual(app.selection_token, 7)
        self.assertEqual(app.active_source, "integration")
        app.speech.cancel.assert_not_called()

    def test_click_on_native_app_titlebar_is_also_internal(self) -> None:
        app = self.make_selection_app(auto_translate=True)
        app.selection_token = 7
        app.active_source = "integration"
        app.root = SimpleNamespace(
            winfo_containing=mock.Mock(return_value=None),
            winfo_exists=mock.Mock(return_value=True),
            winfo_viewable=mock.Mock(return_value=True),
            winfo_id=mock.Mock(return_value=10),
        )

        with mock.patch("desktop_app.win32gui.GetAncestor", return_value=10), mock.patch(
            "desktop_app.win32gui.GetWindowRect", return_value=(100, 50, 700, 750)
        ):
            app._handle_capture_started((9, 2), 140, 60, "mouse")

        self.assertEqual(app.selection_token, 7)
        self.assertEqual(app.active_source, "integration")
        app.speech.cancel.assert_not_called()

    def test_typing_in_app_window_does_not_invalidate_translation(self) -> None:
        app = self.make_selection_app(auto_translate=True)
        app.selection_token = 7
        app.active_source = "integration"
        app.root = SimpleNamespace(
            winfo_exists=mock.Mock(return_value=True),
            winfo_id=mock.Mock(return_value=10),
        )

        with mock.patch("desktop_app.win32gui.GetForegroundWindow", return_value=10), mock.patch(
            "desktop_app.win32gui.GetAncestor", return_value=10
        ):
            app._handle_capture_started((8, 3), kind="keyboard")

        self.assertEqual(app.selection_token, 7)
        self.assertEqual(app.active_source, "integration")
        app.speech.cancel.assert_not_called()

    def test_clicking_us_inside_loading_mini_keeps_chinese_translation_and_speaks(self) -> None:
        app = self.make_selection_app(auto_translate=True)
        source_app = AppInfo("weixin.exe", "微信")
        app._handle_selection("你好", source_app, 100, 100)
        token = app.selection_token

        # The global hook sees the same click before Tk invokes the US button.
        app.root.winfo_containing.return_value = object()
        app._handle_capture_started((9, 2), 130, 130, "mouse")
        app.speak("us")
        app.show_result(
            engine.TranslationResult(
                source="你好",
                translated="hello",
                source_language="zh",
                target_language="en",
                engine="test",
            ),
            token,
        )

        self.assertEqual(app.selection_token, token)
        app.speech.speak.assert_called_once_with("hello", "us")

    def test_retry_request_carries_automatic_us_pronunciation(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.state_lock = threading.Lock()
        watcher.events = queue.SimpleQueue()
        watcher.interaction_id = 4
        watcher.input_generation = 0

        watcher.request_manual_capture(auto_speak_accent="us")

        event = watcher.events.get_nowait()
        self.assertEqual((event.x, event.y, event.origin), (-1, -1, "hotkey"))
        self.assertEqual(event.interaction_id, 5)
        self.assertEqual(event.auto_speak_accent, "us")

    def test_native_retry_waits_until_every_hotkey_key_is_released(self) -> None:
        watcher = self.make_double_alt_watcher()
        watcher.status_callback = mock.Mock()
        watcher._native_hotkey_keys_released = mock.Mock(return_value=True)
        watcher.keys_down = {"ctrl", "r"}

        watcher.request_native_hotkey_capture(
            primary_virtual_key=ord("R"),
            auto_speak_accent="us",
        )

        with self.assertRaises(queue.Empty):
            watcher.events.get_nowait()
        with watcher.state_lock:
            watcher.keys_down.clear()
        event = watcher.events.get(timeout=1.0)

        self.assertEqual((event.origin, event.auto_speak_accent), ("hotkey", "us"))
        self.assertEqual((event.x, event.y), (-1, -1))
        watcher.status_callback.assert_not_called()

    def test_native_retry_fails_closed_when_keys_remain_held(self) -> None:
        watcher = self.make_double_alt_watcher()
        timed_out = threading.Event()
        watcher.status_callback = lambda _message: timed_out.set()
        watcher._native_hotkey_keys_released = mock.Mock(return_value=False)
        watcher.NATIVE_HOTKEY_RELEASE_TIMEOUT_SECONDS = 0.03
        watcher.NATIVE_HOTKEY_RELEASE_POLL_SECONDS = 0.005

        watcher.request_native_hotkey_capture(
            primary_virtual_key=ord("R"),
            auto_speak_accent="us",
        )

        self.assertTrue(timed_out.wait(1.0))
        with self.assertRaises(queue.Empty):
            watcher.events.get_nowait()

    def test_one_clean_alt_tap_never_triggers_capture(self) -> None:
        watcher = self.make_double_alt_watcher()
        with mock.patch("desktop_app.time.monotonic", side_effect=[1.0, 1.05]):
            watcher._on_key_press(keyboard.Key.alt_l)
            watcher._on_key_release(keyboard.Key.alt_l)

        with self.assertRaises(queue.Empty):
            watcher.events.get_nowait()

    def test_two_clean_left_alt_taps_trigger_one_us_capture(self) -> None:
        watcher = self.make_double_alt_watcher()
        with mock.patch(
            "desktop_app.time.monotonic",
            side_effect=[1.0, 1.05, 1.20, 1.25, 1.30, 1.34],
        ):
            watcher._on_key_press(keyboard.Key.alt_l)
            watcher._on_key_release(keyboard.Key.alt_l)
            watcher._on_key_press(keyboard.Key.alt_l)
            watcher._on_key_release(keyboard.Key.alt_l)
            watcher._on_key_press(keyboard.Key.alt_l)
            watcher._on_key_release(keyboard.Key.alt_l)

        event = watcher.events.get_nowait()
        self.assertEqual((event.origin, event.auto_speak_accent), ("hotkey", "us"))
        self.assertEqual((event.x, event.y), (-1, -1))
        with self.assertRaises(queue.Empty):
            watcher.events.get_nowait()

    def test_alt_taps_outside_release_window_do_not_trigger(self) -> None:
        watcher = self.make_double_alt_watcher()
        with mock.patch(
            "desktop_app.time.monotonic",
            side_effect=[1.0, 1.05, 1.42, 1.451],
        ):
            watcher._on_key_press(keyboard.Key.alt_l)
            watcher._on_key_release(keyboard.Key.alt_l)
            watcher._on_key_press(keyboard.Key.alt_l)
            watcher._on_key_release(keyboard.Key.alt_l)

        with self.assertRaises(queue.Empty):
            watcher.events.get_nowait()

    def test_held_or_repeated_alt_is_not_a_clean_tap(self) -> None:
        held = self.make_double_alt_watcher()
        with mock.patch("desktop_app.time.monotonic", side_effect=[1.0, 1.36]):
            held._on_key_press(keyboard.Key.alt_l)
            held._on_key_release(keyboard.Key.alt_l)
        with self.assertRaises(queue.Empty):
            held.events.get_nowait()

        repeated = self.make_double_alt_watcher()
        with mock.patch("desktop_app.time.monotonic", side_effect=[2.0, 2.05, 2.10]):
            repeated._on_key_press(keyboard.Key.alt_l)
            repeated._on_key_press(keyboard.Key.alt_l)
            repeated._on_key_release(keyboard.Key.alt_l)
        with self.assertRaises(queue.Empty):
            repeated.events.get_nowait()

    def test_alt_tab_and_alt_c_never_become_double_alt(self) -> None:
        for primary in ("tab", "c"):
            with self.subTest(primary=primary):
                watcher = self.make_double_alt_watcher()
                key = keyboard.Key.tab if primary == "tab" else keyboard.KeyCode.from_char("c")
                with mock.patch(
                    "desktop_app.time.monotonic",
                    side_effect=[1.0, 1.03, 1.06, 1.08, 1.20, 1.24],
                ):
                    watcher._on_key_press(keyboard.Key.alt_l)
                    watcher._on_key_press(key)
                    watcher._on_key_release(key)
                    watcher._on_key_release(keyboard.Key.alt_l)
                    watcher._on_key_press(keyboard.Key.alt_l)
                    watcher._on_key_release(keyboard.Key.alt_l)
                with self.assertRaises(queue.Empty):
                    watcher.events.get_nowait()

    def test_ctrl_alt_and_right_alt_or_altgr_never_trigger(self) -> None:
        ctrl_alt = self.make_double_alt_watcher()
        with mock.patch(
            "desktop_app.time.monotonic",
            side_effect=[1.0, 1.02, 1.05, 1.08, 1.20, 1.24],
        ):
            ctrl_alt._on_key_press(keyboard.Key.ctrl_l)
            ctrl_alt._on_key_press(keyboard.Key.alt_l)
            ctrl_alt._on_key_release(keyboard.Key.alt_l)
            ctrl_alt._on_key_release(keyboard.Key.ctrl_l)
            ctrl_alt._on_key_press(keyboard.Key.alt_l)
            ctrl_alt._on_key_release(keyboard.Key.alt_l)
        with self.assertRaises(queue.Empty):
            ctrl_alt.events.get_nowait()

        right_alt = self.make_double_alt_watcher()
        with mock.patch(
            "desktop_app.time.monotonic",
            side_effect=[2.0, 2.04, 2.20, 2.24],
        ):
            right_alt._on_key_press(keyboard.Key.alt_r)
            right_alt._on_key_release(keyboard.Key.alt_r)
            right_alt._on_key_press(keyboard.Key.alt_gr)
            right_alt._on_key_release(keyboard.Key.alt_gr)
        with self.assertRaises(queue.Empty):
            right_alt.events.get_nowait()

    def test_key_or_mouse_between_alt_taps_clears_the_candidate(self) -> None:
        for interruption in ("key", "mouse"):
            with self.subTest(interruption=interruption):
                watcher = self.make_double_alt_watcher()
                if interruption == "key":
                    with mock.patch(
                        "desktop_app.time.monotonic",
                        side_effect=[1.0, 1.04, 1.10, 1.12, 1.20, 1.24],
                    ):
                        watcher._on_key_press(keyboard.Key.alt_l)
                        watcher._on_key_release(keyboard.Key.alt_l)
                        watcher._on_key_press("q")
                        watcher._on_key_release("q")
                        watcher._on_key_press(keyboard.Key.alt_l)
                        watcher._on_key_release(keyboard.Key.alt_l)
                else:
                    with mock.patch(
                        "desktop_app.time.monotonic",
                        side_effect=[2.0, 2.04, 2.10, 2.20, 2.24],
                    ):
                        watcher._on_key_press(keyboard.Key.alt_l)
                        watcher._on_key_release(keyboard.Key.alt_l)
                        watcher._on_click(10, 10, mouse.Button.middle, True)
                        watcher._on_key_press(keyboard.Key.alt_l)
                        watcher._on_key_release(keyboard.Key.alt_l)
                with self.assertRaises(queue.Empty):
                    watcher.events.get_nowait()

    def test_scroll_between_alt_taps_clears_the_candidate(self) -> None:
        watcher = self.make_double_alt_watcher()
        with mock.patch("desktop_app.time.monotonic", side_effect=[1.0, 1.04]):
            watcher._on_key_press(keyboard.Key.alt_l)
            watcher._on_key_release(keyboard.Key.alt_l)

        watcher._on_scroll(10, 20, 0, -1)

        with mock.patch("desktop_app.time.monotonic", side_effect=[1.20, 1.24]):
            watcher._on_key_press(keyboard.Key.alt_l)
            watcher._on_key_release(keyboard.Key.alt_l)
        with self.assertRaises(queue.Empty):
            watcher.events.get_nowait()

    def test_disabling_or_rebinding_invalidates_a_partial_double_alt(self) -> None:
        watcher = self.make_double_alt_watcher()
        with mock.patch("desktop_app.time.monotonic", side_effect=[1.0, 1.04]):
            watcher._on_key_press(keyboard.Key.alt_l)
            watcher._on_key_release(keyboard.Key.alt_l)
        old_epoch = watcher.double_alt_epoch

        watcher.set_double_alt_enabled(False)
        watcher._request_double_alt_capture(old_epoch)
        watcher.set_double_alt_enabled(True)
        with mock.patch("desktop_app.time.monotonic", side_effect=[1.20, 1.24]):
            watcher._on_key_press(keyboard.Key.alt_l)
            watcher._on_key_release(keyboard.Key.alt_l)

        with self.assertRaises(queue.Empty):
            watcher.events.get_nowait()

    def test_retry_plays_english_once_without_waiting_for_translation(self) -> None:
        app = self.make_selection_app(auto_translate=False)

        app._handle_selection(
            "integration",
            AppInfo("powerpnt.exe", "Microsoft PowerPoint"),
            10,
            20,
            "us",
        )

        app.speech.speak.assert_called_once_with(
            "integration", "us", mode="system"
        )
        self.assertIsNone(app.pending_auto_speak_token)
        app.enqueue.assert_not_called()

    def test_retry_keeps_the_existing_mini_anchor_in_the_same_window(self) -> None:
        app = self.make_selection_app(auto_translate=False)
        app.display_mode = "mini"
        app.current_app = AppInfo(
            "winword.exe", "Microsoft Word", hwnd=321
        )
        app.active_source = "quickly"
        app.selection_point = (118, 98)
        app.show_mini_loading = mock.Mock()
        app.show_mini_no_activate = mock.Mock()

        app._handle_selection(
            "quickly",
            AppInfo("winword.exe", "Microsoft Word", hwnd=321),
            900,
            700,
            "us",
        )

        self.assertEqual(app.selection_point, (118, 98))
        app.show_mini_loading.assert_called_once_with("quickly", 118, 98)

        app.show_result(
            engine.TranslationResult(
                source="quickly",
                translated="很快地",
                source_language="en",
                target_language="zh",
                engine="test",
            ),
            app.selection_token,
        )

        app.show_mini_no_activate.assert_called_once_with(118, 98)

    def test_first_retry_uses_pointer_anchor_when_no_previous_window_exists(self) -> None:
        app = self.make_selection_app(auto_translate=False)
        app.display_mode = "mini"
        app.show_mini_loading = mock.Mock()

        app._handle_selection(
            "quickly",
            AppInfo("winword.exe", "Microsoft Word", hwnd=321),
            900,
            700,
            "us",
        )

        self.assertEqual(app.selection_point, (900, 700))
        app.show_mini_loading.assert_called_once_with("quickly", 900, 700)

    def test_retry_in_a_different_window_does_not_reuse_a_stale_anchor(self) -> None:
        app = self.make_selection_app(auto_translate=False)
        app.display_mode = "mini"
        app.current_app = AppInfo(
            "winword.exe", "Microsoft Word", hwnd=111
        )
        app.active_source = "quickly"
        app.selection_point = (118, 98)
        app.show_mini_loading = mock.Mock()

        app._handle_selection(
            "another",
            AppInfo("winword.exe", "Microsoft Word", hwnd=222),
            900,
            700,
            "us",
        )

        self.assertEqual(app.selection_point, (900, 700))
        app.show_mini_loading.assert_called_once_with("another", 900, 700)

    def test_normal_mouse_selection_in_the_same_window_uses_the_new_anchor(self) -> None:
        app = self.make_selection_app(auto_translate=False)
        app.display_mode = "mini"
        app.current_app = AppInfo(
            "winword.exe", "Microsoft Word", hwnd=321
        )
        app.active_source = "quickly"
        app.selection_point = (118, 98)
        app.show_mini_loading = mock.Mock()

        app._handle_selection(
            "another",
            AppInfo("winword.exe", "Microsoft Word", hwnd=321),
            900,
            700,
        )

        self.assertEqual(app.selection_point, (900, 700))
        app.show_mini_loading.assert_called_once_with("another", 900, 700)

    def test_double_clicking_a_mini_pronunciation_control_cannot_reanchor_it(self) -> None:
        """A non-activating mini window leaves the source app foreground."""

        app = self.make_selection_app(auto_translate=False)
        app.display_mode = "mini"
        app.current_app = AppInfo(
            "winword.exe", "Microsoft Word", hwnd=321
        )
        app.active_source = "quickly"
        app.selection_point = (118, 98)
        app.show_mini_loading = mock.Mock()
        # The global listener can return Word's old selection after the
        # button's second click.  Its coordinates are nevertheless inside the
        # mini window, so it is not a selection request.
        app._capture_context_is_internal = mock.Mock(return_value=True)

        app._handle_selection(
            "quickly",
            AppInfo("winword.exe", "Microsoft Word", hwnd=321),
            410,
            240,
            capture_identity=(9, 0),
        )

        self.assertEqual(app.selection_point, (118, 98))
        self.assertEqual(app.active_source, "quickly")
        app.show_mini_loading.assert_not_called()
        app._capture_context_is_internal.assert_called_once_with(410, 240, "mouse")

    def test_retry_translates_chinese_even_when_auto_translate_is_off(self) -> None:
        app = self.make_selection_app(auto_translate=False)

        app._handle_selection(
            "你好",
            AppInfo("weixin.exe", "微信"),
            10,
            20,
            "us",
        )

        app.speech.speak.assert_not_called()
        app.enqueue.assert_called_once_with("你好", 1)
        self.assertEqual(app.pending_auto_speak_token, 1)
        self.assertEqual(app.pending_auto_speak_mode, "system")

    def test_natural_preference_retry_uses_ai_for_english_and_translated_chinese(self) -> None:
        english_app = self.make_selection_app(
            auto_translate=False,
            speech_preference="natural",
        )
        english_app._handle_selection(
            "integration",
            AppInfo("powerpnt.exe", "Microsoft PowerPoint"),
            10,
            20,
            "us",
        )
        english_app.speech.speak.assert_called_once_with("integration", "us")

        chinese_app = self.make_selection_app(
            auto_translate=False,
            speech_preference="natural",
        )
        chinese_app._handle_selection(
            "你好",
            AppInfo("powerpnt.exe", "Microsoft PowerPoint"),
            10,
            20,
            "us",
        )
        self.assertEqual(chinese_app.pending_auto_speak_mode, "natural")
        chinese_app._play_pending_auto_speak(
            chinese_app.selection_token,
            "hello",
        )
        chinese_app.speech.speak.assert_called_once_with("hello", "us")

    def test_stale_translation_token_cannot_trigger_pronunciation(self) -> None:
        app = self.make_selection_app()
        app.selection_token = 2
        app.pending_auto_speak_token = 1
        app.pending_auto_speak_accent = "us"

        app._play_pending_auto_speak(1, "stale result")

        app.speech.speak.assert_not_called()

    def test_dismissed_mini_is_not_reopened_by_late_result(self) -> None:
        app = self.make_selection_app(auto_translate=True)
        app.display_mode = "mini"
        app.selection_token = 1
        app.mini_dismissed_token = 1
        app.active_source = "integration"
        app.current_app = AppInfo("powerpnt.exe", "Microsoft PowerPoint")
        app.result_text = mock.Mock()
        app.meta_text = FakeVariable("")
        app.engine_text = FakeVariable("")
        app.mini_source = FakeVariable("")
        app.mini_phonetic = FakeVariable("")
        app.mini_translation = FakeVariable("")
        app.show_mini_no_activate = mock.Mock()
        app._set_text = mock.Mock()
        app._play_pending_auto_speak = mock.Mock()
        result = engine.TranslationResult(
            source="integration",
            translated="集成",
            source_language="en",
            target_language="zh",
            engine="test",
        )

        app.show_result(result, 1)

        app.show_mini_no_activate.assert_not_called()
        self.assertEqual(app.mini_translation.get(), "集成")

    def test_result_and_new_context_are_serialized_without_stale_reappearance(self) -> None:
        result = engine.TranslationResult(
            source="selection A",
            translated="结果 A",
            source_language="en",
            target_language="zh",
            engine="test",
        )

        # Result already queued first: the following real input clears it in
        # the same UI drain, so it cannot remain visible as B starts.
        first = self.make_selection_app(auto_translate=True)
        first.selection_token = 1
        first.active_source = "selection A"
        first.show_result(result, 1)
        first._handle_capture_started((2, 0), 900, 700, "mouse")
        self.assertIsNone(first.current_result)
        self.assertEqual(first.active_source, "")
        self.assertEqual(
            first.result_text.insert.call_args_list[-1].args[-1],
            "正在读取新的选区…",
        )

        # Input invalidation queued first: the late A result fails both token
        # and source identity checks and cannot publish at all.
        second = self.make_selection_app(auto_translate=True)
        second.selection_token = 1
        second.active_source = "selection A"
        second._handle_capture_started((2, 0), 900, 700, "mouse")
        second.result_text.reset_mock()
        second.show_result(result, 1)
        self.assertIsNone(second.current_result)
        second.result_text.insert.assert_not_called()

    @unittest.skip("Superseded by the requested test3 result-display rollback")
    def test_new_selection_replaces_old_result_with_loading_state_atomically(self) -> None:
        app = self.make_selection_app(auto_translate=True)
        app.current_result = SimpleNamespace(source="A", translated="旧译文")
        app.meta_text.set("old engine")
        app.result_text.reset_mock()

        app._handle_selection(
            "selection B",
            AppInfo("powerpnt.exe", "Microsoft PowerPoint"),
            20,
            30,
        )

        self.assertIsNone(app.current_result)
        self.assertEqual(app.active_source, "selection B")
        self.assertEqual(app.meta_text.get(), "")
        self.assertEqual(
            app.result_text.insert.call_args_list[-1].args[-1],
            "正在本地翻译…",
        )

    def test_pause_clears_loading_source_result_pair_and_pending_request(self) -> None:
        values = {"desktop_enabled": True}

        class Store:
            @staticmethod
            def get(key: str, default: object = None) -> object:
                return values.get(key, default)

            @staticmethod
            def set(key: str, value: object) -> bool:
                values[key] = value
                return True

            @staticmethod
            def is_app_enabled(_exe: str) -> bool:
                return True

        app = self.make_selection_app(auto_translate=True)
        app.store = Store()
        app.desktop_enabled = True
        app.pause_button = SimpleNamespace(configure=mock.Mock())
        app.status_text = FakeVariable("")
        app.update_tray_menu = mock.Mock()
        app.active_source = "selection A"
        app.current_result = SimpleNamespace(source="selection A")
        app.request_queue.put_nowait(desktop_module.TranslationRequest("selection A", 1))

        app.set_desktop_enabled(False)

        self.assertEqual(app.active_source, "")
        self.assertIsNone(app.current_result)
        self.assertEqual(app.mini_translation.get(), "桌面取词已暂停")
        with self.assertRaises(queue.Empty):
            app.request_queue.get_nowait()
        self.assertGreaterEqual(app.speech.cancel.call_count, 1)

    def test_disabling_current_app_clears_ghost_translation_state(self) -> None:
        app = self.make_selection_app(auto_translate=True)
        app.current_app = AppInfo("excel.exe", "Microsoft Excel")
        app.active_source = "selection A"
        app.current_result = SimpleNamespace(source="selection A")
        app.panel_app_enabled = FakeVariable(False)
        app.mini_app_enabled = FakeVariable(False)
        app.panel_app_toggle = SimpleNamespace(draw=mock.Mock())
        app.mini_app_toggle = SimpleNamespace(draw=mock.Mock())
        app.status_text = FakeVariable("")
        app.store = SimpleNamespace(
            set_app=mock.Mock(return_value=True),
            is_app_enabled=lambda _exe: True,
        )

        self.assertTrue(app.toggle_current_app())

        self.assertEqual(app.active_source, "")
        self.assertIsNone(app.current_result)
        self.assertIn("关闭划词翻译", app.mini_translation.get())
        self.assertGreaterEqual(app.speech.cancel.call_count, 1)

    def test_closing_or_outside_click_stops_long_sentence_speech(self) -> None:
        app = self.make_selection_app()
        app.mini.winfo_viewable.return_value = True

        app._dismiss_mini_if_outside(500, 500)

        app.speech.cancel.assert_called_once_with()
        app.mini.withdraw.assert_called_once_with()

        app.speech.cancel.reset_mock()
        app.hide_panel()
        app.speech.cancel.assert_called_once_with()
        app.root.withdraw.assert_called_once_with()

        # Moving the same content from mini to panel is not a dismissal.
        app.speech.cancel.reset_mock()
        app.hide_mini(stop_speech=False)
        app.speech.cancel.assert_not_called()

    def test_translation_worker_finishes_a_then_skips_b_for_latest_c(self) -> None:
        app = self.make_selection_app(auto_translate=True)
        app.request_queue = queue.SimpleQueue()
        app.selection_token = 1
        started_a = threading.Event()
        release_a = threading.Event()
        finished_c = threading.Event()
        calls: list[str] = []
        active = 0
        maximum_active = 0

        def translate(text: str) -> SimpleNamespace:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            calls.append(text)
            if text == "A":
                started_a.set()
                self.assertTrue(release_a.wait(1.0))
            if text == "C":
                finished_c.set()
            active -= 1
            return SimpleNamespace(source=text)

        app.translator = SimpleNamespace(load=mock.Mock(), translate=translate)
        app._post_ui = mock.Mock()
        app.request_queue.put(desktop_module.TranslationRequest("A", 1))
        worker = threading.Thread(target=app._translation_loop)
        worker.start()
        self.assertTrue(started_a.wait(1.0))
        with app.auto_speak_lock:
            app.selection_token = 3
        app.request_queue.put(desktop_module.TranslationRequest("B", 2))
        app.request_queue.put(desktop_module.TranslationRequest("C", 3))
        release_a.set()
        self.assertTrue(finished_c.wait(1.0))
        app.request_queue.put(None)
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(calls, ["A", "C"])
        self.assertEqual(maximum_active, 1)

    def test_chinese_speak_while_loading_plays_after_translation(self) -> None:
        app = self.make_selection_app(auto_translate=True)
        app.active_source = "你好"
        app.selection_token = 4
        app.status_text = FakeVariable("")

        app.speak("uk")

        app.speech.speak.assert_not_called()
        self.assertEqual(app.pending_auto_speak_token, 4)
        self.assertEqual(app.pending_auto_speak_accent, "uk")
        self.assertIn("完成后", app.status_text.get())

    def test_stale_queued_selection_is_rejected_after_new_input(self) -> None:
        app = self.make_selection_app()
        app.latest_capture_identity = (7, 4)

        app._handle_selection(
            "old selection",
            AppInfo("powerpnt.exe", "Microsoft PowerPoint"),
            10,
            20,
            "us",
            (6, 3),
        )

        self.assertEqual(app.active_source, "")
        app.speech.speak.assert_not_called()

    def test_hotkey_commands_only_enqueue_capture_or_toggle_ui(self) -> None:
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.store = SimpleNamespace(get=lambda _key, _default=True: True)
        app.watcher = SimpleNamespace(request_manual_capture=mock.Mock())
        app.toggle_mode_from_hotkey = mock.Mock()
        app.status_text = FakeVariable("")

        app._handle_hotkey_command(HotkeyCommand.RETRY_AND_SPEAK_US)
        app._handle_hotkey_command(HotkeyCommand.TOGGLE_WINDOW_MODE)

        app.watcher.request_manual_capture.assert_called_once_with(
            auto_speak_accent="us"
        )
        app.toggle_mode_from_hotkey.assert_called_once_with()

    def test_native_retry_bypasses_tk_queue_to_capture_current_generation(self) -> None:
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        settings = {
            "hotkeys_enabled": True,
            "desktop_enabled": True,
            "retry_hotkey": "Alt+Q",
            "toggle_mode_hotkey": "Alt+C",
        }
        app.store = SimpleNamespace(
            get=lambda key, default=True: settings.get(key, default)
        )
        app.watcher = SimpleNamespace(
            request_manual_capture=mock.Mock(),
            request_native_hotkey_capture=mock.Mock(),
        )
        app._post_ui = mock.Mock()

        app.on_hotkey_command(HotkeyCommand.RETRY_AND_SPEAK_US)

        app.watcher.request_native_hotkey_capture.assert_called_once_with(
            primary_virtual_key=ord("Q"),
            auto_speak_accent="us",
        )
        app.watcher.request_manual_capture.assert_not_called()
        app._post_ui.assert_not_called()

    def test_disabling_hotkeys_invalidates_an_already_queued_hotkey_request(self) -> None:
        watcher = self.make_double_alt_watcher()
        watcher.set_hotkey_requests_enabled(True)
        watcher.request_manual_capture(auto_speak_accent="us")
        event = watcher.events.get_nowait()

        watcher.set_hotkey_requests_enabled(False)
        watcher.store = SimpleNamespace(get=lambda _key, default=True: default)
        with mock.patch("desktop_app.foreground_app") as foreground:
            watcher._process_capture(event)

        foreground.assert_not_called()

    def test_real_keyboard_input_advances_generation_but_injected_input_is_filtered(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.state_lock = threading.Lock()
        watcher.interaction_id = 3
        watcher.input_generation = 7
        watcher.keys_down = set()
        watcher.copy_intent_generation = 0
        watcher.context_menu_armed_until = 0.0

        self.assertFalse(watcher._keyboard_event_filter(0, SimpleNamespace(flags=0x10)))
        self.assertFalse(watcher._keyboard_event_filter(0, SimpleNamespace(flags=0x02)))
        self.assertTrue(watcher._keyboard_event_filter(0, SimpleNamespace(flags=0)))
        watcher._on_key_press("c")
        watcher._on_key_press("c")

        self.assertEqual(watcher.input_generation, 8)
        watcher._on_key_release("c")
        watcher._on_key_press("c")
        self.assertEqual(watcher.input_generation, 9)

    def test_physical_ctrl_c_records_explicit_user_copy_intent(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.state_lock = threading.Lock()
        watcher.interaction_id = 3
        watcher.input_generation = 0
        watcher.keys_down = set()
        watcher.copy_intent_generation = 0
        watcher.context_menu_armed_until = 0.0

        watcher._on_key_press(keyboard.Key.ctrl_l)
        watcher._on_key_press(keyboard.KeyCode.from_char("c"))
        watcher._on_key_press(keyboard.KeyCode.from_char("c"))

        self.assertEqual(watcher.copy_intent_generation, 1)
        watcher._on_key_release(keyboard.KeyCode.from_char("c"))
        watcher._on_key_press(keyboard.KeyCode.from_char("c"))
        self.assertEqual(watcher.copy_intent_generation, 2)
        watcher._on_key_release(keyboard.KeyCode.from_char("c"))
        watcher._on_key_release(keyboard.Key.ctrl_l)
        watcher._on_key_press(keyboard.Key.ctrl_r)
        watcher._on_key_press(keyboard.KeyCode.from_char("c"))
        self.assertEqual(watcher.copy_intent_generation, 3)

    def test_atomic_plain_text_restore_preserves_a_newer_clipboard(self) -> None:
        with mock.patch("desktop_app.clipboard_sequence_number", return_value=12), mock.patch(
            "desktop_app.win32clipboard.OpenClipboard"
        ), mock.patch("desktop_app.win32clipboard.CloseClipboard") as close, mock.patch(
            "desktop_app.win32clipboard.EmptyClipboard"
        ) as empty:
            result = restore_clipboard_text_if_unchanged("old", 11)

        self.assertEqual(result, "preserved")
        empty.assert_not_called()
        close.assert_called_once_with()

    def test_atomic_plain_text_restore_replaces_only_the_expected_clipboard(self) -> None:
        with mock.patch("desktop_app.clipboard_sequence_number", return_value=11), mock.patch(
            "desktop_app.win32clipboard.OpenClipboard"
        ), mock.patch("desktop_app.win32clipboard.CloseClipboard") as close, mock.patch(
            "desktop_app.win32clipboard.EmptyClipboard"
        ) as empty, mock.patch("desktop_app.win32clipboard.SetClipboardText") as set_text:
            result = restore_clipboard_text_if_unchanged("old", 11)

        self.assertEqual(result, "restored")
        empty.assert_called_once_with()
        set_text.assert_called_once()
        close.assert_called_once_with()

    def test_plain_text_restore_does_not_retry_after_clipboard_was_emptied(self) -> None:
        with mock.patch("desktop_app.clipboard_sequence_number", return_value=11), mock.patch(
            "desktop_app.win32clipboard.OpenClipboard"
        ) as open_clipboard, mock.patch(
            "desktop_app.win32clipboard.CloseClipboard"
        ), mock.patch("desktop_app.win32clipboard.EmptyClipboard"), mock.patch(
            "desktop_app.win32clipboard.SetClipboardText",
            side_effect=RuntimeError("clipboard allocation failed"),
        ):
            result = restore_clipboard_text_if_unchanged("old", 11)

        self.assertEqual(result, "failed")
        open_clipboard.assert_called_once_with()

    def test_stale_input_generation_is_rejected_before_foreground_lookup(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.store = SimpleNamespace(get=lambda _key, _default=True: True)
        watcher.state_lock = threading.Lock()
        watcher.interaction_id = 3
        watcher.input_generation = 9

        with mock.patch(
            "desktop_app.foreground_app",
            side_effect=AssertionError("stale request must not inspect a new foreground app"),
        ):
            watcher._process_capture(CaptureRequest(10, 20, 3, "mouse", 8))

    def test_programmatic_focus_change_discards_a_direct_capture_before_publish(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.state_lock = threading.Lock()
        watcher.interaction_id = 3
        watcher.input_generation = 8
        watcher.store = SimpleNamespace(
            get=lambda _key, default=True: default,
            is_app_enabled=lambda _exe: True,
            capture_timing=lambda _exe: SimpleNamespace(
                settle_seconds=0.0,
                clipboard_timeout_seconds=0.1,
            ),
        )
        watcher.status_callback = mock.Mock()
        watcher.selection_callback = mock.Mock()
        watcher._record_diagnostic = mock.Mock()
        watcher._capture_selection = mock.Mock(
            return_value=SimpleNamespace(text="selected", method="uia", protected=False)
        )
        app = AppInfo("notepad.exe", "Notepad", hwnd=100)

        with mock.patch("desktop_app.foreground_app", return_value=app), mock.patch.object(
            watcher,
            "_focus_is_current",
            side_effect=[True, False],
        ):
            watcher._process_capture(CaptureRequest(10, 20, 3, "mouse", 8))

        watcher.selection_callback.assert_not_called()
        watcher._record_diagnostic.assert_called_with(
            app,
            "skipped",
            "uia",
            "focus_changed",
            mock.ANY,
        )

    def test_test3_deduplicates_the_same_text_inside_its_short_window(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.state_lock = threading.Lock()
        watcher.input_generation = 0
        watcher.last_emitted = ("", "", 0.0)
        watcher.store = SimpleNamespace(
            get=lambda _key, default=True: default,
            is_app_enabled=lambda _exe: True,
            capture_timing=lambda _exe: SimpleNamespace(
                settle_seconds=0.0,
                clipboard_timeout_seconds=0.1,
            ),
        )
        watcher.status_callback = mock.Mock()
        watcher.selection_callback = mock.Mock()
        watcher._record_diagnostic = mock.Mock()
        watcher._capture_selection = mock.Mock(
            return_value=SelectionProbeResult("same text", "uia")
        )
        app = AppInfo("notepad.exe", "Notepad", hwnd=100)

        with mock.patch("desktop_app.foreground_app", return_value=app), mock.patch.object(
            watcher,
            "_focus_is_current",
            return_value=True,
        ):
            for interaction_id in (1, 2):
                watcher.interaction_id = interaction_id
                watcher._process_capture(
                    CaptureRequest(10, 20, interaction_id, "mouse", 0)
                )

        self.assertEqual(watcher.selection_callback.call_count, 1)
        self.assertEqual(
            [call.args[-1] for call in watcher.selection_callback.call_args_list],
            [(1, 0)],
        )

    def test_success_diagnostic_is_recorded_only_after_final_publish_check(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.state_lock = threading.Lock()
        watcher.interaction_id = 1
        watcher.input_generation = 0
        watcher.last_emitted = ("", "", 0.0)
        watcher.store = SimpleNamespace(
            get=lambda _key, default=True: default,
            is_app_enabled=lambda _exe: True,
            capture_timing=lambda _exe: SimpleNamespace(
                settle_seconds=0.0,
                clipboard_timeout_seconds=0.1,
            ),
        )
        order: list[str] = []
        watcher.status_callback = mock.Mock()
        watcher.selection_callback = lambda *_args: order.append("publish")
        watcher._record_diagnostic = lambda *_args: order.append("diagnostic")
        watcher._capture_selection = mock.Mock(
            return_value=SelectionProbeResult("selected", "uia")
        )
        app = AppInfo("notepad.exe", "Notepad", hwnd=100)

        with mock.patch("desktop_app.foreground_app", return_value=app), mock.patch.object(
            watcher,
            "_focus_is_current",
            return_value=True,
        ):
            watcher._process_capture(CaptureRequest(10, 20, 1, "mouse", 0))

        self.assertEqual(order, ["diagnostic", "publish"])

    def test_any_mouse_press_invalidates_an_older_capture(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.state_lock = threading.Lock()
        watcher.interaction_id = 4
        watcher.input_generation = 0
        watcher.copy_intent_generation = 0
        watcher.context_menu_armed_until = 0.0
        watcher.press = (1, 1, 0.0)
        watcher.outside_click_callback = mock.Mock()

        watcher._on_click(20, 30, mouse.Button.right, True)

        self.assertEqual(watcher.interaction_id, 5)
        self.assertIsNone(watcher.press)
        self.assertEqual(watcher.copy_intent_generation, 0)
        watcher.outside_click_callback.assert_not_called()

    def test_context_menu_click_is_a_conservative_copy_intent_candidate(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.state_lock = threading.Lock()
        watcher.interaction_id = 0
        watcher.input_generation = 0
        watcher.copy_intent_generation = 0
        watcher.context_menu_armed_until = 0.0
        watcher.press = None
        watcher.last_release = None
        watcher.outside_click_callback = mock.Mock()

        with mock.patch("desktop_app.time.monotonic", return_value=10.0):
            watcher._on_click(10, 10, mouse.Button.right, True)
            self.assertEqual(watcher.copy_intent_generation, 0)
            watcher._on_click(15, 15, mouse.Button.left, True)

        self.assertEqual(watcher.copy_intent_generation, 1)

    def test_right_click_makes_a_late_same_app_copy_ambiguous_and_preserved(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.state_lock = threading.Lock()
        watcher.interaction_id = 5
        watcher.input_generation = 2
        watcher.copy_intent_generation = 0
        watcher.context_menu_armed_until = 0.0
        watcher.press = None
        watcher.last_release = None
        watcher.outside_click_callback = mock.Mock()
        app = AppInfo("weixin.exe", "微信", hwnd=100, process_id=123)

        def inspect_capture(**kwargs: object) -> SimpleNamespace:
            watcher._on_click(20, 30, mouse.Button.right, True)
            owner_check = kwargs["clipboard_change_is_ours"]
            intent_check = kwargs["external_copy_intent_detected"]
            assert callable(owner_check)
            assert callable(intent_check)
            self.assertTrue(owner_check())
            self.assertTrue(intent_check())
            return SimpleNamespace(reason="focus_changed")

        state = PortableClipboardState("old", [13], True, 10, None)
        with mock.patch("desktop_app.capture_portable_clipboard_state", return_value=state), mock.patch(
            "desktop_app.keyboard.Controller"
        ), mock.patch(
            "desktop_app.clipboard_owner_matches_app", return_value=True
        ), mock.patch.object(
            DesktopSelectionWatcher, "_focus_is_current", return_value=True
        ), mock.patch(
            "desktop_app.capture_selected_text_with_clipboard", side_effect=inspect_capture
        ):
            watcher._capture_with_clipboard(app, 5, 2, 1.2)

    def test_last_moment_input_change_never_sends_compatibility_ctrl_c(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.state_lock = threading.Lock()
        watcher.interaction_id = 5
        watcher.input_generation = 2
        watcher.copy_intent_generation = 0
        watcher.stop_event = threading.Event()
        app = AppInfo("weixin.exe", "微信", hwnd=100, process_id=123)
        controller = mock.Mock()

        def inspect_capture(**kwargs: object) -> object:
            watcher.input_generation += 1
            send_copy = kwargs["send_copy"]
            assert callable(send_copy)
            with self.assertRaisesRegex(RuntimeError, "selection changed"):
                send_copy()
            return selection_capture.ClipboardCaptureResult(reason="copy_failed")

        state = PortableClipboardState("old", [13], True, 10, None)
        with mock.patch(
            "desktop_app.capture_portable_clipboard_state", return_value=state
        ), mock.patch(
            "desktop_app.keyboard.Controller", return_value=controller
        ), mock.patch.object(
            DesktopSelectionWatcher, "_focus_is_current", return_value=True
        ), mock.patch(
            "desktop_app.capture_selected_text_with_clipboard",
            side_effect=inspect_capture,
        ):
            watcher._capture_with_clipboard(app, 5, 2, 1.2)

        controller.pressed.assert_not_called()

    @unittest.skip("Superseded by the requested test3 clipboard hot-path rollback")
    def test_mouse_clipboard_gate_runs_before_snapshot_when_modifier_is_held(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.state_lock = threading.Lock()
        watcher.keys_down = {"shift"}
        watcher.copy_intent_generation = 0
        watcher.stop_event = threading.Event()
        app = AppInfo("weixin.exe", "WeChat", hwnd=100, process_id=123)

        with mock.patch(
            "desktop_app.capture_portable_clipboard_state"
        ) as capture_state, mock.patch.object(
            DesktopSelectionWatcher,
            "_physical_modifiers_released",
            return_value=True,
        ):
            result = watcher._capture_with_clipboard(
                app,
                1,
                0,
                1.2,
                require_no_physical_modifiers=True,
            )

        self.assertEqual(result.reason, "modifiers_held")
        capture_state.assert_not_called()

    @unittest.skip("Superseded by the requested test3 clipboard hot-path rollback")
    def test_hotkey_clipboard_gate_also_blocks_held_physical_modifier(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.state_lock = threading.Lock()
        watcher.keys_down = {"shift"}
        watcher.copy_intent_generation = 0
        watcher.stop_event = threading.Event()
        app = AppInfo("weixin.exe", "WeChat", hwnd=100, process_id=123)

        with mock.patch(
            "desktop_app.capture_portable_clipboard_state"
        ) as capture_state, mock.patch.object(
            DesktopSelectionWatcher,
            "_physical_modifiers_released",
            return_value=True,
        ):
            result = watcher._capture_with_clipboard(
                app,
                1,
                0,
                1.2,
                require_no_physical_modifiers=False,
            )

        self.assertEqual(result.reason, "modifiers_held")
        capture_state.assert_not_called()

    @unittest.skip("Superseded by the requested test3 clipboard hot-path rollback")
    def test_every_compatibility_copy_rechecks_modifiers_at_injection_moment(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.state_lock = threading.Lock()
        watcher.keys_down = set()
        watcher.interaction_id = 1
        watcher.input_generation = 0
        watcher.copy_intent_generation = 0
        watcher.stop_event = threading.Event()
        app = AppInfo("weixin.exe", "WeChat", hwnd=100, process_id=123)
        controller = mock.Mock()

        def inspect_capture(**kwargs: object) -> ClipboardCaptureResult:
            send_copy = kwargs["send_copy"]
            assert callable(send_copy)
            with self.assertRaisesRegex(RuntimeError, "physical modifier"):
                send_copy()
            return selection_capture.ClipboardCaptureResult(reason="copy_failed")

        state = PortableClipboardState("old", [13], True, 10, None)
        with mock.patch(
            "desktop_app.capture_portable_clipboard_state", return_value=state
        ), mock.patch(
            "desktop_app.keyboard.Controller", return_value=controller
        ), mock.patch.object(
            DesktopSelectionWatcher, "_focus_is_current", return_value=True
        ), mock.patch.object(
            DesktopSelectionWatcher,
            "_physical_modifiers_released",
            side_effect=[True, False],
        ), mock.patch(
            "desktop_app.capture_selected_text_with_clipboard",
            side_effect=inspect_capture,
        ), mock.patch(
            "desktop_app.clipboard_sequence_number", return_value=10
        ):
            watcher._capture_with_clipboard(
                app,
                1,
                0,
                1.2,
                require_no_physical_modifiers=False,
            )

        controller.pressed.assert_not_called()

    @unittest.skip("Superseded by the requested test3 two-state UIA rollback")
    def test_unknown_selection_security_never_attempts_clipboard_fallback(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.store = SimpleNamespace(
            get=lambda _key, default=True: default,
            is_app_enabled=lambda _exe: True,
            capture_timing=lambda _exe: selection_capture.CaptureTiming(0.0, 1.0),
        )
        watcher.stop_event = threading.Event()
        watcher.state_lock = threading.Lock()
        watcher.interaction_id = 1
        watcher.input_generation = 0
        watcher.hotkey_requests_enabled = True
        watcher._capture_selection = mock.Mock(
            return_value=SelectionProbeResult(
                security=selection_capture.SelectionSecurity.UNKNOWN
            )
        )
        watcher._capture_with_clipboard = mock.Mock()
        watcher._focus_is_current = mock.Mock(return_value=True)
        watcher._record_diagnostic = mock.Mock()
        watcher.status_callback = mock.Mock()
        app = AppInfo("weixin.exe", "WeChat", hwnd=100, process_id=123)

        with mock.patch("desktop_app.foreground_app", return_value=app):
            watcher._process_capture(CaptureRequest(10, 20, 1, "mouse", 0))

        watcher._capture_with_clipboard.assert_not_called()
        watcher._record_diagnostic.assert_called_once_with(
            app, "skipped", "", "security_unknown", mock.ANY
        )

    def test_late_same_app_copy_after_user_input_preserves_the_new_clipboard(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.state_lock = threading.Lock()
        watcher.interaction_id = 5
        watcher.input_generation = 2
        watcher.copy_intent_generation = 0
        watcher.context_menu_armed_until = 0.0
        watcher.keys_down = set()
        watcher.stop_event = threading.Event()
        watcher.press = None
        watcher.last_release = None
        watcher.outside_click_callback = mock.Mock()
        app = AppInfo(
            "weixin.exe",
            "微信",
            path=r"C:\Program Files\Tencent\WeChat\Weixin.exe",
            hwnd=100,
            process_id=123,
        )
        snapshot = SimpleNamespace(
            restore=mock.Mock(return_value="restored"),
            close=mock.Mock(),
        )
        clock = SimpleNamespace(now=0.0)
        interaction_changed = False

        def sleep(seconds: float) -> None:
            nonlocal interaction_changed
            clock.now += seconds
            if not interaction_changed:
                interaction_changed = True
                watcher._on_click(20, 20, mouse.Button.left, True)

        real_capture = selection_capture.capture_selected_text_with_clipboard

        def inspect_capture(**kwargs: object) -> object:
            kwargs.update(
                sequence_number=lambda: 11 if clock.now >= 0.65 else 10,
                send_copy=lambda: None,
                read_text=lambda: "must not be published",
                timeout_seconds=0.9,
                monotonic=lambda: clock.now,
                sleep=sleep,
            )
            return real_capture(**kwargs)

        state = PortableClipboardState("old", [13], True, 10, snapshot)
        with mock.patch("desktop_app.capture_portable_clipboard_state", return_value=state), mock.patch(
            "desktop_app.keyboard.Controller"
        ), mock.patch(
            "desktop_app.clipboard_owner_matches_app", return_value=True
        ), mock.patch.object(
            DesktopSelectionWatcher, "_focus_is_current", return_value=True
        ), mock.patch(
            "desktop_app.capture_selected_text_with_clipboard", side_effect=inspect_capture
        ):
            result = watcher._capture_with_clipboard(app, 5, 2, 1.2)

        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "concurrent_change")
        snapshot.restore.assert_not_called()
        snapshot.close.assert_called_once_with()

    def test_focus_matching_requires_the_exact_foreground_window(self) -> None:
        app = AppInfo(
            "weixin.exe",
            "微信",
            path=r"C:\Program Files\Tencent\WeChat\Weixin.exe",
            hwnd=100,
            process_id=123,
        )

        with mock.patch("desktop_app.win32gui.GetForegroundWindow", return_value=100), mock.patch(
            "desktop_app.win32process.GetWindowThreadProcessId"
        ) as get_pid:
            self.assertTrue(DesktopSelectionWatcher._focus_is_current(app))
            get_pid.assert_not_called()

        with mock.patch("desktop_app.win32gui.GetForegroundWindow", return_value=200), mock.patch(
            "desktop_app.win32process.GetWindowThreadProcessId"
        ) as get_pid, mock.patch("desktop_app.process_path") as get_path:
            self.assertFalse(DesktopSelectionWatcher._focus_is_current(app))
            get_pid.assert_not_called()
            get_path.assert_not_called()

        with mock.patch(
            "desktop_app.win32gui.GetForegroundWindow", side_effect=OSError("gone")
        ):
            self.assertFalse(DesktopSelectionWatcher._focus_is_current(app))

    def test_uia_control_must_belong_to_the_captured_top_level_window(self) -> None:
        root = SimpleNamespace(
            NativeWindowHandle=321,
            GetParentControl=lambda: None,
        )
        child = SimpleNamespace(
            NativeWindowHandle=0,
            GetParentControl=lambda: root,
        )

        with mock.patch("desktop_app.win32gui.GetAncestor", return_value=321):
            self.assertTrue(
                DesktopSelectionWatcher._uia_control_belongs_to_window(child, 321)
            )
            self.assertFalse(
                DesktopSelectionWatcher._uia_control_belongs_to_window(child, 999)
            )

    def test_foreign_uia_controls_are_not_read(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        app = AppInfo("notepad.exe", "Notepad", hwnd=100)
        foreign_control = SimpleNamespace(
            NativeWindowHandle=200,
            GetParentControl=lambda: None,
        )

        with mock.patch(
            "desktop_app.auto.GetFocusedControl", return_value=foreign_control
        ), mock.patch(
            "desktop_app.auto.ControlFromPoint", return_value=foreign_control
        ), mock.patch(
            "desktop_app.win32gui.GetAncestor", return_value=200
        ), mock.patch(
            "desktop_app.read_uia_selected_text",
            return_value=("", False),
        ) as read_uia:
            result = watcher._capture_selection(app, 10, 20)

        self.assertEqual(result.text, "")
        read_uia.assert_called_once_with(
            [],
            mock.ANY,
            3000,
            mock.ANY,
        )

    def test_clipboard_owner_accepts_same_wechat_binary_across_processes(self) -> None:
        app = AppInfo(
            "weixin.exe",
            "微信",
            path=r"C:\Program Files\Tencent\WeChat\Weixin.exe",
            process_id=123,
        )

        self.assertTrue(
            clipboard_owner_matches_app(
                app,
                owner_hwnd=100,
                get_window_process_id=lambda _hwnd: (1, 456),
                get_process_path=lambda _pid: r"c:\program files\tencent\wechat\WEIXIN.EXE",
            )
        )
        self.assertFalse(
            clipboard_owner_matches_app(
                app,
                owner_hwnd=100,
                get_window_process_id=lambda _hwnd: (1, 456),
                get_process_path=lambda _pid: r"D:\Portable\Weixin.exe",
            )
        )
        self.assertIsNone(
            clipboard_owner_matches_app(
                app,
                owner_hwnd=0,
                get_window_process_id=lambda _hwnd: (1, 123),
            )
        )
        self.assertIsNone(
            clipboard_owner_matches_app(
                app,
                owner_hwnd=100,
                get_window_process_id=lambda _hwnd: (1, 456),
                get_process_path=lambda _pid: (_ for _ in ()).throw(
                    PermissionError("protected process")
                ),
            )
        )

    @unittest.skip("WeChat capture support was explicitly removed")
    def test_wechat_skips_deep_uia_probe(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        app = AppInfo("weixin.exe", "微信")

        with mock.patch(
            "desktop_app.auto.GetFocusedControl", return_value=None
        ), mock.patch(
            "desktop_app.auto.ControlFromPoint", return_value=None
        ), mock.patch(
            "desktop_app.probe_uia_selected_text",
            return_value=selection_capture.UiaSelectionProbe(),
        ) as shallow_read, mock.patch(
            "desktop_app.probe_uia_descendant_selected_text",
            side_effect=AssertionError("WeChat must not enter deep UIA"),
        ):
            result = watcher._capture_selection(app, 10, 20)

        self.assertEqual(result.text, "")
        self.assertEqual(result.method, "")
        self.assertIs(
            result.security, selection_capture.SelectionSecurity.UNKNOWN
        )
        shallow_read.assert_called_once()

    def test_word_capture_uses_the_verified_foreground_helper(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        app = AppInfo(
            "winword.exe",
            "Microsoft Word",
            title="Report - Word",
            hwnd=101,
        )

        with mock.patch(
            "desktop_app.read_word_selected_text", return_value="selected word"
        ) as read_word:
            result = watcher._capture_selection(app, 10, 20)

        self.assertEqual(result.text, "selected word")
        self.assertEqual(result.method, "word_com")
        read_word.assert_called_once_with(
            mock.ANY,
            3000,
            "Report - Word",
            101,
        )

    def test_powerpoint_capture_uses_the_verified_foreground_helper(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        app = AppInfo(
            "powerpnt.exe",
            "Microsoft PowerPoint",
            title="Deck - PowerPoint",
            hwnd=202,
        )

        with mock.patch(
            "desktop_app.read_powerpoint_selected_text", return_value="slide selection"
        ) as read_powerpoint:
            result = watcher._capture_selection(app, 10, 20)

        self.assertEqual(result.text, "slide selection")
        self.assertEqual(result.method, "powerpoint_com")
        read_powerpoint.assert_called_once_with(
            mock.ANY,
            3000,
            "Deck - PowerPoint",
            202,
        )

    def test_chromium_pdf_can_use_a_document_descendant_selection(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        app = AppInfo("chrome.exe", "Google Chrome", title="paper.pdf", hwnd=202)
        root = object()

        with mock.patch("desktop_app.auto.GetFocusedControl", return_value=None), mock.patch(
            "desktop_app.auto.ControlFromPoint", return_value=None
        ), mock.patch(
            "desktop_app.auto.ControlFromHandle", return_value=root
        ) as from_handle, mock.patch(
            "desktop_app.read_uia_selected_text",
            return_value=("", False),
        ), mock.patch(
            "desktop_app.read_uia_descendant_selected_text",
            return_value=("PDF selection", False),
        ) as deep_read:
            result = watcher._capture_selection(app, 10, 20)

        self.assertEqual(result.text, "PDF selection")
        self.assertEqual(result.method, "uia_descendant")
        from_handle.assert_called_once_with(202)
        deep_read.assert_called_once()

    def test_classic_outlook_uses_its_word_editor_helper(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        app = AppInfo(
            "outlook.exe",
            "Microsoft Outlook",
            title="Message - Outlook",
            hwnd=303,
        )

        with mock.patch(
            "desktop_app.read_outlook_selected_text", return_value="mail selection"
        ) as read_outlook:
            result = watcher._capture_selection(app, 10, 20)

        self.assertEqual(result.text, "mail selection")
        self.assertEqual(result.method, "outlook_com")
        read_outlook.assert_called_once_with(
            mock.ANY,
            3000,
            "Message - Outlook",
        )

    def test_common_app_defaults_include_office_new_outlook_and_wps_pdf(self) -> None:
        common = dict(COMMON_APPS)

        for exe in (
            "winword.exe",
            "excel.exe",
            "powerpnt.exe",
            "outlook.exe",
            "olk.exe",
            "onenote.exe",
            "wps.exe",
            "et.exe",
            "wpp.exe",
            "pdf.exe",
        ):
            self.assertIn(exe, common)

    def test_diagnostics_store_metadata_only_and_learn_without_immediate_write(self) -> None:
        store = SettingsStore.__new__(SettingsStore)
        store.lock = threading.RLock()
        store.data = {
            "adaptive_wait_enabled": True,
            "capture_diagnostics": {},
            "adaptive_timings": {},
        }
        store._deferred_dirty = False
        store._deferred_timer = None

        with mock.patch.object(store, "_schedule_deferred_save") as schedule:
            store.record_capture_diagnostic(
                "QQ.EXE",
                status="success",
                method="clipboard",
                reason="captured",
                elapsed_ms=125,
            )

        value = store.capture_diagnostic("qq.exe")
        self.assertEqual(
            set(value),
            {"status", "method", "reason", "elapsed_ms", "updated_at", "consecutive_failures"},
        )
        self.assertNotIn("text", value)
        self.assertEqual(store.capture_timing("qq.exe").clipboard_timeout_seconds, 1.5)
        schedule.assert_called_once_with()

    def test_restore_failure_is_shown_as_a_warning_not_green_success(self) -> None:
        settings = SettingsWindow.__new__(SettingsWindow)
        settings.store = SimpleNamespace(
            capture_diagnostic=lambda _exe: {
                "status": "warning",
                "method": "clipboard",
                "reason": "restore_failed",
                "elapsed_ms": 140,
                "consecutive_failures": 0,
            }
        )

        text, color = settings._diagnostic_summary("weixin.exe")

        self.assertIn("还原失败", text)
        self.assertEqual(color, AMBER)

    def test_restore_failure_without_text_is_reported_as_a_failure(self) -> None:
        settings = SettingsWindow.__new__(SettingsWindow)
        settings.store = SimpleNamespace(
            capture_diagnostic=lambda _exe: {
                "status": "failed",
                "method": "clipboard",
                "reason": "restore_failed",
                "elapsed_ms": 140,
                "consecutive_failures": 1,
            }
        )

        text, color = settings._diagnostic_summary("weixin.exe")

        self.assertIn("读取失败", text)
        self.assertIn("还原失败", text)
        self.assertEqual(color, RED)

    def test_settings_shortcut_switch_refreshes_after_tray_change(self) -> None:
        settings = SettingsWindow.__new__(SettingsWindow)
        settings.store = SimpleNamespace(
            get=lambda key, default=True: False if key == "hotkeys_enabled" else default
        )
        settings.shortcut_var = FakeVariable(True)
        settings.shortcut_switch = SimpleNamespace(draw=mock.Mock())

        settings.refresh_shortcut_state()

        self.assertFalse(settings.shortcut_var.get())
        settings.shortcut_switch.draw.assert_called_once_with()

    def test_natural_speed_persists_then_cancels_old_neural_speech(self) -> None:
        app = self.make_selection_app()
        app.store = SimpleNamespace(
            get=lambda key, default=None: "standard" if key == "natural_speech_speed" else default,
            set=mock.Mock(return_value=True),
        )
        app.speech = SimpleNamespace(set_natural_speed=mock.Mock())

        self.assertTrue(app.set_natural_speech_speed("slow"))

        app.store.set.assert_called_once_with("natural_speech_speed", "slow")
        app.speech.set_natural_speed.assert_called_once_with("slow")
        self.assertIn("慢", app.status_text.get())

    def test_natural_speed_save_failure_keeps_live_speech_unchanged(self) -> None:
        app = self.make_selection_app()
        app.store = SimpleNamespace(
            get=lambda _key, default=None: "standard" if default is None else default,
            set=mock.Mock(return_value=False),
        )
        app.speech = SimpleNamespace(set_natural_speed=mock.Mock())

        self.assertFalse(app.set_natural_speech_speed("fast"))

        app.speech.set_natural_speed.assert_not_called()
        self.assertIn("无法保存", app.status_text.get())

    def test_switching_auto_speech_preference_cancels_old_audio(self) -> None:
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.status_text = FakeVariable("")
        app.store = SimpleNamespace(
            get=lambda key, default=None: (
                "speed" if key == "auto_speech_preference" else default
            ),
            set=mock.Mock(return_value=True),
        )
        app.cancel_pending_pronunciation_click = mock.Mock()
        app.cancel_pending_auto_speak = mock.Mock()
        app.speech = SimpleNamespace(
            cancel=mock.Mock(),
            record_timing_event=mock.Mock(),
        )

        self.assertTrue(app.set_auto_speech_preference("natural"))

        app.store.set.assert_called_once_with(
            "auto_speech_preference", "natural"
        )
        app.cancel_pending_pronunciation_click.assert_called_once_with()
        app.cancel_pending_auto_speak.assert_called_once_with()
        app.speech.cancel.assert_called_once_with()
        self.assertIn("优先自然音色", app.status_text.get())

    def test_settings_touchpad_wheel_accumulates_partial_deltas(self) -> None:
        settings = SettingsWindow.__new__(SettingsWindow)
        settings._wheel_remainder = 0
        settings._settings_canvas = SimpleNamespace(yview_scroll=mock.Mock())
        settings.window = SimpleNamespace(winfo_viewable=lambda: True)
        event = SimpleNamespace(delta=60)

        settings._on_mousewheel(event)
        settings._settings_canvas.yview_scroll.assert_not_called()
        settings._on_mousewheel(event)

        settings._settings_canvas.yview_scroll.assert_called_once_with(-1, "units")
        self.assertEqual(settings._wheel_remainder, 0)

    def test_shortcut_focus_is_the_only_settings_state_that_pauses_hotkeys(self) -> None:
        settings = SettingsWindow.__new__(SettingsWindow)
        settings.app = SimpleNamespace(set_shortcut_editor_active=mock.Mock())
        entry = SimpleNamespace(selection_range=mock.Mock())

        settings._shortcut_focus_in(entry)

        entry.selection_range.assert_called_once_with(0, "end")
        settings.app.set_shortcut_editor_active.assert_called_once_with(True)

    def test_legacy_double_alt_setting_migrates_to_new_hotkey_switch(self) -> None:
        store = SettingsStore.__new__(SettingsStore)
        store.path = SimpleNamespace(
            read_text=mock.Mock(
                return_value=json.dumps({"double_alt_retry_enabled": False})
            )
        )

        values = store._load()

        self.assertFalse(values["hotkeys_enabled"])
        self.assertEqual(values["retry_hotkey"], "Double Alt")
        self.assertEqual(values["toggle_mode_hotkey"], "Alt+C")
        self.assertNotIn("double_alt_retry_enabled", values)

    def test_missing_auto_speech_preference_migrates_to_speed(self) -> None:
        store = SettingsStore.__new__(SettingsStore)
        store.path = SimpleNamespace(
            read_text=mock.Mock(return_value=json.dumps({"display_mode": "mini"}))
        )
        store.load_warning = ""

        values = store._load()

        self.assertEqual(values["auto_speech_preference"], "speed")

    def test_auto_speech_preference_persists_and_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            store = SettingsStore.__new__(SettingsStore)
            store.path = path
            store.lock = threading.RLock()
            store._deferred_dirty = False
            store._deferred_timer = None
            store.load_warning = ""
            store.data = dict(desktop_module.DEFAULT_SETTINGS)

            self.assertTrue(store.set("auto_speech_preference", "natural"))

            reloaded = SettingsStore.__new__(SettingsStore)
            reloaded.path = path
            reloaded.load_warning = ""
            self.assertEqual(
                reloaded._load()["auto_speech_preference"],
                "natural",
            )

    def test_existing_custom_hotkeys_survive_settings_load(self) -> None:
        store = SettingsStore.__new__(SettingsStore)
        store.path = SimpleNamespace(
            read_text=mock.Mock(
                return_value=json.dumps(
                    {
                        "retry_hotkey": "Ctrl+Shift+R",
                        "toggle_mode_hotkey": "Alt+M",
                    }
                )
            )
        )

        values = store._load()

        self.assertEqual(values["retry_hotkey"], "Ctrl+Shift+R")
        self.assertEqual(values["toggle_mode_hotkey"], "Alt+M")

    def test_corrupt_settings_are_backed_up_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text("{broken", encoding="utf-8")
            store = SettingsStore.__new__(SettingsStore)
            store.path = path
            store.load_warning = ""

            values = store._load()

            backups = list(path.parent.glob("settings.json.corrupt-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "{broken")
            self.assertFalse(values["desktop_enabled"])
            self.assertTrue(values["enabled_apps"])
            self.assertFalse(any(values["enabled_apps"].values()))
            self.assertIn("安全暂停", store.load_warning)

    def test_valid_atomic_tmp_recovers_a_corrupt_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text("{broken", encoding="utf-8")
            path.with_suffix(".json.tmp").write_text(
                json.dumps(
                    {
                        "desktop_enabled": True,
                        "enabled_apps": {"winword.exe": False},
                    }
                ),
                encoding="utf-8",
            )
            store = SettingsStore.__new__(SettingsStore)
            store.path = path
            store.load_warning = ""

            values = store._load()

            self.assertTrue(values["desktop_enabled"])
            self.assertFalse(values["enabled_apps"]["winword.exe"])
            self.assertIn("已恢复临时设置", store.load_warning)
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_setting_save_failure_rolls_back_memory_value(self) -> None:
        store = SettingsStore.__new__(SettingsStore)
        store.lock = threading.RLock()
        store.data = {"desktop_enabled": True}
        store.save = mock.Mock(return_value=False)

        saved = store.set("desktop_enabled", False)

        self.assertFalse(saved)
        self.assertTrue(store.data["desktop_enabled"])

    def test_app_setting_save_failure_rolls_back_all_metadata(self) -> None:
        store = SettingsStore.__new__(SettingsStore)
        store.lock = threading.RLock()
        store.data = {
            "enabled_apps": {"winword.exe": True},
            "app_names": {},
            "app_paths": {},
            "app_recency": {},
        }
        store.save = mock.Mock(return_value=False)

        saved = store.set_app(
            "winword.exe",
            False,
            name="changed",
            path="C:/changed.exe",
        )

        self.assertFalse(saved)
        self.assertEqual(store.data["enabled_apps"], {"winword.exe": True})
        self.assertEqual(store.data["app_names"], {})
        self.assertEqual(store.data["app_paths"], {})

    def test_settings_entry_recognises_two_local_alt_taps(self) -> None:
        settings = SettingsWindow.__new__(SettingsWindow)
        settings.store = SimpleNamespace(get=lambda _key, default=None: default)
        settings.window = None
        settings.shortcut_status_var = None
        settings.shortcut_status_label = None
        settings._shortcut_keys_down = set()
        settings._alt_press_started = {}
        settings._last_alt_release = {}
        settings._alt_used_in_combo = set()
        value = FakeVariable("")
        alt_event = SimpleNamespace(keysym="Alt_L", state=0)

        with mock.patch(
            "desktop_app.time.monotonic", side_effect=[1.0, 1.1, 1.3, 1.35]
        ):
            settings._record_shortcut_key(
                alt_event,
                value,
                HotkeyCommand.RETRY_AND_SPEAK_US,
                "retry_hotkey",
                allow_double_alt=True,
            )
            settings._release_shortcut_key(
                alt_event,
                value,
                "retry_hotkey",
                allow_double_alt=True,
            )
            settings._record_shortcut_key(
                alt_event,
                value,
                HotkeyCommand.RETRY_AND_SPEAK_US,
                "retry_hotkey",
                allow_double_alt=True,
            )
            settings._release_shortcut_key(
                alt_event,
                value,
                "retry_hotkey",
                allow_double_alt=True,
            )

        self.assertEqual(value.get(), "双击 Alt")

    def test_settings_double_alt_is_interrupted_by_ctrl_between_taps(self) -> None:
        settings = SettingsWindow.__new__(SettingsWindow)
        settings.store = SimpleNamespace(get=lambda _key, default=None: default)
        settings.window = None
        settings.shortcut_status_var = None
        settings.shortcut_status_label = None
        settings._shortcut_keys_down = set()
        settings._alt_press_started = {}
        settings._last_alt_release = {}
        settings._alt_used_in_combo = set()
        value = FakeVariable("Ctrl+Q")
        alt = SimpleNamespace(keysym="Alt_L", state=0)
        ctrl = SimpleNamespace(keysym="Control_L", state=0)

        with mock.patch(
            "desktop_app.time.monotonic", side_effect=[1.0, 1.05, 1.20, 1.25]
        ):
            settings._record_shortcut_key(
                alt,
                value,
                HotkeyCommand.RETRY_AND_SPEAK_US,
                "retry_hotkey",
                allow_double_alt=True,
            )
            settings._release_shortcut_key(
                alt, value, "retry_hotkey", allow_double_alt=True
            )
            settings._record_shortcut_key(
                ctrl,
                value,
                HotkeyCommand.RETRY_AND_SPEAK_US,
                "retry_hotkey",
                allow_double_alt=True,
            )
            settings._release_shortcut_key(
                ctrl, value, "retry_hotkey", allow_double_alt=True
            )
            settings._record_shortcut_key(
                alt,
                value,
                HotkeyCommand.RETRY_AND_SPEAK_US,
                "retry_hotkey",
                allow_double_alt=True,
            )
            settings._release_shortcut_key(
                alt, value, "retry_hotkey", allow_double_alt=True
            )

        self.assertEqual(value.get(), "Ctrl+Q")

    def test_settings_double_alt_rejects_alt_pressed_while_ctrl_is_held(self) -> None:
        settings = SettingsWindow.__new__(SettingsWindow)
        settings.store = SimpleNamespace(get=lambda _key, default=None: default)
        settings.window = None
        settings.shortcut_status_var = None
        settings.shortcut_status_label = None
        settings._shortcut_keys_down = set()
        settings._alt_press_started = {}
        settings._last_alt_release = {}
        settings._alt_used_in_combo = set()
        value = FakeVariable("Ctrl+Q")
        ctrl = SimpleNamespace(keysym="Control_L", state=0)
        alt_with_ctrl = SimpleNamespace(keysym="Alt_L", state=0x0004)

        settings._record_shortcut_key(
            ctrl,
            value,
            HotkeyCommand.RETRY_AND_SPEAK_US,
            "retry_hotkey",
            allow_double_alt=True,
        )
        with mock.patch("desktop_app.time.monotonic", return_value=1.05):
            settings._record_shortcut_key(
                alt_with_ctrl,
                value,
                HotkeyCommand.RETRY_AND_SPEAK_US,
                "retry_hotkey",
                allow_double_alt=True,
            )
            settings._release_shortcut_key(
                alt_with_ctrl, value, "retry_hotkey", allow_double_alt=True
            )

        self.assertEqual(value.get(), "Ctrl+Q")
        self.assertFalse(settings._last_alt_release)

    def test_settings_wheel_interrupts_partial_double_alt_recording(self) -> None:
        settings = SettingsWindow.__new__(SettingsWindow)
        settings._settings_canvas = SimpleNamespace(yview_scroll=mock.Mock())
        settings.window = SimpleNamespace(winfo_viewable=lambda: True)
        settings._wheel_remainder = 0
        settings._shortcut_keys_down = set()
        settings._alt_press_started = {"retry_hotkey": 1.0}
        settings._last_alt_release = {"retry_hotkey": 1.05}
        settings._alt_used_in_combo = set()

        settings._on_mousewheel(SimpleNamespace(delta=120))

        self.assertFalse(settings._alt_press_started)
        self.assertFalse(settings._last_alt_release)

    def test_settings_entry_never_treats_right_alt_as_double_alt(self) -> None:
        settings = SettingsWindow.__new__(SettingsWindow)
        settings.store = SimpleNamespace(get=lambda _key, default=None: default)
        settings.window = None
        settings.shortcut_status_var = None
        settings.shortcut_status_label = None
        settings._shortcut_keys_down = set()
        settings._alt_press_started = {}
        settings._last_alt_release = {}
        settings._alt_used_in_combo = set()
        value = FakeVariable("Ctrl+Q")
        alt_event = SimpleNamespace(keysym="Alt_R", state=0)

        with mock.patch("desktop_app.time.monotonic", side_effect=[1.0, 1.1, 1.2, 1.3]):
            for _ in range(2):
                settings._record_shortcut_key(
                    alt_event,
                    value,
                    HotkeyCommand.RETRY_AND_SPEAK_US,
                    "retry_hotkey",
                    allow_double_alt=True,
                )
                settings._release_shortcut_key(
                    alt_event,
                    value,
                    "retry_hotkey",
                    allow_double_alt=True,
                )

        self.assertEqual(value.get(), "Ctrl+Q")

    def test_settings_double_alt_uses_the_runtime_interval(self) -> None:
        settings = SettingsWindow.__new__(SettingsWindow)
        settings.store = SimpleNamespace(get=lambda _key, default=None: default)
        settings.window = None
        settings.shortcut_status_var = None
        settings.shortcut_status_label = None
        settings._shortcut_keys_down = set()
        settings._alt_press_started = {}
        settings._last_alt_release = {}
        settings._alt_used_in_combo = set()
        value = FakeVariable("Ctrl+Q")
        alt_event = SimpleNamespace(keysym="Alt_L", state=0)

        with mock.patch(
            "desktop_app.time.monotonic", side_effect=[1.0, 1.05, 1.45, 1.50]
        ):
            for _ in range(2):
                settings._record_shortcut_key(
                    alt_event,
                    value,
                    HotkeyCommand.RETRY_AND_SPEAK_US,
                    "retry_hotkey",
                    allow_double_alt=True,
                )
                settings._release_shortcut_key(
                    alt_event,
                    value,
                    "retry_hotkey",
                    allow_double_alt=True,
                )

        self.assertEqual(value.get(), "Ctrl+Q")

    def test_settings_double_alt_rejects_a_long_first_hold(self) -> None:
        settings = SettingsWindow.__new__(SettingsWindow)
        settings.store = SimpleNamespace(get=lambda _key, default=None: default)
        settings.window = None
        settings.shortcut_status_var = None
        settings.shortcut_status_label = None
        settings._shortcut_keys_down = set()
        settings._alt_press_started = {}
        settings._last_alt_release = {}
        settings._alt_used_in_combo = set()
        value = FakeVariable("Ctrl+Q")
        alt_event = SimpleNamespace(keysym="Alt_L", state=0)

        with mock.patch("desktop_app.time.monotonic", side_effect=[1.0, 1.40]):
            settings._record_shortcut_key(
                alt_event,
                value,
                HotkeyCommand.RETRY_AND_SPEAK_US,
                "retry_hotkey",
                allow_double_alt=True,
            )
            settings._release_shortcut_key(
                alt_event,
                value,
                "retry_hotkey",
                allow_double_alt=True,
            )

        self.assertEqual(value.get(), "Ctrl+Q")
        self.assertFalse(settings._last_alt_release)

    def test_settings_editor_pauses_and_resumes_every_hotkey_path(self) -> None:
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.shortcut_editor_active = False
        app.store = SimpleNamespace(
            get=lambda key, default=None: {
                "hotkeys_enabled": True,
                "retry_hotkey": "Double Alt",
                "toggle_mode_hotkey": "Alt+C",
            }.get(key, default)
        )
        app.watcher = SimpleNamespace(
            set_hotkey_requests_enabled=mock.Mock(),
            set_double_alt_enabled=mock.Mock(),
        )
        report = SimpleNamespace(all_registered=True)
        app.hotkey_service = SimpleNamespace(
            stop=mock.Mock(return_value=True),
            restart=mock.Mock(return_value=report),
        )
        app.set_status = mock.Mock()

        app.set_shortcut_editor_active(True)
        app.watcher.set_hotkey_requests_enabled.assert_called_with(False)
        app.watcher.set_double_alt_enabled.assert_called_with(False)
        app.hotkey_service.stop.assert_called_once_with(timeout_seconds=1.0)

        app.set_shortcut_editor_active(False)
        app.watcher.set_hotkey_requests_enabled.assert_called_with(True)
        app.watcher.set_double_alt_enabled.assert_called_with(True)
        app.hotkey_service.restart.assert_called_once()

    def test_same_shortcut_is_rejected_without_touching_settings(self) -> None:
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)

        succeeded, message = app.apply_hotkey_settings("Alt+Q", "alt+q")

        self.assertFalse(succeeded)
        self.assertIn("不能使用同一个", message)

        succeeded, message = app.apply_hotkey_settings("Alt+Q", "Double Alt")
        self.assertFalse(succeeded)
        self.assertIn("仅用于重新获取", message)

    def test_occupied_shortcut_restores_previous_pair_without_saving(self) -> None:
        class EditableStore:
            def __init__(self) -> None:
                self.data = {
                    "retry_hotkey": "Double Alt",
                    "toggle_mode_hotkey": "Alt+C",
                    "hotkeys_enabled": True,
                }
                self.set_calls: list[tuple[str, object, bool]] = []

            def get(self, key: str, default: object = None) -> object:
                return self.data.get(key, default)

            def set(self, key: str, value: object, save: bool = True) -> None:
                self.set_calls.append((key, value, save))
                self.data[key] = value

            def save(self) -> None:
                raise AssertionError("occupied shortcut must not be saved")

        store = EditableStore()
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.store = store
        failed_report = SimpleNamespace(all_registered=False)
        app.hotkey_service = SimpleNamespace(
            restart=mock.Mock(
                side_effect=[failed_report, SimpleNamespace(all_registered=True)]
            )
        )
        app.status_text = FakeVariable("")

        with mock.patch(
            "desktop_app.registration_status_text",
            return_value="Alt+Q 被其他程序占用",
        ):
            succeeded, message = app.apply_hotkey_settings("Alt+Q", "Alt+C")

        self.assertFalse(succeeded)
        self.assertIn("被其他程序占用", message)
        self.assertEqual(store.set_calls, [])
        self.assertEqual(store.data["retry_hotkey"], "Double Alt")
        self.assertEqual(app.hotkey_service.restart.call_count, 2)

    def test_valid_shortcuts_are_saved_after_successful_registration(self) -> None:
        class EditableStore:
            def __init__(self) -> None:
                self.data = {
                    "retry_hotkey": "Double Alt",
                    "toggle_mode_hotkey": "Alt+C",
                    "hotkeys_enabled": True,
                }
                self.save_calls = 0

            def get(self, key: str, default: object = None) -> object:
                return self.data.get(key, default)

            def set(self, key: str, value: object, save: bool = True) -> None:
                self.data[key] = value

            def save(self) -> None:
                self.save_calls += 1

        store = EditableStore()
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.store = store
        app.hotkey_service = SimpleNamespace(
            restart=mock.Mock(return_value=SimpleNamespace(all_registered=True))
        )
        app.settings_window = SimpleNamespace(refresh_shortcut_state=mock.Mock())
        app.update_tray_menu = mock.Mock()
        app.status_text = FakeVariable("")

        succeeded, message = app.apply_hotkey_settings("Ctrl+Shift+R", "Alt+M")

        self.assertTrue(succeeded, message)
        self.assertEqual(store.data["retry_hotkey"], "Ctrl+Shift+R")
        self.assertEqual(store.data["toggle_mode_hotkey"], "Alt+M")
        self.assertEqual(store.save_calls, 1)
        app.hotkey_service.restart.assert_called_once()
        app.settings_window.refresh_shortcut_state.assert_called_once_with()
        app.update_tray_menu.assert_called_once_with()

    def test_disabled_shortcuts_are_still_checked_for_conflicts_before_saving(self) -> None:
        class EditableStore:
            def __init__(self) -> None:
                self.data = {
                    "retry_hotkey": "Double Alt",
                    "toggle_mode_hotkey": "Alt+C",
                    "hotkeys_enabled": False,
                }

            def get(self, key: str, default: object = None) -> object:
                return self.data.get(key, default)

            def set(self, key: str, value: object, save: bool = True) -> None:
                self.data[key] = value

            def save(self) -> bool:
                return True

        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.store = EditableStore()
        app.hotkey_service = SimpleNamespace(
            restart=mock.Mock(return_value=SimpleNamespace(all_registered=True)),
            stop=mock.Mock(return_value=True),
        )
        app.settings_window = SimpleNamespace(refresh_shortcut_state=mock.Mock())
        app.update_tray_menu = mock.Mock()
        app.status_text = FakeVariable("")

        succeeded, _message = app.apply_hotkey_settings("Ctrl+Shift+R", "Alt+M")

        self.assertTrue(succeeded)
        app.hotkey_service.restart.assert_called_once()
        app.hotkey_service.stop.assert_called_once_with(timeout_seconds=1.0)

    def test_shortcut_save_failure_restores_old_values_and_runtime_pair(self) -> None:
        class FailingStore:
            def __init__(self) -> None:
                self.data = {
                    "retry_hotkey": "Double Alt",
                    "toggle_mode_hotkey": "Alt+C",
                    "hotkeys_enabled": True,
                }

            def get(self, key: str, default: object = None) -> object:
                return self.data.get(key, default)

            def set(self, key: str, value: object, save: bool = True) -> None:
                self.data[key] = value

            def save(self) -> bool:
                return False

        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.store = FailingStore()
        app.hotkey_service = SimpleNamespace(
            restart=mock.Mock(
                side_effect=[
                    SimpleNamespace(all_registered=True),
                    SimpleNamespace(all_registered=True),
                ]
            )
        )
        app.status_text = FakeVariable("")

        succeeded, message = app.apply_hotkey_settings("Ctrl+Shift+R", "Alt+M")

        self.assertFalse(succeeded)
        self.assertIn("无法写入磁盘", message)
        self.assertEqual(app.store.data["retry_hotkey"], "Double Alt")
        self.assertEqual(app.store.data["toggle_mode_hotkey"], "Alt+C")
        self.assertEqual(app.hotkey_service.restart.call_count, 2)

    def test_one_capture_exception_does_not_kill_worker_loop(self) -> None:
        class ScriptedQueue:
            def __init__(self, values: list[CaptureRequest | None]) -> None:
                self.values = values

            def get(self) -> CaptureRequest | None:
                return self.values.pop(0)

            def get_nowait(self) -> CaptureRequest | None:
                raise queue.Empty

        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.stopped_event = threading.Event()
        watcher.events = ScriptedQueue(
            [CaptureRequest(1, 2, 1), CaptureRequest(3, 4, 2), None]
        )
        calls: list[CaptureRequest] = []
        statuses: list[str] = []

        def process(event: CaptureRequest) -> None:
            calls.append(event)
            if len(calls) == 1:
                raise RuntimeError("one bad app")

        watcher._process_capture = process
        watcher.status_callback = statuses.append
        fake_initializer = mock.MagicMock()
        fake_initializer.__enter__.return_value = None
        fake_initializer.__exit__.return_value = None

        with mock.patch("desktop_app.log"), mock.patch("desktop_app.pythoncom.CoInitialize"), mock.patch(
            "desktop_app.pythoncom.CoUninitialize"
        ), mock.patch("desktop_app.auto.UIAutomationInitializerInThread", return_value=fake_initializer):
            watcher._capture_loop()

        self.assertEqual(len(calls), 2)
        self.assertTrue(watcher.stopped_event.is_set())
        self.assertIn("监听已自动继续", statuses[0])

    def test_app_never_destroys_windows_before_capture_thread_finishes(self) -> None:
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.watcher = SimpleNamespace(stopped_event=threading.Event())
        app.root = FakeWindow()
        app.mini = FakeWindow()
        app.settings_window = SimpleNamespace(window=FakeWindow())
        app.tray = None

        app._finish_quit_when_capture_stopped()

        self.assertEqual(app.root.destroy_calls, 0)
        self.assertEqual(app.mini.destroy_calls, 0)
        self.assertEqual(app.settings_window.window.destroy_calls, 0)
        self.assertEqual(len(app.root.after_callbacks), 1)

    def test_app_forces_ui_shutdown_after_capture_timeout(self) -> None:
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.watcher = SimpleNamespace(stopped_event=threading.Event())
        app.root = FakeWindow()
        app.mini = FakeWindow()
        app.settings_window = SimpleNamespace(window=FakeWindow())
        app.store = FakeStore()
        app.tray = None
        app.quit_started_at = 0.0

        with mock.patch("desktop_app.log"), mock.patch("desktop_app.time.monotonic", return_value=4.0):
            app._finish_quit_when_capture_stopped()

        self.assertEqual(app.root.destroy_calls, 1)
        self.assertEqual(app.mini.destroy_calls, 1)
        self.assertEqual(app.settings_window.window.destroy_calls, 1)
        self.assertEqual(app.store.flush_calls, 1)


if __name__ == "__main__":
    unittest.main()
