import ast
import unittest
from pathlib import Path


SOURCE_PATH = Path(__file__).resolve().parents[1] / "desktop_app.py"
SOURCE_TREE = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"), filename=str(SOURCE_PATH))


def class_method_node(class_name: str, name: str) -> ast.FunctionDef:
    for node in SOURCE_TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == name:
                    return member
    raise AssertionError(f"{class_name}.{name} was not found")


def method_node(name: str) -> ast.FunctionDef:
    return class_method_node("DesktopTranslatorApp", name)


def direct_method_call(method: ast.FunctionDef, receiver: str, method_name: str) -> ast.Call:
    matches: list[ast.Call] = []
    for node in ast.walk(method):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != method_name:
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id == receiver:
            matches.append(node)
    if len(matches) != 1:
        raise AssertionError(
            f"Expected one {receiver}.{method_name}(...) call, found {len(matches)}"
        )
    return matches[0]


def keyword_value(call: ast.Call, name: str) -> object:
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    return None


def label_for_textvariable(method: ast.FunctionDef, variable_name: str) -> ast.Call:
    matches: list[ast.Call] = []
    for node in ast.walk(method):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "Label":
            continue
        for keyword in node.keywords:
            value = keyword.value
            if (
                keyword.arg == "textvariable"
                and isinstance(value, ast.Attribute)
                and value.attr == variable_name
                and isinstance(value.value, ast.Name)
                and value.value.id == "self"
            ):
                matches.append(node)
    if len(matches) != 1:
        raise AssertionError(
            f"Expected one Label for self.{variable_name}, found {len(matches)}"
        )
    return matches[0]


class UiLayoutContractTests(unittest.TestCase):
    """Layout invariants checked without importing Tk or creating a window."""

    def test_settings_first_show_fits_work_area_before_mapping(self) -> None:
        method = class_method_node("SettingsWindow", "show")
        fit_calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "fit_tk_window_to_work_area"
        ]
        deiconify_calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "deiconify"
            and node.lineno > fit_calls[0].lineno
        ] if fit_calls else []

        self.assertEqual(len(fit_calls), 1)
        self.assertTrue(deiconify_calls)

    def test_mini_reserves_sound_actions_before_expandable_text(self) -> None:
        method = method_node("_build_mini")
        sound_pack = direct_method_call(method, "sound_actions", "pack")
        text_pack = direct_method_call(method, "text_block", "pack")

        self.assertLess(
            sound_pack.lineno,
            text_pack.lineno,
            "Pack the fixed US/UK/close controls before the expandable text block",
        )

    def test_mini_source_label_has_minimal_requested_width(self) -> None:
        label = label_for_textvariable(method_node("_build_mini"), "mini_source")

        self.assertEqual(
            keyword_value(label, "width"),
            1,
            "The source label must be allowed to shrink instead of pushing out sound controls",
        )

    def test_mini_phonetic_label_has_minimal_requested_width(self) -> None:
        label = label_for_textvariable(method_node("_build_mini"), "mini_phonetic")

        self.assertEqual(
            keyword_value(label, "width"),
            1,
            "The phonetic label must be allowed to shrink instead of pushing out sound controls",
        )

    def test_panel_sound_text_column_can_shrink(self) -> None:
        call = direct_method_call(method_node("_build_panel"), "sound_card", "grid_columnconfigure")

        self.assertTrue(call.args and isinstance(call.args[0], ast.Constant))
        self.assertEqual(call.args[0].value, 0)
        self.assertEqual(keyword_value(call, "weight"), 1)
        self.assertEqual(
            keyword_value(call, "minsize"),
            0,
            "The panel sound-text column needs an explicit zero minimum size",
        )

    def test_panel_phonetic_label_has_minimal_requested_width(self) -> None:
        label = label_for_textvariable(method_node("_build_panel"), "phonetic_text")

        self.assertEqual(
            keyword_value(label, "width"),
            1,
            "A long phonetic value must not displace the panel sound controls",
        )

    def test_alt_c_panel_path_never_calls_focus_force(self) -> None:
        for method_name in ("show_panel_no_activate", "toggle_mode_from_hotkey"):
            method = method_node(method_name)
            called_names = {
                node.func.attr
                for node in ast.walk(method)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
            }
            self.assertNotIn("focus_force", called_names)
            if method_name == "show_panel_no_activate":
                style_calls = [
                    node
                    for node in ast.walk(method)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_SET_WINDOW_LONG_PTR"
                ]
                deiconify_calls = [
                    node
                    for node in ast.walk(method)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "deiconify"
                ]
                self.assertTrue(style_calls and deiconify_calls)
                self.assertLess(style_calls[0].lineno, deiconify_calls[0].lineno)
        toggle_calls = {
            node.func.attr
            for node in ast.walk(method_node("toggle_mode_from_hotkey"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("show_panel_no_activate", toggle_calls)
        self.assertNotIn("show_panel", toggle_calls)

    def test_automatic_panel_selection_never_uses_the_focus_stealing_path(self) -> None:
        called_names = {
            node.func.attr
            for node in ast.walk(method_node("_handle_selection"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("show_panel_no_activate", called_names)
        self.assertNotIn("show_panel", called_names)
        self.assertNotIn("focus_force", called_names)
        self.assertNotIn("lift", called_names)

    def test_alt_c_mini_path_sets_noactivate_before_mapping_and_never_lifts(self) -> None:
        method = method_node("show_mini_no_activate")
        called_names = {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        self.assertNotIn("lift", called_names)

        style_calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_SET_WINDOW_LONG_PTR"
        ]
        deiconify_calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "deiconify"
        ]
        self.assertTrue(style_calls and deiconify_calls)
        self.assertLess(style_calls[0].lineno, deiconify_calls[0].lineno)

        toggle_calls = {
            node.func.attr
            for node in ast.walk(method_node("toggle_mode_from_hotkey"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("show_mini_no_activate", toggle_calls)

    def test_mini_stays_nonactivating_for_its_entire_visible_lifetime(self) -> None:
        method = method_node("show_mini_no_activate")
        called_names = {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }

        self.assertNotIn(
            "after_idle",
            called_names,
            "The mini popup must not restore an activating style while visible",
        )

    def test_dynamic_app_names_cannot_displace_mode_or_bottom_actions(self) -> None:
        panel = method_node("_build_panel")
        mode_pack = direct_method_call(panel, "mode_box", "pack")
        panel_label = label_for_textvariable(panel, "app_text")
        self.assertLess(mode_pack.lineno, panel_label.lineno)
        self.assertEqual(keyword_value(panel_label, "width"), 1)

        mini = method_node("_build_mini")
        actions_pack = direct_method_call(mini, "bottom_actions", "pack")
        mini_label = label_for_textvariable(mini, "mini_app_text")
        self.assertLess(actions_pack.lineno, mini_label.lineno)
        self.assertEqual(keyword_value(mini_label, "width"), 1)

    def test_mini_reserves_bottom_actions_before_expandable_translation(self) -> None:
        method = method_node("_build_mini")
        bottom_pack = direct_method_call(method, "bottom", "pack")
        translation_label = label_for_textvariable(method, "mini_translation")

        self.assertEqual(keyword_value(bottom_pack, "side"), "bottom")
        self.assertLess(bottom_pack.lineno, translation_label.lineno)

    def test_mini_replaces_settings_with_copy_translation(self) -> None:
        mini = method_node("_build_mini")
        actions = [
            node
            for node in ast.walk(mini)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "flat_button"
            and len(node.args) >= 3
            and isinstance(node.args[1], ast.Constant)
        ]
        labels = {
            str(node.args[1].value): (
                node.args[2].attr
                if isinstance(node.args[2], ast.Attribute)
                else ""
            )
            for node in actions
        }
        self.assertEqual(labels.get("复制译文"), "copy_translation")
        self.assertNotIn("设置", labels)

    def test_shortcut_editor_reserves_apply_button_before_status_text(self) -> None:
        method = class_method_node("SettingsWindow", "_build")
        apply_pack_calls: list[ast.Call] = []
        status_pack_calls: list[ast.Call] = []
        for node in ast.walk(method):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "pack":
                continue
            owner = node.func.value
            if (
                isinstance(owner, ast.Attribute)
                and isinstance(owner.value, ast.Name)
                and owner.value.id == "self"
                and owner.attr == "shortcut_status_label"
            ):
                status_pack_calls.append(node)
            if (
                isinstance(owner, ast.Call)
                and isinstance(owner.func, ast.Name)
                and owner.func.id == "flat_button"
                and len(owner.args) >= 2
                and isinstance(owner.args[1], ast.Constant)
                and owner.args[1].value == "应用快捷键"
            ):
                apply_pack_calls.append(node)

        self.assertEqual(len(apply_pack_calls), 1)
        self.assertEqual(keyword_value(apply_pack_calls[0], "side"), "right")
        self.assertEqual(len(status_pack_calls), 1)
        self.assertEqual(keyword_value(status_pack_calls[0], "side"), "left")
        self.assertEqual(keyword_value(status_pack_calls[0], "fill"), "x")
        self.assertIs(keyword_value(status_pack_calls[0], "expand"), True)

    def test_shortcut_entry_is_white_and_has_adjacent_double_alt_action(self) -> None:
        method = class_method_node("SettingsWindow", "_shortcut_entry_row")
        entries = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Entry"
        ]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(keyword_value(entry, "width"), 18)
        self.assertEqual(keyword_value(entry, "relief"), "solid")
        backgrounds = [
            keyword.value
            for keyword in entry.keywords
            if keyword.arg == "bg"
        ]
        self.assertEqual(len(backgrounds), 1)
        self.assertIsInstance(backgrounds[0], ast.Name)
        self.assertEqual(backgrounds[0].id, "WHITE")
        self.assertTrue(
            any(
                isinstance(node, ast.Constant) and node.value == "设为双击 Alt"
                for node in ast.walk(method)
            )
        )

    def test_settings_builds_two_separate_shortcut_editors(self) -> None:
        method = class_method_node("SettingsWindow", "_build")
        calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr == "_shortcut_entry_row"
        ]
        self.assertEqual(len(calls), 2)
        setting_keys = {
            node.args[4].value
            for node in calls
            if len(node.args) >= 5 and isinstance(node.args[4], ast.Constant)
        }
        self.assertEqual(setting_keys, {"retry_hotkey", "toggle_mode_hotkey"})

    def test_tray_hotkey_text_is_derived_from_current_settings(self) -> None:
        method = method_node("_tray_menu")
        display_calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "hotkey_text_for_display"
        ]
        self.assertGreaterEqual(len(display_calls), 2)


if __name__ == "__main__":
    unittest.main()
