from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest import mock

import win32con

import desktop_app
from desktop_app import (
    MAX_PORTABLE_CLIPBOARD_BYTES,
    PortableClipboardSnapshot,
    capture_portable_clipboard_state,
)


class PortableClipboardCaptureTests(unittest.TestCase):
    def capture(
        self,
        *,
        formats: list[int],
        names: dict[int, str] | None = None,
        unicode_text: str | None = None,
        payloads: dict[int, bytes] | None = None,
        declared_sizes: dict[int, int] | None = None,
        sequence_values: list[int] | None = None,
        byte_limit: int | None = None,
    ) -> tuple[desktop_app.PortableClipboardState, dict[str, mock.Mock]]:
        names = names or {}
        payloads = payloads or {}
        declared_sizes = declared_sizes or {}
        sequence_values = sequence_values or [10, 10, 10]

        def enum_formats(current: int) -> int:
            if not formats:
                return 0
            if current == 0:
                return formats[0]
            try:
                index = formats.index(current)
            except ValueError:
                return 0
            return formats[index + 1] if index + 1 < len(formats) else 0

        def get_data(format_id: int) -> str:
            if format_id != win32con.CF_UNICODETEXT or unicode_text is None:
                raise TypeError(f"unexpected parsed clipboard format: {format_id}")
            return unicode_text

        def get_handle(format_id: int) -> int:
            if format_id == win32con.CF_UNICODETEXT and unicode_text is not None:
                return format_id + 100_000
            if format_id not in payloads:
                raise TypeError(f"format {format_id} has no portable HGLOBAL payload")
            return format_id + 100_000

        def get_memory(handle: int) -> bytes:
            return payloads[handle - 100_000]

        def global_size(handle: int) -> int:
            format_id = handle - 100_000
            if format_id in declared_sizes:
                return declared_sizes[format_id]
            if format_id == win32con.CF_UNICODETEXT and unicode_text is not None:
                return len(unicode_text.encode("utf-16-le")) + 2
            return len(payloads.get(format_id, b""))

        calls = {
            "open": mock.Mock(),
            "close": mock.Mock(),
            "enum": mock.Mock(side_effect=enum_formats),
            "get_name": mock.Mock(side_effect=lambda format_id: names[format_id]),
            "get_data": mock.Mock(side_effect=get_data),
            "get_handle": mock.Mock(side_effect=get_handle),
            "get_memory": mock.Mock(side_effect=get_memory),
            "global_size": mock.Mock(side_effect=global_size),
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(desktop_app.win32clipboard, "OpenClipboard", calls["open"]))
            stack.enter_context(mock.patch.object(desktop_app.win32clipboard, "CloseClipboard", calls["close"]))
            stack.enter_context(
                mock.patch.object(desktop_app.win32clipboard, "EnumClipboardFormats", calls["enum"])
            )
            stack.enter_context(
                mock.patch.object(desktop_app.win32clipboard, "GetClipboardFormatName", calls["get_name"])
            )
            stack.enter_context(
                mock.patch.object(desktop_app.win32clipboard, "GetClipboardData", calls["get_data"])
            )
            stack.enter_context(
                mock.patch.object(desktop_app.win32clipboard, "GetClipboardDataHandle", calls["get_handle"])
            )
            stack.enter_context(
                mock.patch.object(desktop_app.win32clipboard, "GetGlobalMemory", calls["get_memory"])
            )
            stack.enter_context(mock.patch("desktop_app._GLOBAL_SIZE", calls["global_size"]))
            stack.enter_context(
                mock.patch("desktop_app.clipboard_sequence_number", side_effect=sequence_values)
            )
            stack.enter_context(mock.patch("desktop_app.time.sleep"))
            if byte_limit is not None:
                stack.enter_context(
                    mock.patch("desktop_app.MAX_PORTABLE_CLIPBOARD_BYTES", byte_limit)
                )
            state = capture_portable_clipboard_state()
        return state, calls

    def test_unicode_html_and_rtf_are_materialized_as_managed_values(self) -> None:
        html_format = 0xC101
        rtf_format = 0xC102
        state, calls = self.capture(
            formats=[win32con.CF_UNICODETEXT, html_format, rtf_format],
            names={html_format: "HTML Format", rtf_format: "Rich Text Format"},
            unicode_text="old text",
            payloads={
                html_format: b"Version:1.0\r\n<html>old</html>\x00",
                rtf_format: b"{\\rtf1 old}\x00",
            },
        )

        self.assertTrue(state.known)
        self.assertEqual(state.text, "old text")
        self.assertEqual(state.sequence, 10)
        self.assertIsNotNone(state.snapshot)
        assert state.snapshot is not None
        self.assertEqual(
            state.snapshot.entries,
            [
                (win32con.CF_UNICODETEXT, "old text"),
                (html_format, b"Version:1.0\r\n<html>old</html>\x00"),
                (rtf_format, b"{\\rtf1 old}\x00"),
            ],
        )
        self.assertEqual(calls["open"].call_count, 1)
        self.assertEqual(calls["close"].call_count, 1)

    def test_chromium_source_metadata_is_preserved_as_managed_hglobal(self) -> None:
        html_format = 0xC101
        rfh_token = 0xC2A7
        source_url = 0xC2A5
        state, _calls = self.capture(
            formats=[
                html_format,
                win32con.CF_UNICODETEXT,
                rfh_token,
                source_url,
                win32con.CF_LOCALE,
                win32con.CF_TEXT,
                win32con.CF_OEMTEXT,
            ],
            names={
                html_format: "HTML Format",
                rfh_token: "Chromium internal source RFH token",
                source_url: "Chromium internal source URL",
            },
            unicode_text="old text",
            payloads={
                html_format: b"Version:1.0\r\n<html>old</html>\x00",
                rfh_token: b"managed-rfh-token\x00",
                source_url: b"https://example.test/\x00",
                win32con.CF_LOCALE: b"\x04\x08\x00\x00",
                win32con.CF_TEXT: b"old text\x00",
                win32con.CF_OEMTEXT: b"old text\x00",
            },
        )

        self.assertTrue(state.known)
        self.assertIsNotNone(state.snapshot)
        assert state.snapshot is not None
        self.assertEqual(
            {format_id for format_id, _payload in state.snapshot.entries},
            {
                html_format,
                win32con.CF_UNICODETEXT,
                rfh_token,
                source_url,
                win32con.CF_LOCALE,
                win32con.CF_TEXT,
                win32con.CF_OEMTEXT,
            },
        )
        self.assertIn((rfh_token, b"managed-rfh-token\x00"), state.snapshot.entries)
        self.assertIn((source_url, b"https://example.test/\x00"), state.snapshot.entries)

    def test_similar_unknown_chromium_format_is_still_rejected(self) -> None:
        html_format = 0xC101
        unknown_format = 0xC2A8
        state, _calls = self.capture(
            formats=[html_format, unknown_format],
            names={
                html_format: "HTML Format",
                unknown_format: "Chromium internal source RFH token v2",
            },
            payloads={
                html_format: b"Version:1.0\r\n<html>old</html>\x00",
                unknown_format: b"unknown-layout\x00",
            },
        )

        self.assertTrue(state.known)
        self.assertIsNone(state.snapshot)

    def test_png_and_dib_cover_a_redundant_bitmap_handle(self) -> None:
        png_format = 0xC201
        dib = b"DIB bytes"
        png = b"\x89PNG\r\n\x1a\nPNG bytes"
        state, calls = self.capture(
            formats=[win32con.CF_BITMAP, win32con.CF_DIB, png_format],
            names={png_format: "PNG"},
            payloads={win32con.CF_DIB: dib, png_format: png},
        )

        self.assertIsNotNone(state.snapshot)
        assert state.snapshot is not None
        self.assertEqual(
            state.snapshot.entries,
            [(win32con.CF_DIB, dib), (png_format, png)],
        )
        requested_handles = [call.args[0] for call in calls["get_handle"].call_args_list]
        self.assertNotIn(win32con.CF_BITMAP, requested_handles)

    def test_virtual_file_without_hdrop_is_rejected_before_materialization(self) -> None:
        file_group = 0xC301
        file_contents = 0xC302
        state, calls = self.capture(
            formats=[file_group, file_contents, win32con.CF_UNICODETEXT],
            names={
                file_group: "FileGroupDescriptorW",
                file_contents: "FileContents",
            },
            unicode_text="virtual attachment",
        )

        self.assertTrue(state.known)
        self.assertIsNone(state.snapshot)
        calls["get_data"].assert_not_called()
        calls["get_handle"].assert_not_called()
        calls["get_memory"].assert_not_called()

    def test_configured_64_mib_limit_rejects_an_oversized_snapshot(self) -> None:
        self.assertEqual(MAX_PORTABLE_CLIPBOARD_BYTES, 64 * 1024 * 1024)
        html_format = 0xC401
        state, _calls = self.capture(
            formats=[html_format],
            names={html_format: "HTML Format"},
            payloads={html_format: b"123456789"},
            byte_limit=8,
        )

        self.assertTrue(state.known)
        self.assertIsNone(state.snapshot)

    def test_delayed_rendering_sequence_change_retries_the_whole_snapshot(self) -> None:
        state, calls = self.capture(
            formats=[win32con.CF_UNICODETEXT],
            unicode_text="stable after rendering",
            # Initial, first before/after, then second before/after.
            sequence_values=[20, 20, 21, 21, 21],
        )

        self.assertTrue(state.known)
        self.assertEqual(state.sequence, 21)
        self.assertIsNotNone(state.snapshot)
        self.assertEqual(calls["open"].call_count, 2)
        self.assertEqual(calls["close"].call_count, 2)
        self.assertEqual(calls["get_data"].call_count, 2)

    def test_nonredundant_unsupported_format_fails_closed(self) -> None:
        state, _calls = self.capture(
            formats=[win32con.CF_UNICODETEXT, win32con.CF_ENHMETAFILE],
            unicode_text="old text",
        )

        self.assertTrue(state.known)
        self.assertIsNone(state.snapshot)

    def test_global_size_rejects_large_payload_before_copying_memory(self) -> None:
        html_format = 0xC601
        state, calls = self.capture(
            formats=[html_format],
            names={html_format: "HTML Format"},
            payloads={html_format: b"123456789"},
            declared_sizes={html_format: 9},
            byte_limit=8,
        )

        self.assertIsNone(state.snapshot)
        calls["get_memory"].assert_not_called()

    def test_invalid_zero_global_size_is_rejected_before_materialization(self) -> None:
        html_format = 0xC602
        state, calls = self.capture(
            formats=[html_format],
            names={html_format: "HTML Format"},
            payloads={html_format: b"must not be read"},
            declared_sizes={html_format: 0},
        )

        self.assertTrue(state.known)
        self.assertIsNone(state.snapshot)
        calls["get_memory"].assert_not_called()

    def test_zero_sequence_never_opens_or_materializes_clipboard(self) -> None:
        state, calls = self.capture(
            formats=[win32con.CF_UNICODETEXT],
            unicode_text="old text",
            sequence_values=[0],
        )

        self.assertFalse(state.known)
        self.assertEqual(state.sequence, 0)
        calls["open"].assert_not_called()
        calls["get_data"].assert_not_called()


class PortableClipboardRestoreTests(unittest.TestCase):
    def test_restore_replays_chromium_metadata_byte_for_byte(self) -> None:
        html_format = 0xC101
        rfh_token = 0xC2A7
        source_url = 0xC2A5
        snapshot = PortableClipboardSnapshot(
            [
                (html_format, b"Version:1.0\x00"),
                (win32con.CF_UNICODETEXT, "old text"),
                (rfh_token, b"managed-rfh-token\x00"),
                (source_url, b"https://example.test/\x00"),
            ]
        )
        set_data = mock.Mock()

        with mock.patch.object(
            desktop_app.win32clipboard, "OpenClipboard"
        ), mock.patch.object(
            desktop_app.win32clipboard, "CloseClipboard"
        ), mock.patch.object(
            desktop_app.win32clipboard, "EmptyClipboard"
        ), mock.patch.object(
            desktop_app.win32clipboard, "SetClipboardData", set_data
        ), mock.patch(
            "desktop_app.clipboard_sequence_number", return_value=77
        ):
            result = snapshot.restore(expected_sequence=77)

        self.assertEqual(result, "restored")
        self.assertEqual(
            set_data.call_args_list,
            [
                mock.call(html_format, b"Version:1.0\x00"),
                mock.call(win32con.CF_UNICODETEXT, "old text"),
                mock.call(rfh_token, b"managed-rfh-token\x00"),
                mock.call(source_url, b"https://example.test/\x00"),
            ],
        )

    def test_atomic_restore_preserves_a_newer_clipboard_sequence(self) -> None:
        snapshot = PortableClipboardSnapshot([(win32con.CF_UNICODETEXT, "old")])
        open_clipboard = mock.Mock()
        close_clipboard = mock.Mock()
        empty_clipboard = mock.Mock()
        set_data = mock.Mock()

        with mock.patch.object(
            desktop_app.win32clipboard, "OpenClipboard", open_clipboard
        ), mock.patch.object(
            desktop_app.win32clipboard, "CloseClipboard", close_clipboard
        ), mock.patch.object(
            desktop_app.win32clipboard, "EmptyClipboard", empty_clipboard
        ), mock.patch.object(
            desktop_app.win32clipboard, "SetClipboardData", set_data
        ), mock.patch(
            "desktop_app.clipboard_sequence_number", return_value=101
        ):
            result = snapshot.restore(expected_sequence=100)

        self.assertEqual(result, "preserved")
        open_clipboard.assert_called_once_with()
        close_clipboard.assert_called_once_with()
        empty_clipboard.assert_not_called()
        set_data.assert_not_called()
        self.assertEqual(snapshot.entries, [(win32con.CF_UNICODETEXT, "old")])

    def test_set_failure_after_empty_never_retries_destructive_restore(self) -> None:
        snapshot = PortableClipboardSnapshot(
            [
                (win32con.CF_UNICODETEXT, "old"),
                (0xC501, b"HTML bytes"),
            ]
        )
        open_clipboard = mock.Mock()
        close_clipboard = mock.Mock()
        empty_clipboard = mock.Mock()
        set_data = mock.Mock(side_effect=RuntimeError("allocation failed"))

        with mock.patch.object(
            desktop_app.win32clipboard, "OpenClipboard", open_clipboard
        ), mock.patch.object(
            desktop_app.win32clipboard, "CloseClipboard", close_clipboard
        ), mock.patch.object(
            desktop_app.win32clipboard, "EmptyClipboard", empty_clipboard
        ), mock.patch.object(
            desktop_app.win32clipboard, "SetClipboardData", set_data
        ), mock.patch(
            "desktop_app.clipboard_sequence_number", return_value=200
        ), mock.patch(
            "desktop_app.time.sleep"
        ) as sleep:
            result = snapshot.restore(expected_sequence=200)

        self.assertEqual(result, "failed")
        open_clipboard.assert_called_once_with()
        close_clipboard.assert_called_once_with()
        empty_clipboard.assert_called_once_with()
        set_data.assert_called_once_with(win32con.CF_UNICODETEXT, "old")
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
