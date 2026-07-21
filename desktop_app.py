from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import socket
import sys
import threading
import time
import traceback
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


APP_NAME = "大声发划词翻译"
APP_VERSION = "0.3.0"
APP_AUTHOR = "眼泪斷了线"
MAX_SELECTION_LENGTH = 3000
INSTANCE_PORT = 39048

WHITE = "#FFFFFF"
PAGE = "#F7F8FA"
TEXT = "#171A21"
MUTED = "#6B7280"
FAINT = "#9CA3AF"
LINE = "#E7E9EE"
BLUE = "#3267E3"
BLUE_SOFT = "#EEF3FF"
GREEN = "#16865B"
RED_SOFT = "#FFF1F2"


COMMON_APPS: list[tuple[str, str]] = [
    ("winword.exe", "Microsoft Word"),
    ("wps.exe", "WPS 文字"),
    ("msedge.exe", "Microsoft Edge"),
    ("chrome.exe", "Google Chrome"),
    ("firefox.exe", "Mozilla Firefox"),
    ("acrord32.exe", "Adobe Acrobat Reader"),
    ("acrobat.exe", "Adobe Acrobat"),
    ("notepad.exe", "记事本"),
]

FRIENDLY_NAMES = dict(COMMON_APPS)
FRIENDLY_NAMES.update(
    {
        "powerpnt.exe": "Microsoft PowerPoint",
        "outlook.exe": "Microsoft Outlook",
        "onenote.exe": "Microsoft OneNote",
        "foxitpdfreader.exe": "Foxit PDF Reader",
        "pdfxcview.exe": "PDF-XChange Viewer",
        "pdfxedit.exe": "PDF-XChange Editor",
        "typora.exe": "Typora",
        "code.exe": "Visual Studio Code",
        "wechat.exe": "微信",
        "weixin.exe": "微信",
        "qq.exe": "QQ",
    }
)

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
}

DEFAULT_ENABLED = {exe: True for exe, _name in COMMON_APPS}
DEFAULT_SETTINGS: dict[str, object] = {
    "display_mode": "mini",
    "auto_translate": True,
    "desktop_enabled": True,
    "clipboard_fallback": True,
    "enabled_apps": DEFAULT_ENABLED,
    "custom_common_apps": [],
    "app_names": {},
    "app_paths": {},
    "app_recency": {},
    "other_expanded": False,
    "tray_tip_seen": False,
}


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


def app_icon_path(extension: str) -> Path:
    return engine.resource_path(f"app_icon.{extension}")


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class SettingsStore:
    def __init__(self) -> None:
        self.path = engine.SETTINGS_PATH
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> dict[str, object]:
        values = json.loads(json.dumps(DEFAULT_SETTINGS, ensure_ascii=False))
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                values.update(saved)
                for key in ("enabled_apps", "app_names", "app_paths", "app_recency"):
                    merged = dict(DEFAULT_SETTINGS.get(key, {}))
                    incoming = saved.get(key, {})
                    if isinstance(incoming, dict):
                        merged.update(incoming)
                    values[key] = merged
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        if values.get("display_mode") not in {"mini", "panel"}:
            values["display_mode"] = "mini"
        return values

    def save(self) -> None:
        with self.lock:
            try:
                atomic_write_json(self.path, self.data)
            except OSError as exc:
                log(f"Settings save error: {exc}")

    def get(self, key: str, default: object = None) -> object:
        with self.lock:
            return self.data.get(key, default)

    def set(self, key: str, value: object, save: bool = True) -> None:
        with self.lock:
            self.data[key] = value
            if save:
                self.save()

    def enabled_apps(self) -> dict[str, bool]:
        with self.lock:
            values = self.data.get("enabled_apps", {})
            return dict(values) if isinstance(values, dict) else {}

    def is_app_enabled(self, exe: str) -> bool:
        return bool(self.enabled_apps().get(exe.lower(), False))

    def set_app(
        self,
        exe: str,
        enabled: bool,
        *,
        name: str = "",
        path: str = "",
        touch: bool = True,
        save: bool = True,
    ) -> None:
        exe = exe.lower()
        with self.lock:
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
            if save:
                self.save()

    def set_apps_bulk(self, exes: list[str], enabled: bool) -> None:
        with self.lock:
            now = time.time()
            for index, exe in enumerate(exes):
                self.set_app(exe, enabled, touch=False, save=False)
                recency = self.data.setdefault("app_recency", {})
                if isinstance(recency, dict):
                    recency[exe.lower()] = now - index * 0.001
            self.save()

    def custom_common(self) -> list[str]:
        with self.lock:
            values = self.data.get("custom_common_apps", [])
            return [str(item).lower() for item in values] if isinstance(values, list) else []

    def set_common(self, exe: str, common: bool) -> None:
        exe = exe.lower()
        base = {item for item, _name in COMMON_APPS}
        with self.lock:
            values = self.custom_common()
            if common and exe not in base and exe not in values:
                values.append(exe)
            if not common and exe in values:
                values.remove(exe)
            self.data["custom_common_apps"] = values
            recency = self.data.setdefault("app_recency", {})
            if isinstance(recency, dict):
                recency[exe] = time.time()
            self.save()

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


@dataclass(slots=True)
class AppInfo:
    exe: str
    name: str
    path: str = ""
    title: str = ""


def process_path(process_id: int) -> str:
    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, process_id)
    if not process:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buffer))
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(process)


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
        return AppInfo(exe=exe, name=name, path=path, title=title)
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


def read_clipboard_text() -> tuple[str | None, list[int]]:
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
                    value = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                    if isinstance(value, str):
                        text = value
            finally:
                win32clipboard.CloseClipboard()
            return text, formats
        except Exception:
            formats.clear()
            time.sleep(0.02)
    return None, formats


def write_clipboard_text(text: str | None) -> None:
    for _attempt in range(6):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                if text is not None:
                    win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return
        except Exception:
            time.sleep(0.02)


class DesktopSelectionWatcher:
    def __init__(
        self,
        store: SettingsStore,
        selection_callback: Callable[[str, AppInfo, int, int], None],
        status_callback: Callable[[str], None],
        outside_click_callback: Callable[[int, int], None],
    ) -> None:
        self.store = store
        self.selection_callback = selection_callback
        self.status_callback = status_callback
        self.outside_click_callback = outside_click_callback
        self.stop_event = threading.Event()
        self.events: queue.Queue[tuple[int, int, AppInfo] | None] = queue.Queue(maxsize=1)
        self.listener: mouse.Listener | None = None
        self.worker: threading.Thread | None = None
        self.press: tuple[int, int, float] | None = None
        self.last_release: tuple[int, int, float, str] | None = None
        self.last_emitted: tuple[str, str, float] = ("", "", 0.0)

    def start(self) -> None:
        self.worker = threading.Thread(target=self._capture_loop, daemon=True, name="SelectionCapture")
        self.worker.start()
        self.listener = mouse.Listener(on_click=self._on_click)
        self.listener.daemon = True
        self.listener.start()

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.events.put_nowait(None)
        except queue.Full:
            pass
        if self.listener:
            self.listener.stop()

    def _on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        if button != mouse.Button.left or self.stop_event.is_set():
            return
        now = time.monotonic()
        if pressed:
            self.outside_click_callback(int(x), int(y))
            self.press = (int(x), int(y), now)
            return
        if not bool(self.store.get("desktop_enabled", True)):
            return
        app_info = foreground_app(self.store)
        if not app_info or not self.store.is_app_enabled(app_info.exe):
            return

        dragged = False
        if self.press:
            dragged = math.hypot(int(x) - self.press[0], int(y) - self.press[1]) >= 4
        double_clicked = False
        if self.last_release:
            old_x, old_y, old_time, old_exe = self.last_release
            double_clicked = (
                app_info.exe == old_exe
                and now - old_time <= 0.46
                and math.hypot(int(x) - old_x, int(y) - old_y) <= 7
            )
        self.last_release = (int(x), int(y), now, app_info.exe)
        if not dragged and not double_clicked:
            return

        event = (int(x), int(y), app_info)
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        try:
            self.events.put_nowait(event)
        except queue.Full:
            pass

    def _capture_loop(self) -> None:
        pythoncom.CoInitialize()
        try:
            with auto.UIAutomationInitializerInThread():
                while not self.stop_event.is_set():
                    event = self.events.get()
                    if event is None:
                        return
                    x, y, app_info = event
                    time.sleep(0.075)
                    text, protected = self._capture_selection(app_info, x, y)
                    if (
                        not text
                        and not protected
                        and bool(self.store.get("clipboard_fallback", True))
                    ):
                        text = self._capture_with_clipboard()
                    text = engine.normalize_selection(text or "")
                    if not text or len(text) > MAX_SELECTION_LENGTH:
                        continue
                    old_text, old_exe, old_time = self.last_emitted
                    now = time.monotonic()
                    if text == old_text and app_info.exe == old_exe and now - old_time < 0.7:
                        continue
                    self.last_emitted = (text, app_info.exe, now)
                    self.status_callback(f"已从 {app_info.name} 读取选中文字")
                    self.selection_callback(text, app_info, x, y)
        except Exception as exc:
            log(f"Selection watcher error: {exc}\n{traceback.format_exc()}")
            self.status_callback("桌面取词暂时不可用，请从托盘重新启动")
        finally:
            pythoncom.CoUninitialize()

    def _capture_selection(self, app_info: AppInfo, x: int, y: int) -> tuple[str, bool]:
        if app_info.exe == "winword.exe":
            try:
                word = win32com.client.GetActiveObject("Word.Application")
                selection = word.Selection
                length = int(selection.End) - int(selection.Start)
                if 0 < length <= MAX_SELECTION_LENGTH:
                    return str(selection.Text), False
            except Exception:
                pass

        controls: list[object] = []
        try:
            focused = auto.GetFocusedControl()
            if focused:
                controls.append(focused)
        except Exception:
            pass
        try:
            pointed = auto.ControlFromPoint(x, y)
            if pointed:
                controls.append(pointed)
        except Exception:
            pass

        visited: set[tuple[int, int, int, int]] = set()
        protected = False
        for control in controls:
            current = control
            for _level in range(9):
                if not current:
                    break
                try:
                    rectangle = current.BoundingRectangle
                    key = (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom)
                    if key in visited:
                        current = current.GetParentControl()
                        continue
                    visited.add(key)
                    if bool(getattr(current, "IsPassword", False)):
                        protected = True
                        break
                    for pattern_id in (auto.PatternId.TextPattern2, auto.PatternId.TextPattern):
                        pattern = current.GetPattern(pattern_id)
                        if not pattern:
                            continue
                        ranges = pattern.GetSelection()
                        values: list[str] = []
                        for text_range in ranges or []:
                            value = engine.normalize_selection(text_range.GetText(MAX_SELECTION_LENGTH + 1))
                            if value:
                                values.append(value)
                        selected = "\n".join(values).strip()
                        if selected:
                            return selected, protected
                    current = current.GetParentControl()
                except Exception:
                    try:
                        current = current.GetParentControl()
                    except Exception:
                        break
        return "", protected

    @staticmethod
    def _capture_with_clipboard() -> str:
        old_text, formats = read_clipboard_text()
        image_or_file_formats = {
            win32con.CF_BITMAP,
            win32con.CF_DIB,
            getattr(win32con, "CF_DIBV5", 17),
            win32con.CF_HDROP,
            win32con.CF_METAFILEPICT,
            win32con.CF_ENHMETAFILE,
        }
        if image_or_file_formats.intersection(formats):
            return ""
        before = ctypes.windll.user32.GetClipboardSequenceNumber()
        controller = keyboard.Controller()
        try:
            with controller.pressed(keyboard.Key.ctrl):
                controller.press("c")
                controller.release("c")
            copied = ""
            deadline = time.monotonic() + 0.45
            while time.monotonic() < deadline:
                time.sleep(0.025)
                if ctypes.windll.user32.GetClipboardSequenceNumber() != before:
                    copied, _formats = read_clipboard_text()
                    copied = copied or ""
                    break
            write_clipboard_text(old_text)
            return copied
        except Exception:
            write_clipboard_text(old_text)
            return ""


class ToggleSwitch(tk.Canvas):
    def __init__(
        self,
        parent: tk.Misc,
        variable: tk.BooleanVar,
        command: Callable[[], None],
        *,
        background: str = WHITE,
    ) -> None:
        super().__init__(
            parent,
            width=42,
            height=24,
            bg=background,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.variable = variable
        self.command = command
        self.bind("<Button-1>", self._toggle)
        self.draw()

    def _toggle(self, _event: tk.Event[tk.Misc]) -> None:
        self.variable.set(not self.variable.get())
        self.draw()
        self.command()

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

    def show(self) -> None:
        if self.window and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            self.refresh_apps()
            return
        self.window = tk.Toplevel(self.app.root)
        self.window.title(f"{APP_NAME} · 设置")
        self.window.geometry("620x720")
        self.window.minsize(560, 620)
        self.window.configure(bg=WHITE)
        self.window.protocol("WM_DELETE_WINDOW", self.window.withdraw)
        self.window.attributes("-topmost", True)
        self.app.apply_window_icon(self.window)
        self._build()
        self.refresh_apps()

    def _build(self) -> None:
        assert self.window is not None
        header = tk.Frame(self.window, bg=WHITE, padx=24, pady=18)
        header.pack(fill="x")
        tk.Label(header, text="设置", bg=WHITE, fg=TEXT, font=("Microsoft YaHei UI", 18, "bold")).pack(side="left")
        tk.Label(header, text="修改后立即生效", bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side="right", pady=(7, 0))
        tk.Frame(self.window, bg=LINE, height=1).pack(fill="x")

        canvas = tk.Canvas(self.window, bg=WHITE, highlightthickness=0)
        scrollbar = tk.Scrollbar(self.window, orient="vertical", command=canvas.yview, relief="flat")
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=WHITE)
        window_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        canvas.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))

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
            "仅在无障碍接口读取失败时短暂复制，并恢复原文本剪贴板；图片和文件剪贴板不会被动用",
            fallback_var,
            lambda: self.store.set("clipboard_fallback", fallback_var.get()),
        )

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
    ) -> None:
        row = tk.Frame(parent, bg=WHITE, pady=12)
        row.pack(fill="x")
        labels = tk.Frame(row, bg=WHITE)
        labels.pack(side="left", fill="x", expand=True)
        tk.Label(labels, text=title, bg=WHITE, fg=TEXT, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        tk.Label(labels, text=subtitle, bg=WHITE, fg=MUTED, wraplength=440, justify="left", font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(3, 0))
        ToggleSwitch(row, variable, command).pack(side="right", padx=(12, 0))

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

        base = [exe for exe, _name in COMMON_APPS]
        common = self._sort_apps(base + self.store.custom_common(), base)
        all_apps = set(self.store.enabled_apps()) | set(self.discovered)
        app_names = self.store.get("app_names", {})
        if isinstance(app_names, dict):
            all_apps.update(str(exe).lower() for exe in app_names)
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
        labels = tk.Frame(row, bg=WHITE)
        labels.pack(side="left", fill="x", expand=True)
        name_row = tk.Frame(labels, bg=WHITE)
        name_row.pack(anchor="w")
        tk.Label(name_row, text=self.store.app_name(exe), bg=WHITE, fg=TEXT, font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        if running:
            tk.Label(name_row, text="  正在运行", bg=WHITE, fg=GREEN, font=("Microsoft YaHei UI", 7)).pack(side="left")
        tk.Label(labels, text=exe, bg=WHITE, fg=FAINT, font=("Segoe UI", 7)).pack(anchor="w", pady=(2, 0))

        if exe not in {item for item, _name in COMMON_APPS}:
            action_text = "移回其他" if common else "设为常用"
            flat_button(row, action_text, lambda value=exe, target=not common: self.move_common(value, target), compact=True).pack(side="right", padx=(7, 0))
        variable = tk.BooleanVar(value=self.store.is_app_enabled(exe))
        ToggleSwitch(
            row,
            variable,
            lambda value=exe, state=variable: self.toggle_app(value, state.get()),
        ).pack(side="right", padx=(10, 0))

    def toggle_app(self, exe: str, enabled: bool) -> None:
        info = self.discovered.get(exe)
        self.store.set_app(
            exe,
            enabled,
            name=info.name if info else self.store.app_name(exe),
            path=info.path if info else self.store.app_path(exe),
        )
        self.app.refresh_app_state()

    def set_all_other(self, exes: list[str], enabled: bool) -> None:
        if not exes:
            return
        self.store.set_apps_bulk(exes, enabled)
        self.app.refresh_app_state()
        self._render_app_lists()

    def toggle_other(self) -> None:
        self.other_expanded = not self.other_expanded
        self.store.set("other_expanded", self.other_expanded)
        self._render_app_lists()

    def move_common(self, exe: str, common: bool) -> None:
        self.store.set_common(exe, common)
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
        self.store.set_app(exe, True, name=name, path=path)
        self.store.set_common(exe, True)
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
        self.auto_translate = bool(self.store.get("auto_translate", True))
        self.current_result: engine.TranslationResult | None = None
        self.current_app: AppInfo | None = None
        self.active_source = ""
        self.selection_point = (0, 0)

        self.status_text = tk.StringVar(value="正在启动桌面取词…")
        self.engine_text = tk.StringVar(value="正在加载本地翻译引擎…")
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

        self.request_queue: queue.Queue[str | None] = queue.Queue(maxsize=1)
        self.translator = engine.LocalTranslator(self.set_engine_status)
        self.speech = engine.SpeechPlayer()
        self.translation_worker: threading.Thread | None = None
        self.watcher: DesktopSelectionWatcher | None = None
        self.tray: pystray.Icon | None = None
        self.settings_window = SettingsWindow(self)

        self._build_panel()
        self._build_mini()
        self.refresh_mode_buttons()
        self._position_panel()
        if self.display_mode == "mini":
            self.root.withdraw()

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
        tk.Label(mode_row, textvariable=self.app_text, bg=PAGE, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side="left")
        mode_box = tk.Frame(mode_row, bg="#ECEFF3", padx=3, pady=3)
        mode_box.pack(side="right")
        self.panel_mini_button = flat_button(mode_box, "迷你浮窗", lambda: self.set_mode("mini"), compact=True)
        self.panel_mini_button.pack(side="left")
        self.panel_large_button = flat_button(mode_box, "大窗口", lambda: self.set_mode("panel"), compact=True)
        self.panel_large_button.pack(side="left", padx=(3, 0))

        sound_card = self._card(body)
        sound_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        sound_card.grid_columnconfigure(0, weight=1)
        sound_labels = tk.Frame(sound_card, bg=WHITE)
        sound_labels.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        tk.Label(sound_labels, text="发音", bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        tk.Label(sound_labels, textvariable=self.phonetic_text, bg=WHITE, fg=BLUE, font=("Segoe UI", 13, "bold"), anchor="w").pack(fill="x", pady=(4, 0))
        sound_actions = tk.Frame(sound_card, bg=WHITE)
        sound_actions.grid(row=0, column=1, sticky="e")
        flat_button(sound_actions, "US  美音", lambda: self.speak("us"), compact=True).pack(side="left", padx=(0, 7))
        flat_button(sound_actions, "UK  英音", lambda: self.speak("uk"), compact=True).pack(side="left")

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
        text_block = tk.Frame(top, bg=WHITE)
        text_block.pack(side="left", fill="x", expand=True)
        tk.Label(text_block, textvariable=self.mini_source, bg=WHITE, fg=TEXT, anchor="w", font=("Segoe UI", 11, "bold")).pack(fill="x")
        tk.Label(text_block, textvariable=self.mini_phonetic, bg=WHITE, fg=BLUE, anchor="w", font=("Segoe UI", 9, "bold")).pack(fill="x", pady=(2, 0))
        sound_actions = tk.Frame(top, bg=WHITE)
        sound_actions.pack(side="right", padx=(8, 0))
        flat_button(sound_actions, "US", lambda: self.speak("us"), compact=True).pack(side="left", padx=(0, 5))
        flat_button(sound_actions, "UK", lambda: self.speak("uk"), compact=True).pack(side="left", padx=(0, 4))
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
        tk.Frame(card, bg=LINE, height=1).pack(fill="x", padx=13)
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
        bottom = tk.Frame(card, bg=WHITE, padx=11)
        bottom.pack(fill="x", pady=(0, 7))
        tk.Label(bottom, textvariable=self.mini_app_text, bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 7)).pack(side="left")
        self.mini_app_toggle = ToggleSwitch(bottom, self.mini_app_enabled, self.toggle_current_app)
        self.mini_app_toggle.pack(side="left", padx=(7, 0))
        flat_button(bottom, "展开", self.show_panel, compact=True).pack(side="right")
        flat_button(bottom, "设置", self.open_settings, compact=True).pack(side="right", padx=(0, 5))

    def _position_panel(self) -> None:
        self.root.update_idletasks()
        width = max(620, self.root.winfo_width())
        screen_width = self.root.winfo_screenwidth()
        self.root.geometry(f"+{max(20, screen_width - width - 30)}+45")

    def position_mini(self, x: int, y: int) -> tuple[int, int, int, int]:
        width = 440
        self.mini.update_idletasks()
        # A fixed height clipped the app switch and action buttons when a direct
        # translation wrapped to two or three lines on high-DPI displays.
        height = max(188, min(238, self.mini.winfo_reqheight() + 3))
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        target_x = min(max(8, x + 14), max(8, screen_width - width - 8))
        target_y = y + 18
        if target_y + height > screen_height - 8:
            target_y = max(8, y - height - 14)
        self.mini.geometry(f"{width}x{height}+{target_x}+{target_y}")
        return target_x, target_y, width, height

    def show_mini_no_activate(self, x: int, y: int) -> None:
        target_x, target_y, width, height = self.position_mini(x, y)
        self.mini.deiconify()
        self.mini.update_idletasks()
        try:
            inner_hwnd = self.mini.winfo_id()
            hwnd = ctypes.windll.user32.GetParent(inner_hwnd) or inner_hwnd
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
        except Exception:
            self.mini.lift()

    def set_mode(self, mode: str) -> None:
        if mode not in {"mini", "panel"}:
            return
        self.display_mode = mode
        self.store.set("display_mode", mode)
        if mode == "panel":
            self.hide_mini()
            self.show_panel()
        else:
            self.root.withdraw()
            self.hide_mini()
        self.refresh_mode_buttons()
        if self.settings_window.window and self.settings_window.window.winfo_exists():
            self.settings_window.refresh_mode_buttons()
        self.update_tray_menu()

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
        self.store.set("display_mode", "panel")
        self.hide_mini()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.refresh_mode_buttons()
        self.update_tray_menu()

    def hide_panel(self) -> None:
        self.root.withdraw()

    def hide_mini(self) -> None:
        if self.mini.winfo_exists():
            self.mini.withdraw()

    def hide_active_window(self) -> None:
        if self.mini.winfo_viewable():
            self.hide_mini()
        else:
            self.hide_panel()

    def on_global_mouse_down(self, x: int, y: int) -> None:
        self.root.after(0, self._dismiss_mini_if_outside, x, y)

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

    def set_desktop_enabled(self, enabled: bool) -> None:
        self.desktop_enabled = bool(enabled)
        self.store.set("desktop_enabled", self.desktop_enabled)
        self.pause_button.configure(text="暂停取词" if self.desktop_enabled else "恢复取词")
        self.status_text.set("桌面取词已开启" if self.desktop_enabled else "桌面取词已暂停")
        if not self.desktop_enabled:
            self.hide_mini()
        self.update_tray_menu()

    def toggle_desktop(self) -> None:
        self.set_desktop_enabled(not bool(self.store.get("desktop_enabled", True)))

    def refresh_app_state(self) -> None:
        if not self.current_app:
            return
        enabled = self.store.is_app_enabled(self.current_app.exe)
        self.mini_app_enabled.set(enabled)
        self.panel_app_enabled.set(enabled)
        self.mini_app_toggle.draw()
        self.panel_app_toggle.draw()

    def toggle_current_app(self) -> None:
        if not self.current_app:
            return
        enabled = self.mini_app_enabled.get() if self.mini.winfo_viewable() else self.panel_app_enabled.get()
        self.store.set_app(
            self.current_app.exe,
            enabled,
            name=self.current_app.name,
            path=self.current_app.path,
        )
        self.mini_app_enabled.set(enabled)
        self.panel_app_enabled.set(enabled)
        self.mini_app_toggle.draw()
        self.panel_app_toggle.draw()
        self.status_text.set(f"已在 {self.current_app.name} {'开启' if enabled else '关闭'}划词翻译")
        if not enabled:
            self.hide_mini()

    def set_status(self, value: str) -> None:
        self.root.after(0, self.status_text.set, value)

    def set_engine_status(self, value: str) -> None:
        self.root.after(0, self.engine_text.set, value)

    def on_selection(self, text: str, app_info: AppInfo, x: int, y: int) -> None:
        self.root.after(0, self._handle_selection, text, app_info, x, y)

    def _handle_selection(self, text: str, app_info: AppInfo, x: int, y: int) -> None:
        if not bool(self.store.get("desktop_enabled", True)) or not self.store.is_app_enabled(app_info.exe):
            return
        self.current_app = app_info
        self.active_source = text
        self.selection_point = (x, y)
        self.app_text.set(f"来自 {app_info.name}")
        self.mini_app_text.set(f"{app_info.name}  开启")
        self.refresh_app_state()
        self.source_text.delete("1.0", "end")
        self.source_text.insert("1.0", text)
        self.direction_text.set("中文 → 英文" if engine.contains_chinese(text) else "英文 → 中文")
        self.phonetic_text.set("正在查询音标…" if engine.WORD_PATTERN.fullmatch(text) else "可直接播放整句发音")
        if self.display_mode == "mini":
            self.show_mini_loading(text, x, y)
        else:
            self.root.deiconify()
            self.root.lift()
        if self.auto_translate:
            self.enqueue(text)

    def show_mini_loading(self, text: str, x: int, y: int) -> None:
        self.mini_source.set(text if len(text) <= 48 else text[:47].rstrip() + "…")
        self.mini_phonetic.set("正在查询音标…" if engine.WORD_PATTERN.fullmatch(text) else "整句发音")
        self.mini_translation.set("正在本地翻译…")
        self.show_mini_no_activate(x, y)

    def translate_manual(self) -> None:
        text = engine.normalize_selection(self.source_text.get("1.0", "end"))
        if not text:
            return
        self.active_source = text
        self.direction_text.set("中文 → 英文" if engine.contains_chinese(text) else "英文 → 中文")
        self.enqueue(text)

    def enqueue(self, text: str) -> None:
        while True:
            try:
                self.request_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self.request_queue.put_nowait(text)
        except queue.Full:
            pass
        self.engine_text.set("正在本地翻译…")

    def _translation_loop(self) -> None:
        try:
            self.translator.load()
        except Exception as exc:
            log(f"Engine load error: {exc}\n{traceback.format_exc()}")
            self.set_engine_status("本地引擎加载失败")
            self.root.after(0, messagebox.showerror, APP_NAME, f"本地翻译引擎加载失败：\n{exc}\n\n日志：{engine.LOG_PATH}")
            return
        while True:
            text = self.request_queue.get()
            if text is None:
                return
            try:
                result = self.translator.translate(text)
                self.root.after(0, self.show_result, result)
            except Exception as exc:
                log(f"Translate error: {exc}\n{traceback.format_exc()}")
                self.root.after(0, self.show_error, str(exc))

    @staticmethod
    def compact_translation(text: str, limit: int = 125) -> str:
        parts = [part.strip() for part in text.splitlines() if part.strip()]
        value = "；".join(parts[:2]) if parts else text.strip()
        return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"

    def show_result(self, result: engine.TranslationResult) -> None:
        if result.source != self.active_source:
            return
        self.current_result = result
        self.direction_text.set("中文 → 英文" if result.source_language == "zh" else "英文 → 中文")
        self.phonetic_text.set(f"/{result.phonetic}/" if result.phonetic else "点击右侧按钮播放标准发音")
        self._set_text(self.result_text, result.translated)
        self.meta_text.set(f"{result.engine} · {result.elapsed_ms} ms")
        self.engine_text.set("本地引擎已就绪")
        if self.display_mode == "mini":
            source = result.source if len(result.source) <= 48 else result.source[:47].rstrip() + "…"
            self.mini_source.set(source)
            self.mini_phonetic.set(f"/{result.phonetic}/" if result.phonetic else "整句发音")
            self.mini_translation.set(self.compact_translation(result.translated))
            self.show_mini_no_activate(*self.selection_point)

    def show_error(self, message: str) -> None:
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

    def speak(self, accent: str) -> None:
        text = self.english_text()
        if text:
            self.speech.speak(text, accent)

    def copy_translation(self) -> None:
        if not self.current_result:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.current_result.translated)
        self.status_text.set("译文已复制")
        self.root.after(1200, self.status_text.set, "桌面取词已开启")

    def _tray_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("打开大窗口", lambda _icon, _item: self.root.after(0, self.show_panel), default=True),
            pystray.MenuItem(
                "迷你浮窗模式",
                lambda _icon, _item: self.root.after(0, self.set_mode, "mini"),
                checked=lambda _item: self.display_mode == "mini",
                radio=True,
            ),
            pystray.MenuItem(
                "大窗口模式",
                lambda _icon, _item: self.root.after(0, self.set_mode, "panel"),
                checked=lambda _item: self.display_mode == "panel",
                radio=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "桌面取词已开启",
                lambda _icon, _item: self.root.after(0, self.toggle_desktop),
                checked=lambda _item: bool(self.store.get("desktop_enabled", True)),
            ),
            pystray.MenuItem("应用设置…", lambda _icon, _item: self.root.after(0, self.open_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda _icon, _item: self.root.after(0, self.quit)),
        )

    def start_tray(self) -> None:
        try:
            image = Image.open(app_icon_path("png")).convert("RGBA")
            self.tray = pystray.Icon("DaShengFaTranslator", image, APP_NAME, self._tray_menu())
            threading.Thread(target=self.tray.run, daemon=True, name="TrayIcon").start()
            if not bool(self.store.get("tray_tip_seen", False)):
                self.root.after(1600, self._show_first_tray_tip)
        except Exception as exc:
            log(f"Tray icon error: {exc}\n{traceback.format_exc()}")
            if self.display_mode == "mini":
                self.show_panel()

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
        self.watcher = DesktopSelectionWatcher(
            self.store,
            self.on_selection,
            self.set_status,
            self.on_global_mouse_down,
        )
        self.watcher.start()
        self.start_tray()

    def quit(self) -> None:
        if self.watcher:
            self.watcher.stop()
        try:
            self.request_queue.put_nowait(None)
        except queue.Full:
            pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
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


def acquire_single_instance() -> socket.socket | None:
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind(("127.0.0.1", INSTANCE_PORT))
        guard.listen(1)
        return guard
    except OSError:
        guard.close()
        return None


def main() -> None:
    set_dpi_awareness()
    guard = acquire_single_instance()
    if guard is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, f"{APP_NAME} 已经在运行，请查看系统托盘。")
        root.destroy()
        return
    try:
        DesktopTranslatorApp().run()
    finally:
        guard.close()


if __name__ == "__main__":
    main()
