from __future__ import annotations

import queue
import threading
import unittest
from types import SimpleNamespace

from desktop_app import DesktopSelectionWatcher, DesktopTranslatorApp


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


class WatcherLifecycleTests(unittest.TestCase):
    def test_stop_invalidates_pending_capture_and_replaces_queue_with_sentinel(self) -> None:
        watcher = DesktopSelectionWatcher.__new__(DesktopSelectionWatcher)
        watcher.stop_event = threading.Event()
        watcher.stopped_event = threading.Event()
        watcher.interaction_id = 7
        watcher.events = queue.Queue(maxsize=1)
        watcher.event_lock = threading.Lock()
        watcher.events.put_nowait((1, 2, object(), 7))
        watcher.listener = FakeListener()
        watcher.worker = FakeWorker()

        watcher.stop()

        self.assertTrue(watcher.stop_event.is_set())
        self.assertEqual(watcher.interaction_id, 8)
        self.assertEqual(watcher.listener.stop_calls, 1)
        self.assertIsNone(watcher.events.get_nowait())
        self.assertEqual(watcher.worker.join_timeouts, [])

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


if __name__ == "__main__":
    unittest.main()
