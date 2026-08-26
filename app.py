from __future__ import annotations

import hashlib
import inspect
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
import tempfile
import wave
import winsound
from collections import OrderedDict, deque
from contextlib import closing
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
    path.mkdir(parents=True, exist_ok=True)
    marker = path / ".legacy-migration-complete"
    legacy = base / LEGACY_APP_DIR_NAME
    if legacy.exists() and not marker.exists():
        migration_complete = True
        for filename in ("settings.json", "translation-cache.db"):
            source = legacy / filename
            target = path / filename
            if source.is_file() and not target.exists():
                try:
                    shutil.copy2(source, target)
                except OSError:
                    migration_complete = False
        if migration_complete:
            try:
                marker.write_text("completed\n", encoding="ascii")
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
    # Normalise CRLF as one line break.  Replacing bare ``\r`` first would
    # turn every Windows line ending into two blank lines.
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x07", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_dictionary_text(value: object) -> str:
    """Decode ECDICT's literal line-break separators without touching prose."""

    text = "" if value is None else str(value)
    return text.replace(r"\r\n", "\n").replace(r"\n", "\n").replace(r"\r", "\n")


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
        with closing(sqlite3.connect(self.path)) as db:
            with db:
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
        with self.lock, closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT payload FROM translations WHERE cache_key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[0])
            if str(data.get("engine", "")).startswith("ECDICT"):
                data["translated"] = normalize_dictionary_text(data.get("translated", ""))
                data["definition"] = normalize_dictionary_text(data.get("definition", ""))
                # Older builds cached the English definition as if it were a
                # Chinese translation when ECDICT's translation field was
                # empty.  Ignore that stale value so the local model can
                # replace it with an actual translation.
                if (
                    str(data.get("target_language", "")) == "zh"
                    and not contains_chinese(str(data.get("translated", "")))
                ):
                    return None
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
        with self.lock, closing(sqlite3.connect(self.path)) as db:
            with db:
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
        with closing(sqlite3.connect(uri, uri=True)) as db:
            row = db.execute(
                "SELECT phonetic, translation, definition FROM entries WHERE word = ? COLLATE NOCASE",
                (word,),
            ).fetchone()
        if not row:
            return None
        phonetic, translation, definition = row
        translation = normalize_dictionary_text(translation)
        definition = normalize_dictionary_text(definition)
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

        dictionary_metadata: tuple[str, str] | None = None
        if source_language == "en" and WORD_PATTERN.fullmatch(text) and self.dictionary:
            entry = self.dictionary.lookup(text)
            if entry:
                phonetic, translation, definition = entry
                if translation.strip():
                    result = TranslationResult(
                        source=text,
                        translated=translation,
                        source_language=source_language,
                        target_language=target_language,
                        engine="ECDICT 本地词典",
                        phonetic=phonetic,
                        definition=definition,
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                    )
                    self.cache.put(result)
                    return result
                dictionary_metadata = (phonetic, definition)

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
            phonetic=dictionary_metadata[0] if dictionary_metadata else "",
            definition=dictionary_metadata[1] if dictionary_metadata else "",
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


class KokoroSpeechUnavailable(RuntimeError):
    """Raised when the optional local neural voice cannot be used safely."""


class PiperSpeechUnavailable(RuntimeError):
    """Raised when the bundled local Piper word voices cannot be used safely."""


class BackgroundSpeechYield(RuntimeError):
    """Raised when low-priority neural work must yield to a real user request."""


NATURAL_SPEECH_SPEEDS = {
    "slow": 0.75,
    "standard": 0.85,
    "fast": 1.0,
}
MELO_SPEECH_SPEEDS = {
    "slow": 1.0,
    "standard": 1.0,
    "fast": 1.0,
}
DEFAULT_NATURAL_SPEECH_SPEED = "standard"
AUTO_SPEECH_PREFERENCES = frozenset({"speed", "natural"})
DEFAULT_AUTO_SPEECH_PREFERENCE = "speed"
KOKORO_CPU_THREADS = 4
MELO_CPU_THREADS = min(6, max(2, os.cpu_count() or 4))


def natural_speech_speed_value(profile: object) -> float:
    """Resolve a saved user profile without ever exceeding the old fast rate."""

    return NATURAL_SPEECH_SPEEDS.get(
        str(profile or "").strip().lower(),
        NATURAL_SPEECH_SPEEDS[DEFAULT_NATURAL_SPEECH_SPEED],
    )


def piper_speech_speed_value(profile: object) -> float:
    """Keep the bundled Piper word voices at their native rate."""

    return MELO_SPEECH_SPEEDS.get(
        str(profile or "").strip().lower(),
        MELO_SPEECH_SPEEDS[DEFAULT_NATURAL_SPEECH_SPEED],
    )


def normalize_auto_speech_preference(value: object) -> str:
    """Return the persisted automatic-speech policy, migrating old settings."""

    normalized = str(value or "").strip().lower()
    if normalized not in AUTO_SPEECH_PREFERENCES:
        return DEFAULT_AUTO_SPEECH_PREFERENCE
    return normalized


def speech_mode_for_gesture(preference: object, gesture: str) -> str:
    """Map one finalized user gesture to exactly one audible backend family."""

    normalized = normalize_auto_speech_preference(preference)
    if gesture == "double":
        return "natural" if normalized == "speed" else "system"
    if gesture in {"single", "auto"}:
        return "system" if normalized == "speed" else "natural"
    raise ValueError(f"Unsupported pronunciation gesture: {gesture}")


class PiperSpeechBackend:
    """Local CPU Piper voice synthesis for individual English words."""

    RESOURCE_ROOT = "models/melo"
    STAGING_VERSION = "v0.8.28-en-v2"
    MODEL_FILENAME = "melotts-en-v2-f16.gguf"
    BERT_FILENAME = "bert-base-uncased.gguf"
    RUNTIME_FILENAMES = (
        "crispasr.dll",
        "ggml-base.dll",
        "ggml-cpu.dll",
        "ggml.dll",
    )
    RESOURCE_SHA256 = {
        "melotts-en-v2-f16.gguf": "68c82ea2a18fd8e9d01c37a286a17fcfa630dfe7dbbd0bd08e7454e313e78fd8",
        "bert-base-uncased.gguf": "420f467192804606a067c886274eb4a689f0d776f81afdee3939e51d9eda8478",
        "runtime/crispasr.dll": "c7c25027f2e6a670bfe61e421b7eb637a5f1c1887386062007705d4d8e9c0ae7",
        "runtime/ggml-base.dll": "4a8cc5e28a57f18f26d5ce584cf7cc31f6077adec6abc25fd7ac74da333c7870",
        "runtime/ggml-cpu.dll": "bd109303025d408d0a775a94ae3f856739df765b122cb18137d8c608fd07318d",
        "runtime/ggml.dll": "2b297d595e6f99a45ca5cff74ccb70974b90401fa657482a0f22eb11e7200e5a",
    }
    SPEAKERS = {"us": 0, "uk": 1}
    # CrispASR v0.8.28's Piper session always resamples its returned PCM to
    # 24 kHz, while its output-sample-rate getter incorrectly reports the
    # model's pre-resample 44.1 kHz rate.  This runtime is hash-pinned above,
    # so use the session PCM contract directly and never speed up its samples.
    SESSION_PCM_SAMPLE_RATE = 24_000
    CACHE_MAX_ENTRIES = 64
    CACHE_MAX_AUDIO_BYTES = 1_000_000

    def __init__(self) -> None:
        self._library: object | None = None
        self._session: object | None = None
        self._dll_directory_handle: object | None = None
        self._session_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._next_file_id = 0
        self._audio_cache: OrderedDict[
            tuple[str, str], tuple[bytes, int, float]
        ] = OrderedDict()

    @classmethod
    def _required_resources(cls) -> tuple[tuple[str, ...], ...]:
        return (
            (cls.MODEL_FILENAME,),
            (cls.BERT_FILENAME,),
            *(("runtime", filename) for filename in cls.RUNTIME_FILENAMES),
        )

    @staticmethod
    def _windows_short_path(path_text: str) -> str | None:
        if os.name != "nt":
            return None
        try:
            get_short_path = ctypes.windll.kernel32.GetShortPathNameW
            get_short_path.argtypes = (
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_uint32,
            )
            get_short_path.restype = ctypes.c_uint32
            capacity = max(32_768, len(path_text) + 1)
            buffer = ctypes.create_unicode_buffer(capacity)
            length = int(get_short_path(path_text, buffer, capacity))
            if 0 < length < capacity:
                return buffer.value
        except (AttributeError, OSError, ValueError):
            pass
        return None

    @classmethod
    def _ascii_native_path(cls, path: Path) -> bytes:
        """Encode a path only when the native narrow-path ABI can open it."""

        resolved = str(path.resolve())
        try:
            return resolved.encode("ascii")
        except UnicodeEncodeError:
            if path.exists():
                short_path = cls._windows_short_path(resolved)
                if short_path:
                    try:
                        return short_path.encode("ascii")
                    except UnicodeEncodeError:
                        pass
            raise PiperSpeechUnavailable(
                "Piper native runtime path has no safe ASCII representation"
            )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _expected_digest(cls, relative: tuple[str, ...]) -> str:
        key = "/".join(relative)
        try:
            return cls.RESOURCE_SHA256[key]
        except KeyError as exc:
            raise PiperSpeechUnavailable(
                f"Piper resource has no integrity record: {key}"
            ) from exc

    @staticmethod
    def _same_file(first: Path, second: Path) -> bool:
        try:
            return first.is_file() and second.is_file() and os.path.samefile(
                first, second
            )
        except OSError:
            return False

    @classmethod
    def _prepare_runtime_files(cls) -> Path:
        """Copy native inputs to an ASCII LocalAppData tree, file by file."""

        source_root = resource_path(cls.RESOURCE_ROOT)
        required = cls._required_resources()
        for relative in required:
            source = source_root.joinpath(*relative)
            if not source.is_file() or source.stat().st_size <= 0:
                raise PiperSpeechUnavailable(
                    f"Piper resource is missing or empty: {'/'.join(relative)}"
                )
            expected_digest = cls._expected_digest(relative)
            try:
                actual_digest = cls._sha256_file(source)
            except OSError as exc:
                raise PiperSpeechUnavailable(
                    f"Could not verify Piper resource: {'/'.join(relative)}"
                ) from exc
            if actual_digest != expected_digest:
                raise PiperSpeechUnavailable(
                    f"Piper resource integrity check failed: {'/'.join(relative)}"
                )

        # Installed builds in a normal ASCII path can load the packaged files
        # in place.  Avoid copying roughly 151 MB unless the narrow C ABI
        # genuinely cannot represent the source tree.
        try:
            cls._ascii_native_path(source_root)
            for relative in required:
                cls._ascii_native_path(source_root.joinpath(*relative))
        except PiperSpeechUnavailable:
            pass
        else:
            return source_root

        destination_root = (
            user_data_dir() / "runtime" / "melo" / cls.STAGING_VERSION
        )
        destination_root.mkdir(parents=True, exist_ok=True)
        for relative in required:
            source = source_root.joinpath(*relative)
            destination = destination_root.joinpath(*relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                if (
                    destination.is_file()
                    and destination.stat().st_size == source.stat().st_size
                ):
                    if cls._same_file(source, destination):
                        continue
                    if cls._sha256_file(destination) == cls._expected_digest(relative):
                        continue
            except OSError:
                pass
            staging = destination.with_name(
                f"{destination.name}.staging-{os.getpid()}-{threading.get_ident()}"
            )
            try:
                staging.unlink(missing_ok=True)
                linked = False
                try:
                    os.link(source, staging)
                    linked = cls._same_file(source, staging)
                except OSError:
                    linked = False
                if not linked:
                    staging.unlink(missing_ok=True)
                    # Cross-volume installs and filesystems without hardlink
                    # support still get the verified safe copy path.
                    shutil.copy2(source, staging)
                if staging.stat().st_size != source.stat().st_size:
                    raise PiperSpeechUnavailable(
                        f"Piper staged resource is incomplete: {'/'.join(relative)}"
                    )
                if (
                    not cls._same_file(source, staging)
                    and cls._sha256_file(staging) != cls._expected_digest(relative)
                ):
                    raise PiperSpeechUnavailable(
                        f"Piper staged resource integrity check failed: {'/'.join(relative)}"
                    )
                os.replace(staging, destination)
            except OSError as exc:
                raise PiperSpeechUnavailable(
                    f"Could not stage Piper runtime resource: {'/'.join(relative)}"
                ) from exc
            finally:
                try:
                    staging.unlink(missing_ok=True)
                except OSError:
                    pass

        for relative in required:
            source = source_root.joinpath(*relative)
            destination = destination_root.joinpath(*relative)
            cls._ascii_native_path(destination)
            if not destination.is_file() or destination.stat().st_size <= 0:
                raise PiperSpeechUnavailable(
                    f"Piper staged resource is unavailable: {'/'.join(relative)}"
                )
            if (
                not cls._same_file(source, destination)
                and cls._sha256_file(destination) != cls._expected_digest(relative)
            ):
                raise PiperSpeechUnavailable(
                    f"Piper staged resource integrity check failed: {'/'.join(relative)}"
                )
        return destination_root

    @staticmethod
    def _configure_library(library: object) -> None:
        float_pointer = ctypes.POINTER(ctypes.c_float)
        library.crispasr_session_open_explicit.argtypes = (
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
        )
        library.crispasr_session_open_explicit.restype = ctypes.c_void_p
        library.crispasr_session_set_speaker_id.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
        )
        library.crispasr_session_set_speaker_id.restype = ctypes.c_int
        library.crispasr_session_synthesize.argtypes = (
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int),
        )
        library.crispasr_session_synthesize.restype = float_pointer
        library.crispasr_pcm_free.argtypes = (float_pointer,)
        library.crispasr_pcm_free.restype = None
        library.crispasr_session_close.argtypes = (ctypes.c_void_p,)
        library.crispasr_session_close.restype = None
        library.crispasr_session_last_synth_error.argtypes = (ctypes.c_void_p,)
        library.crispasr_session_last_synth_error.restype = ctypes.c_char_p

    def _load_locked(self) -> tuple[object, object]:
        if self._library is not None and self._session is not None:
            return self._library, self._session
        runtime_root = self._prepare_runtime_files()
        runtime_dir = runtime_root / "runtime"
        model_path = runtime_root / self.MODEL_FILENAME
        try:
            native_runtime_dir = self._ascii_native_path(runtime_dir).decode("ascii")
            native_library_path = self._ascii_native_path(
                runtime_dir / "crispasr.dll"
            ).decode("ascii")
            if hasattr(os, "add_dll_directory"):
                self._dll_directory_handle = os.add_dll_directory(native_runtime_dir)
            library = ctypes.WinDLL(native_library_path)
            self._configure_library(library)
            session = library.crispasr_session_open_explicit(
                self._ascii_native_path(model_path),
                b"melotts",
                MELO_CPU_THREADS,
            )
        except (OSError, AttributeError) as exc:
            raise PiperSpeechUnavailable(f"Piper runtime could not load: {exc}") from exc
        if not session:
            raise PiperSpeechUnavailable("Piper model session could not open")
        self._library = library
        self._session = session
        return library, session

    def synthesize(
        self, text: str, accent: str, speed: float = MELO_SPEECH_SPEEDS["standard"]
    ) -> tuple[Path, float]:
        text = text.strip()
        if accent not in self.SPEAKERS:
            raise PiperSpeechUnavailable("Piper accent is unsupported")
        speed = float(speed)
        if not text:
            raise PiperSpeechUnavailable("Piper text is empty")
        if speed != 1.0:
            raise PiperSpeechUnavailable(
                "Piper post-processing speed is disabled to preserve the original voice"
            )
        cache_key = (text, accent)
        with self._session_lock:
            with self._cache_lock:
                cached = self._audio_cache.get(cache_key)
                if cached is not None:
                    self._audio_cache.move_to_end(cache_key)
                    output_path = self._new_output_path_locked()
            if cached is not None:
                pcm, sample_rate, duration = cached
                self._write_wav(output_path, pcm, sample_rate)
                return output_path, duration

            library, session = self._load_locked()
            if library.crispasr_session_set_speaker_id(
                session, self.SPEAKERS[accent]
            ) != 0:
                raise PiperSpeechUnavailable("Piper speaker selection failed")
            sample_count = ctypes.c_int(0)
            pcm_pointer = library.crispasr_session_synthesize(
                session,
                text.encode("utf-8"),
                ctypes.byref(sample_count),
            )
            if not pcm_pointer or sample_count.value <= 0:
                detail = library.crispasr_session_last_synth_error(session)
                message = detail.decode("utf-8", "replace") if detail else "no audio"
                raise PiperSpeechUnavailable(f"Piper synthesis failed: {message}")
            try:
                raw_floats = ctypes.string_at(
                    pcm_pointer, sample_count.value * ctypes.sizeof(ctypes.c_float)
                )
            finally:
                library.crispasr_pcm_free(pcm_pointer)
            sample_rate = self.SESSION_PCM_SAMPLE_RATE
            try:
                import numpy as np

                audio = np.frombuffer(raw_floats, dtype="<f4")
                if audio.size != sample_count.value or not np.isfinite(audio).all():
                    raise PiperSpeechUnavailable("Piper returned invalid audio samples")
                pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
            except Exception as exc:
                raise PiperSpeechUnavailable(
                    f"Piper audio conversion failed: {exc}"
                ) from exc
            duration = len(pcm) / 2.0 / float(sample_rate)
            with self._cache_lock:
                output_path = self._new_output_path_locked()
                if len(pcm) <= self.CACHE_MAX_AUDIO_BYTES:
                    self._audio_cache[cache_key] = (pcm, sample_rate, duration)
                    self._audio_cache.move_to_end(cache_key)
                    while len(self._audio_cache) > self.CACHE_MAX_ENTRIES:
                        self._audio_cache.popitem(last=False)
            self._write_wav(output_path, pcm, sample_rate)
            return output_path, duration

    def _new_output_path_locked(self) -> Path:
        if self._temp_dir is None:
            self._temp_dir = tempfile.TemporaryDirectory(
                prefix="DaShengFaTranslator-melo-"
            )
        self._next_file_id += 1
        return Path(self._temp_dir.name) / f"speech-{self._next_file_id}.wav"

    @staticmethod
    def _write_wav(output_path: Path, pcm: bytes, sample_rate: int) -> None:
        try:
            with wave.open(str(output_path), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(sample_rate)
                stream.writeframes(pcm)
        except OSError as exc:
            raise PiperSpeechUnavailable(f"Could not prepare Piper audio: {exc}") from exc

    def warm_up(self) -> None:
        """Load the model and exercise both bundled English speakers."""

        for accent in ("us", "uk"):
            path, _duration = self.synthesize(
                "ready",
                accent,
                piper_speech_speed_value(DEFAULT_NATURAL_SPEECH_SPEED),
            )
            self.discard(path)

    @staticmethod
    def play(path: Path) -> None:
        winsound.PlaySound(
            str(path),
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )

    @staticmethod
    def stop() -> None:
        try:
            winsound.PlaySound(None, 0)
        except RuntimeError:
            pass

    @staticmethod
    def discard(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def close(self) -> None:
        self.stop()
        with self._session_lock:
            library, session = self._library, self._session
            self._session = None
            self._library = None
            if library is not None and session is not None:
                try:
                    library.crispasr_session_close(session)
                except Exception as exc:
                    log(f"Piper session close error: {exc}")
            handle, self._dll_directory_handle = self._dll_directory_handle, None
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        with self._cache_lock:
            temporary, self._temp_dir = self._temp_dir, None
            self._audio_cache.clear()
        if temporary is not None:
            temporary.cleanup()

    # Piper overrides the retired CrispASR implementation above.  Keeping the
    # narrow-path helpers in this class also preserves Kokoro's isolated
    # eSpeak staging path on Windows.
    RESOURCE_ROOT = "models/piper"
    VOICES = {
        "us": ("en_US-lessac-high.onnx", "en_US-lessac-high.onnx.json"),
        "uk": ("en_GB-cori-high.onnx", "en_GB-cori-high.onnx.json"),
    }
    SESSION_PCM_SAMPLE_RATE = 22_050
    RESOURCE_SHA256 = {
        "en_US-lessac-high.onnx": "4cabf7c3a638017137f34a1516522032d4fe3f38228a843cc9b764ddcbcd9e09",
        "en_US-lessac-high.onnx.json": "db42b97d9859f257bc1561b8ed980e7fb2398402050a74ddd6cbec931a92412f",
        "en_US-lessac-high.MODEL_CARD.md": "7671826d947a0ffc11dd76af0bd890d93e956b00358696ad71348f21aa827100",
        "en_GB-cori-high.onnx": "470b4dd634c98f8a4850d7626ffc3dfc90774628eeef6605a6dd8f88f30a5903",
        "en_GB-cori-high.onnx.json": "9e7fb5b5671612c22f3c81cbe46c1ae87b031a4632bcb509e499dad6f1e2adec",
        "en_GB-cori-high.MODEL_CARD.md": "136e7bd168b6c35b4a5df01a0253297e5773b5775ceae0af5160f264aa58208f",
        "PIPER_GPL-3.0.txt": "5f631fae467c82b8cd28fd1ec425c816895a35f9d94e36bee0e0164570e8e0f6",
        "README.md": "06a20b0f7054800baa4f6dcff2c11c144ba02abc36809d26abdc4a078db51a5f",
    }

    @classmethod
    def _piper_resource_root(cls) -> Path:
        root = resource_path(cls.RESOURCE_ROOT)
        for name, expected_digest in cls.RESOURCE_SHA256.items():
            path = root / name
            if not path.is_file() or path.stat().st_size <= 0:
                raise PiperSpeechUnavailable(f"Piper resource is missing or empty: {name}")
            if cls._sha256_file(path) != expected_digest:
                raise PiperSpeechUnavailable(f"Piper resource integrity check failed: {name}")
        return root

    @classmethod
    def _prepare_piper_espeak_data(cls, source: Path) -> Path:
        """Return a native-safe Piper eSpeak path, staging only if needed."""

        if not (source / "phontab").is_file():
            raise PiperSpeechUnavailable("Piper eSpeak data is missing")
        try:
            return Path(cls._ascii_native_path(source).decode("ascii"))
        except PiperSpeechUnavailable:
            pass

        # eSpeak's Windows bridge is a narrow-path native library.  A formal
        # package or development venv may sit below a Unicode directory, so
        # stage only its read-only data under LocalAppData before initialisation.
        data_dir = user_data_dir() / "runtime" / "piper" / "espeak-ng-data-1.6.0"
        if not (data_dir / "phontab").is_file():
            staging = data_dir.with_name(
                f"{data_dir.name}.staging-{os.getpid()}-{threading.get_ident()}"
            )
            try:
                if staging.exists():
                    shutil.rmtree(staging)
                data_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, staging)
                if not (staging / "phontab").is_file():
                    raise PiperSpeechUnavailable("Piper eSpeak data copy is incomplete")
                try:
                    staging.replace(data_dir)
                except FileExistsError:
                    if not (data_dir / "phontab").is_file():
                        raise
            except OSError as exc:
                raise PiperSpeechUnavailable(
                    f"Could not prepare Piper eSpeak data: {exc}"
                ) from exc
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
        if not data_dir.is_dir():
            raise PiperSpeechUnavailable("Piper eSpeak data directory is missing")
        try:
            return Path(cls._ascii_native_path(data_dir).decode("ascii"))
        except PiperSpeechUnavailable as exc:
            raise PiperSpeechUnavailable(
                "Piper eSpeak data has no safe ASCII path"
            ) from exc

    @classmethod
    def _piper_espeak_data_dir(cls) -> Path:
        """Resolve Piper's bundled eSpeak data in source and frozen builds.

        Piper's native bridge accepts a narrow Windows path.  Passing an
        explicit, verified path avoids both an upstream build-machine default
        and failures when the project virtual environment lives below a
        Unicode directory.
        """

        if getattr(sys, "frozen", False):
            source = Path(getattr(sys, "_MEIPASS")) / "piper" / "espeak-ng-data"
        else:
            from piper.phonemize_espeak import ESPEAK_DATA_DIR

            source = Path(ESPEAK_DATA_DIR)
        return cls._prepare_piper_espeak_data(source)

    def _load_locked(self, accent: str) -> object:
        loaded_voices = getattr(self, "_piper_voices", None)
        if loaded_voices is None:
            loaded_voices = {}
            self._piper_voices = loaded_voices
        voice = loaded_voices.get(accent)
        if voice is not None:
            return voice
        try:
            model_name, config_name = self.VOICES[accent]
        except KeyError as exc:
            raise PiperSpeechUnavailable("Piper accent is unsupported") from exc
        try:
            from piper import PiperVoice

            root = self._piper_resource_root()
            voice = PiperVoice.load(
                root / model_name,
                config_path=root / config_name,
                use_cuda=False,
                espeak_data_dir=self._piper_espeak_data_dir(),
            )
        except PiperSpeechUnavailable:
            raise
        except Exception as exc:
            raise PiperSpeechUnavailable(f"Piper runtime could not load: {exc}") from exc
        loaded_voices[accent] = voice
        return voice

    def _new_output_path_locked(self) -> Path:
        if self._temp_dir is None:
            self._temp_dir = tempfile.TemporaryDirectory(
                prefix="DaShengFaTranslator-piper-"
            )
        self._next_file_id += 1
        return Path(self._temp_dir.name) / f"speech-{self._next_file_id}.wav"

    def synthesize(
        self,
        text: str,
        accent: str,
        speed: float = 1.0,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> tuple[Path, float]:
        text = text.strip()
        if not text:
            raise PiperSpeechUnavailable("Piper text is empty")
        if accent not in self.VOICES:
            raise PiperSpeechUnavailable("Piper accent is unsupported")
        if float(speed) != 1.0:
            raise PiperSpeechUnavailable(
                "Piper post-processing speed is disabled to preserve the original voice"
            )
        cache_key = (text, accent)
        if cancel_callback is not None and not cancel_callback():
            raise BackgroundSpeechYield("Piper synthesis yielded before loading")
        with self._session_lock:
            with self._cache_lock:
                cached = self._audio_cache.get(cache_key)
                if cached is not None:
                    self._audio_cache.move_to_end(cache_key)
                    output_path = self._new_output_path_locked()
            if cached is not None:
                wav_bytes, sample_rate, duration = cached
                output_path.write_bytes(wav_bytes)
                return output_path, duration

            voice = self._load_locked(accent)
            if cancel_callback is not None and not cancel_callback():
                raise BackgroundSpeechYield("Piper synthesis yielded after loading")
            output_path = self._new_output_path_locked()
            try:
                with wave.open(str(output_path), "wb") as stream:
                    voice.synthesize_wav(text, stream)
                with wave.open(str(output_path), "rb") as stream:
                    sample_rate = stream.getframerate()
                    frame_count = stream.getnframes()
                if sample_rate <= 0 or frame_count <= 0:
                    raise PiperSpeechUnavailable("Piper returned invalid audio samples")
                wav_bytes = output_path.read_bytes()
            except PiperSpeechUnavailable:
                output_path.unlink(missing_ok=True)
                raise
            except Exception as exc:
                output_path.unlink(missing_ok=True)
                raise PiperSpeechUnavailable(f"Piper synthesis failed: {exc}") from exc
            duration = frame_count / float(sample_rate)
            with self._cache_lock:
                if len(wav_bytes) <= self.CACHE_MAX_AUDIO_BYTES:
                    self._audio_cache[cache_key] = (wav_bytes, sample_rate, duration)
                    self._audio_cache.move_to_end(cache_key)
                    while len(self._audio_cache) > self.CACHE_MAX_ENTRIES:
                        self._audio_cache.popitem(last=False)
            return output_path, duration

    def warm_up(self, should_continue: Callable[[], bool] | None = None) -> None:
        for accent in ("us", "uk"):
            if should_continue is not None and not should_continue():
                raise BackgroundSpeechYield("Piper prewarm yielded to the user")
            path, _duration = self.synthesize(
                "a",
                accent,
                1.0,
                cancel_callback=should_continue,
            )
            self.discard(path)

    def close(self) -> None:
        self.stop()
        with self._session_lock:
            self._piper_voices = {}
        with self._cache_lock:
            temporary, self._temp_dir = self._temp_dir, None
            self._audio_cache.clear()
        if temporary is not None:
            temporary.cleanup()


class KokoroSpeechBackend:
    """Lazy, local-only Kokoro ONNX synthesis with disposable WAV playback."""

    MODEL_RESOURCE = "models/kokoro/kokoro-v1.0.int8.onnx"
    VOICES_RESOURCE = "models/kokoro/voices-v1.0.bin"
    VOICES = {
        "us": ("af_bella", "en-us"),
        "uk": ("bf_emma", "en-gb"),
    }
    CACHE_MAX_ENTRIES = 48
    CACHE_MAX_AUDIO_BYTES = 1_000_000
    INTER_CHUNK_SILENCE_SECONDS = 0.12

    def __init__(self) -> None:
        self._engine: object | None = None
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._next_file_id = 0
        self._lock = threading.Lock()
        self._synthesis_lock = threading.Lock()
        self._audio_cache: OrderedDict[
            tuple[str, str, float], tuple[bytes, int, float]
        ] = OrderedDict()

    def _load(self) -> object:
        if self._engine is not None:
            return self._engine
        model_path = resource_path(self.MODEL_RESOURCE)
        voices_path = resource_path(self.VOICES_RESOURCE)
        if not model_path.is_file() or not voices_path.is_file():
            raise KokoroSpeechUnavailable("Kokoro model resources are missing")
        try:
            import espeakng_loader
            import onnxruntime as ort
            from kokoro_onnx import Kokoro
            from kokoro_onnx.config import EspeakConfig
        except ImportError as exc:
            raise KokoroSpeechUnavailable("Kokoro runtime is not installed") from exc
        try:
            # eSpeak-NG's Windows DLL cannot reliably open a Unicode unpack path.
            # A frozen app can live in one, so keep its data in the app's ASCII
            # LocalAppData directory before passing it to the native library.
            data_path = Path(espeakng_loader.get_data_path())
            if getattr(sys, "frozen", False):
                data_path = self._prepare_espeak_data(data_path)
            espeak_config = EspeakConfig(
                lib_path=espeakng_loader.get_library_path(),
                data_path=str(data_path),
            )
            # The short, dynamic inputs used by dictionary lookups are faster
            # with a bounded CPU pool than with ORT's machine-wide default.
            session_options = ort.SessionOptions()
            session_options.intra_op_num_threads = KOKORO_CPU_THREADS
            session_options.inter_op_num_threads = 1
            session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            session = ort.InferenceSession(
                str(model_path),
                sess_options=session_options,
                providers=["CPUExecutionProvider"],
            )
            self._engine = Kokoro.from_session(
                session,
                str(voices_path),
                espeak_config=espeak_config,
            )
        except Exception as exc:
            raise KokoroSpeechUnavailable(f"Kokoro could not load: {exc}") from exc
        return self._engine

    @staticmethod
    def _prepare_espeak_data(source: Path) -> Path:
        """Return a complete ASCII-path copy for eSpeak-NG in frozen builds."""

        if not (source / "phontab").is_file():
            raise KokoroSpeechUnavailable("Kokoro eSpeak data is incomplete")
        destination = user_data_dir() / "runtime" / "espeak-ng-data"
        if (destination / "phontab").is_file():
            return KokoroSpeechBackend._ascii_espeak_data_path(destination)
        staging = destination.with_name(f"{destination.name}.staging-{os.getpid()}")
        try:
            if staging.exists():
                shutil.rmtree(staging)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, staging)
            if not (staging / "phontab").is_file():
                raise KokoroSpeechUnavailable("Kokoro eSpeak data copy is incomplete")
            try:
                staging.replace(destination)
            except FileExistsError:
                # Another process completed the same non-destructive setup first.
                if not (destination / "phontab").is_file():
                    raise
        except OSError as exc:
            raise KokoroSpeechUnavailable(f"Could not prepare Kokoro eSpeak data: {exc}") from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        if not (destination / "phontab").is_file():
            raise KokoroSpeechUnavailable("Kokoro eSpeak data copy is unavailable")
        return KokoroSpeechBackend._ascii_espeak_data_path(destination)

    @staticmethod
    def _ascii_espeak_data_path(destination: Path) -> Path:
        """Return the long ASCII path or a verified ASCII Windows 8.3 alias."""

        try:
            native_path = PiperSpeechBackend._ascii_native_path(destination)
        except PiperSpeechUnavailable as exc:
            raise KokoroSpeechUnavailable(
                "Kokoro eSpeak data has no safe ASCII path"
            ) from exc
        return Path(native_path.decode("ascii"))

    @classmethod
    def _split_synthesis_chunks(cls, text: str) -> list[str]:
        """Split only at explicit sentence, clause, or natural-pause marks."""

        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            return []
        chunks = [
            part.strip()
            for part in re.split(
                r"(?<=[.!?;:,，。！？；：])\s*",
                normalized,
            )
            if part.strip()
        ]
        return chunks or [normalized]

    semantic_segments = _split_synthesis_chunks

    def combine_audio(self, paths: list[Path]) -> tuple[Path, float]:
        """Join complete semantic-segment WAVs without re-encoding them."""

        if not paths:
            raise KokoroSpeechUnavailable("No Kokoro segments are available")
        sample_rate: int | None = None
        frames: list[bytes] = []
        total_frames = 0
        for index, path in enumerate(paths):
            try:
                with wave.open(str(path), "rb") as stream:
                    current_rate = int(stream.getframerate())
                    if stream.getnchannels() != 1 or stream.getsampwidth() != 2:
                        raise KokoroSpeechUnavailable(
                            "Kokoro segment format is inconsistent"
                        )
                    if sample_rate is None:
                        sample_rate = current_rate
                    elif sample_rate != current_rate:
                        raise KokoroSpeechUnavailable(
                            "Kokoro segment sample rates are inconsistent"
                        )
                    payload = stream.readframes(stream.getnframes())
                    frames.append(payload)
                    total_frames += len(payload) // 2
                    if index + 1 < len(paths):
                        silence_frames = max(
                            1,
                            round(current_rate * self.INTER_CHUNK_SILENCE_SECONDS),
                        )
                        frames.append(b"\0\0" * silence_frames)
                        total_frames += silence_frames
            except OSError as exc:
                raise KokoroSpeechUnavailable(
                    f"Could not read Kokoro segment audio: {exc}"
                ) from exc
        assert sample_rate is not None
        with self._lock:
            output_path = self._new_output_path_locked()
        self._write_wav(output_path, b"".join(frames), sample_rate)
        return output_path, total_frames / float(sample_rate)

    def synthesize(
        self,
        text: str,
        accent: str,
        speed: float = NATURAL_SPEECH_SPEEDS["fast"],
        progress_callback: Callable[[int, int], bool] | None = None,
    ) -> tuple[Path, float]:
        text = text.strip()
        voice, language = self.VOICES[accent]
        speed = float(speed)
        if not text or not 0.5 <= speed <= 2.0:
            raise KokoroSpeechUnavailable("Kokoro speed is outside the supported range")
        cache_key = (text, accent, speed)
        # Prewarm, prefetch and a real user request may arrive on independent
        # threads.  One ONNX engine is kept deliberately serial while SAPI
        # remains completely independent of this lock.
        with self._synthesis_lock:
            with self._lock:
                cached = self._audio_cache.get(cache_key)
                if cached is not None:
                    self._audio_cache.move_to_end(cache_key)
                    output_path = self._new_output_path_locked()
            if cached is not None:
                pcm, sample_rate, duration = cached
                self._write_wav(output_path, pcm, sample_rate)
                return output_path, duration
            engine = self._load()
            try:
                import numpy as np

                chunks = self._split_synthesis_chunks(text)
                rendered_chunks: list[object] = []
                sample_rate: int | None = None
                for index, chunk in enumerate(chunks, start=1):
                    if progress_callback is not None and not progress_callback(
                        index, len(chunks)
                    ):
                        raise KokoroSpeechUnavailable(
                            "Kokoro long-speech generation was cancelled"
                        )
                    samples, current_sample_rate = engine.create(
                        chunk,
                        voice=voice,
                        speed=speed,
                        lang=language,
                    )
                    audio_chunk = np.asarray(samples, dtype=np.float32).reshape(-1)
                    if audio_chunk.size == 0 or int(current_sample_rate) <= 0:
                        raise KokoroSpeechUnavailable(
                            f"Kokoro returned no playable audio for segment {index}"
                        )
                    if sample_rate is None:
                        sample_rate = int(current_sample_rate)
                    elif sample_rate != int(current_sample_rate):
                        raise KokoroSpeechUnavailable(
                            "Kokoro returned inconsistent sample rates across segments"
                        )
                    rendered_chunks.append(audio_chunk)
                if not rendered_chunks or sample_rate is None:
                    raise KokoroSpeechUnavailable("Kokoro returned no playable audio")
                if len(rendered_chunks) == 1:
                    audio = rendered_chunks[0]
                else:
                    silence = np.zeros(
                        max(1, round(sample_rate * self.INTER_CHUNK_SILENCE_SECONDS)),
                        dtype=np.float32,
                    )
                    audio = np.concatenate(
                        [
                            item
                            for index, item in enumerate(rendered_chunks)
                            for item in (item, silence if index + 1 < len(rendered_chunks) else None)
                            if item is not None
                        ]
                    )
            except KokoroSpeechUnavailable:
                raise
            except Exception as exc:
                raise KokoroSpeechUnavailable(f"Kokoro synthesis failed: {exc}") from exc
            pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
            duration = len(audio) / float(sample_rate)
            with self._lock:
                output_path = self._new_output_path_locked()
                if len(pcm) <= self.CACHE_MAX_AUDIO_BYTES:
                    self._audio_cache[cache_key] = (pcm, int(sample_rate), duration)
                    self._audio_cache.move_to_end(cache_key)
                    while len(self._audio_cache) > self.CACHE_MAX_ENTRIES:
                        self._audio_cache.popitem(last=False)
            self._write_wav(output_path, pcm, sample_rate)
            return output_path, duration

    def _new_output_path_locked(self) -> Path:
        if self._temp_dir is None:
            self._temp_dir = tempfile.TemporaryDirectory(
                prefix="DaShengFaTranslator-kokoro-"
            )
        self._next_file_id += 1
        return Path(self._temp_dir.name) / f"speech-{self._next_file_id}.wav"

    @staticmethod
    def _write_wav(output_path: Path, pcm: bytes, sample_rate: int) -> None:
        try:
            with wave.open(str(output_path), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(sample_rate)
                stream.writeframes(pcm)
        except OSError as exc:
            raise KokoroSpeechUnavailable(f"Could not prepare Kokoro audio: {exc}") from exc

    def warm_up(self, should_continue: Callable[[], bool] | None = None) -> None:
        """Load and execute a tiny local utterance before the first user click."""

        path, _duration = self.synthesize(
            "a",
            "us",
            natural_speech_speed_value(DEFAULT_NATURAL_SPEECH_SPEED),
            progress_callback=(
                (lambda _index, _total: should_continue())
                if should_continue is not None
                else None
            ),
        )
        self.discard(path)

    @staticmethod
    def play(path: Path) -> None:
        winsound.PlaySound(
            str(path),
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )

    @staticmethod
    def stop() -> None:
        try:
            winsound.PlaySound(None, 0)
        except RuntimeError:
            pass

    @staticmethod
    def discard(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def close(self) -> None:
        self.stop()
        with self._lock:
            temporary, self._temp_dir = self._temp_dir, None
            self._audio_cache.clear()
        if temporary is not None:
            temporary.cleanup()


@dataclass(frozen=True, slots=True)
class KokoroSegmentMetric:
    generation_seconds: float
    audio_seconds: float
    text_weight: int


class KokoroSafeBufferPolicy:
    """Conservative early-start predictor based only on observed real timings."""

    MIN_COMPLETE_SEGMENTS = 2
    MIN_BUFFERED_AUDIO_SECONDS = 5.0
    PREDICTION_MARGIN = 1.5
    PLAYBACK_RESERVE_SECONDS = 0.35

    @staticmethod
    def text_weight(text: str) -> int:
        words = re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)*|[\u3400-\u9fff]", text)
        return max(1, len(words))

    @classmethod
    def can_start(
        cls,
        metrics: list[KokoroSegmentMetric],
        remaining_segments: list[str],
    ) -> tuple[bool, dict[str, float]]:
        buffered = sum(max(0.0, item.audio_seconds) for item in metrics)
        details = {
            "buffered_seconds": buffered,
            "estimated_remaining_generation_seconds": 0.0,
        }
        if (
            len(metrics) < cls.MIN_COMPLETE_SEGMENTS
            or buffered < cls.MIN_BUFFERED_AUDIO_SECONDS
        ):
            return False, details
        if not remaining_segments:
            return True, details
        generated_weight = sum(max(1, item.text_weight) for item in metrics)
        remaining_weight = sum(cls.text_weight(text) for text in remaining_segments)
        observed_audio_per_weight = buffered / float(generated_weight)
        worst_generation_audio_ratio = max(
            max(0.0, item.generation_seconds) / max(0.001, item.audio_seconds)
            for item in metrics
        )
        estimated_remaining_audio = observed_audio_per_weight * remaining_weight
        estimated_generation = (
            estimated_remaining_audio
            * worst_generation_audio_ratio
            * cls.PREDICTION_MARGIN
        )
        details["estimated_remaining_generation_seconds"] = estimated_generation
        safe = (
            estimated_generation + cls.PLAYBACK_RESERVE_SECONDS
            <= buffered
        )
        return safe, details


class SpeechPlayer:
    """Latest-wins speech router with independent neural and SAPI workers."""

    VOICE_LANGUAGE = {"us": "409", "uk": "809"}
    SYSTEM_VOICES = {
        "us": (
            "409",
            "Microsoft Zira Desktop",
            "Microsoft",
            "TTS_MS_EN-US_ZIRA_11.0",
        ),
        "uk": (
            "809",
            "Microsoft Hazel Desktop",
            "Microsoft",
            "TTS_MS_EN-GB_HAZEL_11.0",
        ),
    }
    _PURGE = object()
    _PREWARM = object()

    def __init__(
        self,
        status_callback: Callable[[str], None] | None = None,
        natural_speed: object = DEFAULT_NATURAL_SPEECH_SPEED,
        speech_status_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.status_callback = status_callback
        self.speech_status_callback = speech_status_callback
        # SAPI gets its own COM worker and never waits for model loading,
        # neural prewarm, neural prefetch or neural synthesis.
        self.requests: queue.Queue[object] = queue.Queue(maxsize=1)
        self.natural_requests: queue.Queue[object] = queue.Queue(maxsize=1)
        self._prefetch_condition = threading.Condition()
        self._prefetch_generation = 0
        self._prefetch_pending: tuple[int, str, str, set[str]] | None = None
        self._prefetch_active_key: tuple[str, str] | None = None
        self._prefetch_active_generation = 0
        self._prefetch_active_current: str | None = None
        self._prefetch_active_accents: set[str] = set()
        self.stop_event = threading.Event()
        self._interactive_ai_requested = threading.Event()
        self._candidate_ai_requested = threading.Event()
        self.piper_backend = PiperSpeechBackend()
        self.kokoro_backend = KokoroSpeechBackend()
        # Kept as a compatibility alias for code/tests that inspected the old
        # single-neural-backend player.
        self.neural_backend = self.kokoro_backend
        self._natural_speed_lock = threading.Lock()
        self._natural_speed_profile = DEFAULT_NATURAL_SPEECH_SPEED
        self._request_lock = threading.RLock()
        self._latest_request_id = 0
        self._speech_status_lock = threading.Lock()
        self._readiness_lock = threading.Lock()
        self._backend_readiness: dict[str, bool | None] = {
            "Piper": None,
            "Kokoro": None,
        }
        self._natural_fallback_reported = False
        self._timing_lock = threading.Lock()
        self._timing_events: deque[dict[str, object]] = deque(maxlen=256)
        self.set_natural_speed(natural_speed, cancel_current=False)

        self.worker = threading.Thread(
            target=self._speech_loop,
            daemon=True,
            name="MicrosoftSpeechPlayer",
        )
        self.natural_worker = threading.Thread(
            target=self._natural_loop,
            daemon=True,
            name="NaturalSpeechPlayer",
        )
        self.prefetch_worker = threading.Thread(
            target=self._prefetch_loop,
            daemon=True,
            name="NaturalSpeechPrefetch",
        )
        self.worker.start()
        self.natural_worker.start()
        self.prefetch_worker.start()

        self._emit_speech_status("正在并行预热 AI 发音…")
        self.prewarm_workers: list[threading.Thread] = []
        for label, backend in (
            ("Piper", self.piper_backend),
            ("Kokoro", self.kokoro_backend),
        ):
            worker = threading.Thread(
                target=self._prewarm_backend,
                args=(label, backend),
                daemon=True,
                name=f"{label}SpeechPrewarm",
            )
            self.prewarm_workers.append(worker)
            worker.start()

    @staticmethod
    def _replace_queue(target: queue.Queue[object], value: object) -> None:
        while True:
            try:
                target.get_nowait()
            except queue.Empty:
                break
        try:
            target.put_nowait(value)
        except queue.Full:
            pass

    def _emit_speech_status(self, value: str) -> None:
        callback = getattr(self, "speech_status_callback", None)
        if callback is None:
            return
        lock = getattr(self, "_speech_status_lock", None)
        try:
            if lock is None:
                callback(value)
            else:
                with lock:
                    callback(value)
        except Exception as exc:
            log(f"Speech status callback error: {exc}")

    def record_timing_event(self, event: str, **details: object) -> dict[str, object]:
        """Record a monotonic, structured event for diagnostics and tests."""

        item: dict[str, object] = {
            "event": str(event),
            "monotonic": time.monotonic(),
            **details,
        }
        lock = getattr(self, "_timing_lock", None)
        events = getattr(self, "_timing_events", None)
        if lock is not None and events is not None:
            with lock:
                events.append(item)
        log(
            "Speech timing "
            + json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        )
        return item

    def timing_events(self) -> list[dict[str, object]]:
        lock = getattr(self, "_timing_lock", None)
        events = getattr(self, "_timing_events", None)
        if lock is None or events is None:
            return []
        with lock:
            return [dict(item) for item in events]

    def _prewarm_backend(self, label: str, backend: object) -> None:
        ready = False
        interactive_event = self._interactive_ai_event()
        candidate_event = self._candidate_ai_event()
        while not self.stop_event.is_set() and not ready:
            if interactive_event.is_set() or candidate_event.is_set():
                self.stop_event.wait(0.04)
                continue
            try:
                warm_up = backend.warm_up
                try:
                    accepts_yield_callback = bool(
                        inspect.signature(warm_up).parameters
                    )
                except (TypeError, ValueError):
                    accepts_yield_callback = False
                if accepts_yield_callback:
                    warm_up(
                        lambda: (
                            not self.stop_event.is_set()
                            and not interactive_event.is_set()
                            and not candidate_event.is_set()
                        )
                    )
                else:
                    warm_up()
                ready = True
            except BackgroundSpeechYield:
                continue
            except Exception as exc:
                log(f"{label} speech prewarm skipped: {exc}")
                break
        if self.stop_event.is_set():
            return
        self._mark_backend_readiness(label, ready)

    def _mark_backend_readiness(self, label: str, ready: bool) -> None:
        """Publish one prewarm result while preserving a final-last ordering."""

        # Holding the request lock makes this readiness publication happen
        # wholly before the first user request, or not at all.  A late prewarm
        # result must never overwrite a generating/playing status.
        with self._request_lock:
            with self._readiness_lock:
                self._backend_readiness[label] = ready
                all_finished = all(
                    value is not None for value in self._backend_readiness.values()
                )
                all_ready = all(
                    value is True for value in self._backend_readiness.values()
                )
                if self._latest_request_id > 0:
                    return
                # These emissions remain under the readiness lock so the
                # second worker's final summary cannot overtake the first
                # worker's delayed per-backend message.
                if ready:
                    self._emit_speech_status(f"{label} 已就绪")
                else:
                    self._emit_speech_status(
                        f"{label} 预热失败，将在首次使用时重试"
                    )
                if all_finished:
                    if all_ready:
                        self._emit_speech_status("AI 发音已全部就绪")
                    else:
                        self._emit_speech_status(
                            "AI 发音预热完成，部分引擎将在首次使用时重试"
                        )

    def natural_speed_profile(self) -> str:
        with self._natural_speed_lock:
            return self._natural_speed_profile

    def _interactive_ai_event(self) -> threading.Event:
        event = getattr(self, "_interactive_ai_requested", None)
        if event is None:
            event = threading.Event()
            self._interactive_ai_requested = event
        return event

    def _candidate_ai_event(self) -> threading.Event:
        event = getattr(self, "_candidate_ai_requested", None)
        if event is None:
            event = threading.Event()
            self._candidate_ai_requested = event
        return event

    def set_natural_speed(self, profile: object, *, cancel_current: bool = True) -> str:
        normalized = str(profile or "").strip().lower()
        if normalized not in NATURAL_SPEECH_SPEEDS:
            normalized = DEFAULT_NATURAL_SPEECH_SPEED
        with self._natural_speed_lock:
            changed = normalized != self._natural_speed_profile
            self._natural_speed_profile = normalized
        if changed and cancel_current:
            self.cancel()
        return normalized

    def _next_request_id(self) -> int:
        with self._request_lock:
            self._latest_request_id += 1
            return self._latest_request_id

    def _is_latest_request(self, request_id: int) -> bool:
        lock = getattr(self, "_request_lock", None)
        if lock is None:
            return True
        with lock:
            return request_id == self._latest_request_id

    def _emit_speech_status_if_latest(self, request_id: int, value: str) -> bool:
        """Publish request-owned status at the same linearization boundary."""

        with self._request_lock:
            if (
                request_id != self._latest_request_id
                or self.stop_event.is_set()
            ):
                return False
            self._emit_speech_status(value)
            return True

    def _begin_natural_playback(
        self,
        request_id: int,
        backend: object,
        audio_path: Path,
        accent: str,
    ) -> bool:
        """Atomically revalidate and start neural playback for one request."""

        with self._request_lock:
            if (
                request_id != self._latest_request_id
                or self.stop_event.is_set()
            ):
                return False
            backend.play(audio_path)
            self._emit_speech_status(f"正在播放 {accent.upper()} AI 发音")
            return True

    def _begin_system_playback(
        self,
        request_id: int,
        voice: object,
        selected: object,
        text: str,
        accent: str,
    ) -> bool:
        """Atomically revalidate and start Microsoft SAPI playback."""

        with self._request_lock:
            if (
                request_id != self._latest_request_id
                or self.stop_event.is_set()
            ):
                return False
            voice.Voice = selected
            voice.Rate = -1
            # SVSFlagsAsync | SVSFPurgeBeforeSpeak.
            voice.Speak(text, 3)
            self._emit_speech_status(
                f"正在播放 {accent.upper()} 微软原版发音"
            )
            return True

    def _stop_neural_audio(self) -> None:
        for backend in (
            getattr(self, "piper_backend", None),
            getattr(self, "kokoro_backend", None),
        ):
            if backend is not None:
                try:
                    backend.stop()
                except Exception:
                    pass

    def speak(self, text: str, accent: str, mode: str = "natural") -> None:
        text = text.strip()
        normalized_mode = str(mode or "natural").strip().lower()
        if (
            not text
            or accent not in self.VOICE_LANGUAGE
            or normalized_mode not in {"natural", "system"}
            or self.stop_event.is_set()
        ):
            return
        request_id = self._next_request_id()
        self._stop_neural_audio()
        if normalized_mode == "system":
            self._interactive_ai_event().clear()
            self.cancel_prefetch(reason="system_request")
            self._replace_queue(self.natural_requests, self._PURGE)
            self._replace_queue(
                self.requests,
                (request_id, text, accent, "system"),
            )
            return
        with self._natural_speed_lock:
            profile = self._natural_speed_profile
        self._interactive_ai_event().set()
        self._candidate_ai_event().clear()
        key = (text, profile)
        with self._prefetch_condition:
            if self._prefetch_active_key != key and not (
                self._prefetch_pending is not None
                and (self._prefetch_pending[1], self._prefetch_pending[2]) == key
            ):
                self._prefetch_generation += 1
                self._prefetch_pending = None
                self._prefetch_active_accents.clear()
            self._prefetch_condition.notify_all()
        # A natural request supersedes any current/pending SAPI request.  Its
        # synthesis runs on another worker, so this COM purge never holds it up.
        self._replace_queue(self.requests, self._PURGE)
        self._replace_queue(
            self.natural_requests,
            (request_id, text, accent, profile),
        )

    def prefetch(self, text: str, accent: str = "us") -> None:
        """Fill the routed neural cache without occupying either playback queue."""

        text = text.strip()
        if (
            not text
            or accent not in self.VOICE_LANGUAGE
            or self.stop_event.is_set()
        ):
            return
        with self._natural_speed_lock:
            profile = self._natural_speed_profile
        self._candidate_ai_event().set()
        key = (text, profile)
        with self._prefetch_condition:
            pending = self._prefetch_pending
            if pending is not None and (pending[1], pending[2]) == key:
                pending[3].add(accent)
            elif (
                pending is None
                and self._prefetch_active_key == key
                and self._prefetch_active_generation == self._prefetch_generation
            ):
                # The first accent may already be synthesizing.  Merge the
                # second accent into that same bounded active batch instead of
                # replacing or duplicating the first one.
                if accent != self._prefetch_active_current:
                    self._prefetch_active_accents.add(accent)
            else:
                # One pending slot only: a new text/profile invalidates the
                # remaining accents of an older task and replaces its pending
                # batch atomically.
                self._prefetch_generation += 1
                self._prefetch_pending = (
                    self._prefetch_generation,
                    text,
                    profile,
                    {accent},
                )
            self._prefetch_condition.notify()

    def cancel_prefetch(self, *, reason: str = "cancelled") -> None:
        condition = getattr(self, "_prefetch_condition", None)
        if condition is None:
            return
        with condition:
            self._prefetch_generation += 1
            self._prefetch_pending = None
            self._prefetch_active_accents.clear()
            condition.notify_all()
        self._candidate_ai_event().clear()
        self.record_timing_event("cancelled", reason=reason, scope="ai_candidate")

    def cancel(self) -> None:
        """Stop both playback paths without shutting the player down."""

        if self.stop_event.is_set():
            return
        self._next_request_id()
        self._interactive_ai_event().clear()
        self._stop_neural_audio()
        self.cancel_prefetch(reason="speech_cancelled")
        self._replace_queue(self.natural_requests, self._PURGE)
        self._replace_queue(self.requests, self._PURGE)
        self.record_timing_event("cancelled", reason="speech_cancelled", scope="playback")

    def stop(self, timeout_seconds: float = 0.5) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        try:
            self._next_request_id()
        except Exception:
            pass
        self._stop_neural_audio()
        for target in (
            getattr(self, "requests", None),
            getattr(self, "natural_requests", None),
        ):
            if target is not None:
                self._replace_queue(target, None)
        condition = getattr(self, "_prefetch_condition", None)
        if condition is not None:
            with condition:
                self._prefetch_generation += 1
                self._prefetch_pending = None
                self._prefetch_active_accents.clear()
                condition.notify_all()

        deadline = time.monotonic() + max(0.0, timeout_seconds)
        workers = [
            getattr(self, "worker", None),
            getattr(self, "natural_worker", None),
            getattr(self, "prefetch_worker", None),
            *getattr(self, "prewarm_workers", []),
        ]
        for worker in workers:
            if worker is not None and worker is not threading.current_thread():
                worker.join(max(0.0, deadline - time.monotonic()))
        if any(worker is not None and worker.is_alive() for worker in workers):
            # Closing a native session while its daemon is inside inference can
            # crash the process.  At app shutdown the OS reclaims it safely.
            log("Speech backend close deferred because inference is still running")
            return
        for backend in (
            getattr(self, "piper_backend", None),
            getattr(self, "kokoro_backend", None),
        ):
            if backend is not None:
                backend.close()

    def _report_natural_fallback(self, reason: Exception, label: str = "AI") -> None:
        log(f"{label} natural speech fallback: {reason}")
        self._emit_speech_status(f"{label} 发音失败，正在回退 Windows 微软原版语音")
        if getattr(self, "_natural_fallback_reported", False):
            return
        self._natural_fallback_reported = True
        if self.status_callback:
            self.status_callback("自然发音暂不可用，已使用 Windows 微软原版语音")

    def _finish_interactive_ai_request(self, request_id: int) -> None:
        if self._is_latest_request(request_id):
            self._interactive_ai_event().clear()

    def _natural_backend_for(
        self, text: str, profile: object
    ) -> tuple[object, str, float]:
        if WORD_PATTERN.fullmatch(text):
            return (
                self.piper_backend,
                "Piper",
                piper_speech_speed_value(profile),
            )
        return (
            self.kokoro_backend,
            "Kokoro",
            natural_speech_speed_value(profile),
        )

    def _speak_with_kokoro(
        self,
        backend: KokoroSpeechBackend,
        text: str,
        accent: str,
        profile: object = DEFAULT_NATURAL_SPEECH_SPEED,
    ) -> tuple[bool, object | None]:
        """Compatibility helper retained for focused queue/playback tests."""

        try:
            audio_path, duration = backend.synthesize(
                text, accent, natural_speech_speed_value(profile)
            )
        except Exception as exc:
            self._report_natural_fallback(exc, "Kokoro")
            return False, None
        request_queue = getattr(self, "natural_requests", self.requests)
        try:
            try:
                next_request = request_queue.get_nowait()
            except queue.Empty:
                next_request = None
            if next_request is not None:
                return True, next_request
            backend.play(audio_path)
            deadline = time.monotonic() + max(0.05, duration)
            while not self.stop_event.is_set():
                try:
                    next_request = request_queue.get(timeout=0.04)
                    backend.stop()
                    return True, next_request
                except queue.Empty:
                    if time.monotonic() >= deadline:
                        return True, None
            return True, None
        finally:
            backend.discard(audio_path)

    def _play_buffered_kokoro_segments(
        self,
        request_id: int,
        backend: KokoroSpeechBackend,
        text: str,
        accent: str,
        speed: float,
    ) -> tuple[bool, object | None]:
        """Generate semantic segments and early-start only with a safe buffer."""

        segments = backend.semantic_segments(text)
        if len(segments) < 3:
            return False, None

        metrics: list[KokoroSegmentMetric] = []
        generated_paths: list[Path] = []
        playback_queue: deque[tuple[Path, float]] = deque()
        condition = threading.Condition()
        generation_done = False
        cancelled = False
        playback_started = threading.Event()
        playback_finished = threading.Event()

        def playback_loop() -> None:
            nonlocal cancelled
            first = True
            current_path: Path | None = None
            try:
                while True:
                    with condition:
                        while not playback_queue and not generation_done and not cancelled:
                            condition.wait(0.04)
                        if cancelled or not self._is_latest_request(request_id):
                            return
                        if not playback_queue:
                            return
                        path, duration = playback_queue.popleft()
                        current_path = path
                    if first:
                        if not self._begin_natural_playback(
                            request_id, backend, path, accent
                        ):
                            return
                        first = False
                        playback_started.set()
                        self.record_timing_event(
                            "playback_started",
                            request_id=request_id,
                            backend="Kokoro",
                            accent=accent,
                            buffered_seconds=sum(
                                item.audio_seconds for item in metrics
                            ),
                        )
                        self._emit_speech_status_if_latest(
                            request_id,
                            "已缓冲，正在播放 AI",
                        )
                    else:
                        with self._request_lock:
                            if (
                                request_id != self._latest_request_id
                                or self.stop_event.is_set()
                            ):
                                return
                            backend.play(path)
                    deadline = time.monotonic() + max(0.05, duration)
                    while time.monotonic() < deadline:
                        if (
                            cancelled
                            or self.stop_event.is_set()
                            or not self._is_latest_request(request_id)
                        ):
                            backend.stop()
                            return
                        time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
                    backend.discard(path)
                    current_path = None
            finally:
                if current_path is not None:
                    backend.discard(current_path)
                playback_finished.set()

        playback_worker: threading.Thread | None = None
        next_request: object | None = None
        try:
            self.record_timing_event(
                "ai_synthesis_started",
                request_id=request_id,
                backend="Kokoro",
                accent=accent,
                segments=len(segments),
            )
            for index, segment in enumerate(segments, start=1):
                if not self._is_latest_request(request_id) or self.stop_event.is_set():
                    cancelled = True
                    self.record_timing_event(
                        "cancelled",
                        request_id=request_id,
                        reason="superseded_during_segment_generation",
                    )
                    break
                self._emit_speech_status_if_latest(
                    request_id,
                    f"正在生成第 {index}/{len(segments)} 段",
                )
                started_at = time.monotonic()
                path, duration = backend.synthesize(segment, accent, speed)
                generation_seconds = max(0.0, time.monotonic() - started_at)
                if not self._is_latest_request(request_id) or self.stop_event.is_set():
                    backend.discard(path)
                    cancelled = True
                    self.record_timing_event(
                        "cancelled",
                        request_id=request_id,
                        reason="superseded_after_segment_generation",
                    )
                    break
                metric = KokoroSegmentMetric(
                    generation_seconds=generation_seconds,
                    audio_seconds=max(0.0, duration),
                    text_weight=KokoroSafeBufferPolicy.text_weight(segment),
                )
                metrics.append(metric)
                self.record_timing_event(
                    "segment_completed",
                    request_id=request_id,
                    segment=index,
                    total_segments=len(segments),
                    generation_seconds=generation_seconds,
                    audio_seconds=duration,
                )
                buffered_seconds = sum(item.audio_seconds for item in metrics)
                self.record_timing_event(
                    "buffer_updated",
                    request_id=request_id,
                    completed_segments=len(metrics),
                    total_segments=len(segments),
                    buffered_seconds=buffered_seconds,
                )
                self._emit_speech_status_if_latest(
                    request_id,
                    f"已生成第 {index}/{len(segments)} 段，累计缓冲 {buffered_seconds:.1f} 秒",
                )

                if playback_worker is None:
                    generated_paths.append(path)
                    safe, prediction = KokoroSafeBufferPolicy.can_start(
                        metrics,
                        segments[index:],
                    )
                    self.record_timing_event(
                        "buffer_evaluated",
                        request_id=request_id,
                        safe=safe,
                        **prediction,
                    )
                    if safe and index < len(segments):
                        initial_path, initial_duration = backend.combine_audio(
                            generated_paths
                        )
                        for generated in generated_paths:
                            backend.discard(generated)
                        generated_paths.clear()
                        with condition:
                            playback_queue.append((initial_path, initial_duration))
                        playback_worker = threading.Thread(
                            target=playback_loop,
                            daemon=True,
                            name=f"KokoroBufferedPlayback-{request_id}",
                        )
                        playback_worker.start()
                else:
                    with condition:
                        playback_queue.append((path, duration))
                        condition.notify_all()

            if cancelled:
                with condition:
                    generation_done = True
                    condition.notify_all()
                return True, None

            if playback_worker is None:
                combined_path, combined_duration = backend.combine_audio(
                    generated_paths
                )
                for generated in generated_paths:
                    backend.discard(generated)
                generated_paths.clear()
                if not self._begin_natural_playback(
                    request_id, backend, combined_path, accent
                ):
                    backend.discard(combined_path)
                    return True, None
                playback_started.set()
                self.record_timing_event(
                    "playback_started",
                    request_id=request_id,
                    backend="Kokoro",
                    accent=accent,
                    buffered_seconds=sum(item.audio_seconds for item in metrics),
                    full_sentence_buffered=True,
                )
                deadline = time.monotonic() + max(0.05, combined_duration)
                while not self.stop_event.is_set():
                    if not self._is_latest_request(request_id):
                        backend.stop()
                        break
                    try:
                        next_request = self.natural_requests.get(timeout=0.04)
                        backend.stop()
                        break
                    except queue.Empty:
                        if time.monotonic() >= deadline:
                            return True, None
                return True, next_request

            with condition:
                generation_done = True
                condition.notify_all()
            while not playback_finished.wait(0.04):
                if not self._is_latest_request(request_id) or self.stop_event.is_set():
                    cancelled = True
                    backend.stop()
                    with condition:
                        condition.notify_all()
                    break
                try:
                    next_request = self.natural_requests.get_nowait()
                except queue.Empty:
                    continue
                cancelled = True
                backend.stop()
                with condition:
                    condition.notify_all()
                break
            return True, next_request
        except Exception as exc:
            if playback_worker is None and not playback_started.is_set():
                raise
            cancelled = True
            backend.stop()
            self.record_timing_event(
                "error",
                request_id=request_id,
                backend="Kokoro",
                reason=str(exc),
                fallback_suppressed=True,
            )
            self._emit_speech_status_if_latest(
                request_id,
                "AI 分段生成失败，已停止本次播放",
            )
            with self._request_lock:
                if request_id == self._latest_request_id:
                    self._latest_request_id += 1
                    self._interactive_ai_event().clear()
            return True, None
        finally:
            with condition:
                cancelled = cancelled or not self._is_latest_request(request_id)
                generation_done = True
                queued = list(playback_queue)
                playback_queue.clear()
                condition.notify_all()
            for path, _duration in queued:
                backend.discard(path)
            for path in generated_paths:
                backend.discard(path)

    def _natural_loop(self) -> None:
        request = self.natural_requests.get()
        while request is not None and not self.stop_event.is_set():
            if request is self._PURGE:
                self._stop_neural_audio()
                request = self.natural_requests.get()
                continue
            request_id, text, accent, profile = request
            if not self._emit_speech_status_if_latest(
                request_id, f"正在生成 {accent.upper()} AI 发音…"
            ):
                request = self.natural_requests.get()
                continue
            backend, label, speed = self._natural_backend_for(text, profile)
            audio_path: Path | None = None
            next_request: object | None = None
            completed = False
            try:
                if backend is self.kokoro_backend:
                    handled, next_request = self._play_buffered_kokoro_segments(
                        request_id,
                        backend,
                        text,
                        accent,
                        speed,
                    )
                    if handled:
                        completed = (
                            next_request is None
                            and self._is_latest_request(request_id)
                        )
                        if completed:
                            self._emit_speech_status_if_latest(
                                request_id, "AI 发音播放完成"
                            )
                        if next_request is not None:
                            request = next_request
                        elif not self.stop_event.is_set():
                            request = self.natural_requests.get()
                        continue
                    self.record_timing_event(
                        "ai_synthesis_started",
                        request_id=request_id,
                        backend="Kokoro",
                        accent=accent,
                        segments=1,
                    )
                    synthesis_started_at = time.monotonic()
                    audio_path, duration = backend.synthesize(text, accent, speed)
                else:
                    self.record_timing_event(
                        "ai_synthesis_started",
                        request_id=request_id,
                        backend="Piper",
                        accent=accent,
                        segments=1,
                    )
                    synthesis_started_at = time.monotonic()
                    audio_path, duration = backend.synthesize(text, accent, speed)
                generation_seconds = max(
                    0.0, time.monotonic() - synthesis_started_at
                )
                self.record_timing_event(
                    "segment_completed",
                    request_id=request_id,
                    segment=1,
                    total_segments=1,
                    backend=label,
                    generation_seconds=generation_seconds,
                    audio_seconds=duration,
                )
                self.record_timing_event(
                    "buffer_updated",
                    request_id=request_id,
                    completed_segments=1,
                    total_segments=1,
                    buffered_seconds=duration,
                )
                if not self._begin_natural_playback(
                    request_id, backend, audio_path, accent
                ):
                    request = self.natural_requests.get()
                    continue
                self.record_timing_event(
                    "playback_started",
                    request_id=request_id,
                    backend=label,
                    accent=accent,
                    buffered_seconds=duration,
                    full_sentence_buffered=True,
                )
                deadline = time.monotonic() + max(0.05, duration)
                while not self.stop_event.is_set():
                    if not self._is_latest_request(request_id):
                        backend.stop()
                        break
                    try:
                        next_request = self.natural_requests.get(timeout=0.04)
                        backend.stop()
                        break
                    except queue.Empty:
                        if time.monotonic() >= deadline:
                            completed = self._is_latest_request(request_id)
                            break
            except Exception as exc:
                with self._request_lock:
                    if (
                        request_id == self._latest_request_id
                        and not self.stop_event.is_set()
                    ):
                        self.record_timing_event(
                            "fallback",
                            request_id=request_id,
                            backend=label,
                            reason=str(exc),
                        )
                        self._report_natural_fallback(exc, label)
                        self._replace_queue(
                            self.requests,
                            (request_id, text, accent, "fallback"),
                        )
            finally:
                if audio_path is not None:
                    backend.discard(audio_path)
                self._finish_interactive_ai_request(request_id)
            if completed:
                self._emit_speech_status_if_latest(
                    request_id, "AI 发音播放完成"
                )
            if next_request is not None:
                request = next_request
            elif not self.stop_event.is_set():
                request = self.natural_requests.get()

    def _prefetch_loop(self) -> None:
        while not self.stop_event.is_set():
            with self._prefetch_condition:
                while (
                    self._prefetch_pending is None
                    and not self.stop_event.is_set()
                ):
                    self._prefetch_condition.wait()
                if self.stop_event.is_set():
                    return
                generation, text, profile, accents = self._prefetch_pending
                self._prefetch_pending = None
                self._prefetch_active_key = (text, profile)
                self._prefetch_active_generation = generation
                self._prefetch_active_current = None
                self._prefetch_active_accents = set(accents)

            while not self.stop_event.is_set():
                with self._prefetch_condition:
                    if generation != self._prefetch_generation:
                        if self._prefetch_active_generation == generation:
                            self._prefetch_active_key = None
                            self._prefetch_active_generation = 0
                            self._prefetch_active_current = None
                            self._prefetch_active_accents.clear()
                        break
                    accent = next(
                        (
                            candidate
                            for candidate in ("us", "uk")
                            if candidate in self._prefetch_active_accents
                        ),
                        None,
                    )
                    if accent is None:
                        if self._prefetch_active_generation == generation:
                            self._prefetch_active_key = None
                            self._prefetch_active_generation = 0
                            self._prefetch_active_current = None
                            self._prefetch_active_accents.clear()
                        break
                    self._prefetch_active_accents.remove(accent)
                    self._prefetch_active_current = accent
                backend, label, speed = self._natural_backend_for(text, profile)
                try:
                    with self._prefetch_condition:
                        if generation != self._prefetch_generation:
                            continue
                    self.record_timing_event(
                        "ai_candidate_synthesis_started",
                        generation=generation,
                        backend=label,
                        accent=accent,
                    )
                    if backend is self.kokoro_backend:
                        candidate_segments = backend.semantic_segments(text)
                        candidate_text = candidate_segments[0] if candidate_segments else text
                        path, _duration = backend.synthesize(
                            candidate_text,
                            accent,
                            speed,
                            progress_callback=lambda _index, _total: (
                                generation == self._prefetch_generation
                                and not self.stop_event.is_set()
                            ),
                        )
                    elif isinstance(backend, PiperSpeechBackend):
                        path, _duration = backend.synthesize(
                            text,
                            accent,
                            speed,
                            cancel_callback=lambda: (
                                generation == self._prefetch_generation
                                and not self.stop_event.is_set()
                            ),
                        )
                    else:
                        path, _duration = backend.synthesize(text, accent, speed)
                    backend.discard(path)
                except Exception as exc:
                    log(f"{label} speech prefetch skipped: {exc}")
                finally:
                    with self._prefetch_condition:
                        if self._prefetch_active_generation == generation:
                            self._prefetch_active_current = None

            with self._prefetch_condition:
                if self._prefetch_active_generation == generation:
                    self._prefetch_active_key = None
                    self._prefetch_active_generation = 0
                    self._prefetch_active_current = None
                    self._prefetch_active_accents.clear()

    @staticmethod
    def _token_attribute(token: object, name: str) -> str:
        try:
            return str(token.GetAttribute(name) or "").strip()
        except Exception:
            return ""

    @classmethod
    def _find_system_voice(cls, voice: object, accent: str) -> object | None:
        language, name, vendor, token_suffix = cls.SYSTEM_VOICES[accent]
        tokens = voice.GetVoices()
        candidates: list[tuple[int, int, object]] = []
        for index in range(tokens.Count):
            token = tokens.Item(index)
            try:
                token_id = str(getattr(token, "Id") or "").strip()
            except Exception:
                token_id = ""
            token_languages = {
                item.strip().casefold()
                for item in re.split(",|;", cls._token_attribute(token, "Language"))
                if item.strip()
            }
            if (
                language.casefold() not in token_languages
                or cls._token_attribute(token, "Vendor").casefold()
                != vendor.casefold()
            ):
                continue
            score = 0
            if cls._token_attribute(token, "Name").casefold() == name.casefold():
                score += 2
            if token_id.casefold().endswith(token_suffix.casefold()):
                score += 1
            candidates.append((score, -index, token))
        if not candidates:
            return None
        # Zira/Hazel win when present.  A different Microsoft English token is
        # a safe fallback; a third-party token with the same LCID never enters
        # the candidate set.
        return max(candidates, key=lambda item: (item[0], item[1]))[2]

    def _speech_loop(self) -> None:
        pythoncom.CoInitialize()
        voice = None
        voice_tokens: dict[str, object] = {}
        try:
            request = self.requests.get()
            while request is not None and not self.stop_event.is_set():
                if request is self._PURGE:
                    if voice is not None:
                        try:
                            voice.Speak("", 2)
                        except Exception as exc:
                            log(f"Speech purge error: {exc}\n{traceback.format_exc()}")
                            voice = None
                            voice_tokens.clear()
                    request = self.requests.get()
                    continue
                request_id, text, accent, mode = request
                if not self._is_latest_request(request_id):
                    request = self.requests.get()
                    continue
                try:
                    if voice is None:
                        voice = win32com.client.Dispatch("SAPI.SpVoice")
                    selected = voice_tokens.get(accent)
                    if selected is None:
                        selected = self._find_system_voice(voice, accent)
                        if selected is not None:
                            voice_tokens[accent] = selected
                    if selected is None:
                        raise RuntimeError("系统中没有指定的微软英语语音")
                    if not self._begin_system_playback(
                        request_id, voice, selected, text, accent
                    ):
                        request = self.requests.get()
                        continue
                    request = None
                    completed = False
                    while not self.stop_event.is_set():
                        if not self._is_latest_request(request_id):
                            try:
                                voice.Speak("", 2)
                            except Exception:
                                voice = None
                                voice_tokens.clear()
                            break
                        try:
                            request = self.requests.get(timeout=0.04)
                            break
                        except queue.Empty:
                            if bool(voice.WaitUntilDone(1)):
                                completed = self._is_latest_request(request_id)
                                break
                    if completed:
                        self._emit_speech_status_if_latest(
                            request_id, "微软原版发音播放完成"
                        )
                    if request is None and not self.stop_event.is_set():
                        request = self.requests.get()
                except Exception as exc:
                    log(f"Speech error: {exc}\n{traceback.format_exc()}")
                    with self._request_lock:
                        if (
                            request_id == self._latest_request_id
                            and not self.stop_event.is_set()
                        ):
                            self._emit_speech_status("微软原版发音失败")
                            if self.status_callback:
                                accent_name = "美式" if accent == "us" else "英式"
                                if "指定的微软英语语音" in str(exc):
                                    self.status_callback(
                                        f"未安装 Microsoft {accent_name}英语语音，请在 Windows 语音设置中添加"
                                    )
                                else:
                                    self.status_callback(
                                        "发音失败，请检查 Windows 语音设置"
                                    )
                    voice = None
                    voice_tokens.clear()
                    request = self.requests.get()
        finally:
            if voice is not None:
                try:
                    voice.Speak("", 2)
                except Exception:
                    pass
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
