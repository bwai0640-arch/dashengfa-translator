from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Protocol


WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
PM_NOREMOVE = 0x0000

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

VK_C = 0x43
VK_X = 0x58
VK_F1 = 0x70
VK_F4 = 0x73
VK_F12 = 0x7B

# RegisterHotKey reports ERROR_HOTKEY_ALREADY_REGISTERED when another
# application already owns a combination.
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# Application hot-key identifiers must be between 0x0000 and 0xBFFF.
HOTKEY_RETRY_ID = 0x4101
HOTKEY_TOGGLE_MODE_ID = 0x4102


class HotkeyCommand(str, Enum):
    """Small commands posted by the native hot-key thread."""

    RETRY_AND_SPEAK_US = "retry_and_speak_us"
    TOGGLE_WINDOW_MODE = "toggle_window_mode"


class HotkeyBindingKind(str, Enum):
    """How a binding is detected.

    ``DOUBLE_ALT`` is intentionally not passed to RegisterHotKey. The desktop
    input listener owns that gesture and dispatches the command itself.
    """

    NATIVE = "native"
    DOUBLE_ALT = "double_alt"


_HOTKEY_ID_BY_COMMAND = {
    HotkeyCommand.RETRY_AND_SPEAK_US: HOTKEY_RETRY_ID,
    HotkeyCommand.TOGGLE_WINDOW_MODE: HOTKEY_TOGGLE_MODE_ID,
}

_MODIFIER_TOKENS = {
    "ctrl": (MOD_CONTROL, "Ctrl"),
    "control": (MOD_CONTROL, "Ctrl"),
    "alt": (MOD_ALT, "Alt"),
    "shift": (MOD_SHIFT, "Shift"),
    "win": (MOD_WIN, "Win"),
    "windows": (MOD_WIN, "Win"),
}

_MODIFIER_DISPLAY_ORDER = (
    (MOD_CONTROL, "Ctrl"),
    (MOD_ALT, "Alt"),
    (MOD_SHIFT, "Shift"),
    (MOD_WIN, "Win"),
)

_ALLOWED_MODIFIER_MASK = MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_WIN

# RegisterHotKey owns a combination globally. Letting users bind the ordinary
# editing shortcuts below would make copy, paste, save, undo, and other basic
# commands stop working in every application while this process is running.
# Chorded variants such as Ctrl+Shift+C remain available.
_RESERVED_SINGLE_CTRL_KEYS = frozenset(ord(key) for key in "ACFNOPSVWXYZ")


@dataclass(frozen=True, slots=True)
class HotkeySpec:
    """One validated RegisterHotKey request.

    Callers normally create instances with :func:`parse_hotkey_spec`.  The
    public dataclass remains available for native integrations; the service
    validates and canonicalises injected instances before starting a thread.
    """

    hotkey_id: int
    command: HotkeyCommand
    modifiers: int
    virtual_key: int
    label: str = ""
    binding_kind: HotkeyBindingKind = HotkeyBindingKind.NATIVE

    @property
    def requires_native_registration(self) -> bool:
        return HotkeyBindingKind(self.binding_kind) is HotkeyBindingKind.NATIVE


def _virtual_key_from_token(token: str) -> int | None:
    upper = token.upper()
    if len(upper) == 1 and ("A" <= upper <= "Z" or "0" <= upper <= "9"):
        return ord(upper)
    if upper.startswith("F") and upper[1:].isdigit():
        number = int(upper[1:])
        if 1 <= number <= 12:
            return VK_F1 + number - 1
    return None


def _primary_key_label(virtual_key: int) -> str:
    if ord("A") <= virtual_key <= ord("Z"):
        return chr(virtual_key)
    if ord("0") <= virtual_key <= ord("9"):
        return chr(virtual_key)
    if VK_F1 <= virtual_key <= VK_F12:
        return f"F{virtual_key - VK_F1 + 1}"
    raise ValueError("快捷键主键仅支持 A-Z、0-9 或 F1-F12")


def _validate_native_combination(modifiers: int, virtual_key: int) -> None:
    active_modifiers = int(modifiers) & _ALLOWED_MODIFIER_MASK
    if active_modifiers == MOD_SHIFT:
        raise ValueError(
            "仅按 Shift 的全局快捷键会占用正常的大写输入，请再增加 Ctrl、Alt 或 Win"
        )
    if active_modifiers == MOD_WIN:
        raise ValueError(
            "仅按 Win 的组合通常属于 Windows 系统快捷键，请再增加 Ctrl、Alt 或 Shift"
        )
    if active_modifiers == MOD_ALT and int(virtual_key) == VK_F4:
        raise ValueError("Alt+F4 是 Windows 标准关闭快捷键，不能被注册为全局快捷键")
    if (
        active_modifiers == MOD_CONTROL
        and int(virtual_key) in _RESERVED_SINGLE_CTRL_KEYS
    ):
        key_label = _primary_key_label(int(virtual_key))
        raise ValueError(
            f"Ctrl+{key_label} 是常用编辑快捷键，不能被注册为全局快捷键"
        )


def format_hotkey_spec(spec: HotkeySpec) -> str:
    """Return a stable, user-facing representation such as ``Ctrl+Shift+F8``."""

    try:
        binding_kind = HotkeyBindingKind(spec.binding_kind)
    except (TypeError, ValueError) as exc:
        raise ValueError("未知的快捷键绑定类型") from exc
    if binding_kind is HotkeyBindingKind.DOUBLE_ALT:
        return "双击 Alt"

    modifiers = int(spec.modifiers)
    unknown_bits = modifiers & ~(_ALLOWED_MODIFIER_MASK | MOD_NOREPEAT)
    if unknown_bits:
        raise ValueError(f"快捷键包含不支持的修饰键位：0x{unknown_bits:X}")

    active_modifiers = modifiers & _ALLOWED_MODIFIER_MASK
    if not active_modifiers:
        raise ValueError("快捷键必须至少包含 Ctrl、Alt、Shift 或 Win 中的一个")

    parts = [
        label
        for flag, label in _MODIFIER_DISPLAY_ORDER
        if active_modifiers & flag
    ]
    parts.append(_primary_key_label(int(spec.virtual_key)))
    return "+".join(parts)


def parse_hotkey_spec(
    command: HotkeyCommand | str,
    text: str,
    *,
    hotkey_id: int | None = None,
) -> HotkeySpec:
    """Parse and validate one configurable shortcut.

    Supported combinations contain one or more of Ctrl/Alt/Shift/Win and
    exactly one A-Z, 0-9 or F1-F12 primary key. Parsing is case-insensitive;
    the returned label always uses the canonical display order.
    """

    try:
        parsed_command = HotkeyCommand(command)
    except (TypeError, ValueError) as exc:
        raise ValueError("未知的快捷键功能") from exc

    if not isinstance(text, str) or not text.strip():
        raise ValueError("快捷键不能为空")

    compact_text = " ".join(text.strip().split()).casefold()
    if compact_text in {"double alt", "双击 alt"}:
        if parsed_command is not HotkeyCommand.RETRY_AND_SPEAK_US:
            raise ValueError("双击 Alt 仅用于重新获取并自动美音")
        resolved_id = (
            _HOTKEY_ID_BY_COMMAND[parsed_command]
            if hotkey_id is None
            else int(hotkey_id)
        )
        return HotkeySpec(
            resolved_id,
            parsed_command,
            MOD_ALT,
            0,
            "双击 Alt",
            HotkeyBindingKind.DOUBLE_ALT,
        )

    raw_tokens = text.split("+")
    tokens = [token.strip() for token in raw_tokens]
    if any(not token for token in tokens):
        raise ValueError("快捷键格式无效")

    modifiers = 0
    virtual_key: int | None = None
    seen_modifier_flags: set[int] = set()
    for token in tokens:
        modifier = _MODIFIER_TOKENS.get(token.casefold())
        if modifier is not None:
            flag, _label = modifier
            if flag in seen_modifier_flags:
                raise ValueError(f"修饰键 {token} 重复")
            if virtual_key is not None:
                raise ValueError("修饰键必须写在主键之前")
            seen_modifier_flags.add(flag)
            modifiers |= flag
            continue

        key = _virtual_key_from_token(token)
        if key is None:
            raise ValueError(f"不支持的快捷键按键：{token}")
        if virtual_key is not None:
            raise ValueError("快捷键只能包含一个主键")
        virtual_key = key

    if not modifiers:
        raise ValueError("快捷键必须至少包含 Ctrl、Alt、Shift 或 Win 中的一个")
    if virtual_key is None:
        raise ValueError("快捷键缺少 A-Z、0-9 或 F1-F12 主键")

    _validate_native_combination(modifiers, virtual_key)

    resolved_id = (
        _HOTKEY_ID_BY_COMMAND[parsed_command]
        if hotkey_id is None
        else int(hotkey_id)
    )
    provisional = HotkeySpec(
        resolved_id,
        parsed_command,
        modifiers | MOD_NOREPEAT,
        virtual_key,
    )
    return HotkeySpec(
        provisional.hotkey_id,
        provisional.command,
        provisional.modifiers,
        provisional.virtual_key,
        format_hotkey_spec(provisional),
    )


def _normalise_hotkey_spec(spec: HotkeySpec) -> HotkeySpec:
    if not isinstance(spec, HotkeySpec):
        raise ValueError("快捷键配置必须是 HotkeySpec")
    try:
        command = HotkeyCommand(spec.command)
    except (TypeError, ValueError) as exc:
        raise ValueError("未知的快捷键功能") from exc

    hotkey_id = int(spec.hotkey_id)
    if not 0 <= hotkey_id <= 0xBFFF:
        raise ValueError("快捷键 ID 必须位于 0x0000 到 0xBFFF")

    try:
        binding_kind = HotkeyBindingKind(spec.binding_kind)
    except (TypeError, ValueError) as exc:
        raise ValueError("未知的快捷键绑定类型") from exc

    if binding_kind is HotkeyBindingKind.DOUBLE_ALT:
        if command is not HotkeyCommand.RETRY_AND_SPEAK_US:
            raise ValueError("双击 Alt 仅用于重新获取并自动美音")
        return HotkeySpec(
            hotkey_id,
            command,
            MOD_ALT,
            0,
            "双击 Alt",
            binding_kind,
        )

    modifiers = int(spec.modifiers)
    unknown_bits = modifiers & ~(_ALLOWED_MODIFIER_MASK | MOD_NOREPEAT)
    if unknown_bits:
        raise ValueError(f"快捷键包含不支持的修饰键位：0x{unknown_bits:X}")
    modifiers = (modifiers & _ALLOWED_MODIFIER_MASK) | MOD_NOREPEAT
    _validate_native_combination(modifiers, int(spec.virtual_key))

    provisional = HotkeySpec(
        hotkey_id,
        command,
        modifiers,
        int(spec.virtual_key),
    )
    return HotkeySpec(
        provisional.hotkey_id,
        provisional.command,
        provisional.modifiers,
        provisional.virtual_key,
        format_hotkey_spec(provisional),
        binding_kind,
    )


def normalise_hotkey_specs(specs: Iterable[HotkeySpec]) -> tuple[HotkeySpec, HotkeySpec]:
    """Validate the two app commands and return them in stable command order."""

    normalised = tuple(_normalise_hotkey_spec(spec) for spec in specs)
    if len(normalised) != 2:
        raise ValueError("必须分别配置“重新获取”和“切换窗口”两个快捷键")

    by_command: dict[HotkeyCommand, HotkeySpec] = {}
    ids: set[int] = set()
    combinations: set[tuple[HotkeyBindingKind, int, int]] = set()
    for spec in normalised:
        if spec.command in by_command:
            raise ValueError("同一功能不能配置多个快捷键")
        if spec.hotkey_id in ids:
            raise ValueError("两个快捷键不能使用相同的内部 ID")
        combination = (
            spec.binding_kind,
            spec.modifiers & _ALLOWED_MODIFIER_MASK,
            spec.virtual_key,
        )
        if combination in combinations:
            raise ValueError("两个功能不能使用相同的快捷键")
        by_command[spec.command] = spec
        ids.add(spec.hotkey_id)
        combinations.add(combination)

    missing = [command for command in HotkeyCommand if command not in by_command]
    if missing:
        raise ValueError("必须分别配置“重新获取”和“切换窗口”两个快捷键")

    return (
        by_command[HotkeyCommand.RETRY_AND_SPEAK_US],
        by_command[HotkeyCommand.TOGGLE_WINDOW_MODE],
    )


# Backwards-compatible defaults. Runtime registration uses each service
# instance's validated snapshot rather than reading this module constant.
HOTKEY_SPECS = (
    parse_hotkey_spec(HotkeyCommand.RETRY_AND_SPEAK_US, "双击 Alt"),
    parse_hotkey_spec(HotkeyCommand.TOGGLE_WINDOW_MODE, "Alt+C"),
)


@dataclass(frozen=True, slots=True)
class NativeCallResult:
    succeeded: bool
    error_code: int = 0


@dataclass(frozen=True, slots=True)
class NativeMessage:
    """Result of one GetMessageW call.

    ``result`` has the same three states as GetMessageW: positive for a
    message, zero for WM_QUIT, and -1 for an error.
    """

    result: int
    message: int = 0
    hotkey_id: int = 0
    error_code: int = 0


class HotkeyApi(Protocol):
    """Injectable Win32 boundary used by :class:`WindowsHotkeyService`."""

    def ensure_message_queue(self) -> None: ...

    def current_thread_id(self) -> int: ...

    def register_hotkey(self, spec: HotkeySpec) -> NativeCallResult: ...

    def unregister_hotkey(self, hotkey_id: int) -> NativeCallResult: ...

    def get_message(self) -> NativeMessage: ...

    def post_quit(self, thread_id: int) -> NativeCallResult: ...


class Win32HotkeyApi:
    """Thin ctypes wrapper around the Win32 global-hot-key APIs."""

    def __init__(self) -> None:
        if not hasattr(ctypes, "WinDLL"):
            raise OSError("Global hotkeys require Windows")

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self.user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.user32.PeekMessageW.restype = wintypes.BOOL
        self.user32.RegisterHotKey.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.user32.RegisterHotKey.restype = wintypes.BOOL
        self.user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.UnregisterHotKey.restype = wintypes.BOOL
        self.user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.user32.GetMessageW.restype = ctypes.c_int
        self.user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user32.PostThreadMessageW.restype = wintypes.BOOL
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    @staticmethod
    def _result(succeeded: object) -> NativeCallResult:
        if succeeded:
            return NativeCallResult(True)
        return NativeCallResult(False, int(ctypes.get_last_error()))

    def ensure_message_queue(self) -> None:
        # PostThreadMessageW fails until the destination thread has created its
        # message queue. PeekMessageW is the documented way to create it.
        message = wintypes.MSG()
        self.user32.PeekMessageW(
            ctypes.byref(message),
            None,
            0,
            0,
            PM_NOREMOVE,
        )

    def current_thread_id(self) -> int:
        return int(self.kernel32.GetCurrentThreadId())

    def register_hotkey(self, spec: HotkeySpec) -> NativeCallResult:
        ctypes.set_last_error(0)
        return self._result(
            self.user32.RegisterHotKey(
                None,
                spec.hotkey_id,
                spec.modifiers,
                spec.virtual_key,
            )
        )

    def unregister_hotkey(self, hotkey_id: int) -> NativeCallResult:
        ctypes.set_last_error(0)
        return self._result(self.user32.UnregisterHotKey(None, hotkey_id))

    def get_message(self) -> NativeMessage:
        message = wintypes.MSG()
        ctypes.set_last_error(0)
        result = int(self.user32.GetMessageW(ctypes.byref(message), None, 0, 0))
        error_code = int(ctypes.get_last_error()) if result == -1 else 0
        return NativeMessage(
            result=result,
            message=int(message.message),
            hotkey_id=int(message.wParam),
            error_code=error_code,
        )

    def post_quit(self, thread_id: int) -> NativeCallResult:
        ctypes.set_last_error(0)
        return self._result(
            self.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        )


@dataclass(frozen=True, slots=True)
class HotkeyRegistrationResult:
    spec: HotkeySpec
    registered: bool
    error_code: int = 0
    delegated: bool = False

    @property
    def available(self) -> bool:
        return self.registered or self.delegated


@dataclass(frozen=True, slots=True)
class HotkeyRegistrationReport:
    retry: HotkeyRegistrationResult
    toggle_mode: HotkeyRegistrationResult
    startup_error: str = ""

    @property
    def results(self) -> tuple[HotkeyRegistrationResult, HotkeyRegistrationResult]:
        return self.retry, self.toggle_mode

    @property
    def any_registered(self) -> bool:
        return any(result.available for result in self.results)

    @property
    def all_registered(self) -> bool:
        return all(result.available for result in self.results)

    @classmethod
    def startup_failed(
        cls,
        message: str,
        specs: Iterable[HotkeySpec] | None = None,
    ) -> "HotkeyRegistrationReport":
        retry_spec, mode_spec = normalise_hotkey_specs(
            HOTKEY_SPECS if specs is None else specs
        )
        return cls(
            HotkeyRegistrationResult(retry_spec, False),
            HotkeyRegistrationResult(mode_spec, False),
            startup_error=message,
        )


def registration_status_text(report: HotkeyRegistrationReport) -> str:
    """Return a short Chinese status suitable for the application's status bar."""

    startup_error = getattr(report, "startup_error", "")
    if startup_error:
        if "cancel" in startup_error.casefold():
            return "全局快捷键启动已取消"
        return f"全局快捷键启动失败：{startup_error}"
    if getattr(report, "all_registered", False):
        results = getattr(report, "results", ())
        if results:
            labels = "、".join(result.spec.label for result in results)
            return f"全局快捷键已启用：{labels}"
        # Keep this formatter usable with lightweight registration adapters and
        # test doubles that expose only the aggregate result.
        return "全局快捷键已启用"

    available: list[str] = []
    delegated: list[str] = []
    unavailable: list[str] = []
    for result in report.results:
        if result.delegated:
            delegated.append(result.spec.label)
            continue
        if result.registered:
            available.append(result.spec.label)
            continue
        if result.error_code == ERROR_HOTKEY_ALREADY_REGISTERED:
            unavailable.append(f"{result.spec.label} 被其他程序占用")
        elif result.error_code:
            unavailable.append(
                f"{result.spec.label} 注册失败（错误 {result.error_code}）"
            )
        else:
            unavailable.append(f"{result.spec.label} 注册失败")

    parts = unavailable
    if available:
        parts.append(f"{'、'.join(available)} 可用")
    if delegated:
        parts.append(f"{'、'.join(delegated)} 由应用监听")
    return "；".join(parts)


class WindowsHotkeyService:
    """Register configurable global hotkeys on one Win32 message thread.

    The service never performs capture, speech, or UI work. It only invokes
    ``command_callback`` with a small command value; callers should make that
    callback enqueue work onto their existing worker/UI queues.
    """

    def __init__(
        self,
        command_callback: Callable[[HotkeyCommand], None],
        status_callback: Callable[[str], None] | None = None,
        *,
        report_registration: Callable[[HotkeyRegistrationReport], None] | None = None,
        report_error: Callable[[str], None] | None = None,
        api: HotkeyApi | None = None,
        hotkey_specs: Iterable[HotkeySpec] | None = None,
    ) -> None:
        self._post_command = command_callback
        self._status_callback = status_callback
        self._report_registration = report_registration or (lambda _report: None)
        self._report_error = report_error or (lambda _message: None)
        self._api = api

        self._state_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._hotkey_specs = normalise_hotkey_specs(
            HOTKEY_SPECS if hotkey_specs is None else hotkey_specs
        )
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._stopped.set()
        self._stop_requested = threading.Event()
        self._registration_report: HotkeyRegistrationReport | None = None

    @property
    def hotkey_specs(self) -> tuple[HotkeySpec, HotkeySpec]:
        with self._state_lock:
            return self._hotkey_specs

    @property
    def native_hotkey_specs(self) -> tuple[HotkeySpec, ...]:
        with self._state_lock:
            return tuple(
                spec
                for spec in self._hotkey_specs
                if spec.requires_native_registration
            )

    @property
    def registration_report(self) -> HotkeyRegistrationReport | None:
        with self._state_lock:
            return self._registration_report

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return bool(self._thread and self._thread.is_alive())

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    def start(
        self,
        ready_timeout_seconds: float | None = 1.0,
    ) -> HotkeyRegistrationReport | None:
        """Start once and optionally wait for registration to finish."""

        with self._lifecycle_lock:
            return self._start_locked(ready_timeout_seconds)

    def _start_locked(
        self,
        ready_timeout_seconds: float | None,
    ) -> HotkeyRegistrationReport | None:
        with self._state_lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                ready = threading.Event()
                stopped = threading.Event()
                stop_requested = threading.Event()
                specs = self._hotkey_specs
                self._ready = ready
                self._stopped = stopped
                self._stop_requested = stop_requested
                self._registration_report = None
                thread = threading.Thread(
                    target=self._message_loop,
                    args=(specs, ready, stopped, stop_requested),
                    daemon=True,
                    name="GlobalHotkeys",
                )
                self._thread = thread
                thread.start()
            else:
                ready = self._ready

        if ready_timeout_seconds is None:
            ready.wait()
        elif ready_timeout_seconds > 0:
            ready.wait(ready_timeout_seconds)
        if not ready.is_set():
            return None
        return self.registration_report

    def stop(self, timeout_seconds: float = 1.0) -> bool:
        """Request shutdown and wait briefly for native unregistration."""

        with self._lifecycle_lock:
            return self._stop_locked(timeout_seconds)

    def _stop_locked(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._state_lock:
            thread = self._thread
            thread_id = self._thread_id
            stop_requested = self._stop_requested
            stopped = self._stopped
            stop_requested.set()

        if thread is None or not thread.is_alive():
            stopped.set()
            return True

        if thread_id is not None:
            try:
                result = self._get_api().post_quit(thread_id)
                if not result.succeeded:
                    self._safe_error(
                        f"PostThreadMessageW(WM_QUIT) failed: {result.error_code}"
                    )
            except Exception as exc:
                self._safe_error(f"Could not stop global hotkeys: {exc}")

        if threading.current_thread() is thread:
            return False
        if timeout_seconds <= 0:
            return stopped.is_set() and not thread.is_alive()

        remaining = max(0.0, deadline - time.monotonic())
        if not stopped.wait(remaining):
            return False
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(remaining)
        return not thread.is_alive()

    def restart(
        self,
        hotkey_specs: Iterable[HotkeySpec] | None = None,
        *,
        ready_timeout_seconds: float | None = 1.0,
        stop_timeout_seconds: float = 1.0,
    ) -> HotkeyRegistrationReport | None:
        """Safely unregister, optionally reconfigure, and start a fresh thread.

        New specs are validated before the currently running registrations are
        disturbed. A timeout never starts a second native message thread.
        """

        new_specs = (
            None
            if hotkey_specs is None
            else normalise_hotkey_specs(hotkey_specs)
        )
        with self._lifecycle_lock:
            if not self._stop_locked(stop_timeout_seconds):
                raise TimeoutError("全局快捷键线程未能在限定时间内停止")
            if new_specs is not None:
                with self._state_lock:
                    self._hotkey_specs = new_specs
            return self._start_locked(ready_timeout_seconds)

    def wait_until_stopped(self, timeout_seconds: float | None = None) -> bool:
        return self._stopped.wait(timeout_seconds)

    def _get_api(self) -> HotkeyApi:
        with self._state_lock:
            if self._api is None:
                self._api = Win32HotkeyApi()
            return self._api

    def _publish_report(
        self,
        report: HotkeyRegistrationReport,
        ready: threading.Event,
    ) -> None:
        with self._state_lock:
            self._registration_report = report
        ready.set()
        if self._status_callback is not None:
            try:
                self._status_callback(registration_status_text(report))
            except Exception as exc:
                self._safe_error(f"Hot-key status callback failed: {exc}")
        try:
            self._report_registration(report)
        except Exception as exc:
            self._safe_error(f"Hot-key registration callback failed: {exc}")

    def _safe_error(self, message: str) -> None:
        try:
            self._report_error(message)
        except Exception:
            pass

    @staticmethod
    def _report_from_results(
        results: Iterable[HotkeyRegistrationResult],
    ) -> HotkeyRegistrationReport:
        by_command = {result.spec.command: result for result in results}
        return HotkeyRegistrationReport(
            by_command[HotkeyCommand.RETRY_AND_SPEAK_US],
            by_command[HotkeyCommand.TOGGLE_WINDOW_MODE],
        )

    def _message_loop(
        self,
        specs: tuple[HotkeySpec, HotkeySpec],
        ready: threading.Event,
        stopped: threading.Event,
        stop_requested: threading.Event,
    ) -> None:
        registered: dict[int, HotkeySpec] = {}
        report_published = False
        try:
            api = self._get_api()
            api.ensure_message_queue()
            thread_id = api.current_thread_id()
            with self._state_lock:
                self._thread_id = thread_id

            if stop_requested.is_set():
                self._publish_report(
                    HotkeyRegistrationReport.startup_failed(
                        "Global hot-key startup was cancelled",
                        specs,
                    ),
                    ready,
                )
                report_published = True
                return

            results: list[HotkeyRegistrationResult] = []
            for spec in specs:
                if not spec.requires_native_registration:
                    # The desktop input listener owns gestures such as double
                    # Alt. Treat the validated binding as available without
                    # making a misleading RegisterHotKey call.
                    results.append(
                        HotkeyRegistrationResult(
                            spec,
                            registered=False,
                            delegated=True,
                        )
                    )
                    continue
                if stop_requested.is_set():
                    results.append(HotkeyRegistrationResult(spec, False))
                    continue
                try:
                    native_result = api.register_hotkey(spec)
                except Exception as exc:
                    self._safe_error(f"Could not register {spec.label}: {exc}")
                    native_result = NativeCallResult(False)
                result = HotkeyRegistrationResult(
                    spec,
                    native_result.succeeded,
                    native_result.error_code,
                )
                results.append(result)
                if result.registered:
                    registered[spec.hotkey_id] = spec

            report = self._report_from_results(results)
            self._publish_report(report, ready)
            report_published = True

            if stop_requested.is_set():
                return

            while not stop_requested.is_set():
                native_message = api.get_message()
                # PostThreadMessageW can fail or race a message already queued.
                # Once shutdown has been requested, a message that merely wakes
                # this thread must never dispatch one final user command.
                if stop_requested.is_set():
                    break
                if native_message.result == 0:
                    break
                if native_message.result == -1:
                    self._safe_error(
                        f"GetMessageW failed: {native_message.error_code}"
                    )
                    break
                if native_message.message != WM_HOTKEY:
                    continue
                spec = registered.get(native_message.hotkey_id)
                if spec is None:
                    continue
                if stop_requested.is_set():
                    break
                try:
                    self._post_command(spec.command)
                except Exception as exc:
                    self._safe_error(
                        f"Hot-key command callback failed for {spec.label}: {exc}"
                    )
        except Exception as exc:
            self._safe_error(f"Global hot-key thread failed: {exc}")
            if not report_published:
                self._publish_report(
                    HotkeyRegistrationReport.startup_failed(str(exc), specs),
                    ready,
                )
                report_published = True
        finally:
            try:
                api = self._api
                if api is not None:
                    for hotkey_id, spec in tuple(registered.items()):
                        try:
                            result = api.unregister_hotkey(hotkey_id)
                            if not result.succeeded:
                                self._safe_error(
                                    f"Could not unregister {spec.label}: {result.error_code}"
                                )
                        except Exception as exc:
                            self._safe_error(
                                f"Could not unregister {spec.label}: {exc}"
                            )
            finally:
                if not report_published:
                    self._publish_report(
                        HotkeyRegistrationReport.startup_failed(
                            "Global hot-key thread stopped before registration",
                            specs,
                        ),
                        ready,
                    )
                with self._state_lock:
                    self._thread_id = None
                    if self._thread is threading.current_thread():
                        self._thread = None
                stopped.set()


# Kept as a descriptive alias for callers/tests that adopted the initial name.
GlobalHotkeyService = WindowsHotkeyService
