from __future__ import annotations

import queue
import threading
import time
import unittest

from hotkey_service import (
    ERROR_HOTKEY_ALREADY_REGISTERED,
    HOTKEY_RETRY_ID,
    HOTKEY_SPECS,
    HOTKEY_TOGGLE_MODE_ID,
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    MOD_WIN,
    VK_C,
    VK_F1,
    WM_HOTKEY,
    GlobalHotkeyService,
    HotkeyBindingKind,
    HotkeyCommand,
    HotkeySpec,
    NativeCallResult,
    NativeMessage,
    format_hotkey_spec,
    normalise_hotkey_specs,
    parse_hotkey_spec,
    registration_status_text,
)


class FakeHotkeyApi:
    def __init__(
        self,
        register_results: dict[int, NativeCallResult] | None = None,
    ) -> None:
        self.register_results = register_results or {}
        self.messages: queue.Queue[NativeMessage] = queue.Queue()
        self.queue_ready = threading.Event()
        self.allow_queue_creation = threading.Event()
        self.allow_queue_creation.set()
        self.register_calls: list[tuple[int, int, int, int]] = []
        self.unregister_calls: list[tuple[int, int]] = []
        self.post_quit_calls: list[int] = []
        self.worker_thread_id: int | None = None
        self.native_thread_id = 7331
        self.post_quit_unblocks = True

    def ensure_message_queue(self) -> None:
        self.allow_queue_creation.wait(1.0)
        self.worker_thread_id = threading.get_ident()
        self.queue_ready.set()

    def current_thread_id(self) -> int:
        return self.native_thread_id

    def register_hotkey(self, spec: HotkeySpec) -> NativeCallResult:
        self.register_calls.append(
            (
                spec.hotkey_id,
                spec.modifiers,
                spec.virtual_key,
                threading.get_ident(),
            )
        )
        return self.register_results.get(spec.hotkey_id, NativeCallResult(True))

    def unregister_hotkey(self, hotkey_id: int) -> NativeCallResult:
        self.unregister_calls.append((hotkey_id, threading.get_ident()))
        return NativeCallResult(True)

    def get_message(self) -> NativeMessage:
        return self.messages.get(timeout=1.0)

    def post_quit(self, thread_id: int) -> NativeCallResult:
        self.post_quit_calls.append(thread_id)
        if self.post_quit_unblocks:
            self.messages.put(NativeMessage(0))
        return NativeCallResult(True)


def bindings(
    retry: str = "双击 Alt",
    toggle: str = "Alt+C",
) -> tuple[HotkeySpec, HotkeySpec]:
    return (
        parse_hotkey_spec(HotkeyCommand.RETRY_AND_SPEAK_US, retry),
        parse_hotkey_spec(HotkeyCommand.TOGGLE_WINDOW_MODE, toggle),
    )


class HotkeyParserTests(unittest.TestCase):
    def test_defaults_are_double_alt_and_alt_c(self) -> None:
        retry, toggle = HOTKEY_SPECS

        self.assertEqual(retry.hotkey_id, HOTKEY_RETRY_ID)
        self.assertEqual(retry.command, HotkeyCommand.RETRY_AND_SPEAK_US)
        self.assertEqual(retry.binding_kind, HotkeyBindingKind.DOUBLE_ALT)
        self.assertFalse(retry.requires_native_registration)
        self.assertEqual(retry.label, "双击 Alt")
        self.assertEqual(toggle.hotkey_id, HOTKEY_TOGGLE_MODE_ID)
        self.assertEqual(toggle.modifiers, MOD_ALT | MOD_NOREPEAT)
        self.assertEqual(toggle.virtual_key, VK_C)
        self.assertTrue(toggle.requires_native_registration)

    def test_native_parser_normalises_modifier_order_case_and_f_key(self) -> None:
        spec = parse_hotkey_spec(
            HotkeyCommand.RETRY_AND_SPEAK_US,
            " shift + CTRL + f12 ",
        )

        self.assertEqual(spec.modifiers, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT)
        self.assertEqual(spec.virtual_key, VK_F1 + 11)
        self.assertEqual(spec.label, "Ctrl+Shift+F12")
        self.assertEqual(format_hotkey_spec(spec), spec.label)

    def test_parser_supports_multi_modifier_win_and_all_modifiers(self) -> None:
        win_digit = parse_hotkey_spec(
            HotkeyCommand.TOGGLE_WINDOW_MODE,
            "control+windows+0",
        )
        all_modifiers = parse_hotkey_spec(
            HotkeyCommand.RETRY_AND_SPEAK_US,
            "win+shift+alt+control+Z",
        )

        self.assertEqual(win_digit.label, "Ctrl+Win+0")
        self.assertEqual(
            win_digit.modifiers,
            MOD_CONTROL | MOD_WIN | MOD_NOREPEAT,
        )
        self.assertEqual(all_modifiers.label, "Ctrl+Alt+Shift+Win+Z")

    def test_dangerous_single_modifier_shortcuts_are_rejected(self) -> None:
        cases = (
            ("Shift+A", "正常的大写输入"),
            ("Shift+F12", "正常的大写输入"),
            ("Win+D", "Windows 系统快捷键"),
            ("Win+0", "Windows 系统快捷键"),
            ("Alt+F4", "标准关闭快捷键"),
        )
        for value, message in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, message):
                    parse_hotkey_spec(HotkeyCommand.RETRY_AND_SPEAK_US, value)

        for value in ("Ctrl+Shift+A", "Ctrl+Win+D", "Alt+Shift+F4"):
            with self.subTest(allowed=value):
                self.assertEqual(
                    format_hotkey_spec(
                        parse_hotkey_spec(
                            HotkeyCommand.RETRY_AND_SPEAK_US,
                            value,
                        )
                    ),
                    value,
                )

    def test_single_ctrl_editing_shortcuts_are_rejected_globally(self) -> None:
        for key in "ACFNOPSVWXYZ":
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "常用编辑快捷键"):
                    parse_hotkey_spec(
                        HotkeyCommand.RETRY_AND_SPEAK_US,
                        f"Ctrl+{key}",
                    )

    def test_chorded_ctrl_and_alt_c_remain_available(self) -> None:
        retry = parse_hotkey_spec(
            HotkeyCommand.RETRY_AND_SPEAK_US,
            "Ctrl+Shift+C",
        )
        toggle = parse_hotkey_spec(
            HotkeyCommand.TOGGLE_WINDOW_MODE,
            "Alt+C",
        )

        self.assertEqual(retry.label, "Ctrl+Shift+C")
        self.assertEqual(toggle.label, "Alt+C")

    def test_double_alt_accepts_chinese_and_english_spelling(self) -> None:
        english = parse_hotkey_spec(
            HotkeyCommand.RETRY_AND_SPEAK_US,
            "  DOUBLE   ALT  ",
        )
        chinese = parse_hotkey_spec(
            HotkeyCommand.RETRY_AND_SPEAK_US,
            "双击 Alt",
        )

        self.assertEqual(english.binding_kind, HotkeyBindingKind.DOUBLE_ALT)
        self.assertEqual(chinese.binding_kind, HotkeyBindingKind.DOUBLE_ALT)
        self.assertEqual(english.label, "双击 Alt")
        self.assertEqual(chinese.label, "双击 Alt")

    def test_invalid_text_is_rejected_with_no_silent_fallback(self) -> None:
        invalid_values = (
            "",
            "X",
            "Ctrl",
            "Ctrl+PageUp",
            "Ctrl+A+B",
            "Ctrl+Ctrl+A",
            "Ctrl++A",
            "A+Ctrl",
            "Alt+F13",
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_hotkey_spec(HotkeyCommand.RETRY_AND_SPEAK_US, value)

    def test_manual_native_spec_is_canonicalised_and_no_repeat_is_forced(self) -> None:
        retry = HotkeySpec(
            HOTKEY_RETRY_ID,
            HotkeyCommand.RETRY_AND_SPEAK_US,
            MOD_SHIFT | MOD_CONTROL,
            ord("Q"),
            "not trusted",
        )
        retry_result, _toggle = normalise_hotkey_specs(
            (retry, bindings()[1])
        )

        self.assertEqual(retry_result.label, "Ctrl+Shift+Q")
        self.assertTrue(retry_result.modifiers & MOD_NOREPEAT)

    def test_duplicate_binding_is_rejected_before_native_registration(self) -> None:
        with self.assertRaisesRegex(ValueError, "相同"):
            normalise_hotkey_specs(bindings("Ctrl+Q", "Ctrl+Q"))

    def test_manual_spec_cannot_bypass_reserved_single_ctrl_check(self) -> None:
        retry = HotkeySpec(
            HOTKEY_RETRY_ID,
            HotkeyCommand.RETRY_AND_SPEAK_US,
            MOD_CONTROL | MOD_NOREPEAT,
            VK_C,
            "untrusted Ctrl+C",
        )

        with self.assertRaisesRegex(ValueError, "常用编辑快捷键"):
            normalise_hotkey_specs((retry, bindings()[1]))

    def test_manual_spec_cannot_bypass_single_modifier_safety_checks(self) -> None:
        unsafe_specs = (
            HotkeySpec(
                HOTKEY_RETRY_ID,
                HotkeyCommand.RETRY_AND_SPEAK_US,
                MOD_SHIFT,
                ord("A"),
            ),
            HotkeySpec(
                HOTKEY_RETRY_ID,
                HotkeyCommand.RETRY_AND_SPEAK_US,
                MOD_WIN,
                ord("D"),
            ),
            HotkeySpec(
                HOTKEY_RETRY_ID,
                HotkeyCommand.RETRY_AND_SPEAK_US,
                MOD_ALT,
                VK_F1 + 3,
            ),
        )
        for spec in unsafe_specs:
            with self.subTest(label=spec.label, modifiers=spec.modifiers):
                with self.assertRaises(ValueError):
                    normalise_hotkey_specs((spec, bindings()[1]))

    def test_double_alt_is_rejected_for_window_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "仅用于重新获取"):
            parse_hotkey_spec(HotkeyCommand.TOGGLE_WINDOW_MODE, "Double Alt")

        retry = bindings()[0]
        invalid_toggle = HotkeySpec(
            HOTKEY_TOGGLE_MODE_ID,
            HotkeyCommand.TOGGLE_WINDOW_MODE,
            MOD_ALT,
            0,
            "双击 Alt",
            HotkeyBindingKind.DOUBLE_ALT,
        )
        with self.assertRaisesRegex(ValueError, "仅用于重新获取"):
            normalise_hotkey_specs((retry, invalid_toggle))

    def test_invalid_shape_ids_and_commands_are_rejected(self) -> None:
        retry, toggle = bindings("Ctrl+Q", "Alt+C")
        duplicate_command = HotkeySpec(
            toggle.hotkey_id,
            retry.command,
            toggle.modifiers,
            toggle.virtual_key,
        )
        bad_id = HotkeySpec(
            0xC000,
            retry.command,
            retry.modifiers,
            retry.virtual_key,
        )

        with self.assertRaises(ValueError):
            normalise_hotkey_specs((retry,))
        with self.assertRaises(ValueError):
            normalise_hotkey_specs((retry, duplicate_command))
        with self.assertRaises(ValueError):
            normalise_hotkey_specs((bad_id, toggle))


class GlobalHotkeyServiceTests(unittest.TestCase):
    def make_service(
        self,
        api: FakeHotkeyApi,
        commands: list[HotkeyCommand] | None = None,
        reports: list[object] | None = None,
        errors: list[str] | None = None,
        hotkey_specs: tuple[HotkeySpec, HotkeySpec] | None = None,
    ) -> GlobalHotkeyService:
        command_values = commands if commands is not None else []
        report_values = reports if reports is not None else []
        error_values = errors if errors is not None else []
        return GlobalHotkeyService(
            command_values.append,
            report_registration=report_values.append,
            report_error=error_values.append,
            api=api,
            hotkey_specs=hotkey_specs,
        )

    def test_default_only_registers_native_alt_c_and_reports_both_available(self) -> None:
        api = FakeHotkeyApi()
        service = self.make_service(api)

        report = service.start()

        self.assertIsNotNone(report)
        assert report is not None
        self.assertTrue(report.all_registered)
        self.assertFalse(report.retry.registered)
        self.assertTrue(report.retry.delegated)
        self.assertTrue(report.retry.available)
        self.assertEqual(report.retry.spec.label, "双击 Alt")
        self.assertEqual(service.native_hotkey_specs, (HOTKEY_SPECS[1],))
        self.assertEqual(
            [(call[0], call[1], call[2]) for call in api.register_calls],
            [(HOTKEY_TOGGLE_MODE_ID, MOD_ALT | MOD_NOREPEAT, VK_C)],
        )
        self.assertTrue(service.stop())
        self.assertEqual(
            [call[0] for call in api.unregister_calls],
            [HOTKEY_TOGGLE_MODE_ID],
        )

    def test_dynamic_native_specs_are_registered_and_dispatched(self) -> None:
        api = FakeHotkeyApi()
        configured = bindings("Ctrl+Shift+F12", "Ctrl+Win+0")
        commands: list[HotkeyCommand] = []
        command_seen = threading.Event()

        def post_command(command: HotkeyCommand) -> None:
            commands.append(command)
            if len(commands) == 2:
                command_seen.set()

        service = GlobalHotkeyService(
            post_command,
            api=api,
            hotkey_specs=configured,
        )
        report = service.start()
        api.messages.put(NativeMessage(1, WM_HOTKEY, HOTKEY_RETRY_ID))
        api.messages.put(NativeMessage(1, WM_HOTKEY, HOTKEY_TOGGLE_MODE_ID))

        self.assertIsNotNone(report)
        self.assertTrue(command_seen.wait(1.0))
        self.assertEqual(
            commands,
            [
                HotkeyCommand.RETRY_AND_SPEAK_US,
                HotkeyCommand.TOGGLE_WINDOW_MODE,
            ],
        )
        self.assertEqual(
            [(call[1], call[2]) for call in api.register_calls],
            [
                (MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_F1 + 11),
                (MOD_CONTROL | MOD_WIN | MOD_NOREPEAT, ord("0")),
            ],
        )
        self.assertTrue(service.stop())

    def test_double_alt_is_never_dispatched_as_a_native_message(self) -> None:
        api = FakeHotkeyApi()
        commands: list[HotkeyCommand] = []
        service = self.make_service(api, commands=commands)
        service.start()
        api.messages.put(NativeMessage(1, WM_HOTKEY, HOTKEY_RETRY_ID))
        time.sleep(0.03)

        self.assertEqual(commands, [])
        self.assertTrue(service.stop())

    def test_partial_occupied_registration_uses_configured_label(self) -> None:
        api = FakeHotkeyApi(
            {
                HOTKEY_RETRY_ID: NativeCallResult(
                    False,
                    error_code=ERROR_HOTKEY_ALREADY_REGISTERED,
                )
            }
        )
        statuses: list[str] = []
        configured = bindings("Ctrl+F8", "Alt+Shift+9")
        service = GlobalHotkeyService(
            lambda _command: None,
            statuses.append,
            api=api,
            hotkey_specs=configured,
        )

        report = service.start()

        self.assertIsNotNone(report)
        assert report is not None
        self.assertFalse(report.retry.registered)
        self.assertEqual(report.retry.error_code, ERROR_HOTKEY_ALREADY_REGISTERED)
        self.assertTrue(report.toggle_mode.registered)
        self.assertEqual(statuses, ["Ctrl+F8 被其他程序占用；Alt+Shift+9 可用"])
        self.assertEqual(registration_status_text(report), statuses[0])
        self.assertTrue(service.stop())
        self.assertEqual(
            [call[0] for call in api.unregister_calls],
            [HOTKEY_TOGGLE_MODE_ID],
        )

    def test_native_conflict_reports_double_alt_as_delegated_not_registered(self) -> None:
        api = FakeHotkeyApi(
            {
                HOTKEY_TOGGLE_MODE_ID: NativeCallResult(
                    False,
                    error_code=ERROR_HOTKEY_ALREADY_REGISTERED,
                )
            }
        )
        statuses: list[str] = []
        service = GlobalHotkeyService(
            lambda _command: None,
            statuses.append,
            api=api,
        )

        report = service.start()

        self.assertIsNotNone(report)
        assert report is not None
        self.assertFalse(report.retry.registered)
        self.assertTrue(report.retry.delegated)
        self.assertEqual(
            statuses,
            ["Alt+C 被其他程序占用；双击 Alt 由应用监听"],
        )
        self.assertTrue(service.stop())

    def test_all_registered_status_uses_dynamic_labels(self) -> None:
        api = FakeHotkeyApi()
        statuses: list[str] = []
        service = GlobalHotkeyService(
            lambda _command: None,
            statuses.append,
            api=api,
            hotkey_specs=bindings("Alt+Q", "Ctrl+F2"),
        )

        service.start()

        self.assertEqual(statuses, ["全局快捷键已启用：Alt+Q、Ctrl+F2"])
        self.assertTrue(service.stop())

    def test_registers_and_unregisters_on_the_same_worker_thread(self) -> None:
        api = FakeHotkeyApi()
        service = self.make_service(api, hotkey_specs=bindings("Ctrl+Q", "Alt+C"))

        service.start()

        self.assertTrue(service.stop())
        worker_threads = {call[3] for call in api.register_calls}
        unregister_threads = {call[1] for call in api.unregister_calls}
        self.assertEqual(len(worker_threads), 1)
        self.assertEqual(worker_threads, unregister_threads)
        self.assertEqual(api.post_quit_calls, [api.native_thread_id])

    def test_unknown_and_non_hotkey_messages_are_ignored(self) -> None:
        api = FakeHotkeyApi()
        commands: list[HotkeyCommand] = []
        service = self.make_service(api, commands=commands)
        service.start()
        api.messages.put(NativeMessage(1, 0x0401, HOTKEY_TOGGLE_MODE_ID))
        api.messages.put(NativeMessage(1, WM_HOTKEY, 0x7777))
        time.sleep(0.03)

        self.assertEqual(commands, [])
        self.assertTrue(service.stop())

    def test_stop_requested_before_queue_ready_exits_without_registration(self) -> None:
        api = FakeHotkeyApi()
        api.allow_queue_creation.clear()
        reports: list[object] = []
        service = self.make_service(api, reports=reports)

        self.assertIsNone(service.start(ready_timeout_seconds=0.01))
        self.assertFalse(service.stop(timeout_seconds=0.01))
        api.allow_queue_creation.set()

        self.assertTrue(service.wait_until_stopped(1.0))
        self.assertEqual(api.register_calls, [])
        self.assertEqual(api.unregister_calls, [])
        self.assertEqual(api.post_quit_calls, [])
        self.assertEqual(len(reports), 1)
        self.assertIn("cancelled", reports[0].startup_error.lower())
        self.assertEqual(reports[0].retry.spec.label, "双击 Alt")

    def test_get_message_error_is_reported_and_native_keys_are_released(self) -> None:
        api = FakeHotkeyApi()
        errors: list[str] = []
        service = self.make_service(api, errors=errors)
        service.start()
        api.messages.put(NativeMessage(-1, error_code=5))

        self.assertTrue(service.wait_until_stopped(1.0))
        self.assertTrue(any("GetMessageW failed: 5" in value for value in errors))
        self.assertEqual(
            [call[0] for call in api.unregister_calls],
            [HOTKEY_TOGGLE_MODE_ID],
        )

    def test_command_callback_failure_does_not_kill_message_loop(self) -> None:
        api = FakeHotkeyApi()
        commands: list[HotkeyCommand] = []
        errors: list[str] = []
        second_seen = threading.Event()

        def flaky_post(command: HotkeyCommand) -> None:
            commands.append(command)
            if len(commands) == 1:
                raise RuntimeError("queue unavailable")
            second_seen.set()

        service = GlobalHotkeyService(
            flaky_post,
            report_error=errors.append,
            api=api,
            hotkey_specs=bindings("Ctrl+Q", "Alt+C"),
        )
        service.start()
        api.messages.put(NativeMessage(1, WM_HOTKEY, HOTKEY_RETRY_ID))
        api.messages.put(NativeMessage(1, WM_HOTKEY, HOTKEY_TOGGLE_MODE_ID))

        self.assertTrue(second_seen.wait(1.0))
        self.assertEqual(len(commands), 2)
        self.assertTrue(any("queue unavailable" in value for value in errors))
        self.assertTrue(service.stop())

    def test_start_is_idempotent_and_concurrent_callers_start_one_thread(self) -> None:
        api = FakeHotkeyApi()
        service = self.make_service(api)
        results: list[object] = []
        callers = [threading.Thread(target=lambda: results.append(service.start())) for _ in range(6)]

        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(1.0)

        self.assertEqual(len(results), 6)
        self.assertTrue(all(result is results[0] for result in results))
        self.assertEqual(len(api.register_calls), 1)
        self.assertTrue(service.stop())

    def test_restart_unregisters_old_then_registers_new_specs(self) -> None:
        api = FakeHotkeyApi()
        service = self.make_service(api)
        first_report = service.start()

        second_report = service.restart(bindings("Ctrl+Q", "Ctrl+Win+F3"))

        self.assertIsNot(first_report, second_report)
        self.assertEqual(
            [call[0] for call in api.unregister_calls],
            [HOTKEY_TOGGLE_MODE_ID],
        )
        self.assertEqual(
            [(call[0], call[2]) for call in api.register_calls],
            [
                (HOTKEY_TOGGLE_MODE_ID, VK_C),
                (HOTKEY_RETRY_ID, ord("Q")),
                (HOTKEY_TOGGLE_MODE_ID, VK_F1 + 2),
            ],
        )
        self.assertEqual(
            [spec.label for spec in service.hotkey_specs],
            ["Ctrl+Q", "Ctrl+Win+F3"],
        )
        self.assertTrue(service.stop())

    def test_invalid_restart_keeps_current_registrations_running(self) -> None:
        api = FakeHotkeyApi()
        service = self.make_service(api)
        service.start()

        with self.assertRaises(ValueError):
            service.restart(bindings("Ctrl+Q", "Ctrl+Q"))

        self.assertTrue(service.is_running)
        self.assertEqual(len(api.register_calls), 1)
        self.assertEqual(api.post_quit_calls, [])
        self.assertTrue(service.stop())

    def test_restart_timeout_never_starts_a_second_thread(self) -> None:
        api = FakeHotkeyApi()
        service = self.make_service(api)
        service.start()
        api.post_quit_unblocks = False

        with self.assertRaises(TimeoutError):
            service.restart(
                bindings("Ctrl+Q", "Ctrl+Win+F3"),
                stop_timeout_seconds=0.01,
            )

        self.assertEqual(len(api.register_calls), 1)
        # Release the deliberately blocked fake thread without starting a new one.
        api.messages.put(NativeMessage(0))
        self.assertTrue(service.wait_until_stopped(1.0))

    def test_failed_quit_wakeup_never_dispatches_one_last_hotkey(self) -> None:
        api = FakeHotkeyApi()
        commands: list[HotkeyCommand] = []
        service = self.make_service(api, commands=commands)
        service.start()
        api.post_quit_unblocks = False

        self.assertFalse(service.stop(timeout_seconds=0.01))
        api.messages.put(NativeMessage(1, WM_HOTKEY, HOTKEY_TOGGLE_MODE_ID))

        self.assertTrue(service.wait_until_stopped(1.0))
        self.assertEqual(commands, [])

    def test_repeated_stop_is_safe(self) -> None:
        api = FakeHotkeyApi()
        service = self.make_service(api)
        service.start()

        self.assertTrue(service.stop())
        self.assertTrue(service.stop())
        self.assertEqual(api.post_quit_calls, [api.native_thread_id])


if __name__ == "__main__":
    unittest.main()
