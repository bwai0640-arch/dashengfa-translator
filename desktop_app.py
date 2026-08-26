from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import queue
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import wave
from contextlib import closing
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pystray
import pythoncom
import tkinter as tk
import uiautomation as auto
import win32clipboard
import win32com.client
import win32con
import win32gui
import win32process
from PIL import Image
from pynput import keyboard, mouse
from tkinter import filedialog, messagebox

import app as engine
from capture_runtime import (
    adaptive_wait_from_mapping,
    learn_adaptive_wait,
)
from hotkey_service import (
    HotkeyBindingKind,
    HotkeyCommand,
    WindowsHotkeyService,
    format_hotkey_spec,
    parse_hotkey_spec,
    registration_status_text,
)
from selection_capture import (
    CaptureTiming,
    ClipboardCaptureResult,
    capture_selected_text_with_clipboard,
    read_excel_selected_text,
    read_outlook_selected_text,
    read_powerpoint_selected_text,
    read_uia_descendant_selected_text,
    read_uia_selected_text,
    read_word_selected_text,
    timing_for_app,
)


APP_NAME = "大声发划词翻译"
APP_VERSION = "0.4.0"
APP_AUTHOR = "眼泪斷了线"
MAX_SELECTION_LENGTH = 3000
PIPER_RESOURCE_SHA256 = {
    "models/piper/en_US-lessac-high.onnx": "4cabf7c3a638017137f34a1516522032d4fe3f38228a843cc9b764ddcbcd9e09",
    "models/piper/en_US-lessac-high.onnx.json": "db42b97d9859f257bc1561b8ed980e7fb2398402050a74ddd6cbec931a92412f",
    "models/piper/en_US-lessac-high.MODEL_CARD.md": "7671826d947a0ffc11dd76af0bd890d93e956b00358696ad71348f21aa827100",
    "models/piper/en_GB-cori-high.onnx": "470b4dd634c98f8a4850d7626ffc3dfc90774628eeef6605a6dd8f88f30a5903",
    "models/piper/en_GB-cori-high.onnx.json": "9e7fb5b5671612c22f3c81cbe46c1ae87b031a4632bcb509e499dad6f1e2adec",
    "models/piper/en_GB-cori-high.MODEL_CARD.md": "136e7bd168b6c35b4a5df01a0253297e5773b5775ceae0af5160f264aa58208f",
    "models/piper/PIPER_GPL-3.0.txt": "5f631fae467c82b8cd28fd1ec425c816895a35f9d94e36bee0e0164570e8e0f6",
    "models/piper/README.md": "06a20b0f7054800baa4f6dcff2c11c144ba02abc36809d26abdc4a078db51a5f",
}
CONTEXT_MENU_COPY_WINDOW_SECONDS = 2.0
DOUBLE_ALT_TAP_INTERVAL_SECONDS = 0.40
DOUBLE_ALT_TAP_MAX_HOLD_SECONDS = 0.35
TRANSLATION_LATEST_SETTLE_SECONDS = 0.045
SINGLE_INSTANCE_MUTEX = r"Local\DaShengFaTranslator-4F176327-5F34-4C04-9D76-38A7A63D60D4"

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_OPEN_PROCESS = _KERNEL32.OpenProcess
_OPEN_PROCESS.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_OPEN_PROCESS.restype = wintypes.HANDLE
_QUERY_FULL_PROCESS_IMAGE_NAME = _KERNEL32.QueryFullProcessImageNameW
_QUERY_FULL_PROCESS_IMAGE_NAME.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
)
_QUERY_FULL_PROCESS_IMAGE_NAME.restype = wintypes.BOOL
_CLOSE_HANDLE = _KERNEL32.CloseHandle
_CLOSE_HANDLE.argtypes = (wintypes.HANDLE,)
_CLOSE_HANDLE.restype = wintypes.BOOL
_CREATE_MUTEX = _KERNEL32.CreateMutexW
_CREATE_MUTEX.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_CREATE_MUTEX.restype = wintypes.HANDLE
_GLOBAL_SIZE = _KERNEL32.GlobalSize
_GLOBAL_SIZE.argtypes = (wintypes.HGLOBAL,)
_GLOBAL_SIZE.restype = ctypes.c_size_t

_USER32 = ctypes.WinDLL("user32", use_last_error=True)
_GET_CLIPBOARD_OWNER = _USER32.GetClipboardOwner
_GET_CLIPBOARD_OWNER.argtypes = ()
_GET_CLIPBOARD_OWNER.restype = wintypes.HWND
_GET_CLIPBOARD_SEQUENCE_NUMBER = _USER32.GetClipboardSequenceNumber
_GET_CLIPBOARD_SEQUENCE_NUMBER.argtypes = ()
_GET_CLIPBOARD_SEQUENCE_NUMBER.restype = wintypes.DWORD
_GET_WINDOW_LONG_PTR = _USER32.GetWindowLongPtrW
_GET_WINDOW_LONG_PTR.argtypes = (wintypes.HWND, ctypes.c_int)
_GET_WINDOW_LONG_PTR.restype = ctypes.c_ssize_t
_SET_WINDOW_LONG_PTR = _USER32.SetWindowLongPtrW
_SET_WINDOW_LONG_PTR.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
_SET_WINDOW_LONG_PTR.restype = ctypes.c_ssize_t
_MONITOR_FROM_POINT = _USER32.MonitorFromPoint
_MONITOR_FROM_POINT.argtypes = (wintypes.POINT, wintypes.DWORD)
_MONITOR_FROM_POINT.restype = wintypes.HANDLE
_GET_CURSOR_POS = _USER32.GetCursorPos
_GET_CURSOR_POS.argtypes = (ctypes.POINTER(wintypes.POINT),)
_GET_CURSOR_POS.restype = wintypes.BOOL


class _MonitorInfo(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    )


_GET_MONITOR_INFO = _USER32.GetMonitorInfoW
_GET_MONITOR_INFO.argtypes = (wintypes.HANDLE, ctypes.POINTER(_MonitorInfo))
_GET_MONITOR_INFO.restype = wintypes.BOOL

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
MONITOR_DEFAULTTONEAREST = 0x00000002
WINDOW_WORK_MARGIN = 8

WHITE = "#FFFFFF"
PAGE = "#F7F8FA"
TEXT = "#171A21"
MUTED = "#6B7280"
FAINT = "#9CA3AF"
LINE = "#E7E9EE"
BLUE = "#3267E3"
BLUE_SOFT = "#EEF3FF"
GREEN = "#16865B"
AMBER = "#A16207"
RED = "#C2414B"
RED_SOFT = "#FFF1F2"


@dataclass(frozen=True, slots=True)
class WorkArea:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)


@dataclass(frozen=True, slots=True)
class WindowPlacement:
    x: int
    y: int
    width: int
    height: int


def monitor_work_area_at(x: int, y: int) -> WorkArea:
    """Return the taskbar-excluding work area nearest a physical screen point."""

    monitor = _MONITOR_FROM_POINT(
        wintypes.POINT(int(x), int(y)),
        MONITOR_DEFAULTTONEAREST,
    )
    if not monitor:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    if not _GET_MONITOR_INFO(monitor, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    work = info.rcWork
    area = WorkArea(int(work.left), int(work.top), int(work.right), int(work.bottom))
    if area.width <= 0 or area.height <= 0:
        raise OSError("显示器工作区无效")
    return area


def current_monitor_work_area(fallback_width: int, fallback_height: int) -> WorkArea:
    """Resolve the pointer monitor, falling back to the primary Tk dimensions."""

    point = wintypes.POINT()
    try:
        if _GET_CURSOR_POS(ctypes.byref(point)):
            return monitor_work_area_at(int(point.x), int(point.y))
    except Exception as exc:
        log(f"Could not resolve pointer monitor work area: {exc}")
    return WorkArea(0, 0, max(1, int(fallback_width)), max(1, int(fallback_height)))


def position_popup_in_work_area(
    pointer_x: int,
    pointer_y: int,
    preferred_width: int,
    preferred_height: int,
    work_area: WorkArea,
    *,
    margin: int = WINDOW_WORK_MARGIN,
) -> WindowPlacement:
    """Fit a pointer-adjacent popup wholly inside one monitor work area."""

    safe_margin = max(0, min(int(margin), work_area.width // 2, work_area.height // 2))
    available_width = max(1, work_area.width - safe_margin * 2)
    available_height = max(1, work_area.height - safe_margin * 2)
    width = min(max(1, int(preferred_width)), available_width)
    height = min(max(1, int(preferred_height)), available_height)
    minimum_x = work_area.left + safe_margin
    maximum_x = work_area.right - safe_margin - width
    minimum_y = work_area.top + safe_margin
    maximum_y = work_area.bottom - safe_margin - height
    target_x = min(max(minimum_x, int(pointer_x) + 14), maximum_x)
    target_y = int(pointer_y) + 18
    if target_y > maximum_y:
        target_y = int(pointer_y) - height - 14
    target_y = min(max(minimum_y, target_y), maximum_y)
    return WindowPlacement(target_x, target_y, width, height)


def position_window_in_work_area(
    preferred_width: int,
    preferred_height: int,
    work_area: WorkArea,
    *,
    right_offset: int = 30,
    top_offset: int = 45,
    margin: int = WINDOW_WORK_MARGIN,
) -> WindowPlacement:
    """Fit a normal window inside a work area while retaining its usual corner."""

    safe_margin = max(0, min(int(margin), work_area.width // 2, work_area.height // 2))
    available_width = max(1, work_area.width - safe_margin * 2)
    available_height = max(1, work_area.height - safe_margin * 2)
    width = min(max(1, int(preferred_width)), available_width)
    height = min(max(1, int(preferred_height)), available_height)
    minimum_x = work_area.left + safe_margin
    maximum_x = work_area.right - safe_margin - width
    minimum_y = work_area.top + safe_margin
    maximum_y = work_area.bottom - safe_margin - height
    target_x = work_area.right - width - max(safe_margin, int(right_offset))
    target_y = work_area.top + max(safe_margin, int(top_offset))
    return WindowPlacement(
        min(max(minimum_x, target_x), maximum_x),
        min(max(minimum_y, target_y), maximum_y),
        width,
        height,
    )


COMMON_APPS: list[tuple[str, str]] = [
    ("winword.exe", "Microsoft Word"),
    ("excel.exe", "Microsoft Excel"),
    ("powerpnt.exe", "Microsoft PowerPoint"),
    ("outlook.exe", "Microsoft Outlook"),
    ("olk.exe", "新版 Microsoft Outlook"),
    ("onenote.exe", "Microsoft OneNote"),
    ("wps.exe", "WPS 文字"),
    ("et.exe", "WPS 表格"),
    ("wpp.exe", "WPS 演示"),
    ("pdf.exe", "WPS PDF"),
    ("msedge.exe", "Microsoft Edge"),
    ("chrome.exe", "Google Chrome"),
    ("firefox.exe", "Mozilla Firefox"),
    ("acrord32.exe", "Adobe Acrobat Reader"),
    ("acrobat.exe", "Adobe Acrobat"),
    ("foxitpdfreader.exe", "Foxit PDF Reader"),
    ("qq.exe", "QQ"),
    ("notepad.exe", "记事本"),
]

FRIENDLY_NAMES = dict(COMMON_APPS)
FRIENDLY_NAMES.update(
    {
        "pdfxcview.exe": "PDF-XChange Viewer",
        "pdfxedit.exe": "PDF-XChange Editor",
        "typora.exe": "Typora",
        "code.exe": "Visual Studio Code",
        "qq.exe": "QQ",
    }
)

UNSUPPORTED_APPS = frozenset({"wechat.exe", "weixin.exe"})

IGNORED_APPS = {
    "selectiontranslator.exe",
    "划词翻译发音.exe",
    "dashengfatranslator.exe",
    "desktop_app.exe",
    "python.exe",
    "pythonw.exe",
    "explorer.exe",
    "shellexperiencehost.exe",
    "searchhost.exe",
    "startmenuexperiencehost.exe",
    "applicationframehost.exe",
    "textinputhost.exe",
    "lockapp.exe",
    # 微信自绘消息区无法稳定、安全地暴露系统选区；按产品决定停用。
    *UNSUPPORTED_APPS,
}

DEFAULT_ENABLED = {exe: True for exe, _name in COMMON_APPS}
DEFAULT_SETTINGS: dict[str, object] = {
    "display_mode": "mini",
    "auto_translate": True,
    "desktop_enabled": True,
    "clipboard_fallback": True,
    "hotkeys_enabled": True,
    "retry_hotkey": "Double Alt",
    "toggle_mode_hotkey": "Alt+C",
    "adaptive_wait_enabled": True,
    "natural_speech_speed": engine.DEFAULT_NATURAL_SPEECH_SPEED,
    "auto_speech_preference": engine.DEFAULT_AUTO_SPEECH_PREFERENCE,
    "enabled_apps": DEFAULT_ENABLED,
    "custom_common_apps": [],
    "app_names": {},
    "app_paths": {},
    "app_recency": {},
    "capture_diagnostics": {},
    "adaptive_timings": {},
    "other_expanded": False,
    "tray_tip_seen": False,
}


def hotkey_text_for_display(value: object) -> str:
    """Return the compact user-facing label for a stored hot-key value."""

    text = str(value or "").strip()
    if text.casefold().replace(" ", "") in {"doublealt", "双击alt"}:
        return "双击 Alt"
    return text


def hotkey_text_for_storage(value: object) -> str:
    """Translate the localized double-Alt label back to its portable value."""

    text = str(value or "").strip()
    if text.casefold().replace(" ", "") in {"doublealt", "双击alt"}:
        return "Double Alt"
    return text


def log(message: str) -> None:
    engine.log(message)


def set_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def windows_mouse_gesture_thresholds() -> tuple[float, int, int, int, int]:
    """Return Windows' current double-click and drag accessibility settings."""

    fallback = (0.46, 7, 7, 4, 4)
    try:
        user32 = ctypes.windll.user32
        interval = max(0.1, min(float(user32.GetDoubleClickTime()) / 1000.0, 2.0))
        double_width = max(2, int(user32.GetSystemMetrics(36)))
        double_height = max(2, int(user32.GetSystemMetrics(37)))
        drag_width = max(2, int(user32.GetSystemMetrics(68)))
        drag_height = max(2, int(user32.GetSystemMetrics(69)))
        return (
            interval,
            max(1, double_width // 2),
            max(1, double_height // 2),
            max(1, drag_width // 2),
            max(1, drag_height // 2),
        )
    except Exception:
        return fallback


def app_icon_path(extension: str) -> Path:
    return engine.resource_path(f"app_icon.{extension}")


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def fit_tk_window_to_work_area(
    window: tk.Misc,
    work_area: WorkArea,
    preferred_client_width: int,
    preferred_client_height: int,
    minimum_client_width: int,
    minimum_client_height: int,
) -> WindowPlacement:
    """Size and place a decorated Tk window without crossing its work area."""

    # Temporarily relax Tk's old fixed minimum so a small/RDP work area can be
    # honoured. The final minimum is restored as far as the monitor permits.
    window.minsize(1, 1)
    window.geometry(
        f"{max(1, int(preferred_client_width))}x{max(1, int(preferred_client_height))}"
    )
    window.update_idletasks()
    inner_hwnd = int(window.winfo_id())
    hwnd = int(ctypes.windll.user32.GetParent(inner_hwnd) or inner_hwnd)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    outer_width = max(1, int(right) - int(left))
    outer_height = max(1, int(bottom) - int(top))
    client_width = max(1, int(window.winfo_width()))
    client_height = max(1, int(window.winfo_height()))
    frame_width = max(0, outer_width - client_width)
    frame_height = max(0, outer_height - client_height)
    available_outer_width = max(1, work_area.width - WINDOW_WORK_MARGIN * 2)
    available_outer_height = max(1, work_area.height - WINDOW_WORK_MARGIN * 2)
    target_client_width = min(
        max(1, int(preferred_client_width)),
        max(1, available_outer_width - frame_width),
    )
    target_client_height = min(
        max(1, int(preferred_client_height)),
        max(1, available_outer_height - frame_height),
    )
    window.minsize(
        min(max(1, int(minimum_client_width)), target_client_width),
        min(max(1, int(minimum_client_height)), target_client_height),
    )
    window.geometry(f"{target_client_width}x{target_client_height}")
    window.update_idletasks()
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    placement = position_window_in_work_area(
        max(1, int(right) - int(left)),
        max(1, int(bottom) - int(top)),
        work_area,
    )
    win32gui.SetWindowPos(
        hwnd,
        0,
        placement.x,
        placement.y,
        placement.width,
        placement.height,
        win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
    )
    return placement


class SettingsStore:
    def __init__(self) -> None:
        self.path = engine.SETTINGS_PATH
        self.lock = threading.RLock()
        self._deferred_dirty = False
        self._deferred_timer: threading.Timer | None = None
        self.load_warning = ""
        self.data = self._load()

    def _preserve_corrupt_settings(self) -> str:
        """Copy an unreadable settings file aside without destroying evidence."""

        try:
            source = Path(self.path)
            if not source.is_file():
                return ""
            stamp = time.strftime("%Y%m%d-%H%M%S")
            candidate = source.with_name(f"{source.name}.corrupt-{stamp}")
            suffix = 1
            while candidate.exists():
                candidate = source.with_name(
                    f"{source.name}.corrupt-{stamp}-{suffix}"
                )
                suffix += 1
            shutil.copy2(source, candidate)
            return str(candidate)
        except (OSError, TypeError, ValueError) as exc:
            log(f"Could not preserve damaged settings: {exc}")
            return ""

    @staticmethod
    def _merge_saved_settings(
        values: dict[str, object], saved: dict[str, object]
    ) -> None:
        values.update(saved)
        if "hotkeys_enabled" not in saved:
            values["hotkeys_enabled"] = bool(
                saved.get("double_alt_retry_enabled", True)
            )
        if "retry_hotkey" not in saved:
            values["retry_hotkey"] = "Double Alt"
        if "toggle_mode_hotkey" not in saved:
            values["toggle_mode_hotkey"] = "Alt+C"
        for key in (
            "enabled_apps",
            "app_names",
            "app_paths",
            "app_recency",
            "capture_diagnostics",
            "adaptive_timings",
        ):
            merged = dict(DEFAULT_SETTINGS.get(key, {}))
            incoming = saved.get(key, {})
            if isinstance(incoming, dict):
                merged.update(incoming)
            values[key] = merged

    def _load(self) -> dict[str, object]:
        values = json.loads(json.dumps(DEFAULT_SETTINGS, ensure_ascii=False))
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("设置根节点不是对象")
            self._merge_saved_settings(values, saved)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            backup = self._preserve_corrupt_settings()
            recovered = False
            try:
                path = Path(self.path)
                tmp_path = path.with_suffix(path.suffix + ".tmp")
                tmp_saved = json.loads(tmp_path.read_text(encoding="utf-8"))
                if not isinstance(tmp_saved, dict):
                    raise ValueError("临时设置根节点不是对象")
                self._merge_saved_settings(values, tmp_saved)
                recovered = True
                try:
                    atomic_write_json(path, tmp_saved)
                except OSError as restore_exc:
                    log(f"Could not persist recovered settings: {restore_exc}")
            except (OSError, ValueError, json.JSONDecodeError, TypeError):
                recovered = False
            if recovered:
                suffix = f"；损坏文件已备份到 {backup}" if backup else ""
                self.load_warning = f"检测到上次设置写入中断，已恢复临时设置{suffix}"
                log(f"Recovered settings from temporary file after: {exc}")
                values.pop("double_alt_retry_enabled", None)
                if values.get("display_mode") not in {"mini", "panel"}:
                    values["display_mode"] = "mini"
                if values.get("natural_speech_speed") not in engine.NATURAL_SPEECH_SPEEDS:
                    values["natural_speech_speed"] = engine.DEFAULT_NATURAL_SPEECH_SPEED
                values["auto_speech_preference"] = engine.normalize_auto_speech_preference(
                    values.get("auto_speech_preference")
                )
                return values
            # A damaged or unreadable permission file must not silently
            # re-enable every application.  Start paused and let the user
            # review settings in a visible window.
            values["desktop_enabled"] = False
            values["enabled_apps"] = {
                exe: False for exe, _name in COMMON_APPS
            }
            suffix = f"；原文件已备份到 {backup}" if backup else ""
            self.load_warning = f"设置文件无法读取，已安全暂停划词翻译{suffix}"
            log(f"Settings load error: {exc}{suffix}")
        values.pop("double_alt_retry_enabled", None)
        if values.get("display_mode") not in {"mini", "panel"}:
            values["display_mode"] = "mini"
        if values.get("natural_speech_speed") not in engine.NATURAL_SPEECH_SPEEDS:
            values["natural_speech_speed"] = engine.DEFAULT_NATURAL_SPEECH_SPEED
        values["auto_speech_preference"] = engine.normalize_auto_speech_preference(
            values.get("auto_speech_preference")
        )
        return values

    def save(self) -> bool:
        with self.lock:
            try:
                atomic_write_json(self.path, self.data)
                self._deferred_dirty = False
                return True
            except OSError as exc:
                log(f"Settings save error: {exc}")
                return False

    def _schedule_deferred_save(self) -> None:
        with self.lock:
            self._deferred_dirty = True
            if self._deferred_timer is not None:
                return
            timer = threading.Timer(2.0, self.flush_pending)
            timer.daemon = True
            self._deferred_timer = timer
            timer.start()

    def flush_pending(self) -> None:
        with self.lock:
            timer = self._deferred_timer
            self._deferred_timer = None
            if timer is not None and timer is not threading.current_thread():
                timer.cancel()
            if self._deferred_dirty:
                self.save()

    def get(self, key: str, default: object = None) -> object:
        with self.lock:
            return self.data.get(key, default)

    def set(self, key: str, value: object, save: bool = True) -> bool:
        with self.lock:
            previous = self.data.get(key)
            existed = key in self.data
            self.data[key] = value
            if save and not self.save():
                if existed:
                    self.data[key] = previous
                else:
                    self.data.pop(key, None)
                return False
            return True

    def enabled_apps(self) -> dict[str, bool]:
        with self.lock:
            values = self.data.get("enabled_apps", {})
            if not isinstance(values, dict):
                return {}
            return {
                str(exe).lower(): bool(enabled)
                for exe, enabled in values.items()
                if str(exe).lower() not in UNSUPPORTED_APPS
            }

    def is_app_enabled(self, exe: str) -> bool:
        exe = exe.lower()
        if exe in UNSUPPORTED_APPS:
            return False
        return bool(self.enabled_apps().get(exe, False))

    def set_app(
        self,
        exe: str,
        enabled: bool,
        *,
        name: str = "",
        path: str = "",
        touch: bool = True,
        save: bool = True,
    ) -> bool:
        exe = exe.lower()
        if exe in UNSUPPORTED_APPS:
            return False
        with self.lock:
            previous = json.loads(json.dumps(self.data, ensure_ascii=False)) if save else None
            enabled_apps = self.data.setdefault("enabled_apps", {})
            if isinstance(enabled_apps, dict):
                enabled_apps[exe] = bool(enabled)
            if name:
                names = self.data.setdefault("app_names", {})
                if isinstance(names, dict):
                    names[exe] = name
            if path:
                paths = self.data.setdefault("app_paths", {})
                if isinstance(paths, dict):
                    paths[exe] = path
            if touch:
                recency = self.data.setdefault("app_recency", {})
                if isinstance(recency, dict):
                    recency[exe] = time.time()
            if save and not self.save():
                assert previous is not None
                self.data = previous
                return False
            return True

    def set_apps_bulk(self, exes: list[str], enabled: bool) -> bool:
        with self.lock:
            previous = json.loads(json.dumps(self.data, ensure_ascii=False))
            now = time.time()
            for index, exe in enumerate(exes):
                self.set_app(exe, enabled, touch=False, save=False)
                recency = self.data.setdefault("app_recency", {})
                if isinstance(recency, dict):
                    recency[exe.lower()] = now - index * 0.001
            if not self.save():
                self.data = previous
                return False
            return True

    def custom_common(self) -> list[str]:
        with self.lock:
            values = self.data.get("custom_common_apps", [])
            if not isinstance(values, list):
                return []
            return [
                str(item).lower()
                for item in values
                if str(item).lower() not in UNSUPPORTED_APPS
            ]

    def set_common(self, exe: str, common: bool) -> bool:
        exe = exe.lower()
        if exe in UNSUPPORTED_APPS:
            return False
        base = {item for item, _name in COMMON_APPS}
        with self.lock:
            previous = json.loads(json.dumps(self.data, ensure_ascii=False))
            values = self.custom_common()
            if common and exe not in base and exe not in values:
                values.append(exe)
            if not common and exe in values:
                values.remove(exe)
            self.data["custom_common_apps"] = values
            recency = self.data.setdefault("app_recency", {})
            if isinstance(recency, dict):
                recency[exe] = time.time()
            if not self.save():
                self.data = previous
                return False
            return True

    def app_name(self, exe: str) -> str:
        exe = exe.lower()
        if exe in FRIENDLY_NAMES:
            return FRIENDLY_NAMES[exe]
        names = self.data.get("app_names", {})
        if isinstance(names, dict) and names.get(exe):
            return str(names[exe])
        return Path(exe).stem.replace("_", " ").title()

    def app_path(self, exe: str) -> str:
        paths = self.data.get("app_paths", {})
        return str(paths.get(exe.lower(), "")) if isinstance(paths, dict) else ""

    def recency(self, exe: str) -> float:
        values = self.data.get("app_recency", {})
        if isinstance(values, dict):
            try:
                return float(values.get(exe.lower(), 0.0))
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def capture_timing(self, exe: str) -> CaptureTiming:
        base = timing_for_app(exe)
        if not bool(self.get("adaptive_wait_enabled", True)):
            return base
        with self.lock:
            values = self.data.get("adaptive_timings", {})
            entry = values.get(exe.lower()) if isinstance(values, dict) else None
        state = adaptive_wait_from_mapping(entry, base.clipboard_timeout_seconds)
        return CaptureTiming(base.settle_seconds, state.timeout_seconds)

    def record_capture_diagnostic(
        self,
        exe: str,
        *,
        status: str,
        method: str,
        reason: str,
        elapsed_ms: int,
    ) -> None:
        exe = exe.lower()
        now = time.time()
        with self.lock:
            values = self.data.setdefault("capture_diagnostics", {})
            if not isinstance(values, dict):
                values = {}
                self.data["capture_diagnostics"] = values
            previous = values.get(exe, {})
            previous_failures = 0
            if isinstance(previous, dict):
                try:
                    previous_failures = int(previous.get("consecutive_failures", 0))
                except (TypeError, ValueError):
                    previous_failures = 0
            if status == "success":
                failures = 0
            elif status == "failed":
                failures = previous_failures + 1
            else:
                failures = previous_failures
            values[exe] = {
                "status": status,
                "method": method,
                "reason": reason,
                "elapsed_ms": max(0, int(elapsed_ms)),
                "updated_at": now,
                "consecutive_failures": failures,
            }
            if len(values) > 100:
                def updated_at(key: object) -> float:
                    value = values.get(key, {})
                    if not isinstance(value, dict):
                        return 0.0
                    try:
                        return float(value.get("updated_at", 0.0))
                    except (TypeError, ValueError, OverflowError):
                        return 0.0

                oldest = sorted(
                    values,
                    key=updated_at,
                )
                for key in oldest[: len(values) - 100]:
                    values.pop(key, None)

            if (
                bool(self.data.get("adaptive_wait_enabled", True))
                and method == "clipboard"
                and reason in {"captured", "no_clipboard_change"}
            ):
                adaptive = self.data.setdefault("adaptive_timings", {})
                if not isinstance(adaptive, dict):
                    adaptive = {}
                    self.data["adaptive_timings"] = adaptive
                base = timing_for_app(exe).clipboard_timeout_seconds
                state = adaptive_wait_from_mapping(adaptive.get(exe), base)
                adaptive[exe] = learn_adaptive_wait(
                    state,
                    reason=reason,
                    elapsed_ms=elapsed_ms,
                    minimum_timeout_seconds=base,
                ).to_mapping()
        self._schedule_deferred_save()

    def capture_diagnostic(self, exe: str) -> dict[str, object]:
        with self.lock:
            values = self.data.get("capture_diagnostics", {})
            value = values.get(exe.lower(), {}) if isinstance(values, dict) else {}
            return dict(value) if isinstance(value, dict) else {}

    def reset_adaptive_waits(self) -> None:
        with self.lock:
            self.data["adaptive_timings"] = {}
            self.save()


@dataclass(slots=True)
class AppInfo:
    exe: str
    name: str
    path: str = ""
    title: str = ""
    hwnd: int = 0
    process_id: int = 0


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    x: int
    y: int
    interaction_id: int
    origin: str = "mouse"
    input_generation: int = 0
    auto_speak_accent: str | None = None


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    text: str
    token: int


@dataclass(frozen=True, slots=True)
class SelectionProbeResult:
    text: str = ""
    method: str = ""
    protected: bool = False


def process_path(process_id: int) -> str:
    process = _OPEN_PROCESS(0x1000, False, process_id)
    if not process:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if _QUERY_FULL_PROCESS_IMAGE_NAME(process, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        _CLOSE_HANDLE(process)


def normalized_process_path(path: str) -> str:
    """Normalize an executable path for case-insensitive Windows comparison."""

    if not path:
        return ""
    return os.path.normcase(os.path.normpath(path.strip()))


def clipboard_owner_matches_app(
    app_info: AppInfo,
    *,
    owner_hwnd: int | None = None,
    get_window_process_id: Callable[[int], tuple[int, int]] = win32process.GetWindowThreadProcessId,
    get_process_path: Callable[[int], str] = process_path,
) -> bool | None:
    """Accept a clipboard owner only when it is the selected app process."""

    try:
        owner = int(owner_hwnd if owner_hwnd is not None else _GET_CLIPBOARD_OWNER())
        if not owner or not app_info.process_id:
            return None
        _thread_id, owner_process_id = get_window_process_id(owner)
        if int(owner_process_id) == app_info.process_id:
            return True
        owner_path = get_process_path(int(owner_process_id))
        if not owner_path or not app_info.path:
            return None
        return normalized_process_path(owner_path) == normalized_process_path(app_info.path)
    except Exception:
        return None


def app_info_from_hwnd(hwnd: int, store: SettingsStore) -> AppInfo | None:
    if not hwnd:
        return None
    try:
        _thread_id, process_id = win32process.GetWindowThreadProcessId(hwnd)
        path = process_path(process_id)
        exe = Path(path).name.lower() if path else ""
        if not exe or exe in IGNORED_APPS:
            return None
        title = win32gui.GetWindowText(hwnd).strip()
        name = store.app_name(exe)
        return AppInfo(
            exe=exe,
            name=name,
            path=path,
            title=title,
            hwnd=int(hwnd),
            process_id=int(process_id),
        )
    except Exception:
        return None


def foreground_app(store: SettingsStore) -> AppInfo | None:
    return app_info_from_hwnd(win32gui.GetForegroundWindow(), store)


def discover_visible_apps(store: SettingsStore) -> dict[str, AppInfo]:
    found: dict[str, AppInfo] = {}

    def visit(hwnd: int, _extra: object) -> None:
        try:
            if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd).strip():
                return
            info = app_info_from_hwnd(hwnd, store)
            if info and info.exe not in found:
                found[info.exe] = info
        except Exception:
            return

    win32gui.EnumWindows(visit, None)
    return found


def read_clipboard_text() -> tuple[str | None, list[int], bool]:
    formats: list[int] = []
    text: str | None = None
    for _attempt in range(6):
        try:
            win32clipboard.OpenClipboard()
            try:
                current = 0
                while True:
                    current = win32clipboard.EnumClipboardFormats(current)
                    if not current:
                        break
                    formats.append(current)
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    handle = win32clipboard.GetClipboardDataHandle(
                        win32con.CF_UNICODETEXT
                    )
                    byte_size = int(_GLOBAL_SIZE(handle)) if handle else 0
                    # CF_UNICODETEXT is UTF-16 plus a trailing NUL.  A Python
                    # character can occupy one or two UTF-16 code units (for
                    # example emoji), and GlobalAlloc may add small padding.
                    # Keep enough headroom for 3000 valid characters while
                    # refusing genuinely huge values before materialisation.
                    max_bytes = MAX_SELECTION_LENGTH * 4 + 64
                    if 0 < byte_size <= max_bytes:
                        value = win32clipboard.GetClipboardData(
                            win32con.CF_UNICODETEXT
                        )
                        if isinstance(value, str):
                            text = value
            finally:
                win32clipboard.CloseClipboard()
            return text, formats, True
        except Exception:
            formats.clear()
            time.sleep(0.02)
    return None, formats, False


def clipboard_sequence_number() -> int:
    return int(_GET_CLIPBOARD_SEQUENCE_NUMBER())


def restore_clipboard_text_if_unchanged(text: str | None, expected_sequence: int) -> str:
    if expected_sequence <= 0:
        return "failed"
    for _attempt in range(6):
        opened = False
        clipboard_mutated = False
        try:
            win32clipboard.OpenClipboard()
            opened = True
            if clipboard_sequence_number() != expected_sequence:
                return "preserved"
            win32clipboard.EmptyClipboard()
            clipboard_mutated = True
            if text is not None:
                win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
            return "restored"
        except Exception:
            # Once EmptyClipboard succeeds there is no safe retry: its sequence
            # number has changed and the previous data may already be gone.
            if clipboard_mutated:
                return "failed"
            time.sleep(0.02)
        finally:
            if opened:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
    return "failed"


# Formats backed by native GDI/metafile handles cannot be copied as bytes. A
# DIB/DIBV5 is a portable replacement for bitmap/palette display handles.
NON_PORTABLE_HANDLE_FORMATS = frozenset({2, 3, 9, 14, 0x0082, 0x0083, 0x008E})
BITMAP_HANDLE_FORMATS = frozenset({2, 9, 0x0082})
PORTABLE_BITMAP_FORMATS = frozenset({8, 17})
MAX_PORTABLE_CLIPBOARD_BYTES = 64 * 1024 * 1024
MAX_PORTABLE_CLIPBOARD_FORMATS = 128
PORTABLE_PRIMARY_FORMATS = frozenset({1, 7, 8, 13, 15, 17})
PORTABLE_PRIMARY_NAMES = frozenset(
    {"html format", "rich text format", "rich text format without objects", "png", "jfif", "image/png"}
)
SAFE_STANDARD_HGLOBAL_FORMATS = frozenset({1, 7, 8, 13, 15, 16, 17})
SAFE_REGISTERED_HGLOBAL_NAMES = PORTABLE_PRIMARY_NAMES | frozenset(
    {
        "csv",
        # Chromium adds these small HGLOBAL metadata records beside copied
        # HTML/text. They contain only source identity/URL metadata, have a
        # finite GlobalSize, and can be restored byte-for-byte like HTML.
        "chromium internal source rfh token",
        "chromium internal source url",
        "filename",
        "filenamew",
        "mozilla url",
        "paste succeeded",
        "performed dropeffect",
        "preferred dropeffect",
        "shell idlist array",
        "text/html",
        "text/x-moz-url",
        "uniformresourcelocator",
        "uniformresourcelocatorw",
    }
)
VIRTUAL_FILE_FORMAT_PREFIXES = ("filecontents", "filegroupdescriptor")


@dataclass(slots=True)
class PortableClipboardSnapshot:
    """A managed byte/string snapshot; never retains raw OLE or GDI pointers."""

    entries: list[tuple[int, str | bytes]]

    def restore(self, expected_sequence: int) -> str:
        if expected_sequence <= 0:
            return "failed"
        for _attempt in range(6):
            opened = False
            clipboard_mutated = False
            try:
                win32clipboard.OpenClipboard()
                opened = True
                if clipboard_sequence_number() != expected_sequence:
                    return "preserved"
                win32clipboard.EmptyClipboard()
                clipboard_mutated = True
                for format_id, payload in self.entries:
                    win32clipboard.SetClipboardData(format_id, payload)
                return "restored"
            except Exception:
                if clipboard_mutated:
                    return "failed"
                time.sleep(0.02)
            finally:
                if opened:
                    try:
                        win32clipboard.CloseClipboard()
                    except Exception:
                        pass
        return "failed"

    def close(self) -> None:
        self.entries.clear()


@dataclass(frozen=True, slots=True)
class PortableClipboardState:
    text: str | None
    formats: list[int]
    known: bool
    sequence: int
    snapshot: PortableClipboardSnapshot | None


def capture_portable_clipboard_state() -> PortableClipboardState:
    """Materialize safe clipboard formats and their final sequence together."""

    last_sequence = clipboard_sequence_number()
    if last_sequence <= 0:
        return PortableClipboardState(None, [], False, last_sequence, None)
    for _attempt in range(6):
        before = clipboard_sequence_number()
        if before <= 0:
            return PortableClipboardState(None, [], False, before, None)
        opened = False
        formats: list[int] = []
        format_names: dict[int, str] = {}
        entries: list[tuple[int, str | bytes]] = []
        unsupported: set[int] = set()
        text: str | None = None
        total_bytes = 0
        too_large = False
        too_many_formats = False
        read_succeeded = False
        try:
            win32clipboard.OpenClipboard()
            opened = True
            current = 0
            while True:
                current = win32clipboard.EnumClipboardFormats(current)
                if not current:
                    break
                format_id = int(current)
                formats.append(format_id)
                if len(formats) > MAX_PORTABLE_CLIPBOARD_FORMATS:
                    too_many_formats = True
                    break
                if format_id >= 0xC000:
                    try:
                        format_names[format_id] = str(
                            win32clipboard.GetClipboardFormatName(format_id)
                        ).casefold()
                    except Exception:
                        format_names[format_id] = ""

            has_hdrop = win32con.CF_HDROP in formats
            has_virtual_files = any(
                name.startswith(VIRTUAL_FILE_FORMAT_PREFIXES)
                for name in format_names.values()
            )
            if has_virtual_files and not has_hdrop:
                unsupported.update(formats)
            elif not too_many_formats:
                has_portable_bitmap = bool(
                    set(formats) & PORTABLE_BITMAP_FORMATS
                )
                has_dibv5 = win32con.CF_DIBV5 in formats
                for format_id in formats:
                    name = format_names.get(format_id, "")
                    if name.startswith(VIRTUAL_FILE_FORMAT_PREFIXES):
                        if has_hdrop:
                            # A real CF_HDROP list fully represents the same
                            # files without retaining delayed COM streams.
                            continue
                        unsupported.add(format_id)
                        continue
                    if format_id in BITMAP_HANDLE_FORMATS and has_portable_bitmap:
                        continue
                    if format_id == win32con.CF_DIB and has_dibv5:
                        # DIBV5 is the self-contained superset; keeping both
                        # needlessly doubles large screenshot memory.
                        continue
                    if format_id in NON_PORTABLE_HANDLE_FORMATS:
                        unsupported.add(format_id)
                        continue
                    if format_id < 0xC000:
                        portable_format = format_id in SAFE_STANDARD_HGLOBAL_FORMATS
                    else:
                        portable_format = name in SAFE_REGISTERED_HGLOBAL_NAMES
                    if not portable_format:
                        unsupported.add(format_id)
                        continue
                    try:
                        handle = win32clipboard.GetClipboardDataHandle(format_id)
                        if not handle:
                            raise ValueError("clipboard format has no global-memory handle")
                        declared_size = int(_GLOBAL_SIZE(handle))
                        if declared_size <= 0:
                            raise ValueError("clipboard format has an invalid global-memory size")
                        if total_bytes + declared_size > MAX_PORTABLE_CLIPBOARD_BYTES:
                            too_large = True
                            break
                        if format_id == win32con.CF_UNICODETEXT:
                            value = win32clipboard.GetClipboardData(format_id)
                            if not isinstance(value, str):
                                raise TypeError("Unicode clipboard data is not text")
                            payload: str | bytes = value
                            text = value
                            payload_size = len(value.encode("utf-16-le")) + 2
                        else:
                            payload = bytes(win32clipboard.GetGlobalMemory(handle))
                            payload_size = len(payload)
                        total_bytes += payload_size
                        if total_bytes > MAX_PORTABLE_CLIPBOARD_BYTES:
                            too_large = True
                            break
                        entries.append((format_id, payload))
                    except Exception:
                        unsupported.add(format_id)
            read_succeeded = True
        except Exception:
            formats = []
            entries = []
        finally:
            if opened:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass

        after = clipboard_sequence_number()
        last_sequence = after
        if after <= 0:
            return PortableClipboardState(None, [], False, after, None)
        if not read_succeeded:
            time.sleep(0.02)
            continue
        if before != after:
            time.sleep(0.02)
            continue
        if not formats:
            return PortableClipboardState(None, [], True, after, None)

        captured_formats = {format_id for format_id, _payload in entries}
        captured_names = {format_names.get(format_id, "") for format_id in captured_formats}
        has_primary = bool(
            captured_formats & PORTABLE_PRIMARY_FORMATS
            or captured_names & PORTABLE_PRIMARY_NAMES
        )
        snapshot = None
        if (
            entries
            and has_primary
            and not too_large
            and not too_many_formats
            and not unsupported
        ):
            snapshot = PortableClipboardSnapshot(entries)
        return PortableClipboardState(text, formats, True, after, snapshot)
    return PortableClipboardState(None, [], False, last_sequence, None)


class DesktopSelectionWatcher:
    DIRECT_CLIPBOARD_APPS = frozenset({"qq.exe"})
    PDF_READER_APPS = frozenset(
        {
            "pdf.exe",
            "acrord32.exe",
            "acrobat.exe",
            "foxitpdfreader.exe",
            "pdfxcview.exe",
            "pdfxedit.exe",
        }
    )
    BROWSER_APPS = frozenset({"chrome.exe", "msedge.exe", "firefox.exe"})
    DEEP_UIA_APPS = frozenset(
        {
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
            "chrome.exe",
            "msedge.exe",
            "firefox.exe",
            "acrord32.exe",
            "acrobat.exe",
            "foxitpdfreader.exe",
            "pdfxcview.exe",
            "pdfxedit.exe",
        }
    )
    DOUBLE_ALT_INTERVAL_SECONDS = DOUBLE_ALT_TAP_INTERVAL_SECONDS
    DOUBLE_ALT_MAX_HOLD_SECONDS = DOUBLE_ALT_TAP_MAX_HOLD_SECONDS
    NATIVE_HOTKEY_RELEASE_TIMEOUT_SECONDS = 0.65
    NATIVE_HOTKEY_RELEASE_POLL_SECONDS = 0.015

    def __init__(
        self,
        store: SettingsStore,
        selection_callback: Callable[
            [str, AppInfo, int, int, str | None, tuple[int, int]], None
        ],
        status_callback: Callable[[str], None],
        outside_click_callback: Callable[[int, int], None],
        diagnostic_callback: Callable[[str], None] | None = None,
        capture_started_callback: Callable[[tuple[int, int]], None] | None = None,
    ) -> None:
        self.store = store
        self.selection_callback = selection_callback
        self.status_callback = status_callback
        self.outside_click_callback = outside_click_callback
        self.diagnostic_callback = diagnostic_callback or (lambda _exe: None)
        self.capture_started_callback = capture_started_callback or (lambda _identity: None)
        self.stop_event = threading.Event()
        self.stopped_event = threading.Event()
        # SimpleQueue.put() never waits. The capture worker drains a burst and
        # processes only its newest request, so the global input hook stays fast.
        self.events: queue.SimpleQueue[CaptureRequest | None] = queue.SimpleQueue()
        self.state_lock = threading.Lock()
        self.listener: mouse.Listener | None = None
        self.keyboard_listener: keyboard.Listener | None = None
        self.worker: threading.Thread | None = None
        self.press: tuple[int, int, float] | None = None
        self.last_release: tuple[int, int, float] | None = None
        self.last_emitted: tuple[str, str, float] = ("", "", 0.0)
        self.interaction_id = 0
        self.input_generation = 0
        self.keys_down: set[str] = set()
        self.copy_intent_generation = 0
        self.context_menu_armed_until = 0.0
        self.hotkey_requests_enabled = False
        self.double_alt_enabled = False
        self.double_alt_epoch = 0
        self._alt_down_since: float | None = None
        self._alt_press_dirty = False
        self._last_clean_alt_release = 0.0

    def start(self) -> None:
        self.stopped_event.clear()
        self.worker = threading.Thread(target=self._capture_loop, daemon=True, name="SelectionCapture")
        self.worker.start()
        # Never swallow clicks or keyboard input from the foreground app.  In
        # particular, a user's physical Ctrl+C must continue to reach it.
        self.listener = mouse.Listener(
            on_click=self._on_click,
            on_scroll=self._on_scroll,
            suppress=False,
        )
        self.listener.daemon = True
        self.listener.start()
        self.keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
            win32_event_filter=self._keyboard_event_filter,
            suppress=False,
        )
        self.keyboard_listener.daemon = True
        self.keyboard_listener.start()

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        with self.state_lock:
            self.interaction_id += 1
            self.double_alt_epoch = getattr(self, "double_alt_epoch", 0) + 1
            self._reset_double_alt_state_locked()
        if self.listener:
            self.listener.stop()
        if self.keyboard_listener:
            self.keyboard_listener.stop()
        self.events.put(None)
        if self.worker is None:
            self.stopped_event.set()

    @staticmethod
    def _keyboard_event_filter(_message: int, data: object) -> bool:
        # Pynput Controller/SendInput events carry one of these flags. Returning
        # False skips only our listener callbacks; the key still reaches the app.
        return not bool(int(getattr(data, "flags", 0)) & 0x12)

    def _input_generation_is_current(self, expected: int) -> bool:
        with self.state_lock:
            return self.input_generation == expected

    def _ensure_double_alt_state_locked(self) -> None:
        """Lazily initialise gesture fields for old settings and test doubles."""

        if not hasattr(self, "double_alt_enabled"):
            self.double_alt_enabled = False
        if not hasattr(self, "double_alt_epoch"):
            self.double_alt_epoch = 0
        if not hasattr(self, "_alt_down_since"):
            self._alt_down_since = None
        if not hasattr(self, "_alt_press_dirty"):
            self._alt_press_dirty = False
        if not hasattr(self, "_last_clean_alt_release"):
            self._last_clean_alt_release = 0.0

    def _reset_double_alt_state_locked(self) -> None:
        self._ensure_double_alt_state_locked()
        self._alt_down_since = None
        self._alt_press_dirty = False
        self._last_clean_alt_release = 0.0

    def set_double_alt_enabled(self, enabled: bool) -> None:
        """Enable the delegated gesture and invalidate every partial tap."""

        with self.state_lock:
            self._ensure_double_alt_state_locked()
            self.double_alt_enabled = bool(enabled)
            self.double_alt_epoch += 1
            self._reset_double_alt_state_locked()

    def set_hotkey_requests_enabled(self, enabled: bool) -> None:
        """Enable hot-key work and invalidate requests queued before a change."""

        with self.state_lock:
            enabled = bool(enabled)
            if bool(getattr(self, "hotkey_requests_enabled", True)) == enabled:
                return
            self.hotkey_requests_enabled = enabled
            self.interaction_id = getattr(self, "interaction_id", 0) + 1

    @staticmethod
    def _is_double_alt_key(key: object, key_name: str) -> bool:
        # On Windows pynput aliases right Alt and AltGr. Restricting this
        # gesture to the ordinary left Alt prevents AltGr text input from ever
        # becoming a pronunciation command.
        try:
            if key in {keyboard.Key.alt, keyboard.Key.alt_l}:
                return True
        except Exception:
            pass
        return key_name in {"alt", "alt_l"}

    @staticmethod
    def _key_token(key: object) -> str:
        try:
            if key in {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}:
                return "ctrl"
        except Exception:
            pass
        char = getattr(key, "char", None)
        if isinstance(char, str) and char:
            return char.casefold()
        value = str(key).strip("'\"").casefold()
        if value.startswith("key."):
            value = value[4:]
        if value in {"ctrl_l", "ctrl_r"}:
            return "ctrl"
        return value

    def _on_key_press(self, key: object) -> None:
        try:
            if self.stop_event.is_set():
                return
            key_name = self._key_token(key)
            is_double_alt = self._is_double_alt_key(key, key_name)
            identity: tuple[int, int] | None = None
            now = time.monotonic()
            with self.state_lock:
                self._ensure_double_alt_state_locked()
                if key_name in self.keys_down:
                    if is_double_alt:
                        # A repeated key-down means the key was held, not
                        # lightly tapped. It must not count toward the gesture.
                        self._alt_press_dirty = True
                        self._last_clean_alt_release = 0.0
                else:
                    existing_keys = set(self.keys_down)
                    self.keys_down.add(key_name)
                    self.input_generation += 1
                    physical_copy = (
                        (key_name == "c" and "ctrl" in existing_keys)
                        or (key_name == "ctrl" and "c" in existing_keys)
                    )
                    menu_is_armed = now <= getattr(self, "context_menu_armed_until", 0.0)
                    menu_copy = menu_is_armed and key_name in {"c", "enter"}
                    if physical_copy or menu_copy:
                        self.copy_intent_generation = (
                            getattr(self, "copy_intent_generation", 0) + 1
                        )
                        self.context_menu_armed_until = 0.0
                    elif key_name == "esc":
                        self.context_menu_armed_until = 0.0

                    if not self.double_alt_enabled:
                        self._reset_double_alt_state_locked()
                    elif is_double_alt:
                        if (
                            self._last_clean_alt_release
                            and now - self._last_clean_alt_release
                            > self.DOUBLE_ALT_INTERVAL_SECONDS
                        ):
                            self._last_clean_alt_release = 0.0
                        self._alt_down_since = now
                        self._alt_press_dirty = bool(existing_keys)
                        if existing_keys:
                            self._last_clean_alt_release = 0.0
                    else:
                        # Any key between the two Alt taps breaks the gesture;
                        # a key pressed while Alt is down also dirties that tap.
                        self._last_clean_alt_release = 0.0
                        if self._alt_down_since is not None:
                            self._alt_press_dirty = True
                    identity = (self.interaction_id, self.input_generation)
            if identity is not None:
                self._notify_capture_context(identity)
        except Exception:
            return

    def _on_key_release(self, key: object) -> None:
        try:
            key_name = self._key_token(key)
            is_double_alt = self._is_double_alt_key(key, key_name)
            should_capture = False
            gesture_epoch = -1
            now = time.monotonic()
            with self.state_lock:
                self._ensure_double_alt_state_locked()
                was_down = key_name in self.keys_down
                self.keys_down.discard(key_name)
                if is_double_alt and was_down and self._alt_down_since is not None:
                    held_for = max(0.0, now - self._alt_down_since)
                    clean_tap = (
                        self.double_alt_enabled
                        and not self._alt_press_dirty
                        and not self.keys_down
                        and held_for <= self.DOUBLE_ALT_MAX_HOLD_SECONDS
                    )
                    self._alt_down_since = None
                    self._alt_press_dirty = False
                    if clean_tap:
                        previous = self._last_clean_alt_release
                        if (
                            previous
                            and 0.0 <= now - previous
                            <= self.DOUBLE_ALT_INTERVAL_SECONDS
                        ):
                            self._last_clean_alt_release = 0.0
                            should_capture = True
                            gesture_epoch = self.double_alt_epoch
                        else:
                            self._last_clean_alt_release = now
                    else:
                        self._last_clean_alt_release = 0.0
            if should_capture and not self.stop_event.is_set():
                self._request_double_alt_capture(gesture_epoch)
        except Exception:
            return

    def _request_double_alt_capture(self, expected_epoch: int) -> None:
        """Atomically turn one validated gesture into a US-speech request."""

        if self.stop_event.is_set():
            return
        with self.state_lock:
            self._ensure_double_alt_state_locked()
            if (
                not self.double_alt_enabled
                or not bool(getattr(self, "hotkey_requests_enabled", True))
                or self.double_alt_epoch != expected_epoch
                or self.stop_event.is_set()
            ):
                return
            self.interaction_id += 1
            interaction_id = self.interaction_id
            input_generation = self.input_generation
        self._queue_capture(
            CaptureRequest(
                -1,
                -1,
                interaction_id,
                "hotkey",
                input_generation,
                "us",
            )
        )

    def request_manual_capture(self, auto_speak_accent: str | None = None) -> None:
        if self.stop_event.is_set():
            return
        with self.state_lock:
            if not bool(getattr(self, "hotkey_requests_enabled", True)):
                return
            self.interaction_id += 1
            interaction_id = self.interaction_id
            input_generation = self.input_generation
        self._queue_capture(
            CaptureRequest(
                -1,
                -1,
                interaction_id,
                "hotkey",
                input_generation,
                auto_speak_accent,
            )
        )

    @staticmethod
    def _native_hotkey_keys_released(primary_virtual_key: int) -> bool:
        """Check physical key state independently of the asynchronous hook queue."""

        # WM_HOTKEY is delivered on key-down. Waiting only for pynput's
        # callbacks is racy because that listener has its own message thread.
        # Query every modifier that could turn our later Ctrl+C into a chord,
        # plus the configured primary key, and fail closed if Windows refuses
        # the query.
        virtual_keys = {0x10, 0x11, 0x12, 0x5B, 0x5C}  # Shift, Ctrl, Alt, L/R Win
        if 0 < int(primary_virtual_key) <= 0xFF:
            virtual_keys.add(int(primary_virtual_key))
        try:
            get_async_key_state = ctypes.windll.user32.GetAsyncKeyState
            return all(
                not (int(get_async_key_state(virtual_key)) & 0x8000)
                for virtual_key in virtual_keys
            )
        except Exception:
            return False

    @staticmethod
    def _physical_modifiers_released() -> bool:
        """Fail closed unless Windows confirms every physical modifier is up."""

        try:
            get_async_key_state = ctypes.windll.user32.GetAsyncKeyState
            return all(
                not (int(get_async_key_state(virtual_key)) & 0x8000)
                for virtual_key in (0x10, 0x11, 0x12, 0x5B, 0x5C)
            )
        except Exception:
            return False

    @staticmethod
    def _modifier_token_is_down(keys_down: set[str]) -> bool:
        return bool(
            set(keys_down)
            & {
                "shift",
                "shift_l",
                "shift_r",
                "ctrl",
                "alt",
                "alt_l",
                "alt_r",
                "alt_gr",
                "cmd",
                "cmd_l",
                "cmd_r",
                "win",
                "win_l",
                "win_r",
            }
        )

    def request_native_hotkey_capture(
        self,
        *,
        primary_virtual_key: int,
        auto_speak_accent: str | None = None,
    ) -> None:
        """Queue a native retry only after its physical chord is released."""

        if self.stop_event.is_set():
            return
        with self.state_lock:
            if not bool(getattr(self, "hotkey_requests_enabled", True)):
                return
            expected_interaction_id = self.interaction_id
        waiter = threading.Thread(
            target=self._wait_for_native_hotkey_release,
            args=(
                int(primary_virtual_key),
                auto_speak_accent,
                expected_interaction_id,
            ),
            daemon=True,
            name="NativeHotkeyRelease",
        )
        waiter.start()

    def _wait_for_native_hotkey_release(
        self,
        primary_virtual_key: int,
        auto_speak_accent: str | None,
        expected_interaction_id: int,
    ) -> None:
        deadline = time.monotonic() + self.NATIVE_HOTKEY_RELEASE_TIMEOUT_SECONDS
        while not self.stop_event.is_set():
            with self.state_lock:
                request_is_current = (
                    bool(getattr(self, "hotkey_requests_enabled", True))
                    and self.interaction_id == expected_interaction_id
                )
                if not request_is_current:
                    return
                hook_keys_released = not bool(getattr(self, "keys_down", set()))
                os_keys_released = (
                    hook_keys_released
                    and self._native_hotkey_keys_released(primary_virtual_key)
                )
                if os_keys_released:
                    self.interaction_id += 1
                    event = CaptureRequest(
                        -1,
                        -1,
                        self.interaction_id,
                        "hotkey",
                        self.input_generation,
                        auto_speak_accent,
                    )
                    break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.status_callback("快捷键按键仍未松开，本次重新获取已取消")
                return
            if self.stop_event.wait(
                min(self.NATIVE_HOTKEY_RELEASE_POLL_SECONDS, remaining)
            ):
                return
        else:
            return
        self._queue_capture(event)

    def _on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        """Windows hook callback: only update tiny in-memory state and enqueue."""
        try:
            if self.stop_event.is_set():
                return
            px, py = int(x), int(y)
            now = time.monotonic()
            if pressed:
                with self.state_lock:
                    self._ensure_double_alt_state_locked()
                    self._last_clean_alt_release = 0.0
                    if self._alt_down_since is not None:
                        self._alt_press_dirty = True
                    self.interaction_id += 1
                    if button == mouse.Button.left:
                        self.press = (px, py, now)
                        if now <= getattr(self, "context_menu_armed_until", 0.0):
                            self.copy_intent_generation = (
                                getattr(self, "copy_intent_generation", 0) + 1
                            )
                        self.context_menu_armed_until = 0.0
                    elif button == mouse.Button.right:
                        self.press = None
                        self.context_menu_armed_until = (
                            now + CONTEXT_MENU_COPY_WINDOW_SECONDS
                        )
                    else:
                        self.press = None
                        self.context_menu_armed_until = 0.0
                    identity = (self.interaction_id, self.input_generation)
                self._notify_capture_context(identity)
                if button == mouse.Button.left:
                    self.outside_click_callback(px, py)
                return

            if button != mouse.Button.left:
                return

            with self.state_lock:
                dragged = bool(
                    self.press
                    and math.hypot(px - self.press[0], py - self.press[1]) >= 4
                )
                double_clicked = bool(
                    self.last_release
                    and now - self.last_release[2] <= 0.46
                    and math.hypot(
                        px - self.last_release[0], py - self.last_release[1]
                    )
                    <= 7
                )
                self.last_release = (px, py, now)
                interaction_id = self.interaction_id
                input_generation = self.input_generation
            if dragged or double_clicked:
                self._queue_capture(
                    CaptureRequest(px, py, interaction_id, "mouse", input_generation)
                )
        except Exception:
            return

    def _on_scroll(self, x: int, y: int, _dx: int, _dy: int) -> None:
        """Invalidate pending gestures/captures without work in the hook."""

        try:
            if self.stop_event.is_set():
                return
            with self.state_lock:
                self._ensure_double_alt_state_locked()
                self._last_clean_alt_release = 0.0
                if self._alt_down_since is not None:
                    self._alt_press_dirty = True
                self.interaction_id += 1
                self.input_generation += 1
                identity = (self.interaction_id, self.input_generation)
            self._notify_capture_context(identity)
        except Exception:
            return

    def _queue_capture(self, event: CaptureRequest) -> None:
        if self.stop_event.is_set():
            return
        self._notify_capture_context(
            (event.interaction_id, event.input_generation)
        )
        self.events.put(event)

    def _notify_capture_context(
        self,
        identity: tuple[int, int],
    ) -> None:
        capture_started = getattr(self, "capture_started_callback", None)
        if capture_started:
            try:
                capture_started(identity)
            except Exception:
                pass

    def _latest_queued_request(self, event: CaptureRequest) -> CaptureRequest | None:
        latest = event
        while True:
            try:
                queued = self.events.get_nowait()
            except queue.Empty:
                return latest
            if queued is None:
                return None
            latest = queued

    def _capture_loop(self) -> None:
        com_initialized = False
        try:
            pythoncom.CoInitialize()
            com_initialized = True
            with auto.UIAutomationInitializerInThread():
                while True:
                    event = self.events.get()
                    if event is None or self.stop_event.is_set():
                        return
                    # Coalesce all selection events that arrived while the
                    # previous capture was running, keeping only the newest.
                    event = self._latest_queued_request(event)
                    if event is None:
                        return
                    if self.stop_event.is_set():
                        return
                    try:
                        self._process_capture(event)
                    except Exception as exc:
                        log(f"Selection event error: {exc}\n{traceback.format_exc()}")
                        self.status_callback("本次取词失败，监听已自动继续")
        except Exception as exc:
            log(f"Selection watcher startup error: {exc}\n{traceback.format_exc()}")
            self.status_callback("桌面取词暂时不可用，请从托盘重新启动")
        finally:
            if com_initialized:
                pythoncom.CoUninitialize()
            self.stopped_event.set()

    def _process_capture(self, event: CaptureRequest) -> None:
        if not bool(self.store.get("desktop_enabled", True)):
            return
        with self.state_lock:
            request_is_current = (
                event.interaction_id == self.interaction_id
                and event.input_generation == self.input_generation
                and (
                    event.origin != "hotkey"
                    or bool(getattr(self, "hotkey_requests_enabled", True))
                )
            )
        if not request_is_current:
            return
        app_info = foreground_app(self.store)
        if not app_info:
            if event.origin == "hotkey":
                retry_label = hotkey_text_for_display(
                    self.store.get("retry_hotkey", "Double Alt")
                )
                self.status_callback(f"{retry_label} 获取失败：当前窗口不可读取")
            return
        if not self.store.is_app_enabled(app_info.exe):
            if event.origin == "hotkey":
                self.status_callback(f"请先在设置中为 {app_info.name} 开启划词翻译")
            return

        x, y = event.x, event.y
        if x < 0 or y < 0:
            try:
                x, y = (int(value) for value in mouse.Controller().position)
            except Exception:
                x, y = 0, 0
        timing = self.store.capture_timing(app_info.exe)
        if self.stop_event.wait(timing.settle_seconds):
            return
        with self.state_lock:
            request_is_current = (
                event.interaction_id == self.interaction_id
                and event.input_generation == self.input_generation
            )
        if not request_is_current or not self._focus_is_current(app_info):
            self.status_callback(f"已取消 {app_info.name} 取词：焦点已经切换")
            self._record_diagnostic(app_info, "skipped", "", "focus_changed", 0)
            return

        started = time.monotonic()
        probe = self._capture_selection(app_info, x, y)
        text = probe.text
        method = probe.method
        clipboard_result: ClipboardCaptureResult | None = None
        if (
            not text
            and not probe.protected
            and not self.stop_event.is_set()
            and bool(self.store.get("clipboard_fallback", True))
        ):
            clipboard_result = self._capture_with_clipboard(
                app_info,
                event.interaction_id,
                event.input_generation,
                timing.clipboard_timeout_seconds,
            )
            text = clipboard_result.text
            method = "clipboard"
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        if self.stop_event.is_set():
            return
        with self.state_lock:
            request_is_current = (
                event.interaction_id == self.interaction_id
                and event.input_generation == self.input_generation
            )
        if not request_is_current:
            self._record_diagnostic(app_info, "skipped", method, "focus_changed", elapsed_ms)
            return
        text = engine.normalize_selection(text or "")
        if not text:
            if probe.protected:
                reason = "protected"
                status = "skipped"
                self.status_callback("已跳过密码输入区域")
            elif clipboard_result and clipboard_result.reason == "snapshot_unavailable":
                reason = clipboard_result.reason
                status = "skipped"
                title = app_info.title.casefold()
                pdf_context = app_info.exe in self.PDF_READER_APPS or (
                    app_info.exe in self.BROWSER_APPS and ".pdf" in title
                )
                if pdf_context:
                    self.status_callback(
                        f"未读取到 {app_info.name} PDF 选区：文本层或无障碍选区不可用；为保护原剪贴板已跳过兼容复制"
                    )
                else:
                    self.status_callback(
                        f"未读取到 {app_info.name} 选区：原剪贴板格式无法安全备份或内容过大，已跳过兼容复制"
                    )
            elif clipboard_result and clipboard_result.reason == "restore_failed":
                reason = clipboard_result.reason
                status = "failed"
                self.status_callback("原剪贴板未能还原，本次未得到可用文字")
                log(f"Clipboard restore failed after capture from {app_info.exe}")
            elif clipboard_result and clipboard_result.reason in {
                "focus_changed",
                "concurrent_change",
                "external_change_preserved",
                "owner_unknown",
                "sequence_unavailable",
            }:
                reason = clipboard_result.reason
                status = "skipped"
                self.status_callback(f"已取消 {app_info.name} 取词：选择或剪贴板已经变化")
            else:
                reason = clipboard_result.reason if clipboard_result else "direct_empty"
                status = "failed"
                retry_label = hotkey_text_for_display(
                    self.store.get("retry_hotkey", "Double Alt")
                )
                self.status_callback(
                    f"未从 {app_info.name} 读取到选中文字；可按 {retry_label} 重试"
                )
                log(f"Selection capture missed: app={app_info.exe}, reason={reason}")
            self._record_diagnostic(app_info, status, method, reason, elapsed_ms)
            return
        if len(text) > MAX_SELECTION_LENGTH:
            self.status_callback(f"选中文字超过 {MAX_SELECTION_LENGTH} 字，已忽略")
            self._record_diagnostic(app_info, "skipped", method, "too_long", elapsed_ms)
            return

        with self.state_lock:
            request_is_current = (
                event.interaction_id == self.interaction_id
                and event.input_generation == self.input_generation
            )
        if not request_is_current or not self._focus_is_current(app_info):
            self._record_diagnostic(
                app_info,
                "skipped",
                method,
                "focus_changed",
                elapsed_ms,
            )
            return

        reason = clipboard_result.reason if clipboard_result else "captured"
        diagnostic_status = "warning" if reason == "restore_failed" else "success"
        self._record_diagnostic(app_info, diagnostic_status, method, reason, elapsed_ms)
        old_text, old_exe, old_time = self.last_emitted
        now = time.monotonic()
        if (
            event.origin != "hotkey"
            and text == old_text
            and app_info.exe == old_exe
            and now - old_time < 0.7
        ):
            return
        self.last_emitted = (text, app_info.exe, now)
        if clipboard_result and clipboard_result.reason == "restore_failed":
            self.status_callback(f"已从 {app_info.name} 读取文字，但原剪贴板还原失败")
        elif event.origin == "hotkey":
            retry_label = hotkey_text_for_display(
                self.store.get("retry_hotkey", "Double Alt")
            )
            self.status_callback(
                f"{retry_label} 已重新读取 {app_info.name} 选区，准备播放美音"
            )
        else:
            self.status_callback(f"已从 {app_info.name} 读取选中文字")
        with self.state_lock:
            request_is_current = (
                event.interaction_id == self.interaction_id
                and event.input_generation == self.input_generation
            )
        if not request_is_current or not self._focus_is_current(app_info):
            return
        self.selection_callback(
            text,
            app_info,
            x,
            y,
            event.auto_speak_accent,
            (event.interaction_id, event.input_generation),
        )

    def _record_diagnostic(
        self,
        app_info: AppInfo,
        status: str,
        method: str,
        reason: str,
        elapsed_ms: int,
    ) -> None:
        self.store.record_capture_diagnostic(
            app_info.exe,
            status=status,
            method=method,
            reason=reason,
            elapsed_ms=elapsed_ms,
        )
        self.diagnostic_callback(app_info.exe)

    @staticmethod
    def _focus_is_current(app_info: AppInfo) -> bool:
        """Require the exact foreground window captured for this request."""

        try:
            foreground_hwnd = int(win32gui.GetForegroundWindow())
            return bool(app_info.hwnd and foreground_hwnd == int(app_info.hwnd))
        except Exception:
            return False

    @staticmethod
    def _uia_control_belongs_to_window(control: object, expected_hwnd: int) -> bool:
        """Reject stale UIA controls from another top-level application window."""

        if not control or not expected_hwnd:
            return False
        current = control
        for _level in range(16):
            if not current:
                return False
            try:
                handle = int(getattr(current, "NativeWindowHandle", 0) or 0)
            except Exception:
                return False
            if handle:
                try:
                    root_handle = int(win32gui.GetAncestor(handle, 2) or handle)
                except Exception:
                    return False
                return root_handle == int(expected_hwnd)
            try:
                current = current.GetParentControl()
            except Exception:
                return False
        return False

    def _capture_selection(self, app_info: AppInfo, x: int, y: int) -> SelectionProbeResult:
        if app_info.exe == "winword.exe":
            selected = read_word_selected_text(
                win32com.client.GetActiveObject,
                MAX_SELECTION_LENGTH,
                app_info.title,
                app_info.hwnd,
            )
            if selected:
                return SelectionProbeResult(selected, "word_com")

        if app_info.exe == "powerpnt.exe":
            selected = read_powerpoint_selected_text(
                win32com.client.GetActiveObject,
                MAX_SELECTION_LENGTH,
                app_info.title,
                app_info.hwnd,
            )
            if selected:
                return SelectionProbeResult(selected, "powerpoint_com")

        if app_info.exe == "excel.exe":
            selected = read_excel_selected_text(
                win32com.client.GetActiveObject,
                MAX_SELECTION_LENGTH,
                app_info.title,
                app_info.hwnd,
            )
            if selected:
                return SelectionProbeResult(selected, "excel_com")

        if app_info.exe == "outlook.exe":
            selected = read_outlook_selected_text(
                win32com.client.GetActiveObject,
                MAX_SELECTION_LENGTH,
                app_info.title,
            )
            if selected:
                return SelectionProbeResult(selected, "outlook_com")

        if app_info.exe in self.DIRECT_CLIPBOARD_APPS:
            return SelectionProbeResult()

        controls: list[object] = []
        try:
            focused = auto.GetFocusedControl()
            if focused and self._uia_control_belongs_to_window(
                focused, app_info.hwnd
            ):
                controls.append(focused)
        except Exception:
            pass
        try:
            pointed = auto.ControlFromPoint(x, y)
            if pointed and self._uia_control_belongs_to_window(
                pointed, app_info.hwnd
            ):
                controls.append(pointed)
        except Exception:
            pass

        selected, protected = read_uia_selected_text(
            controls,
            (auto.PatternId.TextPattern2, auto.PatternId.TextPattern),
            MAX_SELECTION_LENGTH,
            engine.normalize_selection,
        )
        if selected or protected:
            return SelectionProbeResult(
                selected,
                "uia" if selected else "",
                protected,
            )

        if app_info.exe in self.DEEP_UIA_APPS and app_info.hwnd:
            try:
                root_control = auto.ControlFromHandle(app_info.hwnd)
            except Exception:
                root_control = None
            selected, protected = read_uia_descendant_selected_text(
                root_control,
                (auto.PatternId.TextPattern2, auto.PatternId.TextPattern),
                MAX_SELECTION_LENGTH,
                engine.normalize_selection,
            )
            if selected or protected:
                return SelectionProbeResult(
                    selected,
                    "uia_descendant" if selected else "",
                    protected,
                )
        return SelectionProbeResult()

    def _capture_with_clipboard(
        self,
        app_info: AppInfo,
        interaction_id: int,
        input_generation: int,
        timeout_seconds: float,
    ) -> ClipboardCaptureResult:
        with self.state_lock:
            copy_intent_generation = getattr(self, "copy_intent_generation", 0)
        clipboard_state = capture_portable_clipboard_state()
        old_text = clipboard_state.text
        formats = clipboard_state.formats
        old_state_known = clipboard_state.known
        old_sequence = clipboard_state.sequence
        snapshot = clipboard_state.snapshot

        controller = keyboard.Controller()
        copy_key = keyboard.KeyCode.from_vk(0x43)

        def send_copy() -> None:
            # Recheck at the last practical moment, after Controller setup, to
            # avoid sending Ctrl+C into a window selected after this request.
            with self.state_lock:
                request_is_current = (
                    interaction_id == self.interaction_id
                    and input_generation == self.input_generation
                )
            if (
                self.stop_event.is_set()
                or not request_is_current
                or not self._focus_is_current(app_info)
            ):
                raise RuntimeError("selection changed before compatibility copy")
            current_sequence = clipboard_sequence_number()
            if current_sequence <= 0 or current_sequence != old_sequence:
                raise RuntimeError("clipboard changed before compatibility copy")
            with controller.pressed(keyboard.Key.ctrl):
                controller.press(copy_key)
                controller.release(copy_key)

        def clipboard_change_is_ours() -> bool | None:
            if not app_info.process_id:
                return None
            try:
                return clipboard_owner_matches_app(app_info)
            except Exception:
                return None

        def external_copy_intent_detected() -> bool:
            with self.state_lock:
                return (
                    getattr(self, "copy_intent_generation", 0)
                    != copy_intent_generation
                    or interaction_id != self.interaction_id
                    or input_generation != self.input_generation
                )

        return capture_selected_text_with_clipboard(
            old_text=old_text,
            old_formats=formats,
            old_state_known=old_state_known,
            old_sequence=old_sequence,
            snapshot_factory=lambda: snapshot,
            sequence_number=clipboard_sequence_number,
            send_copy=send_copy,
            read_text=lambda: read_clipboard_text()[0],
            restore_plain_text=restore_clipboard_text_if_unchanged,
            timeout_seconds=timeout_seconds,
            focus_is_current=lambda: (
                not self.stop_event.is_set()
                and interaction_id == self.interaction_id
                and self._input_generation_is_current(input_generation)
                and self._focus_is_current(app_info)
            ),
            clipboard_change_is_ours=clipboard_change_is_ours,
            external_copy_intent_detected=external_copy_intent_detected,
        )


class ToggleSwitch(tk.Canvas):
    def __init__(
        self,
        parent: tk.Misc,
        variable: tk.BooleanVar,
        command: Callable[[], object],
        *,
        background: str = WHITE,
    ) -> None:
        super().__init__(
            parent,
            width=42,
            height=24,
            bg=background,
            bd=0,
            highlightthickness=2,
            highlightbackground=background,
            highlightcolor=BLUE,
            takefocus=1,
            cursor="hand2",
        )
        self.variable = variable
        self.command = command
        self.bind("<Button-1>", self._toggle)
        self.bind("<space>", self._toggle)
        self.bind("<Return>", self._toggle)
        self.draw()

    def _toggle(self, _event: tk.Event[tk.Misc]) -> None:
        previous = bool(self.variable.get())
        self.variable.set(not previous)
        self.draw()
        try:
            result = self.command()
        except Exception as exc:
            log(f"Toggle action failed: {exc}\n{traceback.format_exc()}")
            result = False
        if result is False:
            self.variable.set(previous)
            self.draw()

    def draw(self) -> None:
        self.delete("all")
        enabled = self.variable.get()
        fill = BLUE if enabled else "#D5D8DE"
        x1, y1, x2, y2 = 1, 2, 41, 22
        radius = 10
        self.create_rectangle(x1 + radius, y1, x2 - radius, y2, fill=fill, outline=fill)
        self.create_oval(x1, y1, x1 + radius * 2, y2, fill=fill, outline=fill)
        self.create_oval(x2 - radius * 2, y1, x2, y2, fill=fill, outline=fill)
        knob_x = 30 if enabled else 12
        self.create_oval(knob_x - 8, 4, knob_x + 8, 20, fill=WHITE, outline=WHITE)


def flat_button(
    parent: tk.Misc,
    text: str,
    command: Callable[[], None],
    *,
    primary: bool = False,
    compact: bool = False,
) -> tk.Button:
    return tk.Button(
        parent,
        text=text,
        command=command,
        relief="flat",
        bd=0,
        highlightthickness=0,
        bg=BLUE if primary else (BLUE_SOFT if compact else WHITE),
        activebackground="#2858C7" if primary else "#E3EBFF",
        fg=WHITE if primary else BLUE,
        activeforeground=WHITE if primary else BLUE,
        font=("Microsoft YaHei UI", 9, "bold" if primary else "normal"),
        padx=14 if not compact else 10,
        pady=8 if not compact else 5,
        cursor="hand2",
    )


class SettingsWindow:
    def __init__(self, app: "DesktopTranslatorApp") -> None:
        self.app = app
        self.store = app.store
        self.window: tk.Toplevel | None = None
        self.list_frame: tk.Frame | None = None
        self.other_expanded = bool(self.store.get("other_expanded", False))
        self.discovered: dict[str, AppInfo] = {}
        self.diagnostic_labels: dict[str, tk.Label] = {}
        self.shortcut_var: tk.BooleanVar | None = None
        self.shortcut_switch: ToggleSwitch | None = None
        self.shortcut_summary_var: tk.StringVar | None = None
        self.retry_hotkey_var: tk.StringVar | None = None
        self.toggle_hotkey_var: tk.StringVar | None = None
        self.shortcut_status_var: tk.StringVar | None = None
        self.shortcut_status_label: tk.Label | None = None
        self._shortcut_keys_down: set[tuple[str, str]] = set()
        self._alt_press_started: dict[str, float] = {}
        self._last_alt_release: dict[str, float] = {}
        self._alt_used_in_combo: set[str] = set()
        self._shortcut_entries: set[tk.Entry] = set()
        self._settings_canvas: tk.Canvas | None = None
        self._wheel_remainder = 0
        self.natural_speed_buttons: dict[str, tk.Button] = {}
        self.auto_speech_preference_buttons: dict[str, tk.Button] = {}

    def show(self) -> None:
        self._reset_shortcut_recording_state()
        if self.window and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            self.refresh_shortcut_state()
            self.refresh_shortcut_fields()
            self.refresh_auto_speech_preference_buttons()
            self.refresh_natural_speed_buttons()
            self.refresh_apps()
            return
        self.window = tk.Toplevel(self.app.root)
        self.window.withdraw()
        self.window.title(f"{APP_NAME} · 设置")
        self.window.geometry("620x720")
        self.window.minsize(560, 620)
        self.window.configure(bg=WHITE)
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self.window.attributes("-topmost", True)
        self.app.apply_window_icon(self.window)
        self._build()
        self.refresh_apps()
        try:
            work_area = current_monitor_work_area(
                self.window.winfo_screenwidth(),
                self.window.winfo_screenheight(),
            )
            fit_tk_window_to_work_area(self.window, work_area, 620, 720, 560, 620)
        except Exception as exc:
            log(f"Could not fit settings window to monitor work area: {exc}")
            self.window.geometry("620x720")
            self.window.minsize(560, 620)
        self.window.deiconify()
        self.window.lift()

    def hide(self) -> None:
        self._reset_shortcut_recording_state()
        if self.window and self.window.winfo_exists():
            self.window.withdraw()
        self.app.set_shortcut_editor_active(False)

    def _reset_shortcut_recording_state(self) -> None:
        for name in (
            "_shortcut_keys_down",
            "_alt_press_started",
            "_last_alt_release",
            "_alt_used_in_combo",
        ):
            state = getattr(self, name, None)
            if state is not None:
                state.clear()

    def _build(self) -> None:
        assert self.window is not None
        header = tk.Frame(self.window, bg=WHITE, padx=24, pady=18)
        header.pack(fill="x")
        tk.Label(header, text="设置", bg=WHITE, fg=TEXT, font=("Microsoft YaHei UI", 18, "bold")).pack(side="left")
        tk.Label(header, text="应用后立即生效", bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side="right", pady=(7, 0))
        tk.Frame(self.window, bg=LINE, height=1).pack(fill="x")

        canvas = tk.Canvas(self.window, bg=WHITE, highlightthickness=0)
        self._settings_canvas = canvas
        scrollbar = tk.Scrollbar(self.window, orient="vertical", command=canvas.yview, relief="flat")
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=WHITE)
        window_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        self.window.bind("<MouseWheel>", self._on_mousewheel)

        general = tk.Frame(body, bg=WHITE, padx=24, pady=18)
        general.pack(fill="x")
        self._section_title(general, "工作方式", "控制桌面取词、窗口模式和兼容读取")

        desktop_var = tk.BooleanVar(value=bool(self.store.get("desktop_enabled", True)))
        self._setting_row(
            general,
            "桌面划词翻译",
            "关闭后软件仍留在托盘，但不再读取任何应用的选中文字",
            desktop_var,
            lambda: self.app.set_desktop_enabled(desktop_var.get()),
        )

        fallback_var = tk.BooleanVar(value=bool(self.store.get("clipboard_fallback", True)))
        self._setting_row(
            general,
            "兼容读取",
            "专用接口失败时短暂复制；会备份并还原常见文本、富文本、图片和文件列表",
            fallback_var,
            lambda: self.store.set("clipboard_fallback", fallback_var.get()),
        )

        self.shortcut_var = tk.BooleanVar(value=bool(self.store.get("hotkeys_enabled", True)))
        self.shortcut_summary_var = tk.StringVar(value=self.app.hotkey_summary())
        self.shortcut_switch = self._setting_row(
            general,
            "全局快捷键",
            self.app.hotkey_summary(),
            self.shortcut_var,
            lambda: self.app.set_hotkeys_enabled(bool(self.shortcut_var and self.shortcut_var.get())),
            subtitle_variable=self.shortcut_summary_var,
        )

        shortcut_editor = tk.Frame(general, bg=WHITE, padx=0, pady=2)
        shortcut_editor.pack(fill="x", pady=(0, 10))
        self.retry_hotkey_var = tk.StringVar(
            value=hotkey_text_for_display(self.store.get("retry_hotkey", "Double Alt"))
        )
        self.toggle_hotkey_var = tk.StringVar(
            value=hotkey_text_for_display(self.store.get("toggle_mode_hotkey", "Alt+C"))
        )
        self._shortcut_entry_row(
            shortcut_editor,
            "重新获取并自动美音",
            self.retry_hotkey_var,
            HotkeyCommand.RETRY_AND_SPEAK_US,
            "retry_hotkey",
            allow_double_alt=True,
        )
        self._shortcut_entry_row(
            shortcut_editor,
            "切换迷你 / 大窗口",
            self.toggle_hotkey_var,
            HotkeyCommand.TOGGLE_WINDOW_MODE,
            "toggle_mode_hotkey",
        )
        shortcut_actions = tk.Frame(shortcut_editor, bg=WHITE)
        shortcut_actions.pack(fill="x", pady=(6, 0))
        flat_button(
            shortcut_actions,
            "应用快捷键",
            self.apply_shortcut_edits,
            primary=True,
            compact=True,
        ).pack(side="right")
        self.shortcut_status_var = tk.StringVar(value="点击输入框后直接按下组合键，也可以粘贴文本。")
        self.shortcut_status_label = tk.Label(
            shortcut_actions,
            textvariable=self.shortcut_status_var,
            bg=WHITE,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=370,
            font=("Microsoft YaHei UI", 8),
        )
        self.shortcut_status_label.pack(side="left", fill="x", expand=True, padx=(0, 10))

        adaptive_var = tk.BooleanVar(value=bool(self.store.get("adaptive_wait_enabled", True)))
        self._setting_row(
            general,
            "按应用自动调节等待",
            "只学习兼容复制的实际响应速度；成功立即返回，不会固定多等",
            adaptive_var,
            lambda: self.store.set("adaptive_wait_enabled", adaptive_var.get()),
        )
        reset_row = tk.Frame(general, bg=WHITE)
        reset_row.pack(fill="x", pady=(0, 4))
        flat_button(reset_row, "恢复默认等待", self.reset_adaptive_waits, compact=True).pack(side="right")

        preference_row = tk.Frame(general, bg=WHITE, pady=12)
        preference_row.pack(fill="x")
        preference_labels = tk.Frame(preference_row, bg=WHITE)
        preference_labels.pack(side="left", fill="x", expand=True)
        tk.Label(
            preference_labels,
            text="自动发音偏好",
            bg=WHITE,
            fg=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w")
        tk.Label(
            preference_labels,
            text="同时控制 US/UK 单击与双击、双击 Alt 自动朗读，以及短语和句子",
            bg=WHITE,
            fg=MUTED,
            wraplength=340,
            justify="left",
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w", pady=(3, 0))
        preference_selector = tk.Frame(preference_row, bg="#F1F3F6", padx=3, pady=3)
        preference_selector.pack(side="right", padx=(12, 0))
        for preference, label in (("speed", "优先速度"), ("natural", "优先自然音色")):
            button = flat_button(
                preference_selector,
                label,
                lambda value=preference: self.choose_auto_speech_preference(value),
                compact=True,
            )
            button.pack(side="left", padx=(3, 0) if preference == "natural" else 0)
            self.auto_speech_preference_buttons[preference] = button
        self.refresh_auto_speech_preference_buttons()

        natural_speed_row = tk.Frame(general, bg=WHITE, pady=12)
        natural_speed_row.pack(fill="x")
        natural_speed_labels = tk.Frame(natural_speed_row, bg=WHITE)
        natural_speed_labels.pack(side="left", fill="x", expand=True)
        tk.Label(natural_speed_labels, text="句子 AI 发音语速", bg=WHITE, fg=TEXT, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        tk.Label(natural_speed_labels, text="仅调整 Kokoro 短语/句子；Piper 单词保持原版语速", bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(3, 0))
        natural_selector = tk.Frame(natural_speed_row, bg="#F1F3F6", padx=3, pady=3)
        natural_selector.pack(side="right")
        for profile, label in (("slow", "慢"), ("standard", "标准"), ("fast", "快")):
            button = flat_button(
                natural_selector,
                label,
                lambda value=profile: self.choose_natural_speed(value),
                compact=True,
            )
            button.pack(side="left", padx=(3, 0) if profile != "slow" else 0)
            self.natural_speed_buttons[profile] = button
        self.refresh_natural_speed_buttons()

        mode_row = tk.Frame(general, bg=WHITE, pady=12)
        mode_row.pack(fill="x")
        labels = tk.Frame(mode_row, bg=WHITE)
        labels.pack(side="left", fill="x", expand=True)
        tk.Label(labels, text="默认显示模式", bg=WHITE, fg=TEXT, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        tk.Label(labels, text="迷你浮窗靠近鼠标；大窗口保留完整内容", bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(3, 0))
        selector = tk.Frame(mode_row, bg="#F1F3F6", padx=3, pady=3)
        selector.pack(side="right")
        self.mini_mode_button = flat_button(selector, "迷你", lambda: self.choose_mode("mini"), compact=True)
        self.mini_mode_button.pack(side="left")
        self.large_mode_button = flat_button(selector, "大窗口", lambda: self.choose_mode("panel"), compact=True)
        self.large_mode_button.pack(side="left", padx=(3, 0))
        self.refresh_mode_buttons()

        tk.Frame(body, bg=LINE, height=8).pack(fill="x")
        apps_header = tk.Frame(body, bg=WHITE, padx=24)
        apps_header.pack(fill="x", pady=(18, 8))
        self._section_title(apps_header, "应用范围", "最近调整的应用会自动排在组内前面")
        tk.Label(
            apps_header,
            text="兼容性诊断仅记录读取方式、耗时和结果，不保存选中文字。",
            bg=WHITE,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w", pady=(7, 0))
        tools = tk.Frame(apps_header, bg=WHITE)
        tools.pack(fill="x", pady=(12, 0))
        flat_button(tools, "添加应用…", self.add_application, compact=True).pack(side="left")
        flat_button(tools, "刷新正在运行的应用", self.refresh_apps, compact=True).pack(side="left", padx=(8, 0))

        self.list_frame = tk.Frame(body, bg=WHITE, padx=24)
        self.list_frame.pack(fill="x", pady=(2, 28))

    @staticmethod
    def _section_title(parent: tk.Misc, title: str, subtitle: str) -> None:
        tk.Label(parent, text=title, bg=WHITE, fg=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        tk.Label(parent, text=subtitle, bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(3, 0))

    @staticmethod
    def _setting_row(
        parent: tk.Misc,
        title: str,
        subtitle: str,
        variable: tk.BooleanVar,
        command: Callable[[], None],
        *,
        subtitle_variable: tk.StringVar | None = None,
    ) -> ToggleSwitch:
        row = tk.Frame(parent, bg=WHITE, pady=12)
        row.pack(fill="x")
        # Reserve the fixed-width control first so long localized text or high
        # DPI fonts can wrap without pushing the switch outside the window.
        toggle = ToggleSwitch(row, variable, command)
        toggle.pack(side="right", padx=(12, 0))
        labels = tk.Frame(row, bg=WHITE)
        labels.pack(side="left", fill="x", expand=True)
        tk.Label(labels, text=title, bg=WHITE, fg=TEXT, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        subtitle_options: dict[str, object] = {"text": subtitle}
        if subtitle_variable is not None:
            subtitle_options = {"textvariable": subtitle_variable}
        tk.Label(
            labels,
            **subtitle_options,
            bg=WHITE,
            fg=MUTED,
            wraplength=390,
            justify="left",
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w", pady=(3, 0))
        return toggle

    def _shortcut_entry_row(
        self,
        parent: tk.Misc,
        title: str,
        variable: tk.StringVar,
        command: HotkeyCommand,
        setting_key: str,
        *,
        allow_double_alt: bool = False,
    ) -> None:
        row = tk.Frame(parent, bg=WHITE, pady=4)
        row.pack(fill="x")

        if allow_double_alt:
            flat_button(
                row,
                "设为双击 Alt",
                lambda: self._set_double_alt(variable),
                compact=True,
            ).pack(side="right", padx=(7, 0))

        entry = tk.Entry(
            row,
            textvariable=variable,
            width=18,
            justify="center",
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=BLUE,
            bg=WHITE,
            fg=TEXT,
            insertbackground=TEXT,
            font=("Segoe UI", 10),
        )
        entry.pack(side="right")
        self._shortcut_entries.add(entry)
        entry.bind(
            "<FocusIn>",
            lambda _event, field=entry: self._shortcut_focus_in(field),
        )
        entry.bind("<FocusOut>", lambda _event: self._shortcut_focus_out())
        entry.bind(
            "<KeyPress>",
            lambda event, field=variable, action=command, key=setting_key, double=allow_double_alt: self._record_shortcut_key(
                event,
                field,
                action,
                key,
                allow_double_alt=double,
            ),
        )
        entry.bind(
            "<KeyRelease>",
            lambda event, field=variable, key=setting_key, double=allow_double_alt: self._release_shortcut_key(
                event,
                field,
                key,
                allow_double_alt=double,
            ),
        )
        tk.Label(
            row,
            text=title,
            bg=WHITE,
            fg=TEXT,
            anchor="w",
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", fill="x", expand=True)

    def _on_mousewheel(self, event: tk.Event[tk.Misc]) -> str:
        canvas = self._settings_canvas
        if canvas is None or not self.window or not self.window.winfo_viewable():
            return "break"
        delta = int(getattr(event, "delta", 0) or 0)
        if delta:
            # Runtime double-Alt is interrupted by a wheel gesture.  The
            # recorder must obey the identical rule or it can save a shortcut
            # the running application would never recognise.
            self._reset_shortcut_recording_state()
        self._wheel_remainder -= delta
        steps = int(self._wheel_remainder / 120)
        if steps:
            canvas.yview_scroll(steps, "units")
            self._wheel_remainder -= steps * 120
        return "break"

    def _shortcut_focus_in(self, entry: tk.Entry) -> None:
        entry.selection_range(0, "end")
        self.app.set_shortcut_editor_active(True)

    def _shortcut_focus_out(self) -> None:
        if self.window and self.window.winfo_exists():
            self.window.after_idle(self._finish_shortcut_focus_change)

    def _finish_shortcut_focus_change(self) -> None:
        if not self.window or not self.window.winfo_exists():
            self.app.set_shortcut_editor_active(False)
            return
        if self.window.focus_get() in self._shortcut_entries:
            return
        self._reset_shortcut_recording_state()
        self.app.set_shortcut_editor_active(False)

    def _set_double_alt(self, variable: tk.StringVar) -> None:
        variable.set("双击 Alt")
        self.set_shortcut_status("已录入双击 Alt，点击“应用快捷键”后生效。")

    @staticmethod
    def _shortcut_key_label(keysym: str) -> str:
        aliases = {
            "Return": "Enter",
            "space": "Space",
            "Prior": "PageUp",
            "Next": "PageDown",
            "Home": "Home",
            "End": "End",
            "Insert": "Insert",
            "Tab": "Tab",
        }
        if keysym in aliases:
            return aliases[keysym]
        if len(keysym) == 1 and keysym.isalnum():
            return keysym.upper()
        if keysym.upper().startswith("F") and keysym[1:].isdigit():
            return keysym.upper()
        return keysym

    def _record_shortcut_key(
        self,
        event: tk.Event[tk.Misc],
        variable: tk.StringVar,
        command: HotkeyCommand,
        setting_key: str,
        *,
        allow_double_alt: bool,
    ) -> str:
        keysym = str(getattr(event, "keysym", ""))
        state = int(getattr(event, "state", 0) or 0)
        lowered = keysym.casefold()
        modifier_keys = {
            "alt_l",
            "alt_r",
            "control_l",
            "control_r",
            "shift_l",
            "shift_r",
            "super_l",
            "super_r",
            "win_l",
            "win_r",
        }

        if keysym == "Escape":
            self._reset_shortcut_recording_state()
            fallback = "Double Alt" if setting_key == "retry_hotkey" else "Alt+C"
            variable.set(hotkey_text_for_display(self.store.get(setting_key, fallback)))
            self.set_shortcut_status("已取消本次录入。")
            return "break"
        if keysym in {"BackSpace", "Delete"}:
            self._reset_shortcut_recording_state()
            variable.set("")
            self.set_shortcut_status("已清空；请选择新的组合键，原设置尚未改变。", error=True)
            return "break"
        if (state & 0x0004) and lowered == "v":
            try:
                assert self.window is not None
                pasted = str(self.window.clipboard_get()).strip()
            except (tk.TclError, AssertionError):
                self.set_shortcut_status("剪贴板里没有可粘贴的快捷键文本。", error=True)
            else:
                variable.set(pasted)
                self.set_shortcut_status("已粘贴，点击“应用快捷键”前会检查格式和占用。")
            return "break"

        key_identity = (setting_key, lowered)
        if lowered in modifier_keys:
            if key_identity in self._shortcut_keys_down:
                return "break"
            prior_keys = {
                key
                for key in self._shortcut_keys_down
                if key[0] == setting_key
            }
            self._shortcut_keys_down.add(key_identity)
            if lowered == "alt_l" and allow_double_alt:
                interrupted = bool(prior_keys) or bool(state & (0x0001 | 0x0004 | 0x0040))
                if interrupted:
                    self._alt_used_in_combo.add(setting_key)
                    self._alt_press_started.pop(setting_key, None)
                    self._last_alt_release.pop(setting_key, None)
                else:
                    now = time.monotonic()
                    self._alt_press_started[setting_key] = now
                    self.set_shortcut_status("轻按并松开左 Alt；再轻按一次可录入“双击 Alt”。")
            else:
                # Any other modifier between taps interrupts the candidate;
                # if Alt is currently held it also dirties that tap.
                self._last_alt_release.pop(setting_key, None)
                if setting_key in self._alt_press_started:
                    self._alt_used_in_combo.add(setting_key)
            return "break"

        if state & 0x0008 or setting_key in self._alt_press_started:
            self._alt_used_in_combo.add(setting_key)
        self._last_alt_release.pop(setting_key, None)
        label_parts: list[str] = []
        if state & 0x0004:
            label_parts.append("Ctrl")
        if state & 0x0008:
            label_parts.append("Alt")
        if state & 0x0001:
            label_parts.append("Shift")
        if state & 0x0040:
            label_parts.append("Win")
        label_parts.append(self._shortcut_key_label(keysym))
        candidate = "+".join(label_parts)
        try:
            spec = parse_hotkey_spec(command, candidate)
        except (TypeError, ValueError) as exc:
            self.set_shortcut_status(f"无法录入 {candidate}：{exc}", error=True)
            return "break"
        variable.set(hotkey_text_for_display(format_hotkey_spec(spec)))
        self.set_shortcut_status("已录入，点击“应用快捷键”后生效。")
        return "break"

    def _release_shortcut_key(
        self,
        event: tk.Event[tk.Misc],
        variable: tk.StringVar,
        setting_key: str,
        *,
        allow_double_alt: bool,
    ) -> None:
        lowered = str(getattr(event, "keysym", "")).casefold()
        self._shortcut_keys_down.discard((setting_key, lowered))
        if lowered not in {"alt_l", "alt_r"} or not allow_double_alt:
            return
        if setting_key in self._alt_used_in_combo:
            self._alt_used_in_combo.discard(setting_key)
            self._alt_press_started.pop(setting_key, None)
            self._last_alt_release.pop(setting_key, None)
            return
        if lowered != "alt_l":
            self._alt_press_started.pop(setting_key, None)
            self._last_alt_release.pop(setting_key, None)
            return
        now = time.monotonic()
        started = self._alt_press_started.pop(setting_key, None)
        if started is None or now - started > DOUBLE_ALT_TAP_MAX_HOLD_SECONDS:
            self._last_alt_release.pop(setting_key, None)
            self.set_shortcut_status("按住时间过长；请连续轻按两次左 Alt。", error=True)
            return
        previous = self._last_alt_release.get(setting_key, 0.0)
        if previous and now - previous <= DOUBLE_ALT_TAP_INTERVAL_SECONDS:
            self._last_alt_release.pop(setting_key, None)
            self._set_double_alt(variable)
            return
        self._last_alt_release[setting_key] = now
        self.set_shortcut_status("再轻按一次左 Alt 可录入“双击 Alt”。")

    def apply_shortcut_edits(self) -> None:
        if self.retry_hotkey_var is None or self.toggle_hotkey_var is None:
            return
        succeeded, message = self.app.apply_hotkey_settings(
            self.retry_hotkey_var.get(),
            self.toggle_hotkey_var.get(),
        )
        self.set_shortcut_status(message, error=not succeeded)
        if succeeded:
            self.refresh_shortcut_fields()

    def set_shortcut_status(self, message: str, *, error: bool = False) -> None:
        status_var = getattr(self, "shortcut_status_var", None)
        status_label = getattr(self, "shortcut_status_label", None)
        if status_var is not None:
            status_var.set(message)
        if status_label is not None:
            status_label.configure(fg=RED if error else GREEN)

    def refresh_shortcut_fields(self) -> None:
        retry_var = getattr(self, "retry_hotkey_var", None)
        toggle_var = getattr(self, "toggle_hotkey_var", None)
        summary_var = getattr(self, "shortcut_summary_var", None)
        if retry_var is not None:
            retry_var.set(
                hotkey_text_for_display(self.store.get("retry_hotkey", "Double Alt"))
            )
        if toggle_var is not None:
            toggle_var.set(
                hotkey_text_for_display(self.store.get("toggle_mode_hotkey", "Alt+C"))
            )
        if summary_var is not None:
            summary_var.set(self.app.hotkey_summary())

    def refresh_shortcut_state(self) -> None:
        if self.shortcut_var is None or self.shortcut_switch is None:
            return
        self.shortcut_var.set(bool(self.store.get("hotkeys_enabled", True)))
        self.shortcut_switch.draw()
        summary_var = getattr(self, "shortcut_summary_var", None)
        if summary_var is not None:
            summary_var.set(self.app.hotkey_summary())

    def refresh_apps(self) -> None:
        if not self.list_frame or not self.list_frame.winfo_exists():
            return
        self.discovered = discover_visible_apps(self.store)
        for info in self.discovered.values():
            self.store.set_app(info.exe, self.store.is_app_enabled(info.exe), name=info.name, path=info.path, touch=False, save=False)
        self.store.save()
        self._render_app_lists()

    def choose_mode(self, mode: str) -> None:
        self.app.set_mode(mode)
        self.refresh_mode_buttons()

    def refresh_mode_buttons(self) -> None:
        if not hasattr(self, "mini_mode_button"):
            return
        for mode, button in (("mini", self.mini_mode_button), ("panel", self.large_mode_button)):
            selected = self.app.display_mode == mode
            button.configure(
                bg=BLUE if selected else BLUE_SOFT,
                fg=WHITE if selected else BLUE,
                activebackground="#2858C7" if selected else "#E3EBFF",
                activeforeground=WHITE if selected else BLUE,
            )

    def choose_natural_speed(self, profile: str) -> None:
        if self.app.set_natural_speech_speed(profile):
            self.refresh_natural_speed_buttons()

    def refresh_natural_speed_buttons(self) -> None:
        if not self.natural_speed_buttons:
            return
        selected_profile = str(
            self.store.get(
                "natural_speech_speed", engine.DEFAULT_NATURAL_SPEECH_SPEED
            )
        ).lower()
        for profile, button in self.natural_speed_buttons.items():
            selected = profile == selected_profile
            button.configure(
                bg=BLUE if selected else BLUE_SOFT,
                fg=WHITE if selected else BLUE,
                activebackground="#2858C7" if selected else "#E3EBFF",
                activeforeground=WHITE if selected else BLUE,
            )

    def choose_auto_speech_preference(self, preference: str) -> None:
        if self.app.set_auto_speech_preference(preference):
            self.refresh_auto_speech_preference_buttons()

    def refresh_auto_speech_preference_buttons(self) -> None:
        if not self.auto_speech_preference_buttons:
            return
        selected_preference = engine.normalize_auto_speech_preference(
            self.store.get(
                "auto_speech_preference",
                engine.DEFAULT_AUTO_SPEECH_PREFERENCE,
            )
        )
        for preference, button in self.auto_speech_preference_buttons.items():
            selected = preference == selected_preference
            button.configure(
                bg=BLUE if selected else BLUE_SOFT,
                fg=WHITE if selected else BLUE,
                activebackground="#2858C7" if selected else "#E3EBFF",
                activeforeground=WHITE if selected else BLUE,
            )

    def _sort_apps(self, exes: list[str], base_order: list[str] | None = None) -> list[str]:
        order = {exe: index for index, exe in enumerate(base_order or [])}
        return sorted(
            set(exes),
            key=lambda exe: (
                0 if self.store.recency(exe) else 1,
                -self.store.recency(exe),
                order.get(exe, 999),
                self.store.app_name(exe).casefold(),
            ),
        )

    def _render_app_lists(self) -> None:
        assert self.list_frame is not None
        for child in self.list_frame.winfo_children():
            child.destroy()
        self.diagnostic_labels.clear()

        base = [exe for exe, _name in COMMON_APPS]
        common = self._sort_apps(base + self.store.custom_common(), base)
        all_apps = (set(self.store.enabled_apps()) | set(self.discovered)) - UNSUPPORTED_APPS
        app_names = self.store.get("app_names", {})
        if isinstance(app_names, dict):
            all_apps.update(
                str(exe).lower()
                for exe in app_names
                if str(exe).lower() not in UNSUPPORTED_APPS
            )
        other = self._sort_apps([exe for exe in all_apps if exe not in set(common)])

        self._group_heading(self.list_frame, "常用应用", "可把其他应用提升到这里")
        for exe in common:
            self._app_row(self.list_frame, exe, common=True, running=exe in self.discovered)

        tk.Frame(self.list_frame, bg=LINE, height=1).pack(fill="x", pady=(14, 12))
        other_header = tk.Frame(self.list_frame, bg=WHITE)
        other_header.pack(fill="x")
        count_text = f"其他应用（{len(other)}）"
        tk.Button(
            other_header,
            text=("收起  ▴  " if self.other_expanded else "展开  ▾  ") + count_text,
            command=self.toggle_other,
            relief="flat",
            bd=0,
            bg=WHITE,
            activebackground=WHITE,
            fg=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
            cursor="hand2",
        ).pack(side="left")
        flat_button(other_header, "全部关闭", lambda: self.set_all_other(other, False), compact=True).pack(side="right")
        flat_button(other_header, "全部开启", lambda: self.set_all_other(other, True), compact=True).pack(side="right", padx=(0, 7))

        if self.other_expanded:
            if not other:
                tk.Label(self.list_frame, text="还没有发现其他可选应用。打开应用后点“刷新”即可。", bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=16)
            for exe in other:
                self._app_row(self.list_frame, exe, common=False, running=exe in self.discovered)
        else:
            tk.Label(self.list_frame, text="其他应用默认收起，避免列表过长。", bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(8, 0))

    @staticmethod
    def _group_heading(parent: tk.Misc, title: str, note: str) -> None:
        heading = tk.Frame(parent, bg=WHITE)
        heading.pack(fill="x", pady=(8, 6))
        tk.Label(heading, text=title, bg=WHITE, fg=TEXT, font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        tk.Label(heading, text=note, bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(9, 0), pady=(2, 0))

    def _app_row(self, parent: tk.Misc, exe: str, *, common: bool, running: bool) -> None:
        row = tk.Frame(parent, bg=WHITE, pady=9)
        row.pack(fill="x")
        initial = (self.store.app_name(exe)[:1] or "A").upper()
        badge = tk.Label(row, text=initial, width=2, height=1, bg=BLUE_SOFT, fg=BLUE, font=("Segoe UI", 10, "bold"))
        badge.pack(side="left", padx=(0, 10))
        controls = tk.Frame(row, bg=WHITE)
        controls.pack(side="right")
        if exe not in {item for item, _name in COMMON_APPS}:
            action_text = "移回其他" if common else "设为常用"
            flat_button(
                controls,
                action_text,
                lambda value=exe, target=not common: self.move_common(value, target),
                compact=True,
            ).pack(side="right", padx=(7, 0))
        variable = tk.BooleanVar(value=self.store.is_app_enabled(exe))
        ToggleSwitch(
            controls,
            variable,
            lambda value=exe, state=variable: self.toggle_app(value, state.get()),
        ).pack(side="right", padx=(10, 0))
        labels = tk.Frame(row, bg=WHITE)
        labels.pack(side="left", fill="x", expand=True)
        name_row = tk.Frame(labels, bg=WHITE)
        name_row.pack(anchor="w")
        tk.Label(name_row, text=self.store.app_name(exe), bg=WHITE, fg=TEXT, font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        if running:
            tk.Label(name_row, text="  正在运行", bg=WHITE, fg=GREEN, font=("Microsoft YaHei UI", 7)).pack(side="left")
        tk.Label(labels, text=exe, bg=WHITE, fg=FAINT, font=("Segoe UI", 7)).pack(anchor="w", pady=(2, 0))
        diagnostic_text, diagnostic_color = self._diagnostic_summary(exe)
        diagnostic = tk.Label(
            labels,
            text=diagnostic_text,
            bg=WHITE,
            fg=diagnostic_color,
            anchor="w",
            justify="left",
            wraplength=300,
            font=("Microsoft YaHei UI", 7),
        )
        diagnostic.pack(anchor="w", pady=(2, 0))
        self.diagnostic_labels[exe] = diagnostic

    def toggle_app(self, exe: str, enabled: bool) -> bool:
        info = self.discovered.get(exe)
        saved = self.store.set_app(
            exe,
            enabled,
            name=info.name if info else self.store.app_name(exe),
            path=info.path if info else self.store.app_path(exe),
        )
        self.app.refresh_app_state()
        if not saved:
            self.app.status_text.set("应用开关无法保存，原设置未改变")
            return False
        if self.window and self.window.winfo_exists():
            self.window.after_idle(self._render_app_lists)
        return True

    def _diagnostic_summary(self, exe: str) -> tuple[str, str]:
        value = self.store.capture_diagnostic(exe)
        if not value:
            return "尚未测试", FAINT
        status = str(value.get("status", ""))
        method = str(value.get("method", ""))
        reason = str(value.get("reason", ""))
        try:
            elapsed_ms = max(0, int(value.get("elapsed_ms", 0)))
            failures = max(0, int(value.get("consecutive_failures", 0)))
        except (TypeError, ValueError):
            elapsed_ms, failures = 0, 0
        method_name = {
            "word_com": "Word 专用读取",
            "excel_com": "Excel 专用读取",
            "outlook_com": "Outlook 专用读取",
            "powerpoint_com": "PPT 专用读取",
            "uia": "无障碍读取",
            "uia_descendant": "深层无障碍读取",
            "clipboard": "兼容读取",
        }.get(method, "直接读取")
        if reason == "restore_failed":
            if status in {"success", "warning"}:
                return "读取成功，但剪贴板还原失败", AMBER
            return "读取失败，且剪贴板还原失败", RED
        if status == "success":
            return f"{method_name} · {elapsed_ms} ms", GREEN
        if reason == "snapshot_unavailable":
            return "已保护富格式剪贴板", AMBER
        if reason == "protected":
            return "已跳过密码区域", FAINT
        if reason == "focus_changed":
            return "焦点切换，已取消", FAINT
        if failures >= 2:
            return f"连续 {failures} 次未读到", RED
        retry_label = hotkey_text_for_display(
            self.store.get("retry_hotkey", "Double Alt")
        )
        return f"本次未读到，可按 {retry_label} 重试", AMBER

    def refresh_diagnostic(self, exe: str) -> None:
        label = self.diagnostic_labels.get(exe.lower())
        if label and label.winfo_exists():
            text, color = self._diagnostic_summary(exe)
            label.configure(text=text, fg=color)

    def reset_adaptive_waits(self) -> None:
        self.store.reset_adaptive_waits()
        self.app.status_text.set("已恢复各应用的默认等待时间")

    def set_all_other(self, exes: list[str], enabled: bool) -> None:
        if not exes:
            return
        if not self.store.set_apps_bulk(exes, enabled):
            self.app.status_text.set("应用批量设置无法保存，原设置未改变")
            return
        self.app.refresh_app_state()
        self._render_app_lists()

    def toggle_other(self) -> None:
        previous = self.other_expanded
        self.other_expanded = not previous
        if not self.store.set("other_expanded", self.other_expanded):
            self.other_expanded = previous
            self.app.status_text.set("设置页展开状态无法保存")
        self._render_app_lists()

    def move_common(self, exe: str, common: bool) -> None:
        if not self.store.set_common(exe, common):
            self.app.status_text.set("常用应用排序无法保存，原设置未改变")
            return
        self._render_app_lists()

    def add_application(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.window,
            title="选择要启用划词翻译的应用",
            filetypes=[("Windows 应用", "*.exe")],
        )
        if not path:
            return
        exe = Path(path).name.lower()
        if exe in IGNORED_APPS:
            messagebox.showinfo(APP_NAME, "这个程序不适合作为划词目标。", parent=self.window)
            return
        name = FRIENDLY_NAMES.get(exe, Path(path).stem)
        if not self.store.set_app(exe, True, name=name, path=path):
            self.app.status_text.set("无法保存新增应用，原设置未改变")
            return
        if not self.store.set_common(exe, True):
            self.app.status_text.set("应用已启用，但无法保存到常用应用列表")
        self.app.refresh_app_state()
        self._render_app_lists()


class DesktopTranslatorApp:
    def __init__(self) -> None:
        self.store = SettingsStore()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("620x720")
        self.root.minsize(580, 660)
        self.root.configure(bg=WHITE)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_panel)
        self.root.bind("<Escape>", lambda _event: self.hide_active_window())
        self.root.attributes("-topmost", True)
        self.tk_icon: tk.PhotoImage | None = None
        self.apply_window_icon(self.root)

        self.display_mode = str(self.store.get("display_mode", "mini"))
        self.desktop_enabled = bool(self.store.get("desktop_enabled", True))
        # The desktop product no longer exposes an "automatic translation"
        # switch.  Migrate the legacy prototype value so an old false setting
        # cannot leave the mini window stuck on a loading message forever.
        self.auto_translate = True
        if not bool(self.store.get("auto_translate", True)):
            self.store.set("auto_translate", True)
        self.current_result: engine.TranslationResult | None = None
        self.current_app: AppInfo | None = None
        self.active_source = ""
        self.selection_point = (0, 0)
        self.selection_token = 0
        self.mini_dismissed_token: int | None = None
        self.pending_auto_speak_token: int | None = None
        self.pending_auto_speak_accent: str | None = None
        self.pending_auto_speak_mode: str | None = None
        self.auto_speak_lock = threading.Lock()
        self.pronunciation_click_delay_ms = max(
            1,
            int(round(windows_mouse_gesture_thresholds()[0] * 1000)),
        )
        self._pending_pronunciation_after_id: str | None = None
        self._pronunciation_click_epoch = 0
        self._speech_status_epoch = 0
        self._pronunciation_mouse_button: tk.Button | None = None
        self.latest_capture_identity: tuple[int, int] | None = None
        self.last_invalidated_capture_identity: tuple[int, int] | None = None

        self.status_text = tk.StringVar(value="正在启动桌面取词…")
        self.engine_text = tk.StringVar(value="正在加载本地翻译引擎…")
        self.speech_progress_text = tk.StringVar(value="正在并行预热 AI 发音…")
        self.direction_text = tk.StringVar(value="英文 → 中文")
        self.phonetic_text = tk.StringVar(value="选择英文后显示音标")
        self.meta_text = tk.StringVar(value="")
        self.app_text = tk.StringVar(value="尚未选中文字")
        self.mini_source = tk.StringVar(value="")
        self.mini_phonetic = tk.StringVar(value="")
        self.mini_translation = tk.StringVar(value="")
        self.mini_app_text = tk.StringVar(value="当前应用")
        self.mini_app_enabled = tk.BooleanVar(value=True)
        self.panel_app_enabled = tk.BooleanVar(value=True)

        self.quitting = False
        self.request_queue: queue.Queue[TranslationRequest | None] = queue.Queue(maxsize=1)
        self.ui_tasks: queue.SimpleQueue[tuple[Callable[..., None], tuple[object, ...]]] = queue.SimpleQueue()
        self.translator = engine.LocalTranslator(self.set_engine_status)
        self.speech = engine.SpeechPlayer(
            self.set_status,
            natural_speed=self.store.get(
                "natural_speech_speed", engine.DEFAULT_NATURAL_SPEECH_SPEED
            ),
            speech_status_callback=self.set_speech_status,
        )
        self.translation_worker: threading.Thread | None = None
        self.watcher: DesktopSelectionWatcher | None = None
        self.hotkey_service: WindowsHotkeyService | None = None
        self.tray: pystray.Icon | None = None
        self.tray_thread: threading.Thread | None = None
        self.tray_ready = threading.Event()
        self.tray_started_at: float | None = None
        self.tray_failure_reported = False
        self.quit_started_at: float | None = None
        self.shortcut_editor_active = False
        self.settings_window = SettingsWindow(self)

        self._build_panel()
        self._build_mini()
        self.refresh_mode_buttons()
        self._position_panel()
        if self.store.load_warning:
            self.status_text.set(self.store.load_warning)
            self.root.deiconify()
        elif self.display_mode == "mini":
            self.root.withdraw()
        self.root.after(15, self._drain_ui_tasks)

    def apply_window_icon(self, window: tk.Misc) -> None:
        png_path = app_icon_path("png")
        if not png_path.exists():
            return
        try:
            if self.tk_icon is None:
                self.tk_icon = tk.PhotoImage(file=str(png_path))
            window.iconphoto(True, self.tk_icon)
        except tk.TclError:
            pass

    def _build_panel(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)

        header = tk.Frame(self.root, bg=WHITE, padx=24, pady=16)
        header.grid(row=0, column=0, sticky="ew")
        brand = tk.Frame(header, bg=WHITE)
        brand.pack(side="left")
        tk.Label(brand, text=APP_NAME, bg=WHITE, fg=TEXT, font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        tk.Label(brand, text=f"本地翻译 · 英美发音 · {APP_AUTHOR}", bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(2, 0))
        actions = tk.Frame(header, bg=WHITE)
        actions.pack(side="right")
        self.pause_button = flat_button(actions, "暂停取词" if self.desktop_enabled else "恢复取词", self.toggle_desktop, compact=True)
        self.pause_button.pack(side="left", padx=(0, 7))
        flat_button(actions, "设置", self.open_settings, compact=True).pack(side="left", padx=(0, 7))
        flat_button(actions, "收进托盘", self.hide_panel, compact=True).pack(side="left")

        tk.Frame(self.root, bg=LINE, height=1).grid(row=0, column=0, sticky="sew")
        body = tk.Frame(self.root, bg=PAGE, padx=22, pady=16)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(4, weight=1)

        mode_row = tk.Frame(body, bg=PAGE)
        mode_row.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        mode_box = tk.Frame(mode_row, bg="#ECEFF3", padx=3, pady=3)
        mode_box.pack(side="right")
        self.panel_mini_button = flat_button(mode_box, "迷你浮窗", lambda: self.set_mode("mini"), compact=True)
        self.panel_mini_button.pack(side="left")
        self.panel_large_button = flat_button(mode_box, "大窗口", lambda: self.set_mode("panel"), compact=True)
        self.panel_large_button.pack(side="left", padx=(3, 0))
        tk.Label(
            mode_row,
            textvariable=self.app_text,
            bg=PAGE,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
            width=1,
        ).pack(side="left", fill="x", expand=True)

        sound_card = self._card(body)
        sound_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        sound_card.grid_columnconfigure(0, weight=1, minsize=0)
        sound_labels = tk.Frame(sound_card, bg=WHITE)
        sound_labels.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        tk.Label(
            sound_labels,
            text="发音 · 单双击由“自动发音偏好”统一控制",
            bg=WHITE,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w")
        tk.Label(
            sound_labels,
            textvariable=self.phonetic_text,
            bg=WHITE,
            fg=BLUE,
            font=("Segoe UI", 13, "bold"),
            anchor="w",
            width=1,
        ).pack(fill="x", pady=(4, 0))
        sound_actions = tk.Frame(sound_card, bg=WHITE)
        sound_actions.grid(row=0, column=1, sticky="e")
        self._make_pronunciation_button(sound_actions, "US  美音", "us").pack(
            side="left", padx=(0, 7)
        )
        self._make_pronunciation_button(sound_actions, "UK  英音", "uk").pack(
            side="left"
        )
        tk.Label(
            sound_card,
            textvariable=self.speech_progress_text,
            bg=WHITE,
            fg=MUTED,
            font=("Microsoft YaHei UI", 7),
            anchor="w",
            justify="left",
            width=1,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(7, 0))

        source_card = self._card(body)
        source_card.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        source_head = tk.Frame(source_card, bg=WHITE)
        source_head.pack(fill="x")
        tk.Label(source_head, text="原文", bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side="left")
        tk.Label(source_head, textvariable=self.direction_text, bg=WHITE, fg=BLUE, font=("Microsoft YaHei UI", 8, "bold")).pack(side="right")
        self.source_text = tk.Text(
            source_card,
            height=2,
            wrap="word",
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg=WHITE,
            fg=TEXT,
            insertbackground=TEXT,
            font=("Segoe UI", 11),
            pady=8,
        )
        self.source_text.pack(fill="x")
        source_tools = tk.Frame(source_card, bg=WHITE)
        source_tools.pack(fill="x")
        flat_button(source_tools, "翻译", self.translate_manual, primary=True, compact=True).pack(side="right")

        result_card = self._card(body)
        result_card.grid(row=4, column=0, sticky="nsew", pady=(0, 10))
        result_card.grid_rowconfigure(1, weight=1)
        result_card.grid_columnconfigure(0, weight=1)
        result_head = tk.Frame(result_card, bg=WHITE)
        result_head.grid(row=0, column=0, sticky="ew")
        tk.Label(result_head, text="直译", bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side="left")
        tk.Label(result_head, textvariable=self.meta_text, bg=WHITE, fg=FAINT, font=("Microsoft YaHei UI", 7)).pack(side="right")
        self.result_text = tk.Text(
            result_card,
            height=4,
            wrap="word",
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg=WHITE,
            fg=TEXT,
            insertbackground=TEXT,
            font=("Microsoft YaHei UI", 11),
            pady=9,
        )
        self.result_text.grid(row=1, column=0, sticky="nsew")
        self._set_text(self.result_text, "双击单词或拖动选择句子，结果会显示在这里。")
        result_tools = tk.Frame(result_card, bg=WHITE)
        result_tools.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        flat_button(result_tools, "复制译文", self.copy_translation, compact=True).pack(side="left")
        current_app = tk.Frame(result_tools, bg=WHITE)
        current_app.pack(side="right")
        tk.Label(current_app, text="在当前应用启用", bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(0, 7))
        self.panel_app_toggle = ToggleSwitch(current_app, self.panel_app_enabled, self.toggle_current_app)
        self.panel_app_toggle.pack(side="left")

        footer = tk.Frame(body, bg=PAGE)
        footer.grid(row=5, column=0, sticky="ew")
        tk.Label(footer, textvariable=self.status_text, bg=PAGE, fg=MUTED, font=("Microsoft YaHei UI", 7)).pack(side="left")
        tk.Label(footer, textvariable=self.engine_text, bg=PAGE, fg=MUTED, font=("Microsoft YaHei UI", 7)).pack(side="right")

    @staticmethod
    def _card(parent: tk.Misc) -> tk.Frame:
        return tk.Frame(parent, bg=WHITE, padx=16, pady=13, highlightthickness=1, highlightbackground=LINE)

    def _make_pronunciation_button(
        self,
        parent: tk.Misc,
        text: str,
        accent: str,
    ) -> tk.Button:
        """Build one accessible button with exclusive single/double-click actions."""

        button: tk.Button

        def activate() -> None:
            self._activate_pronunciation_button(button, accent)

        button = flat_button(parent, text, activate, compact=True)
        button.configure(takefocus=True)
        button.bind(
            "<ButtonPress-1>",
            lambda event: self._on_pronunciation_button_press(event, accent),
            add="+",
        )
        button.bind(
            "<ButtonRelease-1>",
            self._on_pronunciation_button_release,
            add="+",
        )
        button.bind(
            "<Double-ButtonPress-1>",
            lambda event: self._on_pronunciation_button_double_press(event, accent),
            add="+",
        )
        button.bind(
            "<Return>",
            lambda _event: self._invoke_pronunciation_button_from_keyboard(button),
            add="+",
        )
        return button

    def _on_pronunciation_button_press(
        self,
        event: tk.Event[tk.Misc],
        accent: str,
    ) -> None:
        """Start useful synthesis immediately, but defer audible single-click output."""

        self.cancel_pending_pronunciation_click()
        cancel = getattr(self.speech, "cancel", None)
        if callable(cancel):
            # The press is already a new pronunciation intent. Stop an older
            # clip now instead of letting it continue through Tk's double-click
            # arbitration window.
            cancel()
        self._pronunciation_mouse_button = event.widget  # type: ignore[assignment]
        token = self.selection_token
        text = self.english_text()
        recorder = getattr(self.speech, "record_timing_event", None)
        if callable(recorder):
            recorder("button_pressed", accent=accent, selection_token=token)
        if text:
            self._set_speech_status_now(
                f"已收到，正在准备可取消的 {accent.upper()} AI 候选…"
            )
        elif self.active_source and engine.contains_chinese(self.active_source):
            self._set_speech_status_now(
                f"已收到，等待翻译后准备可取消的 {accent.upper()} AI 候选…"
            )
        if token != self.selection_token or not text:
            return
        prefetch = getattr(self.speech, "prefetch", None)
        if callable(prefetch):
            prefetch(text, accent)

    def _on_pronunciation_button_release(
        self,
        event: tk.Event[tk.Misc],
    ) -> None:
        """Clear a mouse-origin marker after Tk's class binding invokes command."""

        try:
            self.root.after_idle(
                self._clear_pronunciation_mouse_button,
                event.widget,
            )
        except (tk.TclError, AttributeError):
            self._clear_pronunciation_mouse_button(event.widget)

    def _clear_pronunciation_mouse_button(self, widget: object) -> None:
        if getattr(self, "_pronunciation_mouse_button", None) is widget:
            self._pronunciation_mouse_button = None

    @staticmethod
    def _speech_preparing_status(accent: str, mode: str) -> str:
        voice_name = "微软原版发音" if mode == "system" else "AI 发音"
        return f"已收到，正在准备 {accent.upper()} {voice_name}…"

    @staticmethod
    def _speech_waiting_for_translation_status(accent: str, mode: str) -> str:
        voice_name = "微软原版发音" if mode == "system" else "AI 发音"
        return f"已收到，等待翻译后准备 {accent.upper()} {voice_name}…"

    def _on_pronunciation_button_double_press(
        self,
        event: tk.Event[tk.Misc],
        accent: str,
    ) -> str:
        """Cancel the pending single and finalize exactly one double-click route."""

        self.cancel_pending_pronunciation_click()
        mode = self._speech_mode_for_gesture("double")
        if mode == "system":
            self._cancel_neural_candidate()
        recorder = getattr(self.speech, "record_timing_event", None)
        if callable(recorder):
            recorder("arbitration_completed", gesture="double", mode=mode, accent=accent)
        self.speak(accent, mode=mode, expected_token=self.selection_token)
        # The first release already restored Tk's button state. Preventing the
        # second ButtonDown means its later release cannot invoke `command` and
        # accidentally schedule a second, natural-voice request.
        return "break"

    def _invoke_pronunciation_button_from_keyboard(self, button: tk.Button) -> str:
        """Keep Space/Return/assistive invoke immediate instead of mouse-delayed."""

        self.cancel_pending_pronunciation_click()
        button.invoke()
        return "break"

    def _activate_pronunciation_button(
        self,
        button: tk.Button,
        accent: str,
    ) -> None:
        mouse_activation = (
            getattr(self, "_pronunciation_mouse_button", None) is button
        )
        self._pronunciation_mouse_button = None
        if mouse_activation:
            self._schedule_single_pronunciation(accent)
            return
        self.cancel_pending_pronunciation_click()
        mode = self._speech_mode_for_gesture("single")
        if mode == "system":
            self._cancel_neural_candidate()
        self.speak(accent, mode=mode, expected_token=self.selection_token)

    def _schedule_single_pronunciation(self, accent: str) -> None:
        self.cancel_pending_pronunciation_click()
        token = self.selection_token
        epoch = self._pronunciation_click_epoch
        delay_ms = int(
            getattr(
                self,
                "pronunciation_click_delay_ms",
                round(windows_mouse_gesture_thresholds()[0] * 1000),
            )
        )

        def play_if_current() -> None:
            if epoch != getattr(self, "_pronunciation_click_epoch", 0):
                return
            self._pending_pronunciation_after_id = None
            if bool(getattr(self, "quitting", False)):
                return
            mode = self._speech_mode_for_gesture("single")
            if mode == "system":
                self._cancel_neural_candidate()
            recorder = getattr(self.speech, "record_timing_event", None)
            if callable(recorder):
                recorder(
                    "arbitration_completed",
                    gesture="single",
                    mode=mode,
                    accent=accent,
                )
            self.speak(accent, mode=mode, expected_token=token)

        try:
            self._pending_pronunciation_after_id = self.root.after(
                max(1, delay_ms),
                play_if_current,
            )
        except (tk.TclError, AttributeError):
            self._pending_pronunciation_after_id = None

    # Retain the old private name for adapters while routing through the unified
    # preference policy.
    def _schedule_natural_pronunciation(self, accent: str) -> None:
        self._schedule_single_pronunciation(accent)

    def _speech_mode_for_gesture(self, gesture: str) -> str:
        store = getattr(self, "store", None)
        preference = (
            store.get(
                "auto_speech_preference",
                engine.DEFAULT_AUTO_SPEECH_PREFERENCE,
            )
            if store is not None
            else engine.DEFAULT_AUTO_SPEECH_PREFERENCE
        )
        return engine.speech_mode_for_gesture(preference, gesture)

    def _cancel_neural_candidate(self) -> None:
        cancel_prefetch = getattr(self.speech, "cancel_prefetch", None)
        if callable(cancel_prefetch):
            cancel_prefetch(reason="gesture_resolved_to_system")

    def cancel_pending_pronunciation_click(self) -> None:
        """Invalidate and remove an unheard mouse-single request, if present."""

        self._pronunciation_click_epoch = (
            int(getattr(self, "_pronunciation_click_epoch", 0)) + 1
        )
        # Any already-queued status belongs to the old click/selection. Core
        # callbacks carry this epoch onto Tk's queue so stale playback text
        # cannot overwrite feedback from a newer user intent.
        self._speech_status_epoch = int(
            getattr(self, "_speech_status_epoch", 0)
        ) + 1
        after_id = getattr(self, "_pending_pronunciation_after_id", None)
        self._pending_pronunciation_after_id = None
        self._pronunciation_mouse_button = None
        if after_id is None:
            return
        try:
            self.root.after_cancel(after_id)
        except (tk.TclError, AttributeError):
            pass
        self._set_speech_status_now("待播放发音已取消")

    def _build_mini(self) -> None:
        self.mini = tk.Toplevel(self.root)
        self.mini.withdraw()
        self.mini.overrideredirect(True)
        self.mini.attributes("-topmost", True)
        self.mini.configure(bg=LINE)
        self.mini.bind("<Escape>", lambda _event: self.hide_mini())

        card = tk.Frame(self.mini, bg=WHITE, highlightthickness=1, highlightbackground=LINE)
        card.pack(fill="both", expand=True)
        top = tk.Frame(card, bg=WHITE, padx=13, pady=9)
        top.pack(fill="x")
        # Reserve the fixed sound controls before giving the remaining width to
        # long source text. Tk pack otherwise lets the text consume the row.
        sound_actions = tk.Frame(top, bg=WHITE)
        sound_actions.pack(side="right", padx=(8, 0))
        self._make_pronunciation_button(sound_actions, "US", "us").pack(
            side="left", padx=(0, 5)
        )
        self._make_pronunciation_button(sound_actions, "UK", "uk").pack(
            side="left", padx=(0, 4)
        )
        tk.Button(
            sound_actions,
            text="×",
            command=self.hide_mini,
            relief="flat",
            bd=0,
            bg=WHITE,
            activebackground=RED_SOFT,
            fg=MUTED,
            font=("Segoe UI", 13),
            padx=5,
            pady=1,
            cursor="hand2",
        ).pack(side="left")
        text_block = tk.Frame(top, bg=WHITE)
        text_block.pack(side="left", fill="x", expand=True)
        tk.Label(
            text_block,
            textvariable=self.mini_source,
            bg=WHITE,
            fg=TEXT,
            anchor="w",
            font=("Segoe UI", 11, "bold"),
            width=1,
        ).pack(fill="x")
        tk.Label(
            text_block,
            textvariable=self.mini_phonetic,
            bg=WHITE,
            fg=BLUE,
            anchor="w",
            font=("Segoe UI", 9, "bold"),
            width=1,
        ).pack(fill="x", pady=(2, 0))
        tk.Label(
            text_block,
            textvariable=self.speech_progress_text,
            bg=WHITE,
            fg=MUTED,
            anchor="w",
            font=("Microsoft YaHei UI", 7),
            width=1,
        ).pack(fill="x", pady=(2, 0))
        tk.Frame(card, bg=LINE, height=1).pack(fill="x", padx=13)
        bottom = tk.Frame(card, bg=WHITE, padx=11)
        # Reserve the fixed bottom row before the expandable translation.
        # This keeps the app switch and actions visible when long text or a
        # high DPI scale makes the requested window taller than the screen.
        bottom.pack(side="bottom", fill="x", pady=(0, 7))
        bottom_actions = tk.Frame(bottom, bg=WHITE)
        bottom_actions.pack(side="right", padx=(8, 0))
        flat_button(bottom_actions, "展开", self.show_panel, compact=True).pack(side="right")
        flat_button(bottom_actions, "复制译文", self.copy_translation, compact=True).pack(side="right", padx=(0, 5))
        current_app = tk.Frame(bottom, bg=WHITE)
        current_app.pack(side="left", fill="x", expand=True)
        self.mini_app_toggle = ToggleSwitch(current_app, self.mini_app_enabled, self.toggle_current_app)
        self.mini_app_toggle.pack(side="right", padx=(7, 0))
        tk.Label(
            current_app,
            textvariable=self.mini_app_text,
            bg=WHITE,
            fg=MUTED,
            font=("Microsoft YaHei UI", 7),
            anchor="w",
            width=1,
        ).pack(side="left", fill="x", expand=True)
        tk.Label(
            card,
            textvariable=self.mini_translation,
            bg=WHITE,
            fg=TEXT,
            justify="left",
            anchor="nw",
            wraplength=400,
            font=("Microsoft YaHei UI", 10),
        ).pack(fill="both", expand=True, padx=13, pady=(8, 5))

    def _position_panel(self) -> None:
        self.root.update_idletasks()
        try:
            work_area = current_monitor_work_area(
                self.root.winfo_screenwidth(),
                self.root.winfo_screenheight(),
            )
            fit_tk_window_to_work_area(self.root, work_area, 620, 720, 580, 660)
        except Exception as exc:
            log(f"Could not fit panel to monitor work area: {exc}")
            width = max(620, self.root.winfo_width())
            screen_width = self.root.winfo_screenwidth()
            self.root.geometry(f"+{max(20, screen_width - width - 30)}+45")

    def position_mini(self, x: int, y: int) -> tuple[int, int, int, int]:
        width = 440
        self.mini.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        preferred_cap = max(238, min(420, screen_height // 3))
        height = max(
            188,
            min(
                self.mini.winfo_reqheight() + 3,
                preferred_cap,
                screen_height - 16,
            ),
        )
        target_x = min(max(8, x + 14), max(8, screen_width - width - 8))
        target_y = y + 18
        if target_y + height > screen_height - 8:
            target_y = max(8, y - height - 14)
        self.mini.geometry(f"{width}x{height}+{target_x}+{target_y}")
        return target_x, target_y, width, height

    def show_mini_no_activate(self, x: int, y: int) -> None:
        target_x, target_y, width, height = self.position_mini(x, y)
        self.mini.update_idletasks()
        hwnd = 0
        previous_style: int | None = None
        try:
            inner_hwnd = self.mini.winfo_id()
            hwnd = ctypes.windll.user32.GetParent(inner_hwnd) or inner_hwnd
            previous_style = int(_GET_WINDOW_LONG_PTR(hwnd, GWL_EXSTYLE))
            # Keep the mini window non-activating for its entire visible
            # lifetime. Restoring the style immediately after mapping leaves a
            # race where Ctrl+C can land in the popup instead of the source app.
            ctypes.set_last_error(0)
            style_result = _SET_WINDOW_LONG_PTR(
                hwnd,
                GWL_EXSTYLE,
                previous_style | WS_EX_NOACTIVATE,
            )
            style_error = int(ctypes.get_last_error())
            if style_result == 0 and style_error:
                raise ctypes.WinError(style_error)
            self.mini.deiconify()
            ctypes.windll.user32.SetWindowPos(
                hwnd,
                -1,
                target_x,
                target_y,
                width,
                height,
                0x0010 | 0x0040,
            )
            ctypes.windll.user32.ShowWindow(hwnd, 4)
        except Exception as exc:
            if hwnd and previous_style is not None:
                self._restore_window_activation_style(hwnd, previous_style)
            self.mini.withdraw()
            log(f"Could not show mini window without activation: {exc}")
            toggle_label = hotkey_text_for_display(
                self.store.get("toggle_mode_hotkey", "Alt+C")
            )
            self.status_text.set(f"无法无干扰地显示迷你浮窗，请按 {toggle_label} 重试")

    def set_mode(self, mode: str) -> None:
        if mode not in {"mini", "panel"}:
            return
        self.display_mode = mode
        saved = self.store.set("display_mode", mode)
        if mode == "panel":
            self.hide_mini(stop_speech=False)
            self.show_panel()
        else:
            self.root.withdraw()
            self.hide_mini(stop_speech=False)
        self.refresh_mode_buttons()
        if self.settings_window.window and self.settings_window.window.winfo_exists():
            self.settings_window.refresh_mode_buttons()
        self.update_tray_menu()
        if not saved:
            self.status_text.set("窗口模式已临时切换，但无法保存到磁盘")

    def refresh_mode_buttons(self) -> None:
        for mode, button in (("mini", self.panel_mini_button), ("panel", self.panel_large_button)):
            selected = self.display_mode == mode
            button.configure(
                bg=BLUE if selected else BLUE_SOFT,
                fg=WHITE if selected else BLUE,
                activebackground="#2858C7" if selected else "#E3EBFF",
                activeforeground=WHITE if selected else BLUE,
            )

    def show_panel(self) -> None:
        self.display_mode = "panel"
        saved = self.store.set("display_mode", "panel")
        self.hide_mini(stop_speech=False)
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.refresh_mode_buttons()
        self.update_tray_menu()
        if not saved:
            self.status_text.set("大窗口已打开，但默认模式无法保存")

    def show_panel_no_activate(self) -> None:
        """Show the full panel without stealing the source app focus."""

        self.hide_mini(stop_speech=False)
        self.root.update_idletasks()
        hwnd = 0
        previous_style: int | None = None
        try:
            inner_hwnd = self.root.winfo_id()
            hwnd = ctypes.windll.user32.GetParent(inner_hwnd) or inner_hwnd
            previous_style = int(_GET_WINDOW_LONG_PTR(hwnd, GWL_EXSTYLE))
            # The no-activate style must exist before Tk maps the first frame;
            # applying it only after deiconify is too late to protect focus.
            ctypes.set_last_error(0)
            style_result = _SET_WINDOW_LONG_PTR(
                hwnd,
                GWL_EXSTYLE,
                previous_style | WS_EX_NOACTIVATE,
            )
            style_error = int(ctypes.get_last_error())
            if style_result == 0 and style_error:
                raise ctypes.WinError(style_error)
            self.root.deiconify()
            ctypes.windll.user32.SetWindowPos(
                hwnd,
                -1,
                self.root.winfo_x(),
                self.root.winfo_y(),
                self.root.winfo_width(),
                self.root.winfo_height(),
                0x0010 | 0x0040,
            )
            ctypes.windll.user32.ShowWindow(hwnd, 4)
            self.root.after_idle(
                self._restore_window_activation_style,
                hwnd,
                previous_style,
            )
        except Exception as exc:
            if hwnd and previous_style is not None:
                self._restore_window_activation_style(hwnd, previous_style)
            log(f"Could not show panel without activation: {exc}")
            self.status_text.set("无法无干扰地显示大窗口，请从托盘打开")

    @staticmethod
    def _restore_window_activation_style(hwnd: int, previous_style: int) -> None:
        try:
            ctypes.set_last_error(0)
            style_result = _SET_WINDOW_LONG_PTR(hwnd, GWL_EXSTYLE, previous_style)
            style_error = int(ctypes.get_last_error())
            if style_result == 0 and style_error:
                raise ctypes.WinError(style_error)
            ctypes.windll.user32.SetWindowPos(
                hwnd,
                0,
                0,
                0,
                0,
                0,
                0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020,
            )
        except Exception as exc:
            log(f"Could not restore window activation style: {exc}")

    def _populate_mini_from_current(self) -> None:
        if not self.active_source:
            return
        source = (
            self.active_source
            if len(self.active_source) <= 48
            else self.active_source[:47].rstrip() + "…"
        )
        self.mini_source.set(source)
        if self.current_result and self.current_result.source == self.active_source:
            self.mini_phonetic.set(
                f"/{self.current_result.phonetic}/"
                if self.current_result.phonetic
                else "整句发音"
            )
            self.mini_translation.set(
                self.compact_translation(self.current_result.translated)
            )
        else:
            self.mini_phonetic.set(
                "正在查询音标…"
                if engine.WORD_PATTERN.fullmatch(self.active_source)
                else "整句发音"
            )
            self.mini_translation.set("正在本地翻译…")

    def toggle_mode_from_hotkey(self) -> None:
        if self.display_mode == "mini":
            self.display_mode = "panel"
            saved = self.store.set("display_mode", "panel")
            self.show_panel_no_activate()
        else:
            self.display_mode = "mini"
            saved = self.store.set("display_mode", "mini")
            self.root.withdraw()
            if self.active_source:
                self.mini_dismissed_token = None
                self._populate_mini_from_current()
                self.show_mini_no_activate(*self.selection_point)
            else:
                self.hide_mini()
        self.refresh_mode_buttons()
        if self.settings_window.window and self.settings_window.window.winfo_exists():
            self.settings_window.refresh_mode_buttons()
        self.update_tray_menu()
        if not saved:
            self.status_text.set("窗口模式已临时切换，但无法保存到磁盘")

    def hide_panel(self) -> None:
        self.cancel_pending_pronunciation_click()
        self.cancel_pending_auto_speak()
        self.speech.cancel()
        self.root.withdraw()

    def hide_mini(self, *, stop_speech: bool = True) -> None:
        # Remember an explicit dismissal for the current asynchronous result.
        # Otherwise a translation that finishes a moment later would map the
        # popup again after the user clicked outside or pressed close.
        if stop_speech:
            self.cancel_pending_pronunciation_click()
            self.cancel_pending_auto_speak()
            self.speech.cancel()
        self.mini_dismissed_token = self.selection_token
        if self.mini.winfo_exists():
            self.mini.withdraw()

    def hide_active_window(self) -> None:
        if self.mini.winfo_viewable():
            self.hide_mini()
        else:
            self.hide_panel()

    def on_global_mouse_down(self, x: int, y: int) -> None:
        self._post_ui(self._handle_global_mouse_down, x, y)

    def _handle_global_mouse_down(self, x: int, y: int) -> None:
        # The global hook sees our own pronunciation click before Tk invokes its
        # button command. Do not let that internal notification cancel the new
        # request; any external press, however, makes the old delayed click stale.
        if self._point_is_inside_app(x, y):
            return
        self.cancel_pending_pronunciation_click()
        self._dismiss_mini_if_outside(x, y)

    def _dismiss_mini_if_outside(self, x: int, y: int) -> None:
        if not self.mini.winfo_viewable():
            return
        left = self.mini.winfo_rootx()
        top = self.mini.winfo_rooty()
        right = left + self.mini.winfo_width()
        bottom = top + self.mini.winfo_height()
        if not (left <= x <= right and top <= y <= bottom):
            self.hide_mini()

    def open_settings(self) -> None:
        self.settings_window.show()

    def set_desktop_enabled(self, enabled: bool) -> bool:
        requested = bool(enabled)
        if not self.store.set("desktop_enabled", requested):
            self.desktop_enabled = bool(
                self.store.get("desktop_enabled", self.desktop_enabled)
            )
            self.pause_button.configure(
                text="暂停取词" if self.desktop_enabled else "恢复取词"
            )
            self.status_text.set("桌面取词开关无法保存，原设置未改变")
            self.update_tray_menu()
            return False
        self.desktop_enabled = requested
        self.pause_button.configure(text="暂停取词" if self.desktop_enabled else "恢复取词")
        self.status_text.set("桌面取词已开启" if self.desktop_enabled else "桌面取词已暂停")
        if not self.desktop_enabled:
            self._invalidate_active_request()
            self.speech.cancel()
            self._clear_translation_display("桌面取词已暂停")
            self.hide_mini()
        self.update_tray_menu()
        return True

    def set_natural_speech_speed(self, profile: object) -> bool:
        normalized = str(profile or "").strip().lower()
        if normalized not in engine.NATURAL_SPEECH_SPEEDS:
            normalized = engine.DEFAULT_NATURAL_SPEECH_SPEED
        previous = str(
            self.store.get(
                "natural_speech_speed", engine.DEFAULT_NATURAL_SPEECH_SPEED
            )
        ).lower()
        if not self.store.set("natural_speech_speed", normalized):
            self.status_text.set("句子 AI 发音语速无法保存，原设置未改变")
            return False
        if previous != normalized:
            self.speech.set_natural_speed(normalized)
        label = {"slow": "慢", "standard": "标准", "fast": "快"}[normalized]
        self.status_text.set(f"句子 AI 发音语速已设为{label}")
        return True

    def set_auto_speech_preference(self, preference: object) -> bool:
        normalized = engine.normalize_auto_speech_preference(preference)
        previous = engine.normalize_auto_speech_preference(
            self.store.get(
                "auto_speech_preference",
                engine.DEFAULT_AUTO_SPEECH_PREFERENCE,
            )
        )
        if not self.store.set("auto_speech_preference", normalized):
            self.status_text.set("自动发音偏好无法保存，原设置未改变")
            return False
        if previous != normalized:
            self.cancel_pending_pronunciation_click()
            self.cancel_pending_auto_speak()
            self.speech.cancel()
            recorder = getattr(self.speech, "record_timing_event", None)
            if callable(recorder):
                recorder(
                    "cancelled",
                    reason="preference_changed",
                    preference=normalized,
                )
        label = "优先速度" if normalized == "speed" else "优先自然音色"
        self.status_text.set(f"自动发音偏好已设为“{label}”")
        return True

    def toggle_desktop(self) -> None:
        self.set_desktop_enabled(not bool(self.store.get("desktop_enabled", True)))

    def hotkey_summary(self) -> str:
        retry = hotkey_text_for_display(self.store.get("retry_hotkey", "Double Alt"))
        toggle = hotkey_text_for_display(self.store.get("toggle_mode_hotkey", "Alt+C"))
        return f"{retry} 重新获取并自动播放美音；{toggle} 切换迷你浮窗与大窗口"

    def _sync_double_alt_gesture(self, retry_spec: object | None = None) -> None:
        watcher = getattr(self, "watcher", None)
        if watcher is None:
            return
        if retry_spec is None:
            retry_spec = self._get_configured_hotkey_specs()[0]
        binding_kind = getattr(retry_spec, "binding_kind", None)
        runtime_enabled = (
            bool(self.store.get("hotkeys_enabled", True))
            and not bool(getattr(self, "shortcut_editor_active", False))
        )
        watcher.set_hotkey_requests_enabled(runtime_enabled)
        watcher.set_double_alt_enabled(
            runtime_enabled and binding_kind is HotkeyBindingKind.DOUBLE_ALT
        )

    def set_shortcut_editor_active(self, active: bool) -> None:
        active = bool(active)
        self.shortcut_editor_active = active
        self._sync_double_alt_gesture()
        service = getattr(self, "hotkey_service", None)
        if service is None or not bool(self.store.get("hotkeys_enabled", True)):
            return
        try:
            if active:
                if not service.stop(timeout_seconds=1.0):
                    raise TimeoutError("快捷键线程未能及时暂停")
            else:
                report = service.restart(
                    hotkey_specs=self._get_configured_hotkey_specs(),
                    ready_timeout_seconds=1.0,
                )
                if report is None or not report.all_registered:
                    message = (
                        "快捷键注册超时"
                        if report is None
                        else registration_status_text(report)
                    )
                    self.set_status(message)
        except Exception as exc:
            log(f"Could not {'pause' if active else 'resume'} hotkeys for editor: {exc}")
            self.set_status("快捷键监听暂时不可用，请关闭设置后重试")

    def _get_configured_hotkey_specs(self) -> tuple[object, object]:
        """Parse persisted shortcuts, falling back safely after manual damage."""

        try:
            retry = parse_hotkey_spec(
                HotkeyCommand.RETRY_AND_SPEAK_US,
                hotkey_text_for_storage(self.store.get("retry_hotkey", "Double Alt")),
            )
        except (TypeError, ValueError):
            retry = parse_hotkey_spec(HotkeyCommand.RETRY_AND_SPEAK_US, "Double Alt")
        try:
            toggle = parse_hotkey_spec(
                HotkeyCommand.TOGGLE_WINDOW_MODE,
                hotkey_text_for_storage(self.store.get("toggle_mode_hotkey", "Alt+C")),
            )
        except (TypeError, ValueError):
            toggle = parse_hotkey_spec(HotkeyCommand.TOGGLE_WINDOW_MODE, "Alt+C")
        if format_hotkey_spec(retry).casefold() == format_hotkey_spec(toggle).casefold():
            retry = parse_hotkey_spec(HotkeyCommand.RETRY_AND_SPEAK_US, "Double Alt")
            toggle = parse_hotkey_spec(HotkeyCommand.TOGGLE_WINDOW_MODE, "Alt+C")
        return retry, toggle

    def apply_hotkey_settings(self, retry_text: str, toggle_text: str) -> tuple[bool, str]:
        """Validate, register and only then persist an edited shortcut pair."""

        retry_text = hotkey_text_for_storage(retry_text)
        toggle_text = hotkey_text_for_storage(toggle_text)
        try:
            retry_spec = parse_hotkey_spec(HotkeyCommand.RETRY_AND_SPEAK_US, retry_text)
        except (TypeError, ValueError) as exc:
            return False, f"“重新获取并自动美音”快捷键无效：{exc}"
        try:
            toggle_spec = parse_hotkey_spec(HotkeyCommand.TOGGLE_WINDOW_MODE, toggle_text)
        except (TypeError, ValueError) as exc:
            return False, f"“切换窗口模式”快捷键无效：{exc}"

        retry_canonical = hotkey_text_for_storage(format_hotkey_spec(retry_spec))
        toggle_canonical = hotkey_text_for_storage(format_hotkey_spec(toggle_spec))
        if toggle_canonical.casefold() == "double alt":
            return False, "双击 Alt 仅用于重新获取并自动美音；窗口切换请使用组合键。"
        if retry_canonical.casefold() == toggle_canonical.casefold():
            return False, "两个功能不能使用同一个快捷键，原设置未改变。"

        old_specs = self._get_configured_hotkey_specs()
        old_retry_text = hotkey_text_for_storage(
            self.store.get("retry_hotkey", "Double Alt")
        )
        old_toggle_text = hotkey_text_for_storage(
            self.store.get("toggle_mode_hotkey", "Alt+C")
        )
        service = getattr(self, "hotkey_service", None)
        if service is None:
            return False, "快捷键服务尚未就绪，原设置未改变。"
        logical_enabled = bool(self.store.get("hotkeys_enabled", True))
        keep_running = logical_enabled and not bool(
            getattr(self, "shortcut_editor_active", False)
        )

        try:
            report = service.restart(
                hotkey_specs=(retry_spec, toggle_spec),
                ready_timeout_seconds=1.0,
            )
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            report = None
            failure_text = f"无法应用快捷键：{exc}"
        else:
            if report is None:
                failure_text = "快捷键注册超时"
            elif not report.all_registered:
                failure_text = registration_status_text(report)
            else:
                failure_text = ""

        if report is None or not report.all_registered:
            rollback_failure = ""
            try:
                if keep_running:
                    rollback_report = service.restart(
                        hotkey_specs=old_specs,
                        ready_timeout_seconds=1.0,
                    )
                    if rollback_report is None or not rollback_report.all_registered:
                        rollback_failure = "；原快捷键恢复失败，请重启软件"
                elif not service.stop(timeout_seconds=1.0):
                    rollback_failure = "；占用检查线程未能及时停止，请重启软件"
            except Exception as exc:
                rollback_failure = "；原快捷键恢复失败，请重启软件"
                log(f"Could not restore previous hotkeys: {exc}")
            message = (
                f"{failure_text}；请换一个组合，原设置未改变"
                f"{rollback_failure}。"
            )
            self.status_text.set(message)
            return False, message

        if not keep_running and not service.stop(timeout_seconds=1.0):
            message = "快捷键占用检查未能及时结束，原设置未改变；请重启软件后再试。"
            self.status_text.set(message)
            return False, message

        self.store.set("retry_hotkey", retry_canonical, save=False)
        self.store.set("toggle_mode_hotkey", toggle_canonical, save=False)
        save_result = self.store.save()
        if save_result is False:
            self.store.set("retry_hotkey", old_retry_text, save=False)
            self.store.set("toggle_mode_hotkey", old_toggle_text, save=False)
            runtime_failure = ""
            try:
                if keep_running:
                    rollback_report = service.restart(
                        hotkey_specs=old_specs,
                        ready_timeout_seconds=1.0,
                    )
                    if rollback_report is None or not rollback_report.all_registered:
                        runtime_failure = "；原快捷键运行态恢复失败，请重启软件"
            except Exception as exc:
                runtime_failure = "；原快捷键运行态恢复失败，请重启软件"
                log(f"Could not restore hotkeys after save failure: {exc}")
            message = f"快捷键设置无法写入磁盘，原设置未改变{runtime_failure}。"
            self.status_text.set(message)
            return False, message

        self._sync_double_alt_gesture(retry_spec)
        self.settings_window.refresh_shortcut_state()
        self.update_tray_menu()
        message = (
            f"快捷键已应用：{hotkey_text_for_display(retry_canonical)}、"
            f"{hotkey_text_for_display(toggle_canonical)}"
        )
        self.status_text.set(message)
        return True, message

    def set_hotkeys_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        old_enabled = bool(self.store.get("hotkeys_enabled", True))
        service = getattr(self, "hotkey_service", None)
        watcher = getattr(self, "watcher", None)

        if enabled:
            if service is None:
                self.status_text.set("快捷键服务尚未就绪，未能启用")
                self.settings_window.refresh_shortcut_state()
                return
            retry, toggle = self._get_configured_hotkey_specs()
            self.status_text.set(
                f"正在检查 {hotkey_text_for_display(format_hotkey_spec(retry))}、"
                f"{hotkey_text_for_display(format_hotkey_spec(toggle))}…"
            )
            try:
                report = service.restart(
                    hotkey_specs=(retry, toggle),
                    ready_timeout_seconds=1.0,
                )
            except Exception as exc:
                report = None
                failure_text = f"全局快捷键启用失败：{exc}"
            else:
                failure_text = (
                    "全局快捷键注册超时"
                    if report is None
                    else registration_status_text(report)
                )
            if report is None or not report.all_registered:
                service.stop(timeout_seconds=1.0)
                self.store.set("hotkeys_enabled", False, save=False)
                self._sync_double_alt_gesture()
                self.status_text.set(failure_text)
                self.settings_window.refresh_shortcut_state()
                self.update_tray_menu()
                return
            if bool(getattr(self, "shortcut_editor_active", False)):
                if not service.stop(timeout_seconds=1.0):
                    self.status_text.set("快捷键占用检查未能结束，暂未启用")
                    self.settings_window.refresh_shortcut_state()
                    return
        else:
            if watcher is not None:
                watcher.set_hotkey_requests_enabled(False)
                watcher.set_double_alt_enabled(False)
            if service is not None and not service.stop(timeout_seconds=1.0):
                self.status_text.set("全局快捷键未能及时关闭，请重试或重启软件")
                self.settings_window.refresh_shortcut_state()
                return

        self.store.set("hotkeys_enabled", enabled, save=False)
        save_result = self.store.save()
        if save_result is False:
            self.store.set("hotkeys_enabled", old_enabled, save=False)
            if service is not None:
                try:
                    if old_enabled and not bool(
                        getattr(self, "shortcut_editor_active", False)
                    ):
                        service.restart(
                            hotkey_specs=self._get_configured_hotkey_specs(),
                            ready_timeout_seconds=1.0,
                        )
                    else:
                        service.stop(timeout_seconds=1.0)
                except Exception as exc:
                    log(f"Could not restore hotkey switch after save failure: {exc}")
            self._sync_double_alt_gesture()
            self.status_text.set("快捷键开关无法写入磁盘，原设置未改变")
            self.settings_window.refresh_shortcut_state()
            self.update_tray_menu()
            return

        self._sync_double_alt_gesture()
        self.settings_window.refresh_shortcut_state()
        self.status_text.set("全局快捷键已启用" if enabled else "全局快捷键已关闭")
        self.update_tray_menu()

    def refresh_app_state(self) -> None:
        if not self.current_app:
            return
        enabled = self.store.is_app_enabled(self.current_app.exe)
        self.mini_app_enabled.set(enabled)
        self.panel_app_enabled.set(enabled)
        self.mini_app_toggle.draw()
        self.panel_app_toggle.draw()

    def toggle_current_app(self) -> bool:
        if not self.current_app:
            return False
        enabled = self.mini_app_enabled.get() if self.mini.winfo_viewable() else self.panel_app_enabled.get()
        if not self.store.set_app(
            self.current_app.exe,
            enabled,
            name=self.current_app.name,
            path=self.current_app.path,
        ):
            previous = self.store.is_app_enabled(self.current_app.exe)
            self.mini_app_enabled.set(previous)
            self.panel_app_enabled.set(previous)
            self.mini_app_toggle.draw()
            self.panel_app_toggle.draw()
            self.status_text.set("当前应用开关无法保存，原设置未改变")
            return False
        self.mini_app_enabled.set(enabled)
        self.panel_app_enabled.set(enabled)
        self.mini_app_toggle.draw()
        self.panel_app_toggle.draw()
        self.status_text.set(f"已在 {self.current_app.name} {'开启' if enabled else '关闭'}划词翻译")
        if not enabled:
            self._invalidate_active_request()
            self.speech.cancel()
            self._clear_translation_display(
                f"已在 {self.current_app.name} 关闭划词翻译"
            )
            self.hide_mini()
        return True

    def _invalidate_active_request(self) -> None:
        """Make queued translation/speech callbacks for the old selection stale."""

        self.cancel_pending_pronunciation_click()
        with self.auto_speak_lock:
            self.selection_token += 1
            self.pending_auto_speak_token = None
            self.pending_auto_speak_accent = None
            self.pending_auto_speak_mode = None

    def _discard_queued_translations(self) -> None:
        request_queue = getattr(self, "request_queue", None)
        if request_queue is None:
            return
        stop_requested = False
        while True:
            try:
                request = request_queue.get_nowait()
            except queue.Empty:
                break
            if request is None:
                stop_requested = True
        if stop_requested:
            try:
                request_queue.put_nowait(None)
            except queue.Full:
                pass

    def _clear_translation_display(self, message: str) -> None:
        """Clear source/result pairs together so the UI can never mix A and B."""

        self._discard_queued_translations()
        self.active_source = ""
        self.current_result = None
        source_text = getattr(self, "source_text", None)
        if source_text is not None:
            try:
                source_text.configure(state="normal")
                source_text.delete("1.0", "end")
            except (tk.TclError, AttributeError):
                pass
        result_text = getattr(self, "result_text", None)
        if result_text is not None:
            try:
                self._set_text(result_text, message)
            except (tk.TclError, AttributeError):
                pass
        for name, value in (
            ("app_text", message),
            ("direction_text", ""),
            ("meta_text", ""),
            ("phonetic_text", "选择英文后显示音标"),
            ("mini_source", ""),
            ("mini_phonetic", ""),
            ("mini_translation", message),
        ):
            variable = getattr(self, name, None)
            if variable is not None:
                try:
                    variable.set(value)
                except (tk.TclError, AttributeError):
                    pass

    def _set_translation_loading_display(self, text: str) -> None:
        """Replace every old-result field before a new translation is queued."""

        result_text = getattr(self, "result_text", None)
        if result_text is not None:
            self._set_text(result_text, "正在本地翻译…")
        self.meta_text.set("")
        self.engine_text.set("正在本地翻译…")

    def set_status(self, value: str) -> None:
        self._post_ui(self.status_text.set, value)

    def set_speech_status(self, value: str) -> None:
        """Receive truthful warm-up/generation/playback stages from SpeechPlayer."""

        epoch = int(getattr(self, "_speech_status_epoch", 0))
        self._post_ui(self._set_speech_status_if_current, epoch, value)

    def _set_speech_status_if_current(self, epoch: int, value: str) -> None:
        if int(epoch) != int(getattr(self, "_speech_status_epoch", 0)):
            return
        self._set_speech_status_now(value)

    def _set_speech_status_now(self, value: str) -> None:
        progress = getattr(self, "speech_progress_text", None)
        if progress is not None:
            progress.set(str(value))

    def set_engine_status(self, value: str) -> None:
        self._post_ui(self.engine_text.set, value)

    def on_hotkey_status(self, value: str) -> None:
        if bool(self.store.get("hotkeys_enabled", True)):
            self.set_status(value)

    def on_hotkey_command(self, command: HotkeyCommand) -> None:
        if bool(getattr(self, "shortcut_editor_active", False)):
            return
        if command == HotkeyCommand.RETRY_AND_SPEAK_US:
            # Keep this off Tk's queue. Native chords are released on a bounded
            # watcher thread before any compatibility Ctrl+C can be injected.
            self._request_retry_capture()
        else:
            self._post_ui(self._handle_hotkey_command, command)

    def _request_retry_capture(self) -> None:
        if not bool(self.store.get("hotkeys_enabled", True)):
            return
        if not bool(self.store.get("desktop_enabled", True)):
            self.set_status("桌面取词已暂停，请先恢复取词")
            return
        if self.watcher:
            retry_spec = self._get_configured_hotkey_specs()[0]
            if retry_spec.binding_kind is HotkeyBindingKind.NATIVE:
                self.watcher.request_native_hotkey_capture(
                    primary_virtual_key=retry_spec.virtual_key,
                    auto_speak_accent="us",
                )
            else:
                # Double left Alt is detected after its second release, so it
                # deliberately keeps the existing zero-delay path.
                self.watcher.request_manual_capture(auto_speak_accent="us")

    def _handle_hotkey_command(self, command: HotkeyCommand) -> None:
        if (
            not bool(self.store.get("hotkeys_enabled", True))
            or bool(getattr(self, "shortcut_editor_active", False))
        ):
            return
        if command == HotkeyCommand.RETRY_AND_SPEAK_US:
            self._request_retry_capture()
        elif command == HotkeyCommand.TOGGLE_WINDOW_MODE:
            self.toggle_mode_from_hotkey()

    def on_selection(
        self,
        text: str,
        app_info: AppInfo,
        x: int,
        y: int,
        auto_speak_accent: str | None = None,
        capture_identity: tuple[int, int] | None = None,
    ) -> None:
        self._post_ui(
            self._handle_selection,
            text,
            app_info,
            x,
            y,
            auto_speak_accent,
            capture_identity,
        )

    def on_capture_started(
        self,
        identity: tuple[int, int],
    ) -> None:
        with self.auto_speak_lock:
            self.latest_capture_identity = identity
            self.pending_auto_speak_token = None
            self.pending_auto_speak_accent = None
            self.pending_auto_speak_mode = None
        # Input hooks run off the Tk thread. Queue timer cancellation instead of
        # calling `after_cancel` here.
        self._post_ui(self.cancel_pending_pronunciation_click)

    def _point_is_inside_app(self, x: int, y: int) -> bool:
        try:
            if self.root.winfo_containing(int(x), int(y)) is not None:
                return True
        except (tk.TclError, AttributeError, TypeError, ValueError):
            pass
        # Tk's `winfo containing` covers client widgets but can return None on
        # a native title bar or resize border.  Those are still clicks inside
        # our application and must not cancel an in-flight translation.
        for window in self._app_toplevels():
            if window is None:
                continue
            try:
                if not window.winfo_exists() or not window.winfo_viewable():
                    continue
                hwnd = int(window.winfo_id())
                hwnd = int(win32gui.GetAncestor(hwnd, 2) or hwnd)
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                if left <= int(x) < right and top <= int(y) < bottom:
                    return True
            except Exception:
                continue
        return False

    def _app_toplevels(self) -> list[object | None]:
        settings = getattr(self, "settings_window", None)
        return [
            getattr(self, "root", None),
            getattr(self, "mini", None),
            getattr(settings, "window", None),
        ]

    def _foreground_is_app_window(self) -> bool:
        try:
            foreground = int(win32gui.GetForegroundWindow() or 0)
            if not foreground:
                return False
            foreground_root = int(win32gui.GetAncestor(foreground, 2) or foreground)
            for window in self._app_toplevels():
                if window is None:
                    continue
                try:
                    if not window.winfo_exists():
                        continue
                    hwnd = int(window.winfo_id())
                    hwnd_root = int(win32gui.GetAncestor(hwnd, 2) or hwnd)
                    if hwnd_root == foreground_root:
                        return True
                except Exception:
                    continue
        except Exception:
            return False
        return False

    def _capture_context_is_internal(
        self,
        x: int | None,
        y: int | None,
        kind: str,
    ) -> bool:
        if x is not None and y is not None and x >= 0 and y >= 0:
            return self._point_is_inside_app(x, y)
        if kind in {"keyboard", "hotkey", "capture"}:
            return self._foreground_is_app_window()
        return False

    def _handle_capture_started(
        self,
        identity: tuple[int, int],
        x: int | None = None,
        y: int | None = None,
        kind: str = "capture",
    ) -> None:
        if self._capture_context_is_internal(x, y, kind):
            return
        self.cancel_pending_pronunciation_click()
        with self.auto_speak_lock:
            self.latest_capture_identity = identity
            if identity != getattr(self, "last_invalidated_capture_identity", None):
                # Invalidate an in-flight A translation as soon as the user
                # starts selecting B.  Waiting until B has been read leaves a
                # visible window where A can publish and reopen the mini popup.
                self.selection_token += 1
                self.last_invalidated_capture_identity = identity
            self.pending_auto_speak_token = None
            self.pending_auto_speak_accent = None
            self.pending_auto_speak_mode = None
        self.speech.cancel()
        self._clear_translation_display("正在读取新的选区…")
        mini = getattr(self, "mini", None)
        if mini is not None:
            try:
                if mini.winfo_viewable():
                    self.hide_mini()
            except (tk.TclError, AttributeError):
                pass

    def on_diagnostic_changed(self, exe: str) -> None:
        self._post_ui(self.settings_window.refresh_diagnostic, exe)

    def _post_ui(self, callback: Callable[..., None], *args: object) -> None:
        if not self.quitting:
            self.ui_tasks.put((callback, args))

    def _drain_ui_tasks(self) -> None:
        for _index in range(64):
            try:
                callback, args = self.ui_tasks.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args)
            except Exception as exc:
                log(f"UI task error: {exc}")
        if not self.quitting:
            self.root.after(15, self._drain_ui_tasks)

    def _handle_selection(
        self,
        text: str,
        app_info: AppInfo,
        x: int,
        y: int,
        auto_speak_accent: str | None = None,
        capture_identity: tuple[int, int] | None = None,
    ) -> None:
        # The popup deliberately does not take focus, so a double-click on its
        # US/UK control can otherwise be observed by the global mouse hook as
        # a double-click in the still-foreground source application.  That
        # stale source selection is then returned with the control's screen
        # coordinates and re-anchors the popup under its own button.  Never
        # let a capture whose pointer is inside one of our windows replace the
        # active selection or its anchor.
        if self._capture_context_is_internal(x, y, "mouse"):
            return
        if not bool(self.store.get("desktop_enabled", True)) or not self.store.is_app_enabled(app_info.exe):
            return
        # A retry hotkey reads the existing selection again. Its capture point
        # is merely the pointer's current location, which may be nowhere near
        # that selection. Keep the last successful anchor while retrying in the
        # same source window so the mini popup does not jump under the pointer.
        selection_point = (int(x), int(y))
        previous_app = getattr(self, "current_app", None)
        if auto_speak_accent and previous_app is not None:
            same_source_window = bool(
                previous_app.hwnd
                and app_info.hwnd
                and int(previous_app.hwnd) == int(app_info.hwnd)
            )
            if same_source_window:
                selection_point = tuple(
                    int(value)
                    for value in getattr(self, "selection_point", selection_point)
                )
        self.cancel_pending_pronunciation_click()
        self.speech.cancel()
        with self.auto_speak_lock:
            if (
                capture_identity is not None
                and capture_identity != self.latest_capture_identity
            ):
                return
            self.selection_token += 1
            token = self.selection_token
            self.pending_auto_speak_token = token if auto_speak_accent else None
            self.pending_auto_speak_accent = auto_speak_accent
            self.pending_auto_speak_mode = (
                self._speech_mode_for_gesture("auto") if auto_speak_accent else None
            )
        self.current_app = app_info
        self.active_source = text
        self.current_result = None
        self.selection_point = selection_point
        self.app_text.set(f"来自 {app_info.name}")
        self.mini_app_text.set(f"{app_info.name}  开启")
        self.refresh_app_state()
        self.source_text.delete("1.0", "end")
        self.source_text.insert("1.0", text)
        self.direction_text.set("中文 → 英文" if engine.contains_chinese(text) else "英文 → 中文")
        self.phonetic_text.set("正在查询音标…" if engine.WORD_PATTERN.fullmatch(text) else "可直接播放整句发音")
        if self.display_mode == "mini":
            self.show_mini_loading(text, *selection_point)
        else:
            self.show_panel_no_activate()
        if auto_speak_accent and not engine.contains_chinese(text):
            self._play_pending_auto_speak(token, text)
        if self.auto_translate or (auto_speak_accent and engine.contains_chinese(text)):
            self.enqueue(text, token)

    def show_mini_loading(self, text: str, x: int, y: int) -> None:
        self.mini_source.set(text if len(text) <= 48 else text[:47].rstrip() + "…")
        self.mini_phonetic.set("正在查询音标…" if engine.WORD_PATTERN.fullmatch(text) else "整句发音")
        self.mini_translation.set("正在本地翻译…")
        self.show_mini_no_activate(x, y)

    def translate_manual(self) -> None:
        text = engine.normalize_selection(self.source_text.get("1.0", "end"))
        if not text:
            return
        self.cancel_pending_pronunciation_click()
        self.speech.cancel()
        with self.auto_speak_lock:
            self.selection_token += 1
            token = self.selection_token
        self.cancel_pending_auto_speak()
        self.active_source = text
        self.current_result = None
        self.direction_text.set("中文 → 英文" if engine.contains_chinese(text) else "英文 → 中文")
        self._set_translation_loading_display(text)
        self.enqueue(text, token)

    def enqueue(self, text: str, token: int) -> None:
        while True:
            try:
                self.request_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self.request_queue.put_nowait(TranslationRequest(text, token))
        except queue.Full:
            pass
        self.engine_text.set("正在本地翻译…")

    def _latest_translation_request(
        self,
        request: TranslationRequest,
    ) -> TranslationRequest | None:
        """Briefly coalesce a B→C burst while keeping model use single-threaded."""

        latest = request
        deadline = time.monotonic() + TRANSLATION_LATEST_SETTLE_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return latest
            try:
                queued = self.request_queue.get(timeout=remaining)
            except queue.Empty:
                return latest
            if queued is None:
                return None
            latest = queued

    def _translation_token_is_current(self, token: int) -> bool:
        with self.auto_speak_lock:
            return token == self.selection_token

    def _translation_loop(self) -> None:
        try:
            self.translator.load()
        except Exception as exc:
            log(f"Engine load error: {exc}\n{traceback.format_exc()}")
            self.set_engine_status("本地引擎加载失败")
            self._post_ui(messagebox.showerror, APP_NAME, f"本地翻译引擎加载失败：\n{exc}\n\n日志：{engine.LOG_PATH}")
            return
        while True:
            request = self.request_queue.get()
            if request is None:
                return
            request = self._latest_translation_request(request)
            if request is None:
                return
            if not self._translation_token_is_current(request.token):
                continue
            try:
                result = self.translator.translate(request.text)
                self._post_ui(self.show_result, result, request.token)
            except Exception as exc:
                log(f"Translate error: {exc}\n{traceback.format_exc()}")
                self._post_ui(self.show_error, str(exc), request.token)

    @staticmethod
    def compact_translation(text: str, limit: int = 125) -> str:
        parts = [part.strip() for part in text.splitlines() if part.strip()]
        value = "；".join(parts[:2]) if parts else text.strip()
        return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"

    def show_result(
        self,
        result: engine.TranslationResult,
        token: int | None = None,
    ) -> None:
        current_token = self.selection_token if token is None else token
        if (
            not self._translation_token_is_current(current_token)
            or result.source != self.active_source
        ):
            return
        self.current_result = result
        self.direction_text.set("中文 → 英文" if result.source_language == "zh" else "英文 → 中文")
        self.phonetic_text.set(f"/{result.phonetic}/" if result.phonetic else "点击右侧按钮播放标准发音")
        self._set_text(self.result_text, result.translated)
        self.meta_text.set(f"{result.engine} · {result.elapsed_ms} ms")
        self.engine_text.set("本地引擎已就绪")
        # A Tk variable trace or nested callback can re-enter the UI loop.  A
        # second identity check prevents a late A result from mapping or
        # speaking after B has already invalidated it.
        if (
            not self._translation_token_is_current(current_token)
            or result.source != self.active_source
        ):
            return
        if self.display_mode == "mini":
            source = result.source if len(result.source) <= 48 else result.source[:47].rstrip() + "…"
            self.mini_source.set(source)
            self.mini_phonetic.set(f"/{result.phonetic}/" if result.phonetic else "整句发音")
            self.mini_translation.set(self.compact_translation(result.translated))
            current_enabled = bool(self.store.get("desktop_enabled", True)) and bool(
                self.current_app and self.store.is_app_enabled(self.current_app.exe)
            )
            if self.mini_dismissed_token != current_token and current_enabled:
                self.show_mini_no_activate(*self.selection_point)
        english = (
            result.translated
            if result.target_language == "en"
            else result.source
        )
        if self._translation_token_is_current(current_token):
            self._play_pending_auto_speak(current_token, english)

    def show_error(self, message: str, token: int | None = None) -> None:
        current_token = self.selection_token if token is None else token
        if current_token != self.selection_token:
            return
        with self.auto_speak_lock:
            if self.pending_auto_speak_token == current_token:
                self.pending_auto_speak_token = None
                self.pending_auto_speak_accent = None
                self.pending_auto_speak_mode = None
        self._set_text(self.result_text, f"翻译失败：{message}")
        self.mini_translation.set(f"翻译失败：{message}")
        self.engine_text.set("翻译失败")

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def english_text(self) -> str:
        if self.active_source and not engine.contains_chinese(self.active_source):
            return self.active_source
        if self.current_result and self.current_result.source == self.active_source:
            return self.current_result.translated if self.current_result.target_language == "en" else self.current_result.source
        return ""

    def speak(
        self,
        accent: str,
        mode: str = "natural",
        *,
        expected_token: int | None = None,
    ) -> None:
        if mode not in {"natural", "system"}:
            mode = "natural"
        with self.auto_speak_lock:
            if expected_token is not None and expected_token != self.selection_token:
                return
        text = self.english_text()
        if text:
            if mode == "system":
                accent_name = "美音" if accent == "us" else "英音"
                self._set_speech_status_now(
                    f"正在播放微软原版{accent_name}…"
                )
                self.speech.speak(text, accent, mode="system")
            else:
                self._set_speech_status_now(
                    self._speech_preparing_status(accent, mode)
                )
                # `natural` is SpeechPlayer's default. Keep the two-argument
                # call compatible with existing adapters and test doubles.
                self.speech.speak(text, accent)
            return
        if self.active_source and engine.contains_chinese(self.active_source):
            with self.auto_speak_lock:
                if expected_token is not None and expected_token != self.selection_token:
                    return
                self.pending_auto_speak_token = self.selection_token
                self.pending_auto_speak_accent = accent
                self.pending_auto_speak_mode = mode
            self._set_speech_status_now(
                self._speech_waiting_for_translation_status(accent, mode)
            )
            self.status_text.set("正在翻译，完成后自动播放发音")
            return
        self._set_speech_status_now("请先选择要发音的英文")
        self.status_text.set("请先选择要发音的英文")

    def _play_pending_auto_speak(self, token: int, text: str) -> None:
        with self.auto_speak_lock:
            if (
                token != self.selection_token
                or self.pending_auto_speak_token != token
                or not self.pending_auto_speak_accent
            ):
                return
            accent = self.pending_auto_speak_accent
            mode = getattr(self, "pending_auto_speak_mode", None) or "natural"
            self.pending_auto_speak_token = None
            self.pending_auto_speak_accent = None
            self.pending_auto_speak_mode = None
        if text.strip():
            if mode == "system":
                self.speech.speak(text, accent, mode="system")
            else:
                self.speech.speak(text, accent)

    def cancel_pending_auto_speak(self) -> None:
        with self.auto_speak_lock:
            self.pending_auto_speak_token = None
            self.pending_auto_speak_accent = None
            self.pending_auto_speak_mode = None

    def copy_translation(self) -> None:
        if not self.current_result:
            self.status_text.set("译文还在生成，请稍候")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.current_result.translated)
        except tk.TclError as exc:
            log(f"Could not copy translation: {exc}")
            self.status_text.set("剪贴板正被其他程序占用，请稍后重试")
            return
        self.status_text.set("译文已复制")
        self.root.after(1200, self._restore_status_after_copy)

    def _restore_status_after_copy(self) -> None:
        # Do not overwrite a newer pause/failure/application status that was
        # set during the confirmation delay.
        if self.status_text.get() != "译文已复制":
            return
        self.status_text.set(
            "桌面取词已开启"
            if bool(self.store.get("desktop_enabled", True))
            else "桌面取词已暂停"
        )

    def _tray_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("打开大窗口", lambda _icon, _item: self._post_ui(self.show_panel), default=True),
            pystray.MenuItem(
                "迷你浮窗模式",
                lambda _icon, _item: self._post_ui(self.set_mode, "mini"),
                checked=lambda _item: self.display_mode == "mini",
                radio=True,
            ),
            pystray.MenuItem(
                "大窗口模式",
                lambda _icon, _item: self._post_ui(self.set_mode, "panel"),
                checked=lambda _item: self.display_mode == "panel",
                radio=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "桌面划词翻译",
                lambda _icon, _item: self._post_ui(self.toggle_desktop),
                checked=lambda _item: bool(self.store.get("desktop_enabled", True)),
            ),
            pystray.MenuItem(
                (
                    "全局快捷键（"
                    f"{hotkey_text_for_display(self.store.get('retry_hotkey', 'Double Alt'))} / "
                    f"{hotkey_text_for_display(self.store.get('toggle_mode_hotkey', 'Alt+C'))}）"
                ),
                lambda _icon, _item: self._post_ui(
                    self.set_hotkeys_enabled,
                    not bool(self.store.get("hotkeys_enabled", True)),
                ),
                checked=lambda _item: bool(self.store.get("hotkeys_enabled", True)),
            ),
            pystray.MenuItem("应用设置…", lambda _icon, _item: self._post_ui(self.open_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda _icon, _item: self._post_ui(self.quit)),
        )

    def start_tray(self) -> None:
        try:
            image = Image.open(app_icon_path("png")).convert("RGBA")
            self.tray = pystray.Icon("DaShengFaTranslator", image, APP_NAME, self._tray_menu())
            self.tray_ready.clear()
            self.tray_failure_reported = False
            self.tray_started_at = time.monotonic()
            self.tray_thread = threading.Thread(
                target=self._run_tray,
                daemon=True,
                name="TrayIcon",
            )
            self.tray_thread.start()
            self.root.after(1600, self._verify_tray_started)
            if not bool(self.store.get("tray_tip_seen", False)):
                self.root.after(1600, self._show_first_tray_tip)
        except Exception as exc:
            log(f"Tray icon error: {exc}\n{traceback.format_exc()}")
            if self.display_mode == "mini":
                self._handle_tray_failure("托盘图标无法启动")

    def _run_tray(self) -> None:
        tray = self.tray
        if tray is None:
            return
        try:
            tray.run(setup=self._mark_tray_ready)
            if not self.quitting:
                raise RuntimeError("托盘线程意外退出")
        except Exception as exc:
            log(f"Tray icon worker error: {exc}\n{traceback.format_exc()}")
            self._post_ui(self._handle_tray_failure, str(exc))

    def _mark_tray_ready(self, icon: pystray.Icon) -> None:
        icon.visible = True
        self.tray_ready.set()

    def _verify_tray_started(self) -> None:
        if self.quitting or self.tray_ready.is_set():
            return
        started = self.tray_started_at or time.monotonic()
        if time.monotonic() - started < 4.5 and bool(
            self.tray_thread and self.tray_thread.is_alive()
        ):
            self.root.after(900, self._verify_tray_started)
            return
        self._handle_tray_failure("托盘在启动期限内没有就绪")

    def _handle_tray_failure(self, reason: str) -> None:
        if self.quitting or self.tray_failure_reported:
            return
        self.tray_failure_reported = True
        log(f"Tray unavailable, keeping panel discoverable: {reason}")
        self.status_text.set("托盘暂时不可用，已保持大窗口可见")
        self.root.deiconify()
        self.root.lift()
        self._position_panel()

    def _show_first_tray_tip(self) -> None:
        if not self.tray:
            return
        try:
            self.tray.notify("已在系统托盘运行。右键图标可切换模式、设置应用或退出。", APP_NAME)
            self.store.set("tray_tip_seen", True)
        except Exception:
            pass

    def update_tray_menu(self) -> None:
        if self.tray:
            try:
                self.tray.update_menu()
            except Exception:
                pass

    def start_background_tasks(self) -> None:
        self.translation_worker = threading.Thread(target=self._translation_loop, daemon=True, name="TranslationWorker")
        self.translation_worker.start()
        hotkey_specs = self._get_configured_hotkey_specs()
        self.watcher = DesktopSelectionWatcher(
            self.store,
            self.on_selection,
            self.set_status,
            self.on_global_mouse_down,
            self.on_diagnostic_changed,
            self.on_capture_started,
        )
        self.watcher.set_double_alt_enabled(
            bool(self.store.get("hotkeys_enabled", True))
            and not bool(getattr(self, "shortcut_editor_active", False))
            and hotkey_specs[0].binding_kind is HotkeyBindingKind.DOUBLE_ALT
        )
        self.watcher.set_hotkey_requests_enabled(
            bool(self.store.get("hotkeys_enabled", True))
            and not bool(getattr(self, "shortcut_editor_active", False))
        )
        self.watcher.start()
        self.hotkey_service = WindowsHotkeyService(
            self.on_hotkey_command,
            self.on_hotkey_status,
            report_error=lambda message: log(f"Global hotkey error: {message}"),
            hotkey_specs=hotkey_specs,
        )
        if bool(self.store.get("hotkeys_enabled", True)):
            self.hotkey_service.start(ready_timeout_seconds=0)
        self.start_tray()

    def quit(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        self.quit_started_at = time.monotonic()
        self.cancel_pending_pronunciation_click()
        self.cancel_pending_auto_speak()
        if self.watcher:
            self.watcher.stop()
        if self.hotkey_service:
            self.hotkey_service.stop(timeout_seconds=0.5)
        self.speech.stop(timeout_seconds=0.5)
        self.store.flush_pending()
        while True:
            try:
                self.request_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self.request_queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.root.withdraw()
            self.hide_mini()
            if self.settings_window.window:
                self.settings_window.window.withdraw()
        except tk.TclError:
            pass
        self._finish_quit_when_capture_stopped()

    def _finish_quit_when_capture_stopped(self) -> None:
        capture_running = bool(
            self.watcher and not self.watcher.stopped_event.is_set()
        )
        hotkey_service = getattr(self, "hotkey_service", None)
        hotkeys_running = bool(hotkey_service and hotkey_service.is_running)
        if capture_running or hotkeys_running:
            quit_started_at = getattr(self, "quit_started_at", None)
            started = quit_started_at if quit_started_at is not None else time.monotonic()
            if time.monotonic() - started < 3.0:
                self.root.after(40, self._finish_quit_when_capture_stopped)
                return
            log("Background input workers did not stop within 3 seconds; continuing UI shutdown")
        self.store.flush_pending()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        try:
            if self.settings_window.window:
                self.settings_window.window.destroy()
            self.mini.destroy()
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.after(0, self.start_background_tasks)
        self.root.mainloop()


class SingleInstanceGuard:
    """Own a per-session Win32 mutex without occupying a network port."""

    def __init__(self, handle: int) -> None:
        self.handle = int(handle)

    def close(self) -> None:
        handle, self.handle = self.handle, 0
        if handle:
            _CLOSE_HANDLE(handle)


def acquire_single_instance() -> SingleInstanceGuard | None:
    ctypes.set_last_error(0)
    handle = int(_CREATE_MUTEX(None, False, SINGLE_INSTANCE_MUTEX) or 0)
    error = int(ctypes.get_last_error())
    if not handle:
        raise ctypes.WinError(error or 1)
    if error == 183:  # ERROR_ALREADY_EXISTS
        _CLOSE_HANDLE(handle)
        return None
    return SingleInstanceGuard(handle)


def run_packaged_smoke_test(report_path: Path) -> int:
    """Verify packaged translation and neural-speech resources without UI."""

    payload: dict[str, object] = {
        "product": APP_NAME,
        "author": APP_AUTHOR,
        "version": APP_VERSION,
        "ok": False,
    }
    exit_code = 1
    try:
        required_resources = (
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
        missing = [
            name for name in required_resources if not engine.resource_path(name).exists()
        ]
        if missing:
            raise FileNotFoundError("缺少运行资源：" + "、".join(missing))

        dictionary_path = engine.resource_path("ecdict.db").resolve()
        dictionary_uri = dictionary_path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(dictionary_uri, uri=True)) as dictionary_db:
            quick_check = dictionary_db.execute("PRAGMA quick_check").fetchone()
            if not quick_check or str(quick_check[0]).casefold() != "ok":
                detail = quick_check[0] if quick_check else "无结果"
                raise RuntimeError(f"本地词典完整性检查失败：{detail}")
            dictionary_row = dictionary_db.execute(
                "SELECT word FROM entries "
                "WHERE translation IS NOT NULL AND trim(translation) <> '' LIMIT 1"
            ).fetchone()
        if not dictionary_row or not str(dictionary_row[0]).strip():
            raise RuntimeError("本地词典没有可用词条")

        for name, expected_hash in PIPER_RESOURCE_SHA256.items():
            digest_builder = hashlib.sha256()
            with engine.resource_path(name).open("rb") as resource_stream:
                for chunk in iter(lambda: resource_stream.read(1024 * 1024), b""):
                    digest_builder.update(chunk)
            digest = digest_builder.hexdigest()
            if digest != expected_hash:
                raise RuntimeError(f"Piper 运行资源摘要不匹配：{name}")

        statuses: list[str] = []
        translator = engine.LocalTranslator(statuses.append)
        translator.load()
        if translator.dictionary is None:
            raise RuntimeError("本地词典没有加载")
        dictionary_word = str(dictionary_row[0]).strip()
        dictionary_result = translator.dictionary.lookup(dictionary_word)
        if not dictionary_result or not any(part.strip() for part in dictionary_result):
            raise RuntimeError("本地词典查询失败")
        en_to_zh = translator.translate("integration smoke sentence")
        zh_to_en = translator.translate("你好")
        if not en_to_zh.translated.strip() or not zh_to_en.translated.strip():
            raise RuntimeError("离线翻译返回空结果")
        def validate_wav(
            path: Path,
            duration: float,
            label: str,
            expected_sample_rate: int | None = None,
        ) -> None:
            if duration <= 0 or not path.is_file():
                raise RuntimeError(f"{label} 返回无效音频")
            try:
                with wave.open(str(path), "rb") as stream:
                    if (
                        stream.getnchannels() != 1
                        or stream.getsampwidth() != 2
                        or stream.getframerate() <= 0
                        or stream.getnframes() <= 0
                    ):
                        raise RuntimeError(f"{label} WAV 参数无效")
                    if (
                        expected_sample_rate is not None
                        and stream.getframerate() != expected_sample_rate
                    ):
                        raise RuntimeError(
                            f"{label} WAV 采样率错误："
                            f"{stream.getframerate()} != {expected_sample_rate}"
                        )
            except (OSError, EOFError, wave.Error) as exc:
                raise RuntimeError(f"{label} WAV 无法解析：{exc}") from exc

        piper_speech = engine.PiperSpeechBackend()
        try:
            for accent in ("us", "uk"):
                audio_path, duration = piper_speech.synthesize("ready", accent, 1.0)
                try:
                    validate_wav(
                        audio_path,
                        duration,
                        f"Piper {accent.upper()}",
                        engine.PiperSpeechBackend.SESSION_PCM_SAMPLE_RATE,
                    )
                finally:
                    piper_speech.discard(audio_path)
        finally:
            piper_speech.close()

        kokoro_speech = engine.KokoroSpeechBackend()
        try:
            for accent in ("us", "uk"):
                audio_path, duration = kokoro_speech.synthesize(
                    "This sentence is ready.",
                    accent,
                    engine.natural_speech_speed_value(
                        engine.DEFAULT_NATURAL_SPEECH_SPEED
                    ),
                )
                try:
                    validate_wav(audio_path, duration, f"Kokoro {accent.upper()}")
                finally:
                    kokoro_speech.discard(audio_path)
        finally:
            kokoro_speech.close()
        payload.update(
            {
                "ok": True,
                "resources": "complete",
                "dictionary": f"ok:{dictionary_word}",
                "en_to_zh": en_to_zh.translated,
                "zh_to_en": zh_to_en.translated,
                "neural_speech": "ok",
                "piper_speech": "ok:us,uk",
                "kokoro_speech": "ok:us,uk",
                "engine_status": statuses[-1] if statuses else "loaded",
            }
        )
        exit_code = 0
    except Exception as exc:
        payload["error_type"] = type(exc).__name__
        payload["error"] = str(exc)
    try:
        report_path = report_path.expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return 2
    return exit_code


def main() -> int:
    if "--smoke-test" in sys.argv:
        index = sys.argv.index("--smoke-test")
        if index + 1 >= len(sys.argv):
            return 2
        return run_packaged_smoke_test(Path(sys.argv[index + 1]))

    set_dpi_awareness()
    try:
        guard = acquire_single_instance()
    except OSError as exc:
        log(f"Single-instance mutex error: {exc}\n{traceback.format_exc()}")
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, "无法建立程序互斥锁，软件未启动。请重新登录 Windows 后再试。")
        root.destroy()
        return 1
    if guard is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, f"{APP_NAME} 已经在运行，请查看系统托盘。")
        root.destroy()
        return 0
    try:
        DesktopTranslatorApp().run()
    finally:
        guard.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
