from __future__ import annotations

import json
import os
import queue
import re
import shutil
import socket
import sqlite3
import sys
import threading
import time
import traceback
import ctypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import ctranslate2
import pythoncom
import sentencepiece as spm
import tkinter as tk
from tkinter import messagebox, ttk
import win32com.client


APP_NAME = "大声发划词翻译"
APP_DIR_NAME = "DaShengFaTranslator"
LEGACY_APP_DIR_NAME = "WordLocalTranslator"
MAX_SELECTION_LENGTH = 3000
WORD_PATTERN = re.compile(r"^[A-Za-z][A-Za-z'’-]*$")
CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")


def resource_path(name: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS"))
        packaged = base / "resources" / name
        return packaged if packaged.exists() else base / name
    return Path(__file__).resolve().parent / "resources" / name


def user_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / APP_DIR_NAME
    first_run_after_rename = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if first_run_after_rename:
        legacy = base / LEGACY_APP_DIR_NAME
        if legacy.exists():
            for filename in ("settings.json", "translation-cache.db"):
                source = legacy / filename
                target = path / filename
                if source.is_file() and not target.exists():
                    try:
                        shutil.copy2(source, target)
                    except OSError:
                        pass
    return path


LOG_PATH = user_data_dir() / "app.log"
SETTINGS_PATH = user_data_dir() / "settings.json"


def log(message: str) -> None:
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def normalize_selection(text: str) -> str:
    text = text.replace("\r", "\n").replace("\x07", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def contains_chinese(text: str) -> bool:
    return bool(CJK_PATTERN.search(text))


def load_settings() -> dict[str, object]:
    defaults: dict[str, object] = {"display_mode": "mini", "auto_translate": True}
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            defaults.update(saved)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return defaults


def save_settings(values: dict[str, object]) -> None:
    try:
        SETTINGS_PATH.write_text(
            json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        log(f"Settings save error: {exc}")


def cursor_position() -> tuple[int, int]:
    class Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = Point()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return int(point.x), int(point.y)


@dataclass(slots=True)
class TranslationResult:
    source: str
    translated: str
    source_language: str
    target_language: str
    engine: str
    phonetic: str = ""
    definition: str = ""
    elapsed_ms: int = 0


class TranslationCache:
    def __init__(self) -> None:
        self.path = user_data_dir() / "translation-cache.db"
        self.lock = threading.Lock()
        with sqlite3.connect(self.path) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS translations (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )

    @staticmethod
    def key(text: str, source_language: str, target_language: str) -> str:
        return f"{source_language}>{target_language}:{text}"

    def get(self, text: str, source_language: str, target_language: str) -> TranslationResult | None:
        key = self.key(text, source_language, target_language)
        with self.lock, sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT payload FROM translations WHERE cache_key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[0])
            data["engine"] = f"{data.get('engine', '本地模型')} · 缓存"
            data["elapsed_ms"] = 0
            return TranslationResult(**data)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def put(self, result: TranslationResult) -> None:
        key = self.key(result.source, result.source_language, result.target_language)
        payload = json.dumps(result.__dict__ if hasattr(result, "__dict__") else {
            "source": result.source,
            "translated": result.translated,
            "source_language": result.source_language,
            "target_language": result.target_language,
            "engine": result.engine,
            "phonetic": result.phonetic,
            "definition": result.definition,
            "elapsed_ms": result.elapsed_ms,
        }, ensure_ascii=False)
        with self.lock, sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO translations VALUES (?, ?, ?)",
                (key, payload, int(time.time())),
            )


class DictionaryLookup:
    def __init__(self) -> None:
        self.path = resource_path("ecdict.db")
        if not self.path.exists():
            raise FileNotFoundError(f"本地词典不存在：{self.path}")

    def lookup(self, word: str) -> tuple[str, str, str] | None:
        uri = self.path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            row = db.execute(
                "SELECT phonetic, translation, definition FROM entries WHERE word = ? COLLATE NOCASE",
                (word,),
            ).fetchone()
        if not row:
            return None
        phonetic, translation, definition = row
        if not translation and not definition:
            return None
        return phonetic or "", translation or "", definition or ""


class ModelPair:
    def __init__(self, folder_name: str) -> None:
        folder = resource_path(f"models/{folder_name}")
        model_dir = folder / "model"
        tokenizer_file = folder / "sentencepiece.model"
        if not model_dir.exists() or not tokenizer_file.exists():
            raise FileNotFoundError(f"翻译模型不完整：{folder}")
        self.translator = ctranslate2.Translator(
            str(model_dir),
            device="cpu",
            compute_type="int8",
            inter_threads=1,
            intra_threads=max(1, min(4, (os.cpu_count() or 2) // 2)),
        )
        # SentencePiece's Windows file loader can fail when the project path contains
        # Chinese characters. Loading the same model bytes avoids that path limitation.
        self.tokenizer = spm.SentencePieceProcessor(model_proto=tokenizer_file.read_bytes())

    def translate_batch(self, chunks: list[str]) -> list[str]:
        tokenized = [self.tokenizer.encode(chunk, out_type=str) for chunk in chunks]
        results = self.translator.translate_batch(
            tokenized,
            replace_unknowns=True,
            max_batch_size=16,
            batch_type="tokens",
            beam_size=4,
            num_hypotheses=1,
            length_penalty=0.2,
        )
        translated: list[str] = []
        for result in results:
            value = self.tokenizer.decode_pieces(result.hypotheses[0])
            value = value.replace("\u2581", " ").strip()
            value = re.sub(r"\s+([,.;:!?，。；：！？])", r"\1", value)
            translated.append(value)
        return translated


class LocalTranslator:
    def __init__(self, status_callback: Callable[[str], None]) -> None:
        self.status_callback = status_callback
        self.dictionary: DictionaryLookup | None = None
        self.models: dict[str, ModelPair] = {}
        self.cache = TranslationCache()

    def load(self) -> None:
        self.status_callback("正在加载本地词典…")
        self.dictionary = DictionaryLookup()
        self.status_callback("正在加载英译中模型…")
        self.models["en-zh"] = ModelPair("translate-en_zh-1_9")
        self.status_callback("本地引擎已就绪")

    @staticmethod
    def split_chunks(text: str, max_length: int = 420) -> list[str]:
        paragraphs = [part.strip() for part in re.split(r"[\r\n]+", text) if part.strip()]
        chunks: list[str] = []
        for paragraph in paragraphs or [text]:
            pieces = re.findall(r".*?(?:[.!?。！？]+(?=\s|$)|$)", paragraph)
            pieces = [piece.strip() for piece in pieces if piece.strip()]
            for piece in pieces or [paragraph]:
                while len(piece) > max_length:
                    cut = max(
                        piece.rfind(" ", 0, max_length),
                        piece.rfind("，", 0, max_length),
                        piece.rfind(",", 0, max_length),
                        piece.rfind("；", 0, max_length),
                        piece.rfind(";", 0, max_length),
                    )
                    if cut < max_length // 2:
                        cut = max_length
                    chunks.append(piece[:cut].strip())
                    piece = piece[cut:].strip()
                if piece:
                    chunks.append(piece)
        return chunks or [text]

    def translate(self, text: str) -> TranslationResult:
        started = time.perf_counter()
        text = normalize_selection(text)
        source_language, target_language = ("zh", "en") if contains_chinese(text) else ("en", "zh")

        cached = self.cache.get(text, source_language, target_language)
        if cached:
            return cached

        if source_language == "en" and WORD_PATTERN.fullmatch(text) and self.dictionary:
            entry = self.dictionary.lookup(text)
            if entry:
                phonetic, translation, definition = entry
                result = TranslationResult(
                    source=text,
                    translated=translation or definition,
                    source_language=source_language,
                    target_language=target_language,
                    engine="ECDICT 本地词典",
                    phonetic=phonetic,
                    definition=definition,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
                self.cache.put(result)
                return result

        pair_key = f"{source_language}-{target_language}"
        model = self.models.get(pair_key)
        if model is None:
            if pair_key == "zh-en":
                self.status_callback("首次加载中译英模型…")
                model = ModelPair("translate-zh_en-1_9")
                self.models[pair_key] = model
            else:
                raise RuntimeError("本地翻译模型尚未准备完成")
        chunks = self.split_chunks(text)
        values = model.translate_batch(chunks)
        separator = " " if target_language == "en" else ""
        result = TranslationResult(
            source=text,
            translated=separator.join(values),
            source_language=source_language,
            target_language=target_language,
            engine="Argos/OPUS 本地模型",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        self.cache.put(result)
        return result


class WordWatcher(threading.Thread):
    def __init__(
        self,
        selection_callback: Callable[[str], None],
        status_callback: Callable[[str], None],
    ) -> None:
        super().__init__(daemon=True, name="WordWatcher")
        self.selection_callback = selection_callback
        self.status_callback = status_callback
        self.stop_event = threading.Event()
        self.last_seen = ""
        self.last_emitted = ""
        self.changed_at = 0.0

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        pythoncom.CoInitialize()
        word = None
        connected = False
        try:
            while not self.stop_event.is_set():
                try:
                    if word is None:
                        word = win32com.client.GetActiveObject("Word.Application")
                        connected = True
                        self.status_callback("已连接 Word")

                    selection = word.Selection
                    length = int(selection.End) - int(selection.Start)
                    text = ""
                    if 0 < length <= MAX_SELECTION_LENGTH:
                        text = normalize_selection(str(selection.Text))

                    now = time.monotonic()
                    if text != self.last_seen:
                        self.last_seen = text
                        self.changed_at = now
                        if not text and self.last_emitted:
                            self.last_emitted = ""
                            self.selection_callback("")
                    elif (
                        text
                        and text != self.last_emitted
                        and now - self.changed_at >= 0.22
                    ):
                        self.last_emitted = text
                        self.selection_callback(text)
                    self.stop_event.wait(0.08)
                except Exception:
                    word = None
                    self.last_seen = ""
                    self.last_emitted = ""
                    if connected:
                        connected = False
                        self.status_callback("等待 Word 打开…")
                    self.stop_event.wait(1.0)
        finally:
            pythoncom.CoUninitialize()


class SpeechPlayer:
    VOICE_LANGUAGE = {"us": "409", "uk": "809"}

    def speak(self, text: str, accent: str) -> None:
        if not text.strip():
            return
        threading.Thread(
            target=self._speak_worker,
            args=(text, accent),
            daemon=True,
            name="SpeechPlayer",
        ).start()

    def _speak_worker(self, text: str, accent: str) -> None:
        pythoncom.CoInitialize()
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            language = self.VOICE_LANGUAGE[accent]
            tokens = voice.GetVoices()
            selected = None
            for index in range(tokens.Count):
                token = tokens.Item(index)
                if token.GetAttribute("Language").lower() == language:
                    selected = token
                    break
            if selected is None:
                raise RuntimeError("系统中没有对应的英语语音")
            voice.Voice = selected
            voice.Rate = -1
            voice.Speak(text)
        except Exception as exc:
            log(f"Speech error: {exc}\n{traceback.format_exc()}")
        finally:
            pythoncom.CoUninitialize()


class _LegacyTranslatorApp:
    BG = "#F4F6F8"
    CARD = "#FFFFFF"
    TEXT = "#17202A"
    MUTED = "#667085"
    BLUE = "#2563EB"
    GREEN = "#168A55"

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("540x650")
        self.root.minsize(470, 560)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.attributes("-topmost", True)

        self.auto_translate = tk.BooleanVar(value=True)
        self.word_status = tk.StringVar(value="正在连接 Word…")
        self.engine_status = tk.StringVar(value="正在启动本地引擎…")
        self.direction = tk.StringVar(value="英文 → 中文")
        self.meta = tk.StringVar(value="")
        self.phonetic = tk.StringVar(value="")

        self.current_result: TranslationResult | None = None
        self.request_queue: queue.Queue[str | None] = queue.Queue(maxsize=1)
        self.translator = LocalTranslator(self.set_engine_status)
        self.speech = SpeechPlayer()

        self._build_styles()
        self._build_ui()
        self._position_window()

        self.worker: threading.Thread | None = None
        self.watcher: WordWatcher | None = None

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Sub.TLabel", background=self.BG, foreground=self.MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Card.TFrame", background=self.CARD)
        style.configure("CardTitle.TLabel", background=self.CARD, foreground=self.MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Direction.TLabel", background=self.CARD, foreground=self.BLUE, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Status.TLabel", background=self.BG, foreground=self.MUTED, font=("Microsoft YaHei UI", 8))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(14, 7))
        style.configure("Tool.TButton", font=("Microsoft YaHei UI", 9), padding=(10, 6))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(20, 16, 20, 12))
        outer.pack(fill="both", expand=True)

        title_row = ttk.Frame(outer)
        title_row.pack(fill="x")
        ttk.Label(title_row, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(title_row, text="● 本地模式", foreground=self.GREEN, background=self.BG, font=("Microsoft YaHei UI", 9, "bold")).pack(side="right", pady=(6, 0))
        ttk.Label(outer, text="在 Word 中双击单词，或拖动选中短语和句子", style="Sub.TLabel").pack(anchor="w", pady=(2, 14))

        source_card = ttk.Frame(outer, style="Card.TFrame", padding=14)
        source_card.pack(fill="x", pady=(0, 10))
        source_header = ttk.Frame(source_card, style="Card.TFrame")
        source_header.pack(fill="x")
        ttk.Label(source_header, textvariable=self.direction, style="Direction.TLabel").pack(side="left")
        ttk.Label(source_header, text="选中的原文", style="CardTitle.TLabel").pack(side="right")
        self.source_text = tk.Text(
            source_card,
            height=5,
            wrap="word",
            relief="flat",
            borderwidth=0,
            bg=self.CARD,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            font=("Segoe UI", 11),
            padx=0,
            pady=10,
        )
        self.source_text.pack(fill="x")
        ttk.Button(source_card, text="翻译当前文字", style="Accent.TButton", command=self.translate_manual).pack(anchor="e")

        result_card = ttk.Frame(outer, style="Card.TFrame", padding=14)
        result_card.pack(fill="both", expand=True, pady=(0, 10))
        result_header = ttk.Frame(result_card, style="Card.TFrame")
        result_header.pack(fill="x")
        ttk.Label(result_header, text="翻译结果", style="CardTitle.TLabel").pack(side="left")
        ttk.Label(result_header, textvariable=self.meta, style="CardTitle.TLabel").pack(side="right")
        ttk.Label(result_card, textvariable=self.phonetic, background=self.CARD, foreground=self.BLUE, font=("Segoe UI", 11)).pack(anchor="w", pady=(7, 0))
        self.result_text = tk.Text(
            result_card,
            height=10,
            wrap="word",
            relief="flat",
            borderwidth=0,
            bg=self.CARD,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            font=("Microsoft YaHei UI", 11),
            padx=0,
            pady=8,
        )
        self.result_text.pack(fill="both", expand=True)
        self.result_text.insert("1.0", "本地引擎准备好后，翻译会显示在这里。")
        self.result_text.configure(state="disabled")

        tools = ttk.Frame(outer)
        tools.pack(fill="x", pady=(0, 8))
        ttk.Button(tools, text="🔊 美音", style="Tool.TButton", command=lambda: self.speak("us")).pack(side="left")
        ttk.Button(tools, text="🔊 英音", style="Tool.TButton", command=lambda: self.speak("uk")).pack(side="left", padx=(8, 0))
        ttk.Button(tools, text="复制译文", style="Tool.TButton", command=self.copy_translation).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(tools, text="自动翻译", variable=self.auto_translate).pack(side="right")

        footer = ttk.Frame(outer)
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.word_status, style="Status.TLabel").pack(side="left")
        ttk.Label(footer, textvariable=self.engine_status, style="Status.TLabel").pack(side="right")

    def _position_window(self) -> None:
        self.root.update_idletasks()
        width = self.root.winfo_width()
        screen_width = self.root.winfo_screenwidth()
        self.root.geometry(f"+{max(20, screen_width - width - 30)}+70")

    def set_word_status(self, value: str) -> None:
        self.root.after(0, self.word_status.set, value)

    def set_engine_status(self, value: str) -> None:
        self.root.after(0, self.engine_status.set, value)

    def on_word_selection(self, text: str) -> None:
        self.root.after(0, self._handle_word_selection, text)

    def _handle_word_selection(self, text: str) -> None:
        if not self.auto_translate.get():
            return
        self._show_source_and_enqueue(text)

    def _show_source_and_enqueue(self, text: str) -> None:
        self.source_text.delete("1.0", "end")
        self.source_text.insert("1.0", text)
        self.enqueue(text)

    def translate_manual(self) -> None:
        text = normalize_selection(self.source_text.get("1.0", "end"))
        if text:
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
        self.engine_status.set("正在翻译…")

    def _translation_worker(self) -> None:
        try:
            self.translator.load()
        except Exception as exc:
            log(f"Engine load error: {exc}\n{traceback.format_exc()}")
            self.set_engine_status("本地引擎加载失败")
            self.root.after(0, messagebox.showerror, APP_NAME, f"本地翻译引擎加载失败：\n{exc}\n\n日志：{LOG_PATH}")
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

    def show_result(self, result: TranslationResult) -> None:
        self.current_result = result
        self.direction.set("中文 → 英文" if result.source_language == "zh" else "英文 → 中文")
        self.phonetic.set(f"/{result.phonetic}/" if result.phonetic else "")
        content = result.translated
        if result.definition and result.definition.strip() and result.definition.strip() != result.translated.strip():
            content += f"\n\n英文释义\n{result.definition}"
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        self.result_text.insert("1.0", content)
        self.result_text.configure(state="disabled")
        self.meta.set(f"{result.engine} · {result.elapsed_ms} ms")
        self.engine_status.set("本地引擎已就绪")

    def show_error(self, message: str) -> None:
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        self.result_text.insert("1.0", f"翻译失败：{message}")
        self.result_text.configure(state="disabled")
        self.engine_status.set("翻译失败")

    def english_text(self) -> str:
        if self.current_result:
            return self.current_result.source if self.current_result.source_language == "en" else self.current_result.translated
        text = normalize_selection(self.source_text.get("1.0", "end"))
        return text if not contains_chinese(text) else ""

    def speak(self, accent: str) -> None:
        text = self.english_text()
        if text:
            self.speech.speak(text, accent)

    def copy_translation(self) -> None:
        if not self.current_result:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.current_result.translated)
        self.engine_status.set("译文已复制")
        self.root.after(1200, self.engine_status.set, "本地引擎已就绪")

    def close(self) -> None:
        if self.watcher:
            self.watcher.stop()
        try:
            self.request_queue.put_nowait(None)
        except queue.Full:
            pass
        self.root.destroy()

    def run(self) -> None:
        self.root.after(0, self._start_background_tasks)
        self.root.mainloop()

    def _start_background_tasks(self) -> None:
        self.worker = threading.Thread(target=self._translation_worker, daemon=True)
        self.worker.start()
        self.watcher = WordWatcher(self.on_word_selection, self.set_word_status)
        self.watcher.start()


class TranslatorApp:
    BG = "#F4F6F8"
    CARD = "#FFFFFF"
    TEXT = "#17202A"
    MUTED = "#667085"
    BLUE = "#2563EB"
    GREEN = "#168A55"

    def __init__(self) -> None:
        settings = load_settings()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("570x650")
        self.root.minsize(500, 650)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.attributes("-topmost", True)

        self.display_mode = tk.StringVar(value=str(settings.get("display_mode", "mini")))
        if self.display_mode.get() not in {"mini", "panel"}:
            self.display_mode.set("mini")
        self.auto_translate = tk.BooleanVar(value=bool(settings.get("auto_translate", True)))
        self.word_status = tk.StringVar(value="正在连接 Word…")
        self.engine_status = tk.StringVar(value="正在启动本地引擎…")
        self.direction = tk.StringVar(value="英文 → 中文")
        self.meta = tk.StringVar(value="")
        self.phonetic = tk.StringVar(value="音标会显示在这里")
        self.mini_source = tk.StringVar(value="")
        self.mini_phonetic = tk.StringVar(value="")
        self.mini_translation = tk.StringVar(value="")

        self.current_result: TranslationResult | None = None
        self.active_source = ""
        self.request_queue: queue.Queue[str | None] = queue.Queue(maxsize=1)
        self.translator = LocalTranslator(self.set_engine_status)
        self.speech = SpeechPlayer()
        self.worker: threading.Thread | None = None
        self.watcher: WordWatcher | None = None

        self._build_styles()
        self._build_panel_ui()
        self._build_mini_ui()
        self._position_panel()
        if self.display_mode.get() == "mini":
            self.root.withdraw()

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Sub.TLabel", background=self.BG, foreground=self.MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Card.TFrame", background=self.CARD)
        style.configure("CardTitle.TLabel", background=self.CARD, foreground=self.MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Direction.TLabel", background=self.CARD, foreground=self.BLUE, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Phonetic.TLabel", background=self.CARD, foreground=self.BLUE, font=("Segoe UI", 12, "bold"))
        style.configure("Status.TLabel", background=self.BG, foreground=self.MUTED, font=("Microsoft YaHei UI", 8))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(12, 7))
        style.configure("Tool.TButton", font=("Microsoft YaHei UI", 9), padding=(9, 6))

    def _build_panel_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(20, 15, 20, 11))
        outer.pack(fill="both", expand=True)

        title_row = ttk.Frame(outer)
        title_row.pack(fill="x")
        ttk.Label(title_row, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(title_row, text="● 本地模式", foreground=self.GREEN, background=self.BG, font=("Microsoft YaHei UI", 9, "bold")).pack(side="right", pady=(6, 0))
        ttk.Label(outer, text="在 Word 中双击单词，或拖动选中短语和句子", style="Sub.TLabel").pack(anchor="w", pady=(1, 7))

        mode_row = ttk.Frame(outer)
        mode_row.pack(fill="x", pady=(0, 9))
        ttk.Label(mode_row, text="显示方式：", style="Sub.TLabel").pack(side="left")
        ttk.Radiobutton(mode_row, text="迷你浮窗", value="mini", variable=self.display_mode, command=self.change_mode).pack(side="left")
        ttk.Radiobutton(mode_row, text="大窗口", value="panel", variable=self.display_mode, command=self.change_mode).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(mode_row, text="自动翻译", variable=self.auto_translate, command=self.save_preferences).pack(side="right")

        source_card = ttk.Frame(outer, style="Card.TFrame", padding=13)
        source_card.pack(fill="x", pady=(0, 10))
        source_header = ttk.Frame(source_card, style="Card.TFrame")
        source_header.pack(fill="x")
        ttk.Label(source_header, textvariable=self.direction, style="Direction.TLabel").pack(side="left")
        ttk.Label(source_header, text="选中的原文", style="CardTitle.TLabel").pack(side="right")
        self.source_text = tk.Text(
            source_card,
            height=4,
            wrap="word",
            relief="flat",
            borderwidth=0,
            bg=self.CARD,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            font=("Segoe UI", 11),
            padx=0,
            pady=8,
        )
        self.source_text.pack(fill="x")

        pronunciation = ttk.Frame(source_card, style="Card.TFrame")
        pronunciation.pack(fill="x", pady=(1, 0))
        pronunciation.columnconfigure(0, weight=1)
        ttk.Label(pronunciation, textvariable=self.phonetic, style="Phonetic.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Button(pronunciation, text="🔊 美音", style="Tool.TButton", command=lambda: self.speak("us")).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(pronunciation, text="🔊 英音", style="Tool.TButton", command=lambda: self.speak("uk")).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(pronunciation, text="翻译", style="Accent.TButton", command=self.translate_manual).grid(row=0, column=3)

        result_card = ttk.Frame(outer, style="Card.TFrame", padding=13)
        result_card.pack(fill="both", expand=True, pady=(0, 9))
        result_header = ttk.Frame(result_card, style="Card.TFrame")
        result_header.pack(fill="x")
        ttk.Label(result_header, text="翻译结果", style="CardTitle.TLabel").pack(side="left")
        ttk.Label(result_header, textvariable=self.meta, style="CardTitle.TLabel").pack(side="right")
        self.result_text = tk.Text(
            result_card,
            height=9,
            wrap="word",
            relief="flat",
            borderwidth=0,
            bg=self.CARD,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            font=("Microsoft YaHei UI", 11),
            padx=0,
            pady=9,
        )
        self.result_text.pack(fill="both", expand=True)
        self.result_text.insert("1.0", "本地引擎准备好后，翻译会显示在这里。")
        self.result_text.configure(state="disabled")

        bottom_tools = ttk.Frame(outer)
        bottom_tools.pack(fill="x", pady=(0, 7))
        ttk.Button(bottom_tools, text="复制译文", style="Tool.TButton", command=self.copy_translation).pack(side="left")
        ttk.Button(bottom_tools, text="隐藏窗口", style="Tool.TButton", command=self.hide_panel).pack(side="left", padx=(7, 0))

        footer = ttk.Frame(outer)
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.word_status, style="Status.TLabel").pack(side="left")
        ttk.Label(footer, textvariable=self.engine_status, style="Status.TLabel").pack(side="right")

    def _build_mini_ui(self) -> None:
        self.mini = tk.Toplevel(self.root)
        self.mini.withdraw()
        self.mini.overrideredirect(True)
        self.mini.attributes("-topmost", True)
        self.mini.configure(bg="#CBD5E1")

        card = tk.Frame(self.mini, bg=self.CARD, highlightthickness=1, highlightbackground="#CBD5E1")
        card.pack(fill="both", expand=True)

        top = tk.Frame(card, bg=self.CARD)
        top.pack(fill="x", padx=(11, 5), pady=(7, 3))
        tk.Button(top, text="×", command=self.hide_mini, relief="flat", bg=self.CARD, activebackground="#FEE2E2", fg=self.MUTED, font=("Segoe UI", 12), padx=5, pady=0).pack(side="right", anchor="n")
        sound_row = tk.Frame(top, bg=self.CARD)
        sound_row.pack(side="right", anchor="n", padx=(4, 3))
        tk.Button(sound_row, text="🔊 美", command=lambda: self.speak("us"), relief="flat", bg="#E8F0FE", activebackground="#DCE8FC", fg=self.BLUE, font=("Microsoft YaHei UI", 9), padx=7, pady=3).pack(side="left", padx=(0, 4))
        tk.Button(sound_row, text="🔊 英", command=lambda: self.speak("uk"), relief="flat", bg="#E8F0FE", activebackground="#DCE8FC", fg=self.BLUE, font=("Microsoft YaHei UI", 9), padx=7, pady=3).pack(side="left")
        source_block = tk.Frame(top, bg=self.CARD)
        source_block.pack(side="left", fill="x", expand=True, anchor="n")
        tk.Label(source_block, textvariable=self.mini_source, bg=self.CARD, fg=self.TEXT, anchor="w", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(source_block, textvariable=self.mini_phonetic, bg=self.CARD, fg=self.BLUE, anchor="w", font=("Segoe UI", 10, "bold")).pack(anchor="w")

        tk.Frame(card, bg="#E5E7EB", height=1).pack(fill="x", padx=10)
        translation = tk.Label(card, textvariable=self.mini_translation, bg=self.CARD, fg=self.TEXT, justify="left", anchor="nw", wraplength=350, font=("Microsoft YaHei UI", 10))
        translation.pack(fill="both", expand=True, padx=11, pady=(7, 0))
        tk.Button(card, text="展开大窗口 ›", command=self.expand_panel, relief="flat", bg=self.CARD, activebackground="#F3F4F6", fg=self.MUTED, font=("Microsoft YaHei UI", 8), padx=2, pady=1).pack(anchor="e", padx=9, pady=(0, 4))

    def _position_panel(self) -> None:
        self.root.update_idletasks()
        width = self.root.winfo_width()
        screen_width = self.root.winfo_screenwidth()
        self.root.geometry(f"+{max(20, screen_width - width - 28)}+55")

    def position_mini(self) -> None:
        self.mini.update_idletasks()
        width, height = 390, 150
        pointer_x = self.root.winfo_pointerx()
        pointer_y = self.root.winfo_pointery()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = min(max(8, pointer_x + 14), max(8, screen_width - width - 8))
        y = pointer_y + 18
        if y + height > screen_height - 8:
            y = max(8, pointer_y - height - 14)
        self.mini.geometry(f"{width}x{height}+{x}+{y}")

    def save_preferences(self) -> None:
        save_settings({"display_mode": self.display_mode.get(), "auto_translate": self.auto_translate.get()})

    def change_mode(self) -> None:
        self.save_preferences()
        if self.display_mode.get() == "panel":
            self.hide_mini()
            self.root.deiconify()
            self.root.lift()
        else:
            self.root.withdraw()
            if self.current_result:
                self.update_mini_result(self.current_result)
                self.position_mini()
                self.mini.deiconify()
                self.mini.lift()

    def expand_panel(self) -> None:
        self.display_mode.set("panel")
        self.change_mode()

    def hide_panel(self) -> None:
        self.display_mode.set("mini")
        self.change_mode()

    def hide_mini(self) -> None:
        self.mini.withdraw()

    def set_word_status(self, value: str) -> None:
        self.root.after(0, self.word_status.set, value)

    def set_engine_status(self, value: str) -> None:
        self.root.after(0, self.engine_status.set, value)

    def on_word_selection(self, text: str) -> None:
        self.root.after(0, self._handle_word_selection, text)

    def _handle_word_selection(self, text: str) -> None:
        if not text:
            self.hide_mini()
            return
        if not self.auto_translate.get():
            return
        self.active_source = text
        self.source_text.delete("1.0", "end")
        self.source_text.insert("1.0", text)
        self.direction.set("中文 → 英文" if contains_chinese(text) else "英文 → 中文")
        self.phonetic.set("正在查询音标…" if WORD_PATTERN.fullmatch(text) else "可直接播放整句发音")
        if self.display_mode.get() == "mini":
            self.show_mini_loading(text)
        else:
            self.root.deiconify()
        self.enqueue(text)

    def show_mini_loading(self, text: str) -> None:
        self.mini_source.set(text if len(text) <= 46 else text[:45] + "…")
        self.mini_phonetic.set("正在查询音标…" if WORD_PATTERN.fullmatch(text) else "")
        self.mini_translation.set("正在本地翻译…")
        self.position_mini()
        self.mini.deiconify()
        self.mini.lift()

    def translate_manual(self) -> None:
        text = normalize_selection(self.source_text.get("1.0", "end"))
        if text:
            self.active_source = text
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
        self.engine_status.set("正在翻译…")

    def _translation_worker(self) -> None:
        try:
            self.translator.load()
        except Exception as exc:
            log(f"Engine load error: {exc}\n{traceback.format_exc()}")
            self.set_engine_status("本地引擎加载失败")
            self.root.after(0, messagebox.showerror, APP_NAME, f"本地翻译引擎加载失败：\n{exc}\n\n日志：{LOG_PATH}")
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
    def compact_translation(text: str, limit: int = 115) -> str:
        parts = [part.strip() for part in text.splitlines() if part.strip()]
        value = "；".join(parts[:2]) if parts else text.strip()
        return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"

    def update_mini_result(self, result: TranslationResult) -> None:
        source = result.source if len(result.source) <= 46 else result.source[:45] + "…"
        self.mini_source.set(source)
        self.mini_phonetic.set(f"/{result.phonetic}/" if result.phonetic else "")
        self.mini_translation.set(self.compact_translation(result.translated))

    def show_result(self, result: TranslationResult) -> None:
        if result.source != self.active_source:
            return
        self.current_result = result
        self.direction.set("中文 → 英文" if result.source_language == "zh" else "英文 → 中文")
        self.phonetic.set(f"/{result.phonetic}/" if result.phonetic else "英语发音")
        content = result.translated
        if result.definition and result.definition.strip() and result.definition.strip() != result.translated.strip():
            content += f"\n\n英文释义\n{result.definition}"
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        self.result_text.insert("1.0", content)
        self.result_text.configure(state="disabled")
        self.meta.set(f"{result.engine} · {result.elapsed_ms} ms")
        self.engine_status.set("本地引擎已就绪")
        if self.display_mode.get() == "mini":
            self.update_mini_result(result)
            self.position_mini()
            self.mini.deiconify()
            self.mini.lift()

    def show_error(self, message: str) -> None:
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        self.result_text.insert("1.0", f"翻译失败：{message}")
        self.result_text.configure(state="disabled")
        self.mini_translation.set(f"翻译失败：{message}")
        self.engine_status.set("翻译失败")

    def english_text(self) -> str:
        if self.active_source and not contains_chinese(self.active_source):
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
        self.engine_status.set("译文已复制")
        self.root.after(1200, self.engine_status.set, "本地引擎已就绪")

    def close(self) -> None:
        if self.watcher:
            self.watcher.stop()
        try:
            self.request_queue.put_nowait(None)
        except queue.Full:
            pass
        self.mini.destroy()
        self.root.destroy()

    def run(self) -> None:
        self.root.after(0, self._start_background_tasks)
        self.root.mainloop()

    def _start_background_tasks(self) -> None:
        self.worker = threading.Thread(target=self._translation_worker, daemon=True)
        self.worker.start()
        self.watcher = WordWatcher(self.on_word_selection, self.set_word_status)
        self.watcher.start()


def acquire_single_instance() -> socket.socket | None:
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind(("127.0.0.1", 39047))
        guard.listen(1)
        return guard
    except OSError:
        guard.close()
        return None


def main() -> None:
    guard = acquire_single_instance()
    if guard is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, "大声发划词翻译已经在运行。")
        root.destroy()
        return
    try:
        TranslatorApp().run()
    except Exception as exc:
        log(f"Fatal error: {exc}\n{traceback.format_exc()}")
        raise
    finally:
        guard.close()


if __name__ == "__main__":
    main()
