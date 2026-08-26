from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence


PP_SELECTION_TEXT = 3
OL_EDITOR_WORD = 4
TEXT_ONLY_CLIPBOARD_FORMATS = frozenset({1, 7, 13, 16})
EXCEL_MAX_CAPTURE_CELLS = 4096
UIA_MAX_SELECTION_RANGES = 64


@dataclass(frozen=True, slots=True)
class CaptureTiming:
    settle_seconds: float
    clipboard_timeout_seconds: float


DEFAULT_TIMING = CaptureTiming(settle_seconds=0.075, clipboard_timeout_seconds=0.55)
APP_TIMINGS: dict[str, CaptureTiming] = {
    "powerpnt.exe": CaptureTiming(settle_seconds=0.14, clipboard_timeout_seconds=1.10),
    "qq.exe": CaptureTiming(settle_seconds=0.13, clipboard_timeout_seconds=1.50),
}


def timing_for_app(exe: str) -> CaptureTiming:
    return APP_TIMINGS.get(exe.lower(), DEFAULT_TIMING)


def read_powerpoint_selected_text(
    get_active_object: Callable[[str], object],
    max_length: int,
    expected_window_title: str = "",
    expected_window_handle: int = 0,
) -> str:
    """Read only a stable text selection from the foreground PowerPoint window."""
    if max_length <= 0 or not (expected_window_title or expected_window_handle):
        return ""
    try:
        powerpoint = get_active_object("PowerPoint.Application")
        window = getattr(powerpoint, "ActiveWindow")
        if not window or not _office_window_matches(
            window, expected_window_title, expected_window_handle
        ):
            return ""
        selection = getattr(window, "Selection")
        if int(selection.Type) != PP_SELECTION_TEXT:
            return ""
        text_range = getattr(selection, "TextRange")
        identity = (
            int(getattr(selection, "Type")),
            int(getattr(text_range, "Start")),
            int(getattr(text_range, "Length")),
        )
        if identity[2] <= 0 or identity[2] > max_length:
            return ""
        text = str(getattr(text_range, "Text"))
        if not text or len(text) > max_length:
            return ""

        current_window = getattr(powerpoint, "ActiveWindow")
        if not current_window or not _office_window_matches(
            current_window, expected_window_title, expected_window_handle
        ):
            return ""
        current_selection = getattr(current_window, "Selection")
        current_range = getattr(current_selection, "TextRange")
        current_identity = (
            int(getattr(current_selection, "Type")),
            int(getattr(current_range, "Start")),
            int(getattr(current_range, "Length")),
        )
        return text if current_identity == identity else ""
    except Exception:
        return ""


def _normalized_hwnd(value: object) -> int:
    """Compare Office's signed INT32 HWND with Win32's unsigned handle value."""
    return int(value) & 0xFFFFFFFF


def _office_window_matches(
    window: object,
    expected_window_title: str,
    expected_window_handle: int,
) -> bool:
    if expected_window_handle:
        for member_name in ("Hwnd", "HWND"):
            try:
                actual_handle = getattr(window, member_name)
            except Exception:
                continue
            try:
                return _normalized_hwnd(actual_handle) == _normalized_hwnd(
                    expected_window_handle
                )
            except Exception:
                return False
        return False
    if expected_window_title:
        try:
            caption = str(getattr(window, "Caption")).strip()
        except Exception:
            return False
        return bool(caption and caption.casefold() in expected_window_title.casefold())
    # A running Office COM object is not necessarily the foreground instance.
    # Without either foreground identity signal, returning nothing is safer.
    return False


def read_word_selected_text(
    get_active_object: Callable[[str], object],
    max_length: int,
    expected_window_title: str = "",
    expected_window_handle: int = 0,
) -> str:
    """Read only a stable selection from the verified foreground Word window."""
    if max_length <= 0:
        return ""
    try:
        word = get_active_object("Word.Application")
        window = getattr(word, "ActiveWindow")
        if not window or not _office_window_matches(
            window, expected_window_title, expected_window_handle
        ):
            return ""

        selection = getattr(word, "Selection")
        start = int(getattr(selection, "Start"))
        end = int(getattr(selection, "End"))
        if end <= start or end - start > max_length:
            return ""
        text = str(getattr(selection, "Text"))
        if not text or len(text) > max_length:
            return ""

        current_window = getattr(word, "ActiveWindow")
        if not current_window or not _office_window_matches(
            current_window, expected_window_title, expected_window_handle
        ):
            return ""
        current_selection = getattr(word, "Selection")
        current_identity = (
            int(getattr(current_selection, "Start")),
            int(getattr(current_selection, "End")),
        )
        if current_identity != (start, end):
            return ""
        return text
    except Exception:
        return ""


def _outlook_call_window(application: object, member_name: str) -> object | None:
    """Call Outlook's ActiveInspector/ActiveExplorer method defensively."""
    value = getattr(application, member_name)
    return value() if callable(value) else value


def _outlook_caption_matches(window: object, expected_window_title: str) -> bool:
    """Match Outlook's object-model caption to the verified Win32 title."""
    if not expected_window_title:
        return False
    caption = str(getattr(window, "Caption")).strip()
    return bool(caption and caption.casefold() in expected_window_title.casefold())


@dataclass(frozen=True, slots=True)
class _OutlookEditorSurface:
    kind: str
    document: object
    identity: tuple[object, ...]


def _outlook_com_identity(value: object) -> object | None:
    """Return pywin32's COM identity token without dereferencing document text."""
    try:
        identity = getattr(value, "_oleobj_")
    except Exception:
        return None
    return identity if identity is not None else None


def _outlook_item_identity(item: object) -> tuple[object, ...] | None:
    """Identify an Outlook item by its MAPI key or its live COM identity.

    Saved Outlook items expose an EntryID and their parent folder's StoreID.
    Unsaved inline replies do not necessarily have an EntryID, so their
    canonical pywin32 COM identity is the only fail-closed live-session key.
    Neither path requests the subject or message body.
    """
    try:
        entry_id = str(getattr(item, "EntryID")).strip()
    except Exception:
        entry_id = ""
    if entry_id:
        try:
            store_id = str(getattr(getattr(item, "Parent"), "StoreID")).strip()
        except Exception:
            store_id = ""
        if store_id:
            return "mapi", store_id, entry_id

    identity = _outlook_com_identity(item)
    return ("com", identity) if identity is not None else None


def _outlook_collection_item(collection: object, index: int) -> object:
    item = getattr(collection, "Item")
    return item(index) if callable(item) else item[index]


def _outlook_preview_item(explorer: object) -> object | None:
    """Return only an unambiguous item selected for the Explorer preview."""
    selection = getattr(explorer, "Selection")
    if int(getattr(selection, "Count")) != 1:
        return None
    return _outlook_collection_item(selection, 1)


def _outlook_editor_surface(
    kind: str,
    window: object,
    document: object,
    item: object,
) -> _OutlookEditorSurface | None:
    window_identity = _outlook_com_identity(window)
    document_identity = _outlook_com_identity(document)
    item_identity = _outlook_item_identity(item)
    if (
        window_identity is None
        or document_identity is None
        or item_identity is None
    ):
        return None
    return _OutlookEditorSurface(
        kind,
        document,
        (kind, window_identity, document_identity, item_identity),
    )


def _outlook_active_word_editor(
    outlook: object,
    expected_window_title: str,
) -> _OutlookEditorSurface | None:
    """Return only a foreground-matched classic Outlook Word editor.

    Explorer inline replies take priority over the normal reading pane.  The
    latter is accepted only through Outlook's PreviewPane Word-editor surface.
    """
    try:
        inspector = _outlook_call_window(outlook, "ActiveInspector")
    except Exception:
        inspector = None
    if inspector and _outlook_caption_matches(inspector, expected_window_title):
        try:
            is_word_mail = getattr(inspector, "IsWordMail")
            if not bool(is_word_mail() if callable(is_word_mail) else is_word_mail):
                return None
            if int(getattr(inspector, "EditorType")) != OL_EDITOR_WORD:
                return None
            document = getattr(inspector, "WordEditor")
            item = getattr(inspector, "CurrentItem")
            return (
                _outlook_editor_surface("inspector", inspector, document, item)
                if document and item
                else None
            )
        except Exception:
            return None

    try:
        explorer = _outlook_call_window(outlook, "ActiveExplorer")
    except Exception:
        explorer = None
    if not explorer or not _outlook_caption_matches(explorer, expected_window_title):
        return None
    try:
        document = getattr(explorer, "ActiveInlineResponseWordEditor")
        if document:
            item = getattr(explorer, "ActiveInlineResponse")
            return (
                _outlook_editor_surface("inline", explorer, document, item)
                if item
                else None
            )
    except Exception:
        pass

    try:
        preview = getattr(explorer, "PreviewPane")
        if not preview:
            return None
        is_word_mail = getattr(preview, "IsWordMail")
        if not bool(is_word_mail() if callable(is_word_mail) else is_word_mail):
            return None
        if int(getattr(preview, "EditorType")) != OL_EDITOR_WORD:
            return None
        document = getattr(preview, "WordEditor")
        item = _outlook_preview_item(explorer)
        return (
            _outlook_editor_surface("preview", explorer, document, item)
            if document and item
            else None
        )
    except Exception:
        return None


def _outlook_selection_identity(selection: object) -> tuple[int, int, int]:
    return (
        int(getattr(selection, "Start")),
        int(getattr(selection, "End")),
        int(getattr(selection, "StoryType")),
    )


def _outlook_selection_belongs_to_document(
    selection: object,
    document: object,
) -> bool:
    """Verify containment without requesting the message's full body text."""
    content = getattr(document, "Content")
    in_range = getattr(selection, "InRange")
    return bool(in_range(content) if callable(in_range) else False)


def read_outlook_selected_text(
    get_active_object: Callable[[str], object],
    max_length: int,
    expected_window_title: str = "",
) -> str:
    """Read a stable selection from a verified classic Outlook editor.

    Supported paths are an Inspector's WordEditor, an Explorer's active inline
    response, and a classic Explorer PreviewPane backed by a Word editor.  The
    new Outlook app, an unverified window, or unsupported/unstable COM state
    fails closed so callers can continue with UI Automation.
    """
    if max_length <= 0 or not expected_window_title:
        return ""
    try:
        outlook = get_active_object("Outlook.Application")
        active_editor = _outlook_active_word_editor(outlook, expected_window_title)
        if not active_editor:
            return ""
        document = active_editor.document

        word = getattr(document, "Application")
        selection = getattr(word, "Selection")
        identity = _outlook_selection_identity(selection)
        start, end, _story_type = identity
        if end <= start or end - start > max_length:
            return ""
        if not _outlook_selection_belongs_to_document(selection, document):
            return ""
        text = str(getattr(selection, "Text"))
        if not text or len(text) > max_length:
            return ""

        # Re-resolve the active Outlook surface, then re-read that editor's
        # selection.  This rejects a click/window switch racing the COM read.
        current_editor = _outlook_active_word_editor(outlook, expected_window_title)
        if not current_editor or current_editor.identity != active_editor.identity:
            return ""
        current_document = current_editor.document
        current_word = getattr(current_document, "Application")
        current_selection = getattr(current_word, "Selection")
        if _outlook_selection_identity(current_selection) != identity:
            return ""
        if not _outlook_selection_belongs_to_document(
            current_selection, current_document
        ):
            return ""
        return text
    except Exception:
        return ""


def _excel_collection_item(collection: object, index: int) -> object:
    item = getattr(collection, "Item")
    return item(index) if callable(item) else item[index]


def _excel_range_identity(selection: object) -> tuple[str, str, str]:
    """Return a read-only identity that changes with book, sheet, or range."""
    sheet = getattr(selection, "Parent")
    workbook = getattr(sheet, "Parent")
    return (
        str(getattr(workbook, "Name")),
        str(getattr(sheet, "Name")),
        str(getattr(selection, "Address")),
    )


def _excel_value_matrix(value: object, rows: int, columns: int) -> list[list[object]] | None:
    if rows == 1 and columns == 1:
        return [[value]]
    if not isinstance(value, (tuple, list)):
        return None
    outer = list(value)
    if len(outer) == rows and all(isinstance(row, (tuple, list)) for row in outer):
        matrix = [list(row) for row in outer]
        return matrix if all(len(row) == columns for row in matrix) else None
    # Some COM test doubles and automation bridges flatten one-dimensional ranges.
    if rows == 1 and len(outer) == columns:
        return [outer]
    if columns == 1 and len(outer) == rows:
        return [[item] for item in outer]
    return None


def _excel_cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def read_excel_selected_text(
    get_active_object: Callable[[str], object],
    max_length: int,
    expected_window_title: str = "",
    expected_window_handle: int = 0,
    *,
    max_cells: int = EXCEL_MAX_CAPTURE_CELLS,
) -> str:
    """Read the current foreground Excel cell selection without mutating Excel.

    Cell values are emitted row-by-row with tabs between columns and newlines
    between rows/areas. Oversized, non-range, changing, or unverified selections
    fail closed before their values are requested.
    """
    if max_length <= 0 or max_cells <= 0:
        return ""
    cell_limit = min(max_cells, max_length)
    try:
        excel = get_active_object("Excel.Application")
        if not bool(getattr(excel, "Ready")):
            # Excel commonly rejects automation while a formula is being edited.
            # Never substitute the whole active cell for a partial edit selection.
            return ""
        window = getattr(excel, "ActiveWindow")
        if not window or not _office_window_matches(
            window, expected_window_title, expected_window_handle
        ):
            return ""

        selection = getattr(excel, "Selection")
        if not selection:
            return ""
        selection_identity = _excel_range_identity(selection)
        areas = getattr(selection, "Areas")
        area_count = int(getattr(areas, "Count"))
        if area_count <= 0 or area_count > cell_limit:
            return ""

        # Size every area before reading Value2. This keeps a whole-column or
        # whole-sheet selection from materializing a huge SAFEARRAY in Python.
        area_layouts: list[tuple[object, int, int]] = []
        total_cells = 0
        for index in range(1, area_count + 1):
            area = _excel_collection_item(areas, index)
            count = int(getattr(area, "CountLarge"))
            if count <= 0 or total_cells + count > cell_limit:
                return ""
            rows = int(getattr(getattr(area, "Rows"), "Count"))
            columns = int(getattr(getattr(area, "Columns"), "Count"))
            if rows <= 0 or columns <= 0 or rows * columns != count:
                return ""
            total_cells += count
            area_layouts.append((area, rows, columns))

        area_texts: list[str] = []
        has_text = False
        rendered_length = 0
        for area, rows, columns in area_layouts:
            matrix = _excel_value_matrix(getattr(area, "Value2"), rows, columns)
            if matrix is None:
                return ""
            rendered_rows: list[str] = []
            for row in matrix:
                rendered_cells = [_excel_cell_text(value) for value in row]
                has_text = has_text or any(rendered_cells)
                rendered_row = "\t".join(rendered_cells)
                rendered_length += len(rendered_row)
                if rendered_rows:
                    rendered_length += 1
                if rendered_length > max_length:
                    return ""
                rendered_rows.append(rendered_row)
            rendered_area = "\n".join(rendered_rows)
            if area_texts:
                rendered_length += 1
                if rendered_length > max_length:
                    return ""
            area_texts.append(rendered_area)

        # Re-check readiness, foreground workbook window, and selection identity
        # so a concurrent click cannot publish values from the old range.
        if not bool(getattr(excel, "Ready")):
            return ""
        current_window = getattr(excel, "ActiveWindow")
        if not current_window or not _office_window_matches(
            current_window, expected_window_title, expected_window_handle
        ):
            return ""
        current_selection = getattr(excel, "Selection")
        if not current_selection or _excel_range_identity(current_selection) != selection_identity:
            return ""

        text = "\n".join(area_texts)
        return text if has_text and len(text) <= max_length else ""
    except Exception:
        # COM_E_CALL_REJECTED is expected during formula edit and busy states.
        return ""


def read_uia_selected_text(
    controls: Sequence[object],
    pattern_ids: Sequence[object],
    max_length: int,
    normalize: Callable[[str], str],
) -> tuple[str, bool]:
    """Probe focused/pointed UIA controls without relying on their rectangles."""
    if max_length <= 0:
        return "", False
    control_chains: list[list[object]] = []
    for control in controls:
        current = control
        chain: list[object] = []
        for _level in range(9):
            if not current:
                break
            chain.append(current)
            try:
                if bool(getattr(current, "IsPassword")):
                    return "", True
            except Exception:
                return "", True
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
                    rendered_length = 0
                    for range_index, text_range in enumerate(pattern.GetSelection() or []):
                        if range_index >= UIA_MAX_SELECTION_RANGES:
                            return "", False
                        raw_value = str(text_range.GetText(max_length + 1))
                        if len(raw_value) > max_length:
                            return "", False
                        value = normalize(raw_value)
                        if not value:
                            continue
                        rendered_length += len(value) + (1 if values else 0)
                        if rendered_length > max_length:
                            return "", False
                        values.append(value)
                    selected = "\n".join(values).strip()
                    if selected:
                        return selected, False
                except Exception:
                    continue
    return "", False


def read_uia_descendant_selected_text(
    root_control: object,
    pattern_ids: Sequence[object],
    max_length: int,
    normalize: Callable[[str], str],
    *,
    max_nodes: int = 160,
    max_depth: int = 12,
    timeout_seconds: float = 0.12,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[str, bool]:
    """Find a selected UIA text range below a window without reading its document.

    Chromium PDF viewers and some document applications expose their active
    ``TextPattern`` on a nested Document/Edit control rather than on the focused
    control.  This breadth-first probe is deliberately bounded by node count,
    depth, and elapsed time.  It only calls ``GetSelection`` and ``GetText`` on
    the returned selected ranges; it never requests ``DocumentRange`` or a
    control's full text/value.

    The boolean result is true when a password control was encountered.  In
    that case the whole probe fails closed, even if another descendant might
    expose text.
    """
    if (
        not root_control
        or max_length <= 0
        or max_nodes <= 0
        or max_depth < 0
        or timeout_seconds <= 0
    ):
        return "", False

    try:
        deadline = monotonic() + timeout_seconds
    except Exception:
        return "", False

    def expired() -> bool:
        try:
            return monotonic() >= deadline
        except Exception:
            return True

    queue: deque[tuple[object, int]] = deque([(root_control, 0)])
    scanned_nodes: list[object] = []
    queued_nodes = 1
    visited_nodes = 0

    while queue and visited_nodes < max_nodes:
        if expired():
            return "", False
        current, depth = queue.popleft()
        visited_nodes += 1

        try:
            is_password = bool(getattr(current, "IsPassword"))
        except Exception:
            return "", True
        if is_password:
            return "", True
        scanned_nodes.append(current)

        if depth >= max_depth or queued_nodes >= max_nodes or expired():
            continue
        try:
            child = current.GetFirstChildControl()
        except Exception:
            continue
        while child and queued_nodes < max_nodes:
            if expired():
                return "", False
            queue.append((child, depth + 1))
            queued_nodes += 1
            if queued_nodes >= max_nodes:
                break
            try:
                child = child.GetNextSiblingControl()
            except Exception:
                break

    for current in scanned_nodes:
        for pattern_id in pattern_ids:
            if expired():
                return "", False
            try:
                pattern = current.GetPattern(pattern_id)
                if not pattern:
                    continue
                selected_ranges = pattern.GetSelection() or ()
            except Exception:
                continue
            if expired():
                return "", False

            values: list[str] = []
            rendered_length = 0
            try:
                for range_index, text_range in enumerate(selected_ranges):
                    if range_index >= UIA_MAX_SELECTION_RANGES:
                        return "", False
                    if expired():
                        return "", False
                    raw_value = str(text_range.GetText(max_length + 1))
                    if expired():
                        return "", False
                    if len(raw_value) > max_length:
                        return "", False
                    value = normalize(raw_value)
                    if not value:
                        continue
                    rendered_length += len(value) + (1 if values else 0)
                    if rendered_length > max_length:
                        return "", False
                    values.append(value)
            except Exception:
                continue
            selected = "\n".join(values).strip()
            if selected:
                return selected, False

    return "", False


class ClipboardSnapshot(Protocol):
    def restore(self, expected_sequence: int) -> bool | str: ...

    def close(self) -> None: ...


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
    restore_plain_text: Callable[[str | None, int], bool | str],
    timeout_seconds: float,
    focus_is_current: Callable[[], bool] = lambda: True,
    clipboard_change_is_ours: Callable[[], bool | None] = lambda: True,
    external_copy_intent_detected: Callable[[], bool] = lambda: False,
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
    if old_sequence <= 0:
        return ClipboardCaptureResult(reason="sequence_unavailable")
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
        try:
            before = sequence_number()
        except Exception:
            reason = "sequence_unavailable"
            return ClipboardCaptureResult(reason=reason)
        if before <= 0:
            reason = "sequence_unavailable"
            return ClipboardCaptureResult(reason=reason)
        if before != old_sequence:
            reason = "concurrent_change"
            return ClipboardCaptureResult(reason=reason)
        try:
            focus_current = focus_is_current()
        except Exception:
            focus_current = False
        if not focus_current:
            reason = "focus_changed"
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
            if current_sequence <= 0:
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
                external_copy_intent = external_copy_intent_detected()
            except Exception:
                # If intent tracking itself fails, preserving the changed
                # clipboard is safer than overwriting a possible user copy.
                external_copy_intent = True
            if external_copy_intent:
                preserve_external_change = True
                reason = "concurrent_change"
                break
            try:
                change_is_ours = clipboard_change_is_ours()
            except Exception:
                change_is_ours = None
            if change_is_ours is False:
                preserve_external_change = True
                reason = "concurrent_change"
                break
            if change_is_ours is None:
                # The owner is the only reliable attribution signal here. A
                # clipboard manager or another process can update the board
                # without keyboard input, so an unknown owner must be preserved
                # rather than overwritten with our earlier snapshot.
                preserve_external_change = True
                cancelled_reason = "owner_unknown"
                reason = cancelled_reason
                break
            if cancelled_reason:
                reason = cancelled_reason
                break
            value = read_text()
            try:
                sequence_after_read = sequence_number()
            except Exception:
                if not cancelled_reason:
                    cancelled_reason = "sequence_unavailable"
                continue
            if sequence_after_read <= 0:
                if not cancelled_reason:
                    cancelled_reason = "sequence_unavailable"
                continue
            if sequence_after_read != current_sequence:
                # Clipboard contents changed while they were being read. Do
                # not translate that unstable value; inspect the newest owner
                # on the next pass, or preserve it if it belongs elsewhere.
                observed_sequence = sequence_after_read
                try:
                    external_copy_intent = external_copy_intent_detected()
                except Exception:
                    external_copy_intent = True
                if external_copy_intent:
                    preserve_external_change = True
                    reason = "concurrent_change"
                    break
                try:
                    newest_change_is_ours = clipboard_change_is_ours()
                except Exception:
                    newest_change_is_ours = None
                if newest_change_is_ours is False:
                    preserve_external_change = True
                    reason = "concurrent_change"
                    break
                if newest_change_is_ours is None:
                    preserve_external_change = True
                    cancelled_reason = "owner_unknown"
                    reason = cancelled_reason
                    break
                continue
            try:
                focus_current = focus_is_current()
            except Exception:
                focus_current = False
            if not focus_current:
                cancelled_reason = "focus_changed"
                reason = cancelled_reason
                break
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
                if latest_sequence is None or latest_sequence <= 0:
                    # Sequence-number compare-and-swap is unavailable, so do
                    # not risk replacing a newer clipboard value.
                    restored = True
                    reason = "external_change_preserved" if copied else "sequence_unavailable"
                elif latest_sequence != observed_sequence:
                    # A user or another application copied something after our read.
                    # Preserve that newer clipboard content instead of overwriting it.
                    restored = True
                    reason = "external_change_preserved" if copied else "concurrent_change"
                else:
                    # A physical Ctrl+C can arrive after the selected text was
                    # read but before this final restore. Check intent at the
                    # last practical moment so the user's copy always wins.
                    try:
                        late_external_copy_intent = external_copy_intent_detected()
                    except Exception:
                        late_external_copy_intent = True
                    if late_external_copy_intent:
                        preserve_external_change = True
                        restored = True
                        reason = (
                            "external_change_preserved" if copied else "concurrent_change"
                        )
                    elif snapshot is not None:
                        try:
                            restore_result = snapshot.restore(observed_sequence)
                            if restore_result == "preserved":
                                restored = True
                                reason = "external_change_preserved" if copied else "concurrent_change"
                            elif restore_result == "restored":
                                restored = True
                            elif restore_result == "failed":
                                restored = False
                            else:
                                restored = bool(restore_result)
                        except Exception:
                            restored = False
                    elif can_restore_as_plain_text:
                        try:
                            restore_result = restore_plain_text(old_text, observed_sequence)
                            if restore_result == "preserved":
                                restored = True
                                reason = "external_change_preserved" if copied else "concurrent_change"
                            elif restore_result == "restored":
                                restored = True
                            elif restore_result == "failed":
                                restored = False
                            else:
                                restored = bool(restore_result)
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
