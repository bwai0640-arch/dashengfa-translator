from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Protocol, Sequence


PP_SELECTION_TEXT = 3
TEXT_ONLY_CLIPBOARD_FORMATS = frozenset({1, 7, 13, 16})


@dataclass(frozen=True, slots=True)
class CaptureTiming:
    settle_seconds: float
    clipboard_timeout_seconds: float


DEFAULT_TIMING = CaptureTiming(settle_seconds=0.075, clipboard_timeout_seconds=0.55)
APP_TIMINGS: dict[str, CaptureTiming] = {
    "powerpnt.exe": CaptureTiming(settle_seconds=0.14, clipboard_timeout_seconds=1.10),
    "wechat.exe": CaptureTiming(settle_seconds=0.13, clipboard_timeout_seconds=0.90),
    "weixin.exe": CaptureTiming(settle_seconds=0.13, clipboard_timeout_seconds=0.90),
    "qq.exe": CaptureTiming(settle_seconds=0.13, clipboard_timeout_seconds=0.90),
}


def timing_for_app(exe: str) -> CaptureTiming:
    return APP_TIMINGS.get(exe.lower(), DEFAULT_TIMING)


def read_powerpoint_selected_text(
    get_active_object: Callable[[str], object],
    max_length: int,
    expected_window_title: str = "",
) -> str:
    """Read only an actual PowerPoint text selection, never a whole shape."""
    try:
        powerpoint = get_active_object("PowerPoint.Application")
        window = powerpoint.ActiveWindow
        if expected_window_title:
            caption = str(window.Caption).strip()
            if not caption or caption.casefold() not in expected_window_title.casefold():
                return ""
        selection = window.Selection
        if int(selection.Type) != PP_SELECTION_TEXT:
            return ""
        text = str(selection.TextRange.Text)
        return text if 0 < len(text) <= max_length else ""
    except Exception:
        return ""


def read_uia_selected_text(
    controls: Sequence[object],
    pattern_ids: Sequence[object],
    max_length: int,
    normalize: Callable[[str], str],
) -> tuple[str, bool]:
    """Probe focused/pointed UIA controls without relying on their rectangles."""
    control_chains: list[list[object]] = []
    for control in controls:
        current = control
        chain: list[object] = []
        for _level in range(9):
            if not current:
                break
            chain.append(current)
            try:
                if bool(getattr(current, "IsPassword", False)):
                    return "", True
            except Exception:
                pass
            try:
                current = current.GetParentControl()
            except Exception:
                break
        control_chains.append(chain)

    for chain in control_chains:
        for current in chain:
            for pattern_id in pattern_ids:
                try:
                    pattern = current.GetPattern(pattern_id)
                    if not pattern:
                        continue
                    values: list[str] = []
                    for text_range in pattern.GetSelection() or []:
                        value = normalize(text_range.GetText(max_length + 1))
                        if value:
                            values.append(value)
                    selected = "\n".join(values).strip()
                    if selected:
                        return selected, False
                except Exception:
                    continue
    return "", False


def hresult_succeeded(value: int) -> bool:
    return int(value) >= 0


class ClipboardSnapshot(Protocol):
    def restore(self) -> bool: ...

    def close(self) -> None: ...


class _Ole32Api:
    def __init__(self) -> None:
        self.dll = ctypes.OleDLL("ole32")
        self.dll.OleInitialize.argtypes = [ctypes.c_void_p]
        self.dll.OleInitialize.restype = ctypes.c_long
        self.dll.OleUninitialize.argtypes = []
        self.dll.OleUninitialize.restype = None
        self.dll.OleGetClipboard.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.dll.OleGetClipboard.restype = ctypes.c_long
        self.dll.OleSetClipboard.argtypes = [ctypes.c_void_p]
        self.dll.OleSetClipboard.restype = ctypes.c_long
        self.dll.OleFlushClipboard.argtypes = []
        self.dll.OleFlushClipboard.restype = ctypes.c_long

    def initialize(self) -> bool:
        return hresult_succeeded(self.dll.OleInitialize(None))

    def uninitialize(self) -> None:
        self.dll.OleUninitialize()

    def get_clipboard(self) -> int | None:
        pointer = ctypes.c_void_p()
        result = self.dll.OleGetClipboard(ctypes.byref(pointer))
        if not hresult_succeeded(result) or not pointer.value:
            return None
        return int(pointer.value)

    def set_clipboard(self, pointer: int) -> bool:
        return hresult_succeeded(self.dll.OleSetClipboard(ctypes.c_void_p(pointer)))

    def flush_clipboard(self) -> bool:
        return hresult_succeeded(self.dll.OleFlushClipboard())

    @staticmethod
    def release(pointer: int) -> None:
        # IDataObject inherits IUnknown; Release is the third vtable entry.
        instance = ctypes.c_void_p(pointer)
        vtable = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
        release(instance)


@lru_cache(maxsize=1)
def _ole32_api() -> _Ole32Api:
    return _Ole32Api()


def initialize_ole_clipboard() -> bool:
    try:
        return _ole32_api().initialize()
    except Exception:
        return False


def uninitialize_ole_clipboard() -> None:
    try:
        _ole32_api().uninitialize()
    except Exception:
        pass


class OleClipboardSnapshot:
    """Short-lived IDataObject snapshot used to restore every clipboard format."""

    def __init__(self, pointer: int, api: _Ole32Api) -> None:
        self.pointer = pointer
        self.api = api

    @classmethod
    def capture(
        cls,
        *,
        retries: int = 6,
        retry_delay: float = 0.02,
        sleep: Callable[[float], None] = time.sleep,
    ) -> OleClipboardSnapshot | None:
        try:
            api = _ole32_api()
        except Exception:
            return None
        for _attempt in range(retries):
            try:
                pointer = api.get_clipboard()
                if pointer:
                    return cls(pointer, api)
            except Exception:
                pass
            sleep(retry_delay)
        return None

    def restore(self) -> bool:
        if not self.pointer:
            return False
        for _attempt in range(6):
            try:
                if self.api.set_clipboard(self.pointer):
                    # Materialize the data so restoration survives app/thread shutdown.
                    for _flush_attempt in range(6):
                        if self.api.flush_clipboard():
                            return True
                        time.sleep(0.02)
                    return False
            except Exception:
                pass
            time.sleep(0.02)
        return False

    def close(self) -> None:
        if not self.pointer:
            return
        pointer, self.pointer = self.pointer, 0
        try:
            self.api.release(pointer)
        except Exception:
            pass


@dataclass(frozen=True, slots=True)
class ClipboardCaptureResult:
    text: str = ""
    attempted: bool = False
    restored: bool = True
    reason: str = ""


def capture_selected_text_with_clipboard(
    *,
    old_text: str | None,
    old_formats: Sequence[int],
    old_state_known: bool,
    old_sequence: int,
    snapshot_factory: Callable[[], ClipboardSnapshot | None],
    sequence_number: Callable[[], int],
    send_copy: Callable[[], None],
    read_text: Callable[[], str | None],
    restore_plain_text: Callable[[str | None], bool],
    timeout_seconds: float,
    focus_is_current: Callable[[], bool] = lambda: True,
    clipboard_change_is_ours: Callable[[], bool] = lambda: True,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ClipboardCaptureResult:
    """Copy a selection temporarily and restore the previous clipboard safely."""
    try:
        focus_current = focus_is_current()
    except Exception:
        focus_current = False
    if not focus_current:
        return ClipboardCaptureResult(reason="focus_changed")
    try:
        snapshot = snapshot_factory()
    except Exception:
        snapshot = None
    format_set = set(old_formats)
    can_restore_as_plain_text = (
        not format_set
        or (
            13 in format_set
            and old_text is not None
            and format_set.issubset(TEXT_ONLY_CLIPBOARD_FORMATS)
        )
    )
    if snapshot is None and (not old_state_known or not can_restore_as_plain_text):
        return ClipboardCaptureResult(reason="snapshot_unavailable")

    copied = ""
    reason = "no_clipboard_change"
    attempted = False
    restored = True
    clipboard_changed = False
    observed_sequence: int | None = None
    preserve_external_change = False
    cancelled_reason = ""
    try:
        before = sequence_number()
        if before != old_sequence:
            reason = "concurrent_change"
            return ClipboardCaptureResult(reason=reason)
        attempted = True
        try:
            send_copy()
        except Exception:
            reason = "copy_failed"
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            sleep(0.025)
            try:
                current_sequence = sequence_number()
            except Exception:
                if not cancelled_reason:
                    cancelled_reason = "sequence_unavailable"
                continue
            try:
                focus_current = focus_is_current()
            except Exception:
                focus_current = False
            if not focus_current and not cancelled_reason:
                cancelled_reason = "focus_changed"
            if current_sequence == before:
                continue
            clipboard_changed = True
            observed_sequence = current_sequence
            try:
                change_is_ours = clipboard_change_is_ours()
            except Exception:
                change_is_ours = False
            if not change_is_ours:
                preserve_external_change = True
                reason = "concurrent_change"
                break
            if cancelled_reason:
                reason = cancelled_reason
                break
            value = read_text()
            if value:
                copied = value
                reason = "captured"
                break
        if cancelled_reason and not clipboard_changed:
            reason = cancelled_reason
        if clipboard_changed and not copied and reason == "no_clipboard_change":
            reason = "no_text"
    except Exception:
        reason = "copy_failed"
    finally:
        try:
            if clipboard_changed and observed_sequence is not None and not preserve_external_change:
                try:
                    latest_sequence = sequence_number()
                except Exception:
                    latest_sequence = None
                if latest_sequence is None or latest_sequence != observed_sequence:
                    # A user or another application copied something after our read.
                    # Preserve that newer clipboard content instead of overwriting it.
                    restored = True
                    reason = "external_change_preserved" if copied else "concurrent_change"
                elif snapshot is not None:
                    try:
                        restored = snapshot.restore()
                    except Exception:
                        restored = False
                elif can_restore_as_plain_text:
                    try:
                        restored = restore_plain_text(old_text)
                    except Exception:
                        restored = False
        finally:
            if snapshot is not None:
                try:
                    snapshot.close()
                except Exception:
                    pass

    if not restored:
        reason = "restore_failed"
    return ClipboardCaptureResult(
        text=copied,
        attempted=attempted,
        restored=restored,
        reason=reason,
    )
