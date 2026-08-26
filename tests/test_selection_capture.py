from __future__ import annotations

import unittest
from types import SimpleNamespace

import selection_capture

from selection_capture import (
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


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeSnapshot:
    def __init__(self, restore_result: bool = True) -> None:
        self.restore_result = restore_result
        self.restore_calls = 0
        self.close_calls = 0

    def restore(self, _expected_sequence: int) -> bool:
        self.restore_calls += 1
        return self.restore_result

    def close(self) -> None:
        self.close_calls += 1


class PowerPointCaptureTests(unittest.TestCase):
    def test_reads_only_a_real_text_selection(self) -> None:
        selection = SimpleNamespace(
            Type=3,
            TextRange=SimpleNamespace(Start=1, Length=8, Text="Duration"),
        )
        powerpoint = SimpleNamespace(
            ActiveWindow=SimpleNamespace(Selection=selection, Caption="Deck", Hwnd=101)
        )

        value = read_powerpoint_selected_text(
            lambda _name: powerpoint,
            3000,
            "Deck - PowerPoint",
        )

        self.assertEqual(value, "Duration")

    def test_does_not_read_an_entire_selected_shape(self) -> None:
        selection = SimpleNamespace(Type=2, TextRange=SimpleNamespace(Text="whole shape"))
        powerpoint = SimpleNamespace(
            ActiveWindow=SimpleNamespace(Selection=selection, Caption="Deck")
        )

        value = read_powerpoint_selected_text(
            lambda _name: powerpoint, 3000, "Deck - PowerPoint"
        )

        self.assertEqual(value, "")

    def test_requires_a_foreground_identity_before_reading_selection(self) -> None:
        class UnreadableSelection:
            @property
            def Type(self) -> int:
                raise AssertionError("selection must not be read")

        powerpoint = SimpleNamespace(
            ActiveWindow=SimpleNamespace(Selection=UnreadableSelection(), Caption="Deck")
        )

        value = read_powerpoint_selected_text(lambda _name: powerpoint, 3000)

        self.assertEqual(value, "")

    def test_accepts_uppercase_powerpoint_hwnd_and_prioritizes_the_handle(self) -> None:
        selection = SimpleNamespace(
            Type=3,
            TextRange=SimpleNamespace(Start=2, Length=8, Text="Duration"),
        )
        window = SimpleNamespace(
            Selection=selection,
            Caption="Actual deck",
            HWND=-268435455,
        )
        powerpoint = SimpleNamespace(ActiveWindow=window)

        value = read_powerpoint_selected_text(
            lambda _name: powerpoint,
            3000,
            expected_window_title="Different deck - PowerPoint",
            expected_window_handle=0xF0000001,
        )

        self.assertEqual(value, "Duration")

    def test_discards_text_if_powerpoint_selection_changes_during_read(self) -> None:
        old_selection = SimpleNamespace(
            Type=3,
            TextRange=SimpleNamespace(Start=1, Length=3, Text="old"),
        )
        new_selection = SimpleNamespace(
            Type=3,
            TextRange=SimpleNamespace(Start=5, Length=3, Text="new"),
        )

        class ChangingWindow:
            Caption = "Deck"
            Hwnd = 101

            def __init__(self) -> None:
                self.reads = 0

            @property
            def Selection(self) -> object:
                self.reads += 1
                return old_selection if self.reads == 1 else new_selection

        powerpoint = SimpleNamespace(ActiveWindow=ChangingWindow())

        value = read_powerpoint_selected_text(
            lambda _name: powerpoint, 3000, expected_window_handle=101
        )

        self.assertEqual(value, "")

    def test_discards_text_if_the_active_powerpoint_window_changes(self) -> None:
        selection = SimpleNamespace(
            Type=3,
            TextRange=SimpleNamespace(Start=1, Length=3, Text="old"),
        )
        foreground = SimpleNamespace(Selection=selection, Caption="Deck", Hwnd=101)
        background = SimpleNamespace(Selection=selection, Caption="Deck", Hwnd=202)

        class ChangingApplication:
            def __init__(self) -> None:
                self.reads = 0

            @property
            def ActiveWindow(self) -> object:
                self.reads += 1
                return foreground if self.reads == 1 else background

        value = read_powerpoint_selected_text(
            lambda _name: ChangingApplication(),
            3000,
            expected_window_handle=101,
        )

        self.assertEqual(value, "")

    def test_rejects_a_powerpoint_instance_whose_window_is_not_foreground(self) -> None:
        selection = SimpleNamespace(Type=3, TextRange=SimpleNamespace(Text="wrong deck"))
        powerpoint = SimpleNamespace(
            ActiveWindow=SimpleNamespace(Selection=selection, Caption="Other deck")
        )

        value = read_powerpoint_selected_text(
            lambda _name: powerpoint, 3000, "Wanted deck - PowerPoint"
        )

        self.assertEqual(value, "")

    def test_powerpoint_and_qq_get_longer_capture_windows(self) -> None:
        self.assertGreater(timing_for_app("POWERPNT.EXE").clipboard_timeout_seconds, 1.0)
        self.assertGreater(timing_for_app("qq.exe").clipboard_timeout_seconds, 0.8)
        self.assertLess(timing_for_app("weixin.exe").clipboard_timeout_seconds, 0.8)


class FakeWordSelection:
    def __init__(self, start: int, end: int, text: str) -> None:
        self.Start = start
        self.End = end
        self._text = text
        self.text_reads = 0

    @property
    def Text(self) -> str:
        self.text_reads += 1
        return self._text


class FakeWordApplication:
    def __init__(
        self,
        selection: FakeWordSelection,
        *,
        caption: str = "Report.docx",
        hwnd: int = 101,
        next_selection: FakeWordSelection | None = None,
    ) -> None:
        self.ActiveWindow = SimpleNamespace(Caption=caption, Hwnd=hwnd)
        self._selection = selection
        self._next_selection = next_selection
        self.selection_reads = 0

    @property
    def Selection(self) -> FakeWordSelection:
        self.selection_reads += 1
        if self.selection_reads > 1 and self._next_selection is not None:
            return self._next_selection
        return self._selection


class WordCaptureTests(unittest.TestCase):
    def test_reads_selection_from_the_verified_foreground_word_window(self) -> None:
        selection = FakeWordSelection(10, 18, "Duration")
        word = FakeWordApplication(selection)

        value = read_word_selected_text(
            lambda name: word if name == "Word.Application" else None,
            3000,
            expected_window_handle=101,
        )

        self.assertEqual(value, "Duration")
        self.assertEqual(selection.text_reads, 1)

    def test_handle_takes_priority_over_a_mismatched_title(self) -> None:
        selection = FakeWordSelection(0, 8, "selected")
        word = FakeWordApplication(selection, caption="Actual.docx")

        value = read_word_selected_text(
            lambda _name: word,
            3000,
            expected_window_title="Different.docx - Word",
            expected_window_handle=101,
        )

        self.assertEqual(value, "selected")

    def test_title_is_used_only_when_no_window_handle_is_available(self) -> None:
        selection = FakeWordSelection(0, 8, "selected")
        word = FakeWordApplication(selection, caption="Report.docx")

        value = read_word_selected_text(
            lambda _name: word,
            3000,
            expected_window_title="Report.docx - Word",
        )

        self.assertEqual(value, "selected")

    def test_background_or_unverified_word_instance_fails_closed(self) -> None:
        selection = FakeWordSelection(0, 5, "wrong")
        word = FakeWordApplication(selection, hwnd=202)

        without_identity = read_word_selected_text(lambda _name: word, 3000)
        wrong_handle = read_word_selected_text(
            lambda _name: word, 3000, expected_window_handle=101
        )

        self.assertEqual(without_identity, "")
        self.assertEqual(wrong_handle, "")
        self.assertEqual(word.selection_reads, 0)
        self.assertEqual(selection.text_reads, 0)

    def test_empty_or_oversized_selection_is_rejected_before_text_read(self) -> None:
        empty = FakeWordSelection(5, 5, "whole caret context")
        oversized = FakeWordSelection(0, 10, "0123456789")

        empty_value = read_word_selected_text(
            lambda _name: FakeWordApplication(empty),
            5,
            expected_window_handle=101,
        )
        oversized_value = read_word_selected_text(
            lambda _name: FakeWordApplication(oversized),
            5,
            expected_window_handle=101,
        )

        self.assertEqual(empty_value, "")
        self.assertEqual(oversized_value, "")
        self.assertEqual(empty.text_reads, 0)
        self.assertEqual(oversized.text_reads, 0)

    def test_selection_change_during_read_discards_the_old_text(self) -> None:
        old_selection = FakeWordSelection(0, 3, "old")
        new_selection = FakeWordSelection(4, 7, "new")
        word = FakeWordApplication(old_selection, next_selection=new_selection)

        value = read_word_selected_text(
            lambda _name: word, 3000, expected_window_handle=101
        )

        self.assertEqual(value, "")
        self.assertEqual(old_selection.text_reads, 1)
        self.assertEqual(new_selection.text_reads, 0)


class FakeOutlookSelection(FakeWordSelection):
    def __init__(
        self,
        start: int,
        end: int,
        text: str,
        *,
        story_type: int = 1,
        belongs: bool = True,
    ) -> None:
        super().__init__(start, end, text)
        self.StoryType = story_type
        self.belongs = belongs
        self.in_range_calls = 0

    def InRange(self, _content: object) -> bool:
        self.in_range_calls += 1
        return self.belongs


class FakeOutlookItem:
    def __init__(
        self,
        entry_id: str = "item-1",
        *,
        store_id: str = "store-1",
        com_identity: object | None = None,
    ) -> None:
        self.EntryID = entry_id
        self.Parent = SimpleNamespace(StoreID=store_id)
        self._oleobj_ = com_identity if com_identity is not None else object()


class FakeOutlookItemSelection:
    def __init__(self, *items: object) -> None:
        self._items = items
        self.Count = len(items)

    def Item(self, index: int) -> object:
        return self._items[index - 1]


class FakeOutlookDocument:
    def __init__(
        self,
        selection: FakeOutlookSelection,
        *,
        next_selection: FakeOutlookSelection | None = None,
        com_identity: object | None = None,
    ) -> None:
        self.Content = object()
        self._selection = selection
        self._next_selection = next_selection
        self.selection_reads = 0
        self.Application = self
        self._oleobj_ = com_identity if com_identity is not None else object()

    @property
    def Selection(self) -> FakeOutlookSelection:
        self.selection_reads += 1
        if self.selection_reads > 1 and self._next_selection is not None:
            return self._next_selection
        return self._selection


class FakeOutlookInspector:
    def __init__(
        self,
        document: object | None,
        *,
        caption: str = "Selected message",
        is_word_mail: bool = True,
        editor_type: int = 4,
        item: object | None = None,
        com_identity: object | None = None,
    ) -> None:
        self.Caption = caption
        self.WordEditor = document
        self.EditorType = editor_type
        self._is_word_mail = is_word_mail
        self.CurrentItem = item if item is not None else FakeOutlookItem()
        self._oleobj_ = com_identity if com_identity is not None else object()

    def IsWordMail(self) -> bool:
        return self._is_word_mail


class FakeOutlookExplorer:
    def __init__(
        self,
        document: object | None,
        *,
        caption: str = "Inbox - Outlook",
        preview: object | None = None,
        inline_item: object | None = None,
        selected_items: tuple[object, ...] | None = None,
        com_identity: object | None = None,
    ) -> None:
        self.Caption = caption
        self.ActiveInlineResponseWordEditor = document
        self.ActiveInlineResponse = (
            inline_item
            if inline_item is not None
            else (FakeOutlookItem() if document is not None else None)
        )
        self.PreviewPane = preview
        if selected_items is None:
            selected_items = (FakeOutlookItem(),) if preview is not None else ()
        self.Selection = FakeOutlookItemSelection(*selected_items)
        self._oleobj_ = com_identity if com_identity is not None else object()


class FakeOutlookPreviewPane:
    def __init__(
        self,
        document: object | None,
        *,
        is_word_mail: bool = True,
        editor_type: int = 4,
    ) -> None:
        self.WordEditor = document
        self.EditorType = editor_type
        self._is_word_mail = is_word_mail

    def IsWordMail(self) -> bool:
        return self._is_word_mail


class FakeOutlookApplication:
    def __init__(
        self,
        *,
        inspector: object | None = None,
        explorer: object | None = None,
        next_inspector: object | None = None,
        next_explorer: object | None = None,
    ) -> None:
        self.inspector = inspector
        self.explorer = explorer
        self.next_inspector = next_inspector
        self.next_explorer = next_explorer
        self.inspector_reads = 0
        self.explorer_reads = 0

    def ActiveInspector(self) -> object | None:
        self.inspector_reads += 1
        if self.inspector_reads > 1 and self.next_inspector is not None:
            return self.next_inspector
        return self.inspector

    def ActiveExplorer(self) -> object | None:
        self.explorer_reads += 1
        if self.explorer_reads > 1 and self.next_explorer is not None:
            return self.next_explorer
        return self.explorer


class OutlookCaptureTests(unittest.TestCase):
    def test_reads_a_stable_selection_from_a_foreground_inspector(self) -> None:
        selection = FakeOutlookSelection(2, 10, "Duration")
        document = FakeOutlookDocument(selection)
        outlook = FakeOutlookApplication(
            inspector=FakeOutlookInspector(document, caption="Selected message")
        )

        value = read_outlook_selected_text(
            lambda name: outlook if name == "Outlook.Application" else None,
            3000,
            "Selected message - Outlook",
        )

        self.assertEqual(value, "Duration")
        self.assertEqual(selection.text_reads, 1)
        self.assertEqual(selection.in_range_calls, 2)

    def test_inspector_item_switch_with_same_caption_range_and_com_surfaces_fails_closed(self) -> None:
        window_identity = object()
        document_identity = object()
        old_document = FakeOutlookDocument(
            FakeOutlookSelection(0, 3, "old"),
            com_identity=document_identity,
        )
        new_document = FakeOutlookDocument(
            FakeOutlookSelection(0, 3, "new"),
            com_identity=document_identity,
        )
        outlook = FakeOutlookApplication(
            inspector=FakeOutlookInspector(
                old_document,
                item=FakeOutlookItem("mail-A"),
                com_identity=window_identity,
            ),
            next_inspector=FakeOutlookInspector(
                new_document,
                item=FakeOutlookItem("mail-B"),
                com_identity=window_identity,
            ),
        )

        value = read_outlook_selected_text(
            lambda _name: outlook,
            3000,
            "Selected message - Outlook",
        )

        self.assertEqual(value, "")
        self.assertEqual(old_document.selection_reads, 1)
        self.assertEqual(new_document.selection_reads, 0)

    def test_reads_only_an_active_inline_reply_from_an_explorer(self) -> None:
        selection = FakeOutlookSelection(0, 5, "reply")
        document = FakeOutlookDocument(selection)
        outlook = FakeOutlookApplication(
            explorer=FakeOutlookExplorer(document, caption="Inbox - Outlook")
        )

        value = read_outlook_selected_text(
            lambda _name: outlook,
            3000,
            "Inbox - Outlook",
        )

        self.assertEqual(value, "reply")
        self.assertEqual(document.selection_reads, 2)

    def test_inline_reply_switch_with_same_caption_range_and_editor_fails_closed(self) -> None:
        window_identity = object()
        document_identity = object()
        old_document = FakeOutlookDocument(
            FakeOutlookSelection(0, 5, "draft"),
            com_identity=document_identity,
        )
        new_document = FakeOutlookDocument(
            FakeOutlookSelection(0, 5, "other"),
            com_identity=document_identity,
        )
        outlook = FakeOutlookApplication(
            explorer=FakeOutlookExplorer(
                old_document,
                inline_item=FakeOutlookItem("", com_identity=object()),
                com_identity=window_identity,
            ),
            next_explorer=FakeOutlookExplorer(
                new_document,
                inline_item=FakeOutlookItem("", com_identity=object()),
                com_identity=window_identity,
            ),
        )

        value = read_outlook_selected_text(
            lambda _name: outlook,
            3000,
            "Inbox - Outlook",
        )

        self.assertEqual(value, "")
        self.assertEqual(old_document.selection_reads, 1)
        self.assertEqual(new_document.selection_reads, 0)

    def test_reads_a_stable_selection_from_the_classic_preview_pane(self) -> None:
        selection = FakeOutlookSelection(3, 10, "preview")
        document = FakeOutlookDocument(selection)
        preview = FakeOutlookPreviewPane(document)
        outlook = FakeOutlookApplication(
            explorer=FakeOutlookExplorer(
                None,
                caption="Inbox - Outlook",
                preview=preview,
            )
        )

        value = read_outlook_selected_text(
            lambda _name: outlook,
            3000,
            "Inbox - Outlook",
        )

        self.assertEqual(value, "preview")
        self.assertEqual(document.selection_reads, 2)
        self.assertEqual(selection.in_range_calls, 2)

    def test_preview_item_switch_with_same_caption_range_and_editor_fails_closed(self) -> None:
        window_identity = object()
        document_identity = object()
        old_document = FakeOutlookDocument(
            FakeOutlookSelection(3, 10, "message"),
            com_identity=document_identity,
        )
        new_document = FakeOutlookDocument(
            FakeOutlookSelection(3, 10, "changed"),
            com_identity=document_identity,
        )
        outlook = FakeOutlookApplication(
            explorer=FakeOutlookExplorer(
                None,
                preview=FakeOutlookPreviewPane(old_document),
                selected_items=(FakeOutlookItem("mail-A"),),
                com_identity=window_identity,
            ),
            next_explorer=FakeOutlookExplorer(
                None,
                preview=FakeOutlookPreviewPane(new_document),
                selected_items=(FakeOutlookItem("mail-B"),),
                com_identity=window_identity,
            ),
        )

        value = read_outlook_selected_text(
            lambda _name: outlook,
            3000,
            "Inbox - Outlook",
        )

        self.assertEqual(value, "")
        self.assertEqual(old_document.selection_reads, 1)
        self.assertEqual(new_document.selection_reads, 0)

    def test_inline_reply_takes_priority_over_the_preview_pane(self) -> None:
        inline_selection = FakeOutlookSelection(0, 6, "inline")
        preview_selection = FakeOutlookSelection(0, 7, "preview")
        inline_document = FakeOutlookDocument(inline_selection)
        preview_document = FakeOutlookDocument(preview_selection)
        outlook = FakeOutlookApplication(
            explorer=FakeOutlookExplorer(
                inline_document,
                preview=FakeOutlookPreviewPane(preview_document),
            )
        )

        value = read_outlook_selected_text(
            lambda _name: outlook,
            3000,
            "Inbox - Outlook",
        )

        self.assertEqual(value, "inline")
        self.assertEqual(inline_document.selection_reads, 2)
        self.assertEqual(preview_document.selection_reads, 0)

    def test_preview_pane_must_expose_a_word_mail_editor(self) -> None:
        selection = FakeOutlookSelection(0, 7, "preview")
        document = FakeOutlookDocument(selection)

        for preview in (
            FakeOutlookPreviewPane(document, is_word_mail=False),
            FakeOutlookPreviewPane(document, editor_type=2),
            FakeOutlookPreviewPane(None),
        ):
            with self.subTest(preview=preview):
                outlook = FakeOutlookApplication(
                    explorer=FakeOutlookExplorer(None, preview=preview)
                )
                value = read_outlook_selected_text(
                    lambda _name: outlook,
                    3000,
                    "Inbox - Outlook",
                )
                self.assertEqual(value, "")

        self.assertEqual(document.selection_reads, 0)
        self.assertEqual(selection.text_reads, 0)

    def test_preview_pane_requires_one_unambiguous_selected_item(self) -> None:
        selection = FakeOutlookSelection(0, 7, "preview")
        document = FakeOutlookDocument(selection)

        for selected_items in ((), (FakeOutlookItem(), FakeOutlookItem("item-2"))):
            with self.subTest(count=len(selected_items)):
                outlook = FakeOutlookApplication(
                    explorer=FakeOutlookExplorer(
                        None,
                        preview=FakeOutlookPreviewPane(document),
                        selected_items=selected_items,
                    )
                )
                value = read_outlook_selected_text(
                    lambda _name: outlook,
                    3000,
                    "Inbox - Outlook",
                )
                self.assertEqual(value, "")

        self.assertEqual(document.selection_reads, 0)
        self.assertEqual(selection.text_reads, 0)

    def test_requires_the_outlook_caption_to_match_the_foreground_title(self) -> None:
        selection = FakeOutlookSelection(0, 5, "wrong")
        document = FakeOutlookDocument(selection)
        outlook = FakeOutlookApplication(
            inspector=FakeOutlookInspector(document, caption="Background message")
        )

        missing_title = read_outlook_selected_text(lambda _name: outlook, 3000)
        mismatched_title = read_outlook_selected_text(
            lambda _name: outlook,
            3000,
            "Foreground message - Outlook",
        )

        self.assertEqual(missing_title, "")
        self.assertEqual(mismatched_title, "")
        self.assertEqual(document.selection_reads, 0)
        self.assertEqual(selection.text_reads, 0)

    def test_rejects_a_non_word_inspector_before_requesting_the_document(self) -> None:
        selection = FakeOutlookSelection(0, 5, "wrong")
        document = FakeOutlookDocument(selection)
        not_word_mail = FakeOutlookApplication(
            inspector=FakeOutlookInspector(document, is_word_mail=False)
        )
        wrong_editor = FakeOutlookApplication(
            inspector=FakeOutlookInspector(document, editor_type=2)
        )

        self.assertEqual(
            read_outlook_selected_text(
                lambda _name: not_word_mail, 3000, "Selected message - Outlook"
            ),
            "",
        )
        self.assertEqual(
            read_outlook_selected_text(
                lambda _name: wrong_editor, 3000, "Selected message - Outlook"
            ),
            "",
        )
        self.assertEqual(document.selection_reads, 0)
        self.assertEqual(selection.text_reads, 0)

    def test_empty_oversized_or_foreign_selection_is_not_read(self) -> None:
        cases = (
            FakeOutlookSelection(5, 5, "caret"),
            FakeOutlookSelection(0, 10, "0123456789"),
            FakeOutlookSelection(0, 5, "other", belongs=False),
        )

        for index, selection in enumerate(cases):
            with self.subTest(case=index):
                document = FakeOutlookDocument(selection)
                outlook = FakeOutlookApplication(
                    inspector=FakeOutlookInspector(document)
                )
                value = read_outlook_selected_text(
                    lambda _name: outlook,
                    5,
                    "Selected message - Outlook",
                )
                self.assertEqual(value, "")
                self.assertEqual(selection.text_reads, 0)

    def test_discards_text_when_the_selection_or_active_surface_changes(self) -> None:
        old_selection = FakeOutlookSelection(0, 3, "old")
        new_selection = FakeOutlookSelection(4, 7, "new")
        changing_document = FakeOutlookDocument(
            old_selection, next_selection=new_selection
        )
        selection_changes = FakeOutlookApplication(
            inspector=FakeOutlookInspector(changing_document)
        )
        stable_document = FakeOutlookDocument(FakeOutlookSelection(0, 3, "old"))
        surface_changes = FakeOutlookApplication(
            inspector=FakeOutlookInspector(stable_document),
            next_inspector=FakeOutlookInspector(
                stable_document, caption="Different message"
            ),
        )

        self.assertEqual(
            read_outlook_selected_text(
                lambda _name: selection_changes,
                3000,
                "Selected message - Outlook",
            ),
            "",
        )
        self.assertEqual(old_selection.text_reads, 1)
        self.assertEqual(new_selection.text_reads, 0)
        self.assertEqual(
            read_outlook_selected_text(
                lambda _name: surface_changes,
                3000,
                "Selected message - Outlook",
            ),
            "",
        )

    def test_unavailable_or_new_outlook_com_object_fails_closed(self) -> None:
        self.assertEqual(
            read_outlook_selected_text(
                lambda _name: (_ for _ in ()).throw(RuntimeError("no COM")),
                3000,
                "Outlook",
            ),
            "",
        )

    def test_missing_document_com_identity_fails_closed_before_selection_read(self) -> None:
        selection = FakeOutlookSelection(0, 5, "never")
        document = FakeOutlookDocument(selection)
        del document._oleobj_
        outlook = FakeOutlookApplication(
            inspector=FakeOutlookInspector(document)
        )

        value = read_outlook_selected_text(
            lambda _name: outlook,
            3000,
            "Selected message - Outlook",
        )

        self.assertEqual(value, "")
        self.assertEqual(document.selection_reads, 0)
        self.assertEqual(selection.text_reads, 0)


class FakeExcelArea:
    def __init__(self, rows: int, columns: int, value: object) -> None:
        self.CountLarge = rows * columns
        self.Rows = SimpleNamespace(Count=rows)
        self.Columns = SimpleNamespace(Count=columns)
        self._value = value
        self.value_reads = 0

    @property
    def Value2(self) -> object:
        self.value_reads += 1
        return self._value


class FakeExcelAreas:
    def __init__(self, *areas: FakeExcelArea) -> None:
        self._areas = areas
        self.Count = len(areas)

    def Item(self, index: int) -> FakeExcelArea:
        return self._areas[index - 1]


class FakeExcelRange:
    def __init__(
        self,
        address: str,
        *areas: FakeExcelArea,
        workbook: str = "Book1.xlsx",
        sheet: str = "Sheet1",
    ) -> None:
        self.Address = address
        self.Areas = FakeExcelAreas(*areas)
        book = SimpleNamespace(Name=workbook)
        self.Parent = SimpleNamespace(Name=sheet, Parent=book)


class FakeExcelApplication:
    def __init__(
        self,
        selection: object,
        *,
        caption: str = "Book1.xlsx",
        hwnd: int = 101,
        ready: bool = True,
        next_selection: object | None = None,
    ) -> None:
        self.Ready = ready
        self.ActiveWindow = SimpleNamespace(Caption=caption, Hwnd=hwnd)
        self._selection = selection
        self._next_selection = next_selection
        self.selection_reads = 0

    @property
    def Selection(self) -> object:
        self.selection_reads += 1
        if self.selection_reads > 1 and self._next_selection is not None:
            return self._next_selection
        return self._selection


class ExcelCaptureTests(unittest.TestCase):
    def test_reads_single_and_multiple_cells_as_two_dimensional_text(self) -> None:
        area = FakeExcelArea(2, 2, ((None, "Duration"), (31, True)))
        excel = FakeExcelApplication(FakeExcelRange("$A$1:$B$2", area))

        value = read_excel_selected_text(
            lambda name: excel if name == "Excel.Application" else None,
            3000,
            expected_window_handle=101,
        )

        self.assertEqual(value, "\tDuration\n31\tTRUE")
        self.assertEqual(area.value_reads, 1)

    def test_preserves_multiple_selection_areas_in_order(self) -> None:
        first = FakeExcelArea(1, 2, (("A", "B"),))
        second = FakeExcelArea(2, 1, (("C",), ("D",)))
        selection = FakeExcelRange("$A$1:$B$1,$D$3:$D$4", first, second)
        excel = FakeExcelApplication(selection)

        value = read_excel_selected_text(
            lambda _name: excel, 3000, expected_window_handle=101
        )

        self.assertEqual(value, "A\tB\nC\nD")

    def test_accepts_the_signed_excel_hwnd_for_the_same_foreground_window(self) -> None:
        area = FakeExcelArea(1, 1, "selected")
        excel = FakeExcelApplication(
            FakeExcelRange("$A$1", area), hwnd=-268435455
        )

        value = read_excel_selected_text(
            lambda _name: excel,
            3000,
            expected_window_handle=0xF0000001,
        )

        self.assertEqual(value, "selected")

    def test_title_is_a_safe_fallback_when_no_hwnd_is_available(self) -> None:
        area = FakeExcelArea(1, 1, "selected")
        excel = FakeExcelApplication(
            FakeExcelRange("$A$1", area), caption="Quarterly report.xlsx"
        )

        value = read_excel_selected_text(
            lambda _name: excel,
            3000,
            expected_window_title="Quarterly report.xlsx - Excel",
        )

        self.assertEqual(value, "selected")

    def test_refuses_an_unverified_or_background_excel_instance(self) -> None:
        area = FakeExcelArea(1, 1, "wrong workbook")
        excel = FakeExcelApplication(FakeExcelRange("$A$1", area), hwnd=202)

        without_identity = read_excel_selected_text(lambda _name: excel, 3000)
        wrong_handle = read_excel_selected_text(
            lambda _name: excel, 3000, expected_window_handle=101
        )

        self.assertEqual(without_identity, "")
        self.assertEqual(wrong_handle, "")
        self.assertEqual(excel.selection_reads, 0)
        self.assertEqual(area.value_reads, 0)

    def test_rejects_a_whole_column_before_requesting_its_values(self) -> None:
        area = FakeExcelArea(1_048_576, 1, (("must not be read",),))
        excel = FakeExcelApplication(FakeExcelRange("$A:$A", area))

        value = read_excel_selected_text(
            lambda _name: excel, 3000, expected_window_handle=101
        )

        self.assertEqual(value, "")
        self.assertEqual(area.value_reads, 0)

    def test_enforces_the_rendered_text_limit(self) -> None:
        area = FakeExcelArea(1, 2, (("A", "B"),))
        excel = FakeExcelApplication(FakeExcelRange("$A$1:$B$1", area))

        value = read_excel_selected_text(
            lambda _name: excel, 2, expected_window_handle=101
        )

        self.assertEqual(value, "")

    def test_formula_edit_or_busy_state_fails_closed(self) -> None:
        area = FakeExcelArea(1, 1, "whole cell must not replace edit selection")
        excel = FakeExcelApplication(
            FakeExcelRange("$A$1", area), ready=False
        )

        value = read_excel_selected_text(
            lambda _name: excel, 3000, expected_window_handle=101
        )

        self.assertEqual(value, "")
        self.assertEqual(excel.selection_reads, 0)
        self.assertEqual(area.value_reads, 0)

    def test_non_range_selection_fails_closed(self) -> None:
        excel = FakeExcelApplication(SimpleNamespace(Name="Chart 1"))

        value = read_excel_selected_text(
            lambda _name: excel, 3000, expected_window_handle=101
        )

        self.assertEqual(value, "")

    def test_discards_values_if_the_selection_changes_during_capture(self) -> None:
        old_area = FakeExcelArea(1, 1, "old")
        new_area = FakeExcelArea(1, 1, "new")
        excel = FakeExcelApplication(
            FakeExcelRange("$A$1", old_area),
            next_selection=FakeExcelRange("$B$1", new_area),
        )

        value = read_excel_selected_text(
            lambda _name: excel, 3000, expected_window_handle=101
        )

        self.assertEqual(value, "")
        self.assertEqual(old_area.value_reads, 1)
        self.assertEqual(new_area.value_reads, 0)


class FakeTextRange:
    def __init__(self, text: str) -> None:
        self.text = text

    def GetText(self, _max_length: int) -> str:
        return self.text


class CountingTextRange(FakeTextRange):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text_reads = 0

    def GetText(self, max_length: int) -> str:
        self.text_reads += 1
        return super().GetText(max_length)


class FakePattern:
    def __init__(self, text: str) -> None:
        self.text = text

    def GetSelection(self) -> list[FakeTextRange]:
        return [FakeTextRange(self.text)]


class FakeMultiPattern:
    def __init__(self, *values: str) -> None:
        self.values = values

    def GetSelection(self) -> list[FakeTextRange]:
        return [FakeTextRange(value) for value in self.values]


class FakeRangesPattern:
    def __init__(self, ranges: list[FakeTextRange]) -> None:
        self.ranges = ranges

    def GetSelection(self) -> list[FakeTextRange]:
        return self.ranges


class FakeControl:
    def __init__(
        self,
        patterns: dict[object, object] | None = None,
        *,
        password: bool = False,
        parent: FakeControl | None = None,
        children: list[FakeControl] | None = None,
    ) -> None:
        self.patterns = patterns or {}
        self._password = password
        self.parent = parent
        self.children = children or []
        self.pattern_reads = 0
        for child in self.children:
            child.parent = self

    @property
    def BoundingRectangle(self) -> object:
        raise AssertionError("selection probing must not depend on BoundingRectangle")

    @property
    def IsPassword(self) -> bool:
        return self._password

    def GetPattern(self, pattern_id: object) -> object:
        self.pattern_reads += 1
        value = self.patterns.get(pattern_id)
        if isinstance(value, Exception):
            raise value
        return value

    def GetParentControl(self) -> FakeControl | None:
        return self.parent

    def GetFirstChildControl(self) -> FakeControl | None:
        return self.children[0] if self.children else None

    def GetNextSiblingControl(self) -> FakeControl | None:
        if not self.parent:
            return None
        siblings = self.parent.children
        try:
            index = siblings.index(self)
        except ValueError:
            return None
        return siblings[index + 1] if index + 1 < len(siblings) else None


class EmptySelectionPattern:
    @property
    def DocumentRange(self) -> object:
        raise AssertionError("deep selection probing must never read DocumentRange")

    def GetSelection(self) -> list[FakeTextRange]:
        return []


class DocumentRangeGuardPattern(FakePattern):
    @property
    def DocumentRange(self) -> object:
        raise AssertionError("deep selection probing must never read DocumentRange")


class AdvancingPattern(FakePattern):
    def __init__(self, text: str, clock: FakeClock, advance: float) -> None:
        super().__init__(text)
        self.clock = clock
        self.advance = advance

    def GetSelection(self) -> list[FakeTextRange]:
        self.clock.now += self.advance
        return super().GetSelection()


class BrokenPasswordControl(FakeControl):
    @property
    def IsPassword(self) -> bool:
        raise RuntimeError("UIA provider would not disclose IsPassword")


class UiaCaptureTests(unittest.TestCase):
    @unittest.skip("Superseded by the requested test3 two-state UIA rollback")
    def test_security_probe_distinguishes_safe_password_and_unknown(self) -> None:
        safe = FakeControl({"text": EmptySelectionPattern()})
        password = FakeControl(password=True)
        unknown = BrokenPasswordControl()

        safe_result = probe_uia_selected_text(
            [safe], ["text"], 3000, lambda text: text.strip()
        )
        password_result = probe_uia_selected_text(
            [password], ["text"], 3000, lambda text: text.strip()
        )
        unknown_result = probe_uia_selected_text(
            [unknown], ["text"], 3000, lambda text: text.strip()
        )

        self.assertIs(safe_result.security, SelectionSecurity.SAFE)
        self.assertIs(password_result.security, SelectionSecurity.PASSWORD)
        self.assertIs(unknown_result.security, SelectionSecurity.UNKNOWN)
        self.assertEqual((safe_result.text, password_result.text, unknown_result.text), ("", "", ""))

    @unittest.skip("Superseded by the requested test3 two-state UIA rollback")
    def test_missing_uia_control_is_unknown_not_safe(self) -> None:
        result = probe_uia_selected_text(
            [], ["text"], 3000, lambda text: text.strip()
        )

        self.assertIs(result.security, SelectionSecurity.UNKNOWN)

    def test_rectangle_failure_does_not_hide_a_valid_text_pattern(self) -> None:
        control = FakeControl({"text2": FakePattern("Duration")})

        value, protected = read_uia_selected_text(
            [control], ["text2", "text"], 3000, lambda text: text.strip()
        )

        self.assertEqual(value, "Duration")
        self.assertFalse(protected)

    def test_one_broken_pattern_does_not_skip_the_next_pattern(self) -> None:
        control = FakeControl(
            {"text2": RuntimeError("unsupported"), "text": FakePattern("WeChat selection")}
        )

        value, protected = read_uia_selected_text(
            [control], ["text2", "text"], 3000, lambda text: text.strip()
        )

        self.assertEqual(value, "WeChat selection")
        self.assertFalse(protected)

    def test_password_control_stops_before_any_text_read(self) -> None:
        control = FakeControl({"text": FakePattern("secret")}, password=True)

        value, protected = read_uia_selected_text(
            [control], ["text"], 3000, lambda text: text.strip()
        )

        self.assertEqual(value, "")
        self.assertTrue(protected)
        self.assertEqual(control.pattern_reads, 0)

    def test_password_parent_is_checked_before_child_text_pattern(self) -> None:
        parent = FakeControl(password=True)
        child = FakeControl({"text": FakePattern("secret")}, parent=parent)

        value, protected = read_uia_selected_text(
            [child], ["text"], 3000, lambda text: text.strip()
        )

        self.assertEqual(value, "")
        self.assertTrue(protected)
        self.assertEqual(child.pattern_reads, 0)
        self.assertEqual(parent.pattern_reads, 0)

    def test_password_property_failure_fails_closed_before_pattern_read(self) -> None:
        control = BrokenPasswordControl({"text": FakePattern("secret")})

        value, protected = read_uia_selected_text(
            [control], ["text"], 3000, lambda text: text.strip()
        )

        self.assertEqual(value, "")
        self.assertTrue(protected)
        self.assertEqual(control.pattern_reads, 0)

    def test_single_and_combined_ranges_must_fit_the_text_limit(self) -> None:
        oversized = FakeControl({"text": FakePattern("123456")})
        combined = FakeControl({"text": FakeMultiPattern("123", "456")})

        oversized_value = read_uia_selected_text(
            [oversized], ["text"], 5, lambda text: text.strip()
        )
        combined_value = read_uia_selected_text(
            [combined], ["text"], 6, lambda text: text.strip()
        )

        self.assertEqual(oversized_value, ("", False))
        self.assertEqual(combined_value, ("", False))

    def test_excessive_empty_selection_ranges_fail_closed_at_the_cap(self) -> None:
        ranges = [CountingTextRange("") for _ in range(65)]
        control = FakeControl({"text": FakeRangesPattern(ranges)})

        value = read_uia_selected_text(
            [control], ["text"], 3000, lambda text: text.strip()
        )

        self.assertEqual(value, ("", False))
        self.assertEqual(sum(item.text_reads for item in ranges), 64)


class UiaDescendantCaptureTests(unittest.TestCase):
    def read_deep(self, root: FakeControl, **limits: object) -> tuple[str, bool]:
        return read_uia_descendant_selected_text(
            root,
            ["text2", "text"],
            3000,
            lambda text: text.strip(),
            **limits,
        )

    def test_finds_a_text_pattern_on_a_deep_document_descendant(self) -> None:
        document = FakeControl(
            {"text": DocumentRangeGuardPattern("PDF selection")}
        )
        root = FakeControl(children=[FakeControl(children=[FakeControl(children=[document])])])

        value, protected = self.read_deep(root)

        self.assertEqual(value, "PDF selection")
        self.assertFalse(protected)

    def test_node_limit_stops_before_a_later_sibling(self) -> None:
        target = FakeControl({"text": FakePattern("must stay unread")})
        root = FakeControl(
            children=[FakeControl(), FakeControl(), target]
        )

        value, protected = self.read_deep(root, max_nodes=3)

        self.assertEqual(value, "")
        self.assertFalse(protected)

    def test_depth_limit_stops_before_a_deeper_document(self) -> None:
        target = FakeControl({"text": FakePattern("too deep")})
        root = FakeControl(
            children=[FakeControl(children=[FakeControl(children=[target])])]
        )

        value, protected = self.read_deep(root, max_depth=2)

        self.assertEqual(value, "")
        self.assertFalse(protected)

    def test_elapsed_time_limit_discards_a_slow_selection(self) -> None:
        clock = FakeClock()
        slow = FakeControl(
            {"text": AdvancingPattern("late PDF selection", clock, 0.2)}
        )

        value, protected = self.read_deep(
            FakeControl(children=[slow]),
            timeout_seconds=0.1,
            monotonic=clock.monotonic,
        )

        self.assertEqual(value, "")
        self.assertFalse(protected)

    @unittest.skip("Superseded by the requested test3 two-state UIA rollback")
    def test_incomplete_deep_scans_are_explicitly_unknown(self) -> None:
        target = FakeControl({"text": FakePattern("must stay unread")})
        root = FakeControl(children=[FakeControl(), FakeControl(), target])

        result = probe_uia_descendant_selected_text(
            root,
            ["text"],
            3000,
            lambda text: text.strip(),
            max_nodes=3,
        )

        self.assertEqual(result.text, "")
        self.assertIs(result.security, SelectionSecurity.UNKNOWN)

    def test_password_descendant_fails_closed_before_text_read(self) -> None:
        password_pattern = DocumentRangeGuardPattern("secret")
        password = FakeControl({"text": password_pattern}, password=True)
        ordinary = FakeControl({"text": FakePattern("ordinary selection")})

        # The candidate deliberately precedes the password node in BFS order.
        # A one-pass implementation would leak it before seeing the password.
        value, protected = self.read_deep(FakeControl(children=[ordinary, password]))

        self.assertEqual(value, "")
        self.assertTrue(protected)
        self.assertEqual(password.pattern_reads, 0)
        self.assertEqual(ordinary.pattern_reads, 0)

    def test_password_property_failure_fails_the_entire_tree_closed(self) -> None:
        broken = BrokenPasswordControl({"text": FakePattern("must stay unread")})
        ordinary = FakeControl({"text": FakePattern("ordinary selection")})

        # The apparently valid selection must remain unread until every node's
        # security property in the bounded tree has been validated.
        value, protected = self.read_deep(FakeControl(children=[ordinary, broken]))

        self.assertEqual(value, "")
        self.assertTrue(protected)
        self.assertEqual(broken.pattern_reads, 0)
        self.assertEqual(ordinary.pattern_reads, 0)

    def test_empty_selection_does_not_fall_back_to_document_range(self) -> None:
        value, protected = self.read_deep(
            FakeControl(children=[FakeControl({"text": EmptySelectionPattern()})])
        )

        self.assertEqual(value, "")
        self.assertFalse(protected)

    def test_broken_descendant_is_skipped_and_a_sibling_can_succeed(self) -> None:
        broken = FakeControl({"text2": RuntimeError("stale UIA node")})
        target = FakeControl({"text": FakePattern("selected by sibling")})

        value, protected = self.read_deep(
            FakeControl(children=[broken, target])
        )

        self.assertEqual(value, "selected by sibling")
        self.assertFalse(protected)

    def test_selection_larger_than_limit_is_rejected(self) -> None:
        root = FakeControl(
            children=[FakeControl({"text": FakePattern("123456")})]
        )

        value, protected = read_uia_descendant_selected_text(
            root,
            ["text"],
            5,
            lambda text: text.strip(),
        )

        self.assertEqual(value, "")
        self.assertFalse(protected)

    def test_excessive_empty_selection_ranges_fail_closed_at_the_cap(self) -> None:
        ranges = [CountingTextRange("") for _ in range(65)]
        root = FakeControl(
            children=[FakeControl({"text": FakeRangesPattern(ranges)})]
        )

        value, protected = self.read_deep(root)

        self.assertEqual(value, "")
        self.assertFalse(protected)
        self.assertEqual(sum(item.text_reads for item in ranges), 64)

class ClipboardCaptureTests(unittest.TestCase):
    def test_unsafe_raw_ole_snapshot_implementation_is_not_exposed(self) -> None:
        self.assertFalse(hasattr(selection_capture, "OleClipboardSnapshot"))

    def run_capture(self, **overrides: object) -> tuple[ClipboardCaptureResult, FakeSnapshot, list[str | None]]:
        clock = FakeClock()
        snapshot = FakeSnapshot()
        restored_text: list[str | None] = []
        values: dict[str, object] = {
            "old_text": "old",
            "old_formats": [2, 15, 49324],
            "old_state_known": True,
            "old_sequence": 1,
            "snapshot_factory": lambda: snapshot,
            "sequence_number": lambda: 2 if clock.now >= 0.65 else 1,
            "send_copy": lambda: None,
            "read_text": lambda: "selected",
            "restore_plain_text": lambda value, _expected: restored_text.append(value) is None,
            "timeout_seconds": 0.9,
            "monotonic": clock.monotonic,
            "sleep": clock.sleep,
        }
        values.update(overrides)
        result = capture_selected_text_with_clipboard(**values)
        return result, snapshot, restored_text

    def test_rich_or_image_clipboard_no_longer_blocks_safe_capture(self) -> None:
        result, snapshot, restored_text = self.run_capture()

        self.assertEqual(result.text, "selected")
        self.assertEqual(result.reason, "captured")
        self.assertTrue(result.restored)
        self.assertEqual(snapshot.restore_calls, 1)
        self.assertEqual(snapshot.close_calls, 1)
        self.assertEqual(restored_text, [])

    def test_does_not_send_copy_when_rich_clipboard_cannot_be_snapshotted(self) -> None:
        copy_calls: list[bool] = []
        result, _snapshot, _restored = self.run_capture(
            snapshot_factory=lambda: None,
            send_copy=lambda: copy_calls.append(True),
        )

        self.assertEqual(result.reason, "snapshot_unavailable")
        self.assertFalse(result.attempted)
        self.assertEqual(copy_calls, [])

    def test_plain_text_clipboard_still_works_without_ole_snapshot(self) -> None:
        result, _snapshot, restored_text = self.run_capture(
            old_formats=[1, 7, 13, 16],
            snapshot_factory=lambda: None,
        )

        self.assertEqual(result.text, "selected")
        self.assertTrue(result.restored)
        self.assertEqual(restored_text, ["old"])

    def test_ansi_only_clipboard_is_not_cleared_without_a_snapshot(self) -> None:
        copy_calls: list[bool] = []
        result, _snapshot, restored_text = self.run_capture(
            old_text=None,
            old_formats=[1, 7, 16],
            snapshot_factory=lambda: None,
            send_copy=lambda: copy_calls.append(True),
        )

        self.assertEqual(result.reason, "snapshot_unavailable")
        self.assertEqual(copy_calls, [])
        self.assertEqual(restored_text, [])

    def test_unknown_clipboard_state_never_allows_an_unprotected_copy(self) -> None:
        copy_calls: list[bool] = []
        result, _snapshot, _restored = self.run_capture(
            old_formats=[],
            old_state_known=False,
            snapshot_factory=lambda: None,
            send_copy=lambda: copy_calls.append(True),
        )

        self.assertEqual(result.reason, "snapshot_unavailable")
        self.assertEqual(copy_calls, [])

    def test_zero_sequence_never_sends_copy(self) -> None:
        copy_calls: list[bool] = []
        result, snapshot, _restored = self.run_capture(
            old_sequence=0,
            sequence_number=lambda: 0,
            send_copy=lambda: copy_calls.append(True),
        )

        self.assertEqual(result.reason, "sequence_unavailable")
        self.assertFalse(result.attempted)
        self.assertEqual(copy_calls, [])
        self.assertEqual(snapshot.restore_calls, 0)

    def test_zero_sequence_during_wait_is_not_treated_as_a_clipboard_change(self) -> None:
        values = iter([1, 0, 2, 2])
        read_calls: list[bool] = []

        result, snapshot, _restored = self.run_capture(
            sequence_number=lambda: next(values),
            clipboard_change_is_ours=lambda: True,
            read_text=lambda: read_calls.append(True) or "must not be translated",
        )

        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "sequence_unavailable")
        self.assertEqual(read_calls, [])
        self.assertEqual(snapshot.restore_calls, 1)

    def test_zero_sequence_after_read_never_publishes_an_unstable_value(self) -> None:
        values = iter([1, 2, 0, 2, 2])
        read_calls: list[bool] = []

        result, snapshot, _restored = self.run_capture(
            sequence_number=lambda: next(values),
            clipboard_change_is_ours=lambda: True,
            read_text=lambda: read_calls.append(True) or "unstable",
        )

        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "sequence_unavailable")
        self.assertEqual(read_calls, [True])
        self.assertEqual(snapshot.restore_calls, 1)

    def test_clipboard_change_during_snapshot_cancels_before_copy(self) -> None:
        copy_calls: list[bool] = []
        result, snapshot, _restored = self.run_capture(
            old_sequence=1,
            sequence_number=lambda: 2,
            send_copy=lambda: copy_calls.append(True),
        )

        self.assertEqual(result.reason, "concurrent_change")
        self.assertFalse(result.attempted)
        self.assertEqual(copy_calls, [])
        self.assertEqual(snapshot.restore_calls, 0)
        self.assertEqual(snapshot.close_calls, 1)

    def test_waits_long_enough_for_a_slow_wechat_copy(self) -> None:
        result, _snapshot, _restored = self.run_capture()

        self.assertEqual(result.text, "selected")

    def test_qq_safety_window_catches_a_copy_after_one_second(self) -> None:
        clock = FakeClock()
        result, _snapshot, _restored = self.run_capture(
            sequence_number=lambda: 2 if clock.now >= 1.05 else 1,
            timeout_seconds=timing_for_app("qq.exe").clipboard_timeout_seconds,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(result.text, "selected")

    def test_partial_copy_exception_is_still_observed_and_restored(self) -> None:
        def fail_copy() -> None:
            raise RuntimeError("keyboard unavailable")

        result, snapshot, _restored = self.run_capture(send_copy=fail_copy)

        self.assertEqual(result.text, "selected")
        self.assertEqual(result.reason, "captured")
        self.assertEqual(snapshot.restore_calls, 1)
        self.assertEqual(snapshot.close_calls, 1)

    def test_copy_exception_without_clipboard_change_never_rewrites_it(self) -> None:
        def fail_copy() -> None:
            raise RuntimeError("keyboard unavailable")

        result, snapshot, _restored = self.run_capture(
            send_copy=fail_copy,
            sequence_number=lambda: 1,
            timeout_seconds=0.1,
        )

        self.assertEqual(result.reason, "copy_failed")
        self.assertEqual(snapshot.restore_calls, 0)
        self.assertEqual(snapshot.close_calls, 1)

    def test_cancels_before_copy_if_focus_has_changed(self) -> None:
        copy_calls: list[bool] = []
        result, snapshot, _restored = self.run_capture(
            focus_is_current=lambda: False,
            send_copy=lambda: copy_calls.append(True),
        )

        self.assertEqual(result.reason, "focus_changed")
        self.assertFalse(result.attempted)
        self.assertEqual(copy_calls, [])
        self.assertEqual(snapshot.restore_calls, 0)

    def test_focus_change_after_copy_restores_only_our_clipboard_change(self) -> None:
        clock = FakeClock()
        snapshot = FakeSnapshot()

        result = capture_selected_text_with_clipboard(
            old_text="old",
            old_formats=[2, 15],
            old_state_known=True,
            old_sequence=1,
            snapshot_factory=lambda: snapshot,
            sequence_number=lambda: 2 if clock.now >= 0.05 else 1,
            send_copy=lambda: None,
            read_text=lambda: "must not be read",
            restore_plain_text=lambda _value, _expected: True,
            timeout_seconds=0.9,
            focus_is_current=lambda: clock.now < 0.05,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "focus_changed")
        self.assertEqual(snapshot.restore_calls, 1)

    def test_focus_change_before_slow_wechat_copy_waits_and_restores_target_change(self) -> None:
        clock = FakeClock()
        snapshot = FakeSnapshot()

        result = capture_selected_text_with_clipboard(
            old_text="old",
            old_formats=[2, 15],
            old_state_known=True,
            old_sequence=1,
            snapshot_factory=lambda: snapshot,
            sequence_number=lambda: 2 if clock.now >= 0.65 else 1,
            send_copy=lambda: None,
            read_text=lambda: "must not be read after cancellation",
            restore_plain_text=lambda _value, _expected: True,
            timeout_seconds=0.9,
            focus_is_current=lambda: clock.now < 0.05,
            clipboard_change_is_ours=lambda: True,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "focus_changed")
        self.assertEqual(snapshot.restore_calls, 1)

    def test_focus_change_preserves_clipboard_owned_by_another_process(self) -> None:
        clock = FakeClock()
        snapshot = FakeSnapshot()

        result = capture_selected_text_with_clipboard(
            old_text="old",
            old_formats=[2, 15],
            old_state_known=True,
            old_sequence=1,
            snapshot_factory=lambda: snapshot,
            sequence_number=lambda: 2 if clock.now >= 0.1 else 1,
            send_copy=lambda: None,
            read_text=lambda: "external",
            restore_plain_text=lambda _value, _expected: True,
            timeout_seconds=0.3,
            focus_is_current=lambda: clock.now < 0.05,
            clipboard_change_is_ours=lambda: False,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "concurrent_change")
        self.assertEqual(snapshot.restore_calls, 0)

    def test_first_change_from_another_owner_is_never_read_or_overwritten(self) -> None:
        read_calls: list[bool] = []
        result, snapshot, _restored = self.run_capture(
            clipboard_change_is_ours=lambda: False,
            read_text=lambda: read_calls.append(True) or "external text",
        )

        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "concurrent_change")
        self.assertEqual(read_calls, [])
        self.assertEqual(snapshot.restore_calls, 0)

    def test_explicit_user_copy_intent_wins_even_with_the_same_clipboard_owner(self) -> None:
        read_calls: list[bool] = []
        result, snapshot, _restored = self.run_capture(
            clipboard_change_is_ours=lambda: True,
            external_copy_intent_detected=lambda: True,
            read_text=lambda: read_calls.append(True) or "user copy",
        )

        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "concurrent_change")
        self.assertEqual(read_calls, [])
        self.assertEqual(snapshot.restore_calls, 0)

    def test_unknown_owner_is_not_translated_or_overwritten(self) -> None:
        read_calls: list[bool] = []
        result, snapshot, _restored = self.run_capture(
            clipboard_change_is_ours=lambda: None,
            read_text=lambda: read_calls.append(True) or "uncertain text",
        )

        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "owner_unknown")
        self.assertTrue(result.restored)
        self.assertEqual(read_calls, [])
        self.assertEqual(snapshot.restore_calls, 0)

    def test_no_clipboard_change_does_not_rewrite_clipboard(self) -> None:
        result, snapshot, restored_text = self.run_capture(
            sequence_number=lambda: 1,
            timeout_seconds=0.1,
        )

        self.assertEqual(result.reason, "no_clipboard_change")
        self.assertEqual(snapshot.restore_calls, 0)
        self.assertEqual(snapshot.close_calls, 1)
        self.assertEqual(restored_text, [])

    def test_newer_external_clipboard_change_is_preserved(self) -> None:
        clock = FakeClock()
        read_complete = False
        returned_stable_sequence = False

        def sequence_number() -> int:
            nonlocal returned_stable_sequence
            if read_complete:
                if not returned_stable_sequence:
                    returned_stable_sequence = True
                    return 2
                return 3
            return 2 if clock.now >= 0.05 else 1

        def read_text() -> str:
            nonlocal read_complete
            read_complete = True
            return "selected"

        result, snapshot, restored_text = self.run_capture(
            sequence_number=sequence_number,
            read_text=read_text,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(result.text, "selected")
        self.assertEqual(result.reason, "external_change_preserved")
        self.assertEqual(snapshot.restore_calls, 0)
        self.assertEqual(restored_text, [])

    def test_clipboard_change_during_text_read_is_never_translated_or_restored(self) -> None:
        clock = FakeClock()
        current_sequence = 1

        def send_copy() -> None:
            nonlocal current_sequence
            current_sequence = 2

        def read_text() -> str:
            nonlocal current_sequence
            current_sequence = 3
            return "external text"

        result, snapshot, restored_text = self.run_capture(
            old_formats=[13],
            snapshot_factory=lambda: None,
            sequence_number=lambda: current_sequence,
            send_copy=send_copy,
            read_text=read_text,
            clipboard_change_is_ours=lambda: current_sequence == 2,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "concurrent_change")
        self.assertEqual(snapshot.restore_calls, 0)
        self.assertEqual(restored_text, [])

    def test_rich_clipboard_restore_failure_never_downgrades_to_plain_text(self) -> None:
        snapshot = FakeSnapshot(restore_result=False)
        restored_text: list[str | None] = []
        result, _unused, _restored = self.run_capture(
            snapshot_factory=lambda: snapshot,
            restore_plain_text=lambda value, _expected: restored_text.append(value) is None,
        )

        self.assertEqual(result.reason, "restore_failed")
        self.assertFalse(result.restored)
        self.assertEqual(restored_text, [])


if __name__ == "__main__":
    unittest.main()
