from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import desktop_app
from desktop_app import (
    DesktopTranslatorApp,
    WorkArea,
    monitor_work_area_at,
    position_popup_in_work_area,
    position_window_in_work_area,
)


class WindowPositioningTests(unittest.TestCase):
    def test_monitor_work_area_reads_signed_taskbar_excluding_rect(self) -> None:
        def populate_info(_monitor: object, info_pointer: object) -> bool:
            info = info_pointer._obj
            info.rcMonitor = desktop_app.wintypes.RECT(-1920, 0, 0, 1080)
            info.rcWork = desktop_app.wintypes.RECT(-1920, 0, 0, 1040)
            return True

        with mock.patch(
            "desktop_app._MONITOR_FROM_POINT",
            return_value=123,
        ) as monitor_from_point, mock.patch(
            "desktop_app._GET_MONITOR_INFO",
            side_effect=populate_info,
        ):
            area = monitor_work_area_at(-960, 540)

        self.assertEqual(area, WorkArea(-1920, 0, 0, 1040))
        point = monitor_from_point.call_args.args[0]
        self.assertEqual((point.x, point.y), (-960, 540))

    def test_popup_stays_on_left_monitor_with_negative_coordinates(self) -> None:
        area = WorkArea(-1920, 0, 0, 1040)

        placement = position_popup_in_work_area(-960, 540, 440, 313, area)

        self.assertLess(placement.x, 0)
        self.assertGreaterEqual(placement.x, area.left + 8)
        self.assertLessEqual(placement.x + placement.width, area.right - 8)
        self.assertGreaterEqual(placement.y, area.top + 8)
        self.assertLessEqual(placement.y + placement.height, area.bottom - 8)

    def test_popup_stays_on_right_monitor(self) -> None:
        area = WorkArea(1920, 0, 3840, 1040)

        placement = position_popup_in_work_area(2880, 540, 440, 313, area)

        self.assertGreaterEqual(placement.x, area.left + 8)
        self.assertLessEqual(placement.x + placement.width, area.right - 8)

    def test_mini_adapter_uses_test3_primary_screen_clamping(self) -> None:
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
        area = WorkArea(-1920, 0, 0, 1040)
        with mock.patch("desktop_app.monitor_work_area_at", return_value=area):
            placement = app.position_mini(-960, 540)

        self.assertEqual(placement[0], 8)
        # Tk treats a negative wm geometry offset as right-edge-relative, so
        # only size is set here; the signed coordinates are passed to Win32 by
        # show_mini_no_activate.
        app.mini.geometry.assert_called_once_with("440x313+8+558")

    def test_popup_avoids_right_and_bottom_taskbars(self) -> None:
        # The physical display is 1366x768; rcWork excludes a 60px right
        # taskbar and a 40px bottom taskbar.
        area = WorkArea(0, 0, 1306, 728)

        placement = position_popup_in_work_area(900, 470, 440, 256, area)

        self.assertLessEqual(placement.x + placement.width, 1298)
        self.assertLessEqual(placement.y + placement.height, 720)

    def test_popup_shrinks_inside_a_narrow_short_work_area(self) -> None:
        area = WorkArea(-400, 20, 0, 320)

        placement = position_popup_in_work_area(-5, 315, 440, 500, area)

        self.assertEqual((placement.width, placement.height), (384, 284))
        self.assertEqual((placement.x, placement.y), (-392, 28))
        self.assertEqual(placement.x + placement.width, -8)
        self.assertEqual(placement.y + placement.height, 312)

    def test_panel_fits_1366_by_728_work_area(self) -> None:
        area = WorkArea(0, 0, 1366, 728)

        placement = position_window_in_work_area(620, 720, area)

        self.assertEqual(placement, desktop_app.WindowPlacement(716, 8, 620, 712))
        self.assertLessEqual(placement.x + placement.width, area.right - 8)
        self.assertLessEqual(placement.y + placement.height, area.bottom - 8)

    def test_panel_adapter_uses_pointer_monitor_and_fit_helper(self) -> None:
        app = DesktopTranslatorApp.__new__(DesktopTranslatorApp)
        app.root = SimpleNamespace(
            update_idletasks=mock.Mock(),
            winfo_screenwidth=lambda: 1366,
            winfo_screenheight=lambda: 768,
        )
        area = WorkArea(0, 0, 1366, 728)
        with mock.patch(
            "desktop_app.current_monitor_work_area",
            return_value=area,
        ), mock.patch("desktop_app.fit_tk_window_to_work_area") as fit:
            app._position_panel()

        fit.assert_called_once_with(app.root, area, 620, 720, 580, 660)


if __name__ == "__main__":
    unittest.main()
