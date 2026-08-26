import json
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
import wave
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app as engine


_ORIGINAL_LOG_PATH = engine.LOG_PATH
_TEST_LOG_DIRECTORY: tempfile.TemporaryDirectory[str] | None = None


def setUpModule() -> None:
    global _TEST_LOG_DIRECTORY
    _TEST_LOG_DIRECTORY = tempfile.TemporaryDirectory(prefix="dashengfa-engine-tests-")
    engine.LOG_PATH = Path(_TEST_LOG_DIRECTORY.name) / "app.log"


def tearDownModule() -> None:
    engine.LOG_PATH = _ORIGINAL_LOG_PATH
    if _TEST_LOG_DIRECTORY is not None:
        _TEST_LOG_DIRECTORY.cleanup()


class TranslationTextTests(unittest.TestCase):
    def test_legacy_data_migration_retries_after_a_temporary_copy_failure(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            legacy = base / engine.LEGACY_APP_DIR_NAME
            legacy.mkdir()
            (legacy / "settings.json").write_text("{}", encoding="utf-8")
            (legacy / "translation-cache.db").write_bytes(b"cache")
            original_copy = engine.shutil.copy2
            attempts = 0

            def flaky_copy(source: object, target: object) -> object:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("temporary")
                return original_copy(source, target)

            with mock.patch.dict(engine.os.environ, {"LOCALAPPDATA": folder}), mock.patch.object(
                engine.shutil, "copy2", side_effect=flaky_copy
            ):
                data_path = engine.user_data_dir()

            self.assertFalse((data_path / ".legacy-migration-complete").exists())

            with mock.patch.dict(engine.os.environ, {"LOCALAPPDATA": folder}):
                data_path = engine.user_data_dir()

            self.assertTrue((data_path / "settings.json").exists())
            self.assertTrue((data_path / "translation-cache.db").exists())
            self.assertTrue((data_path / ".legacy-migration-complete").exists())

    def test_normalize_selection_collapses_crlf_to_one_newline(self) -> None:
        self.assertEqual(engine.normalize_selection("line1\r\nline2"), "line1\nline2")

    def test_dictionary_decodes_escaped_line_breaks(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ecdict.db"
            with closing(sqlite3.connect(path)) as db:
                with db:
                    db.execute(
                        "CREATE TABLE entries (word TEXT, phonetic TEXT, translation TEXT, definition TEXT)"
                    )
                    db.execute(
                        "INSERT INTO entries VALUES (?, ?, ?, ?)",
                        ("integration", "test", r"综合\n[化] 集成", r"one\r\ntwo"),
                    )
            dictionary = engine.DictionaryLookup.__new__(engine.DictionaryLookup)
            dictionary.path = path

            phonetic, translation, definition = dictionary.lookup("integration")

        self.assertEqual(phonetic, "test")
        self.assertEqual(translation, "综合\n[化] 集成")
        self.assertEqual(definition, "one\ntwo")

    def test_cached_dictionary_result_is_sanitized_after_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache = engine.TranslationCache.__new__(engine.TranslationCache)
            cache.path = Path(folder) / "cache.db"
            cache.lock = threading.Lock()
            with closing(sqlite3.connect(cache.path)) as db:
                with db:
                    db.execute(
                        "CREATE TABLE translations (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at INTEGER NOT NULL)"
                    )
                    payload = {
                        "source": "integration",
                        "translated": r"综合\n集成",
                        "source_language": "en",
                        "target_language": "zh",
                        "engine": "ECDICT 本地词典",
                        "phonetic": "",
                        "definition": r"one\ntwo",
                        "elapsed_ms": 9,
                    }
                    db.execute(
                        "INSERT INTO translations VALUES (?, ?, ?)",
                        ("en>zh:integration", json.dumps(payload, ensure_ascii=False), 1),
                    )

            result = cache.get("integration", "en", "zh")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.translated, "综合\n集成")
        self.assertEqual(result.definition, "one\ntwo")

    def test_stale_english_definition_cache_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache = engine.TranslationCache.__new__(engine.TranslationCache)
            cache.path = Path(folder) / "cache.db"
            cache.lock = threading.Lock()
            with closing(sqlite3.connect(cache.path)) as db:
                with db:
                    db.execute(
                        "CREATE TABLE translations (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at INTEGER NOT NULL)"
                    )
                    payload = {
                        "source": "absconce",
                        "translated": "to hide oneself",
                        "source_language": "en",
                        "target_language": "zh",
                        "engine": "ECDICT 本地词典",
                        "phonetic": "",
                        "definition": "to hide oneself",
                        "elapsed_ms": 3,
                    }
                    db.execute(
                        "INSERT INTO translations VALUES (?, ?, ?)",
                        ("en>zh:absconce", json.dumps(payload), 1),
                    )

            result = cache.get("absconce", "en", "zh")

        self.assertIsNone(result)

    def test_missing_chinese_dictionary_translation_falls_back_to_model(self) -> None:
        translator = engine.LocalTranslator.__new__(engine.LocalTranslator)
        translator.status_callback = mock.Mock()
        translator.dictionary = SimpleNamespace(
            lookup=lambda _word: ("phonetic", "", "English definition")
        )
        model = SimpleNamespace(translate_batch=mock.Mock(return_value=["中文译文"]))
        translator.models = {"en-zh": model}
        translator.cache = SimpleNamespace(get=lambda *_args: None, put=mock.Mock())

        result = translator.translate("absconce")

        self.assertEqual(result.translated, "中文译文")
        self.assertEqual(result.phonetic, "phonetic")
        self.assertEqual(result.definition, "English definition")
        self.assertEqual(result.engine, "Argos/OPUS 本地模型")
        translator.cache.put.assert_called_once_with(result)


class SpeechQueueTests(unittest.TestCase):
    @staticmethod
    def fixture_resource_hashes(payload: bytes = b"fixture") -> dict[str, str]:
        digest = engine.hashlib.sha256(payload).hexdigest()
        return {
            key: digest for key in engine.PiperSpeechBackend.RESOURCE_SHA256
        }

    def make_unstarted_player(self) -> engine.SpeechPlayer:
        player = engine.SpeechPlayer.__new__(engine.SpeechPlayer)
        player.status_callback = None
        player.speech_status_callback = None
        player.requests = queue.Queue(maxsize=1)
        player.natural_requests = queue.Queue(maxsize=1)
        player._prefetch_condition = threading.Condition()
        player._prefetch_generation = 0
        player._prefetch_pending = None
        player._prefetch_active_key = None
        player._prefetch_active_generation = 0
        player._prefetch_active_current = None
        player._prefetch_active_accents = set()
        player.stop_event = threading.Event()
        player._natural_speed_lock = threading.Lock()
        player._natural_speed_profile = engine.DEFAULT_NATURAL_SPEECH_SPEED
        player._request_lock = threading.Lock()
        player._latest_request_id = 0
        player._speech_status_lock = threading.Lock()
        player._natural_fallback_reported = False
        player.piper_backend = SimpleNamespace(stop=mock.Mock())
        player.kokoro_backend = SimpleNamespace(stop=mock.Mock())
        return player

    def test_kokoro_request_is_discarded_when_a_newer_request_arrives(self) -> None:
        player = self.make_unstarted_player()
        with tempfile.TemporaryDirectory() as folder:
            audio_path = Path(folder) / "old.wav"
            audio_path.write_bytes(b"placeholder")
            backend = SimpleNamespace(
                synthesize=mock.Mock(return_value=(audio_path, 0.1)),
                play=mock.Mock(),
                stop=mock.Mock(),
                discard=lambda path: path.unlink(missing_ok=True),
            )
            player.natural_requests.put(("latest", "uk"))

            handled, next_request = player._speak_with_kokoro(
                backend, "old", "us"
            )

        self.assertTrue(handled)
        self.assertEqual(next_request, ("latest", "uk"))
        backend.play.assert_not_called()
        self.assertFalse(audio_path.exists())

    def test_kokoro_backend_rejects_missing_model_resources(self) -> None:
        backend = engine.KokoroSpeechBackend()
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            engine, "resource_path", return_value=Path(folder) / "missing"
        ):
            with self.assertRaises(engine.KokoroSpeechUnavailable):
                backend.synthesize("integration", "us")

    def test_kokoro_prepares_espeak_data_in_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "unpacked" / "espeak-ng-data"
            source.mkdir(parents=True)
            (source / "phontab").write_bytes(b"fixture")
            (source / "voices").mkdir()
            with mock.patch.object(engine, "user_data_dir", return_value=root / "appdata"):
                destination = engine.KokoroSpeechBackend._prepare_espeak_data(source)
            self.assertEqual(destination, root / "appdata" / "runtime" / "espeak-ng-data")
            self.assertEqual((destination / "phontab").read_bytes(), b"fixture")

    def test_kokoro_returns_ascii_short_path_for_unicode_espeak_cache(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "unpacked" / "espeak-ng-data"
            source.mkdir(parents=True)
            (source / "phontab").write_bytes(b"fixture")
            short_path = Path(r"C:\SHORT\ESPEAK~1")
            with mock.patch.object(
                engine, "user_data_dir", return_value=root / "用户"
            ), mock.patch.object(
                engine.PiperSpeechBackend,
                "_ascii_native_path",
                return_value=str(short_path).encode("ascii"),
            ):
                destination = engine.KokoroSpeechBackend._prepare_espeak_data(source)

        self.assertEqual(destination, short_path)

    def test_kokoro_rejects_espeak_cache_without_ascii_representation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "用户" / "espeak-ng-data"
            destination.mkdir(parents=True)
            with mock.patch.object(
                engine.PiperSpeechBackend,
                "_ascii_native_path",
                side_effect=engine.PiperSpeechUnavailable("no short path"),
            ):
                with self.assertRaises(engine.KokoroSpeechUnavailable):
                    engine.KokoroSpeechBackend._ascii_espeak_data_path(destination)

    def test_kokoro_reuses_prefetched_audio_for_the_same_voice_and_speed(self) -> None:
        backend = engine.KokoroSpeechBackend()
        backend._engine = SimpleNamespace(
            create=mock.Mock(return_value=([0.0, 0.25, -0.25], 24000))
        )
        try:
            first, _duration = backend.synthesize("comfortable", "us", 0.85)
            backend.discard(first)
            second, _duration = backend.synthesize("comfortable", "us", 0.85)
            self.assertTrue(second.is_file())
            backend.discard(second)
        finally:
            backend.close()
        backend._engine.create.assert_called_once_with(
            "comfortable", voice="af_bella", speed=0.85, lang="en-us"
        )

    def test_kokoro_splits_long_text_and_joins_all_segments(self) -> None:
        backend = engine.KokoroSpeechBackend()
        backend._engine = SimpleNamespace(
            create=mock.Mock(return_value=([0.0, 0.25, -0.25], 24000))
        )
        text = "One short sentence. Two short sentence. Three short sentence."
        progress: list[tuple[int, int]] = []
        expected_chunks = backend._split_synthesis_chunks(text)
        try:
            path, duration = backend.synthesize(
                text,
                "us",
                1.0,
                progress_callback=lambda index, total: (
                    progress.append((index, total)) or True
                ),
            )
            with wave.open(str(path), "rb") as stream:
                self.assertEqual(stream.getframerate(), 24000)
                self.assertGreater(stream.getnframes(), 9)
            backend.discard(path)
        finally:
            backend.close()

        self.assertGreater(len(expected_chunks), 1)
        self.assertEqual(
            [call.args[0] for call in backend._engine.create.call_args_list],
            expected_chunks,
        )
        self.assertEqual(
            progress,
            [(index, len(expected_chunks)) for index in range(1, len(expected_chunks) + 1)],
        )
        self.assertGreater(duration, 0.12)

    def test_kokoro_stops_before_the_next_segment_when_cancelled(self) -> None:
        backend = engine.KokoroSpeechBackend()
        backend._engine = SimpleNamespace(
            create=mock.Mock(return_value=([0.0, 0.25, -0.25], 24000))
        )
        with self.assertRaisesRegex(
            engine.KokoroSpeechUnavailable, "cancelled"
        ):
            backend.synthesize(
                "One short sentence. Two short sentence.",
                "us",
                1.0,
                progress_callback=lambda index, _total: index < 2,
            )
        backend.close()
        self.assertEqual(backend._engine.create.call_count, 1)

    def test_latest_speech_request_replaces_previous_request(self) -> None:
        player = self.make_unstarted_player()

        player.speak("first", "us")
        player.speak("second", "uk")

        request_id, text, accent, profile = player.natural_requests.get_nowait()
        self.assertEqual((text, accent, profile), ("second", "uk", "standard"))
        self.assertEqual(request_id, 2)
        self.assertIs(player.requests.get_nowait(), engine.SpeechPlayer._PURGE)
        with self.assertRaises(queue.Empty):
            player.natural_requests.get_nowait()

    def test_sentence_length_never_changes_an_explicit_ai_route(self) -> None:
        player = self.make_unstarted_player()
        statuses: list[str] = []
        player.speech_status_callback = statuses.append
        text = "This deliberately long sentence is routed straight to the local Windows voice."

        player.speak(text, "us")

        request_id, queued_text, accent, profile = player.natural_requests.get_nowait()
        self.assertEqual((queued_text, accent, profile), (text, "us", "standard"))
        self.assertEqual(request_id, player._latest_request_id)
        self.assertIs(player.requests.get_nowait(), engine.SpeechPlayer._PURGE)
        self.assertFalse(any("3 秒" in status for status in statuses))

    def test_long_sentence_can_be_a_double_click_candidate_without_routing_to_sapi(self) -> None:
        player = self.make_unstarted_player()

        player.prefetch(
            "This deliberately long sentence must not occupy the Kokoro prefetch worker.",
            "us",
        )

        self.assertIsNotNone(player._prefetch_pending)
        assert player._prefetch_pending is not None
        self.assertEqual(player._prefetch_pending[1], "This deliberately long sentence must not occupy the Kokoro prefetch worker.")

    def test_safe_buffer_policy_starts_only_after_real_buffer_and_prediction(self) -> None:
        safe_metrics = [
            engine.KokoroSegmentMetric(1.0, 3.0, 5),
            engine.KokoroSegmentMetric(1.2, 3.0, 5),
        ]
        safe, details = engine.KokoroSafeBufferPolicy.can_start(
            safe_metrics,
            ["A final natural clause."],
        )
        self.assertTrue(safe)
        self.assertGreaterEqual(details["buffered_seconds"], 5.0)

        suddenly_slow = [
            engine.KokoroSegmentMetric(1.0, 2.5, 5),
            engine.KokoroSegmentMetric(4.5, 2.5, 5),
        ]
        safe, details = engine.KokoroSafeBufferPolicy.can_start(
            suddenly_slow,
            ["A final natural clause."],
        )
        self.assertFalse(safe)
        self.assertGreater(
            details["estimated_remaining_generation_seconds"],
            details["buffered_seconds"],
        )

    def test_semantic_split_never_hard_cuts_unpunctuated_text(self) -> None:
        text = "one " * 500
        self.assertEqual(
            engine.KokoroSpeechBackend.semantic_segments(text),
            [text.strip()],
        )

    def test_buffered_kokoro_early_play_starts_after_two_complete_segments(self) -> None:
        player = self.make_unstarted_player()
        player._latest_request_id = 1
        played = threading.Event()
        synth_calls: list[str] = []

        class Backend:
            @staticmethod
            def semantic_segments(_text: str) -> list[str]:
                return ["First clause,", "second clause,", "final clause."]

            def synthesize(self, text: str, _accent: str, _speed: float):
                if text == "final clause.":
                    self_test.assertTrue(played.wait(0.5))
                synth_calls.append(text)
                return Path(f"segment-{len(synth_calls)}.wav"), 0.01

            @staticmethod
            def combine_audio(_paths: list[Path]):
                return Path("initial-buffer.wav"), 0.01

            @staticmethod
            def play(_path: Path) -> None:
                played.set()

            stop = mock.Mock()
            discard = mock.Mock()

        self_test = self
        backend = Backend()
        player.kokoro_backend = backend
        with mock.patch.object(
            engine.KokoroSafeBufferPolicy,
            "can_start",
            side_effect=lambda metrics, _remaining: (
                len(metrics) >= 2,
                {
                    "buffered_seconds": sum(item.audio_seconds for item in metrics),
                    "estimated_remaining_generation_seconds": 0.0,
                },
            ),
        ):
            handled, next_request = player._play_buffered_kokoro_segments(
                1,
                backend,
                "ignored",
                "us",
                1.0,
            )

        self.assertTrue(handled)
        self.assertIsNone(next_request)
        self.assertEqual(len(synth_calls), 3)
        self.assertTrue(played.is_set())

    def test_unsafe_buffer_waits_for_all_segments_before_playing(self) -> None:
        player = self.make_unstarted_player()
        player._latest_request_id = 1
        player._timing_lock = threading.Lock()
        player._timing_events = []
        synth_calls: list[str] = []

        class Backend:
            @staticmethod
            def semantic_segments(_text: str) -> list[str]:
                return ["First clause,", "second clause,", "final clause."]

            @staticmethod
            def synthesize(text: str, _accent: str, _speed: float):
                synth_calls.append(text)
                return Path(f"segment-{len(synth_calls)}.wav"), 0.01

            @staticmethod
            def combine_audio(_paths: list[Path]):
                return Path("full-buffer.wav"), 0.01

            @staticmethod
            def play(_path: Path) -> None:
                if len(synth_calls) != 3:
                    raise AssertionError("playback started before full safe buffer")

            stop = mock.Mock()
            discard = mock.Mock()

        backend = Backend()
        player.kokoro_backend = backend
        with mock.patch.object(
            engine.KokoroSafeBufferPolicy,
            "can_start",
            return_value=(
                False,
                {
                    "buffered_seconds": 0.0,
                    "estimated_remaining_generation_seconds": 10.0,
                },
            ),
        ):
            handled, next_request = player._play_buffered_kokoro_segments(
                1,
                backend,
                "ignored",
                "us",
                1.0,
            )

        self.assertTrue(handled)
        self.assertIsNone(next_request)
        self.assertEqual(len(synth_calls), 3)
        event_names = [item["event"] for item in player.timing_events()]
        self.assertLess(
            event_names.index("ai_synthesis_started"),
            event_names.index("segment_completed"),
        )
        self.assertLess(
            event_names.index("segment_completed"),
            event_names.index("buffer_updated"),
        )
        self.assertLess(
            event_names.index("buffer_updated"),
            event_names.index("playback_started"),
        )

    def test_cancelled_long_sentence_never_starts_old_audio(self) -> None:
        player = self.make_unstarted_player()
        player._latest_request_id = 1

        class Backend:
            @staticmethod
            def semantic_segments(_text: str) -> list[str]:
                return ["First clause,", "second clause,", "final clause."]

            def synthesize(self, text: str, _accent: str, _speed: float):
                if text == "second clause,":
                    player._latest_request_id = 2
                return Path(f"{text[:3]}.wav"), 3.0

            combine_audio = mock.Mock()
            play = mock.Mock()
            stop = mock.Mock()
            discard = mock.Mock()

        backend = Backend()
        player.kokoro_backend = backend
        handled, next_request = player._play_buffered_kokoro_segments(
            1,
            backend,
            "ignored",
            "us",
            1.0,
        )

        self.assertTrue(handled)
        self.assertIsNone(next_request)
        backend.play.assert_not_called()
        backend.combine_audio.assert_not_called()

    def test_segment_failure_after_ai_start_never_switches_to_sapi(self) -> None:
        player = self.make_unstarted_player()
        player._latest_request_id = 1
        played = threading.Event()

        class Backend:
            @staticmethod
            def semantic_segments(_text: str) -> list[str]:
                return ["First clause,", "second clause,", "final clause."]

            @staticmethod
            def synthesize(text: str, _accent: str, _speed: float):
                if text == "final clause.":
                    if not played.wait(0.5):
                        raise AssertionError("buffered AI did not start")
                    raise engine.KokoroSpeechUnavailable("simulated slowdown failure")
                return Path(f"{text[:3]}.wav"), 0.01

            @staticmethod
            def combine_audio(_paths: list[Path]):
                return Path("initial-buffer.wav"), 0.05

            @staticmethod
            def play(_path: Path) -> None:
                played.set()

            stop = mock.Mock()
            discard = mock.Mock()

        backend = Backend()
        player.kokoro_backend = backend
        with mock.patch.object(
            engine.KokoroSafeBufferPolicy,
            "can_start",
            side_effect=lambda metrics, _remaining: (
                len(metrics) >= 2,
                {
                    "buffered_seconds": 5.0,
                    "estimated_remaining_generation_seconds": 1.0,
                },
            ),
        ):
            handled, next_request = player._play_buffered_kokoro_segments(
                1,
                backend,
                "ignored",
                "us",
                1.0,
            )

        self.assertTrue(handled)
        self.assertIsNone(next_request)
        backend.stop.assert_called()
        with self.assertRaises(queue.Empty):
            player.requests.get_nowait()

    def test_new_request_wins_barrier_before_old_natural_play_and_status(self) -> None:
        player = self.make_unstarted_player()
        player._request_lock = threading.RLock()
        player._latest_request_id = 1
        statuses: list[str] = []
        player.speech_status_callback = statuses.append
        old_backend = SimpleNamespace(play=mock.Mock())
        barrier = threading.Barrier(2)
        results: list[bool] = []

        def begin_old() -> None:
            barrier.wait(timeout=1.0)
            results.append(
                player._begin_natural_playback(
                    1, old_backend, Path("old.wav"), "us"
                )
            )

        player._request_lock.acquire()
        worker = threading.Thread(target=begin_old)
        worker.start()
        try:
            barrier.wait(timeout=1.0)
            player.speak("new", "uk", mode="natural")
        finally:
            player._request_lock.release()
        worker.join(1.0)

        self.assertEqual(results, [False])
        old_backend.play.assert_not_called()
        self.assertNotIn("正在播放 US AI 发音", statuses)

    def test_new_request_wins_barrier_before_old_sapi_play_and_status(self) -> None:
        player = self.make_unstarted_player()
        player._request_lock = threading.RLock()
        player._latest_request_id = 1
        statuses: list[str] = []
        player.speech_status_callback = statuses.append
        old_voice = SimpleNamespace(Voice=None, Rate=0, Speak=mock.Mock())
        old_token = object()
        barrier = threading.Barrier(2)
        results: list[bool] = []

        def begin_old() -> None:
            barrier.wait(timeout=1.0)
            results.append(
                player._begin_system_playback(
                    1, old_voice, old_token, "old", "us"
                )
            )

        player._request_lock.acquire()
        worker = threading.Thread(target=begin_old)
        worker.start()
        try:
            barrier.wait(timeout=1.0)
            player.speak("new", "uk", mode="system")
        finally:
            player._request_lock.release()
        worker.join(1.0)

        self.assertEqual(results, [False])
        old_voice.Speak.assert_not_called()
        self.assertNotIn("正在播放 US 微软原版发音", statuses)

    def test_new_natural_speed_cancels_old_audio_and_applies_to_next_request(self) -> None:
        player = self.make_unstarted_player()
        player.speak("first", "us")

        self.assertEqual(player.set_natural_speed("slow"), "slow")
        self.assertIs(player.requests.get_nowait(), engine.SpeechPlayer._PURGE)
        self.assertIs(
            player.natural_requests.get_nowait(), engine.SpeechPlayer._PURGE
        )

        player.speak("second", "us")
        _request_id, text, accent, profile = player.natural_requests.get_nowait()
        self.assertEqual((text, accent, profile), ("second", "us", "slow"))

    def test_prefetch_queues_an_idle_neural_request(self) -> None:
        player = self.make_unstarted_player()

        player.prefetch("comfortable", "us")

        generation, text, profile, accents = player._prefetch_pending
        self.assertEqual(
            (generation, text, profile, accents),
            (1, "comfortable", "standard", {"us"}),
        )

    def test_sequential_prefetch_merges_us_and_uk_for_the_same_text(self) -> None:
        player = self.make_unstarted_player()

        player.prefetch("comfortable", "us")
        player.prefetch("comfortable", "uk")

        generation, text, profile, accents = player._prefetch_pending
        self.assertEqual(generation, 1)
        self.assertEqual((text, profile), ("comfortable", "standard"))
        self.assertEqual(accents, {"us", "uk"})

    def test_concurrent_prefetch_merges_both_accents_atomically(self) -> None:
        player = self.make_unstarted_player()
        barrier = threading.Barrier(2)

        def request(accent: str) -> None:
            barrier.wait(timeout=1.0)
            player.prefetch("comfortable", accent)

        workers = [
            threading.Thread(target=request, args=(accent,))
            for accent in ("us", "uk")
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(1.0)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        generation, text, profile, accents = player._prefetch_pending
        self.assertEqual(generation, 1)
        self.assertEqual((text, profile), ("comfortable", "standard"))
        self.assertEqual(accents, {"us", "uk"})

    def test_new_prefetch_text_replaces_the_old_bounded_batch(self) -> None:
        player = self.make_unstarted_player()
        player.prefetch("comfortable", "us")
        player.prefetch("comfortable", "uk")

        player.prefetch("replacement", "uk")

        generation, text, profile, accents = player._prefetch_pending
        self.assertEqual(generation, 2)
        self.assertEqual((text, profile, accents), ("replacement", "standard", {"uk"}))

    def test_second_accent_merges_while_first_accent_is_synthesizing(self) -> None:
        player = self.make_unstarted_player()
        us_started = threading.Event()
        release_us = threading.Event()
        both_finished = threading.Event()
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def synthesize(_text: str, accent: str, _speed: float) -> tuple[Path, float]:
                calls.append(accent)
                if accent == "us":
                    us_started.set()
                    release_us.wait(1.0)
                path = root / f"{accent}.wav"
                path.write_bytes(b"fixture")
                if len(calls) == 2:
                    both_finished.set()
                return path, 0.01

            player.piper_backend = SimpleNamespace(
                synthesize=synthesize,
                discard=lambda path: path.unlink(missing_ok=True),
                stop=mock.Mock(),
            )
            worker = threading.Thread(target=player._prefetch_loop)
            player.prefetch("comfortable", "us")
            worker.start()
            try:
                self.assertTrue(us_started.wait(0.5))
                player.prefetch("comfortable", "uk")
                release_us.set()
                self.assertTrue(both_finished.wait(1.0))
            finally:
                player.stop_event.set()
                with player._prefetch_condition:
                    player._prefetch_condition.notify_all()
                worker.join(1.0)

        self.assertEqual(calls, ["us", "uk"])

    def test_kokoro_profile_never_exceeds_previous_fast_rate(self) -> None:
        self.assertEqual(engine.natural_speech_speed_value("fast"), 1.0)
        self.assertEqual(engine.natural_speech_speed_value("standard"), 0.85)
        self.assertEqual(engine.natural_speech_speed_value("slow"), 0.75)
        self.assertEqual(
            engine.natural_speech_speed_value("unexpected"),
            engine.natural_speech_speed_value(engine.DEFAULT_NATURAL_SPEECH_SPEED),
        )

    def test_melo_word_voice_never_uses_post_processing_speed(self) -> None:
        self.assertEqual(engine.piper_speech_speed_value("slow"), 1.0)
        self.assertEqual(engine.piper_speech_speed_value("standard"), 1.0)
        self.assertEqual(engine.piper_speech_speed_value("fast"), 1.0)
        self.assertEqual(engine.piper_speech_speed_value("unexpected"), 1.0)

    def test_natural_words_route_to_melo_and_sentences_route_to_kokoro(self) -> None:
        player = self.make_unstarted_player()

        backend, label, speed = player._natural_backend_for("comfortable", "fast")
        self.assertIs(backend, player.piper_backend)
        self.assertEqual((label, speed), ("Piper", 1.0))

        backend, label, speed = player._natural_backend_for(
            "This is comfortable.", "fast"
        )
        self.assertIs(backend, player.kokoro_backend)
        self.assertEqual((label, speed), ("Kokoro", 1.0))

    @unittest.skip("CrispASR/Melo runtime was replaced by Piper")
    def test_melo_c_abi_synthesizes_and_frees_owned_pcm(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runtime_root = Path(folder)
            (runtime_root / "runtime").mkdir()
            samples = (engine.ctypes.c_float * 4)(0.0, 0.25, -0.25, 0.0)

            def synthesize(_session: object, _text: bytes, out_count: object) -> object:
                engine.ctypes.cast(
                    out_count, engine.ctypes.POINTER(engine.ctypes.c_int)
                )[0] = 4
                return engine.ctypes.cast(
                    samples, engine.ctypes.POINTER(engine.ctypes.c_float)
                )

            library = SimpleNamespace(
                crispasr_session_open_explicit=mock.Mock(return_value=1234),
                crispasr_session_set_speaker_id=mock.Mock(return_value=0),
                crispasr_session_synthesize=mock.Mock(side_effect=synthesize),
                crispasr_pcm_free=mock.Mock(),
                crispasr_session_close=mock.Mock(),
                crispasr_session_output_sample_rate=mock.Mock(return_value=44_100),
                crispasr_session_last_synth_error=mock.Mock(return_value=None),
            )
            backend = engine.PiperSpeechBackend()
            with mock.patch.object(
                backend, "_prepare_runtime_files", return_value=runtime_root
            ), mock.patch.object(engine.ctypes, "WinDLL", return_value=library):
                path, duration = backend.synthesize("word", "uk", 1.0)
                try:
                    self.assertTrue(path.is_file())
                    with wave.open(str(path), "rb") as stream:
                        self.assertEqual(stream.getframerate(), 24_000)
                        self.assertEqual(stream.getnframes(), 4)
                    self.assertAlmostEqual(duration, 4 / 24_000)
                finally:
                    backend.discard(path)
                    backend.close()

            library.crispasr_session_open_explicit.assert_called_once_with(
                str(runtime_root / engine.PiperSpeechBackend.MODEL_FILENAME).encode(
                    "ascii"
                ),
                b"melotts",
                engine.MELO_CPU_THREADS,
            )
            library.crispasr_session_set_speaker_id.assert_called_once_with(1234, 1)
            library.crispasr_session_output_sample_rate.assert_not_called()
            library.crispasr_pcm_free.assert_called_once()
            library.crispasr_session_close.assert_called_once_with(1234)

    def test_melo_rejects_non_native_post_processing_speed(self) -> None:
        backend = engine.PiperSpeechBackend()
        with self.assertRaisesRegex(
            engine.PiperSpeechUnavailable, "post-processing speed is disabled"
        ):
            backend.synthesize("word", "us", 0.8)

    @unittest.skip("Piper loads Unicode paths directly and no longer stages CrispASR")
    def test_melo_stages_only_when_resource_path_is_not_ascii(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source_root = root / "模型"
            for relative in engine.PiperSpeechBackend._required_resources():
                path = source_root.joinpath(*relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            appdata = root / "ascii-appdata"
            with mock.patch.object(
                engine, "resource_path", return_value=source_root
            ), mock.patch.object(
                engine, "user_data_dir", return_value=appdata
            ), mock.patch.object(
                engine.PiperSpeechBackend,
                "RESOURCE_SHA256",
                self.fixture_resource_hashes(),
            ):
                with mock.patch.object(
                    engine.PiperSpeechBackend,
                    "_windows_short_path",
                    return_value=None,
                ), mock.patch.object(
                    engine.os, "link", wraps=engine.os.link
                ) as hardlink, mock.patch.object(
                    engine.shutil, "copy2", wraps=engine.shutil.copy2
                ) as copy_file:
                    staged = engine.PiperSpeechBackend._prepare_runtime_files()

            self.assertEqual(
                staged,
                appdata
                / "runtime"
                / "melo"
                / engine.PiperSpeechBackend.STAGING_VERSION,
            )
            for relative in engine.PiperSpeechBackend._required_resources():
                self.assertEqual(staged.joinpath(*relative).read_bytes(), b"fixture")
            self.assertEqual(
                hardlink.call_count,
                len(engine.PiperSpeechBackend._required_resources()),
            )
            copy_file.assert_not_called()

    @unittest.skip("Piper loads packaged resources directly")
    def test_melo_staging_falls_back_to_copy_when_hardlink_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source_root = root / "资源"
            for relative in engine.PiperSpeechBackend._required_resources():
                path = source_root.joinpath(*relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            appdata = root / "ascii-appdata"
            with mock.patch.object(
                engine, "resource_path", return_value=source_root
            ), mock.patch.object(
                engine, "user_data_dir", return_value=appdata
            ), mock.patch.object(
                engine.PiperSpeechBackend,
                "_windows_short_path",
                return_value=None,
            ), mock.patch.object(
                engine.PiperSpeechBackend,
                "RESOURCE_SHA256",
                self.fixture_resource_hashes(),
            ), mock.patch.object(
                engine.os, "link", side_effect=OSError("unsupported")
            ), mock.patch.object(
                engine.shutil, "copy2", wraps=engine.shutil.copy2
            ) as copy_file:
                staged = engine.PiperSpeechBackend._prepare_runtime_files()

            self.assertTrue(copy_file.called)
            for relative in engine.PiperSpeechBackend._required_resources():
                self.assertEqual(staged.joinpath(*relative).read_bytes(), b"fixture")

    @unittest.skip("Piper validates packaged resources instead of a staged cache")
    def test_same_size_corrupt_melo_cache_is_replaced_by_verified_copy(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source_root = root / "资源"
            appdata = root / "ascii-appdata"
            destination_root = (
                appdata
                / "runtime"
                / "melo"
                / engine.PiperSpeechBackend.STAGING_VERSION
            )
            for relative in engine.PiperSpeechBackend._required_resources():
                source = source_root.joinpath(*relative)
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"fixture")
                destination = destination_root.joinpath(*relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"fixture")
            corrupted = destination_root / engine.PiperSpeechBackend.MODEL_FILENAME
            corrupted.write_bytes(b"corrupt")

            with mock.patch.object(
                engine, "resource_path", return_value=source_root
            ), mock.patch.object(
                engine, "user_data_dir", return_value=appdata
            ), mock.patch.object(
                engine.PiperSpeechBackend, "_windows_short_path", return_value=None
            ), mock.patch.object(
                engine.PiperSpeechBackend,
                "RESOURCE_SHA256",
                self.fixture_resource_hashes(),
            ), mock.patch.object(
                engine.os, "link", side_effect=OSError("unsupported")
            ), mock.patch.object(
                engine.shutil, "copy2", wraps=engine.shutil.copy2
            ) as copy_file:
                prepared = engine.PiperSpeechBackend._prepare_runtime_files()

            self.assertEqual(prepared, destination_root)
            self.assertEqual(corrupted.read_bytes(), b"fixture")
            self.assertEqual(copy_file.call_count, 1)

    @unittest.skip("Piper validates packaged resources before loading")
    def test_corrupt_melo_copy_is_rejected_before_cache_replace(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source_root = root / "资源"
            appdata = root / "ascii-appdata"
            for relative in engine.PiperSpeechBackend._required_resources():
                source = source_root.joinpath(*relative)
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"fixture")
            original_copy = engine.shutil.copy2

            def corrupt_copy(source: object, destination: object) -> object:
                result = original_copy(source, destination)
                Path(destination).write_bytes(b"corrupt")
                return result

            with mock.patch.object(
                engine, "resource_path", return_value=source_root
            ), mock.patch.object(
                engine, "user_data_dir", return_value=appdata
            ), mock.patch.object(
                engine.PiperSpeechBackend, "_windows_short_path", return_value=None
            ), mock.patch.object(
                engine.PiperSpeechBackend,
                "RESOURCE_SHA256",
                self.fixture_resource_hashes(),
            ), mock.patch.object(
                engine.os, "link", side_effect=OSError("unsupported")
            ), mock.patch.object(
                engine.shutil, "copy2", side_effect=corrupt_copy
            ):
                with self.assertRaises(engine.PiperSpeechUnavailable):
                    engine.PiperSpeechBackend._prepare_runtime_files()

    def test_melo_native_path_uses_an_ascii_windows_short_path(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            unicode_path = Path(folder) / "模型"
            unicode_path.mkdir()
            with mock.patch.object(
                engine.PiperSpeechBackend,
                "_windows_short_path",
                return_value=r"C:\SHORT\MODEL~1",
            ):
                native = engine.PiperSpeechBackend._ascii_native_path(unicode_path)

        self.assertEqual(native, b"C:\\SHORT\\MODEL~1")

    @unittest.skip("Piper does not copy its packaged ONNX resources")
    def test_ascii_melo_resources_load_in_place_without_copying(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source_root = Path(folder) / "melo"
            for relative in engine.PiperSpeechBackend._required_resources():
                path = source_root.joinpath(*relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            with mock.patch.object(
                engine, "resource_path", return_value=source_root
            ), mock.patch.object(
                engine.PiperSpeechBackend,
                "RESOURCE_SHA256",
                self.fixture_resource_hashes(),
            ), mock.patch.object(engine.shutil, "copy2") as copy_file:
                prepared = engine.PiperSpeechBackend._prepare_runtime_files()

            self.assertEqual(prepared, source_root)
            copy_file.assert_not_called()

    def test_piper_high_quality_us_and_uk_words_are_real_valid_wav(self) -> None:
        backend = engine.PiperSpeechBackend()
        try:
            for accent in ("us", "uk"):
                path, duration = backend.synthesize("particularly", accent, 1.0)
                try:
                    self.assertGreater(duration, 0.0)
                    with wave.open(str(path), "rb") as stream:
                        self.assertEqual(stream.getnchannels(), 1)
                        self.assertEqual(stream.getsampwidth(), 2)
                        self.assertEqual(stream.getframerate(), 22_050)
                        self.assertGreater(stream.getnframes(), 0)
                finally:
                    backend.discard(path)
        finally:
            backend.close()

    def test_piper_uses_an_existing_ascii_espeak_data_path_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "espeak-ng-data"
            source.mkdir()
            (source / "phontab").write_bytes(b"fixture")
            native_path = Path(r"C:\PIPER\ESPEAK")
            with mock.patch.object(
                engine.PiperSpeechBackend,
                "_ascii_native_path",
                return_value=str(native_path).encode("ascii"),
            ), mock.patch.object(engine.shutil, "copytree") as copy_tree:
                prepared = engine.PiperSpeechBackend._prepare_piper_espeak_data(source)

        self.assertEqual(prepared, native_path)
        copy_tree.assert_not_called()

    def test_piper_stages_unicode_espeak_data_when_no_short_path_exists(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "用户" / "espeak-ng-data"
            source.mkdir(parents=True)
            (source / "phontab").write_bytes(b"fixture")
            (source / "voices").mkdir()
            native_path = Path(r"C:\PIPER\STAGED")
            with mock.patch.object(
                engine, "user_data_dir", return_value=root / "appdata"
            ), mock.patch.object(
                engine.PiperSpeechBackend,
                "_ascii_native_path",
                side_effect=(
                    engine.PiperSpeechUnavailable("no source short path"),
                    str(native_path).encode("ascii"),
                ),
            ):
                prepared = engine.PiperSpeechBackend._prepare_piper_espeak_data(source)

            staged = root / "appdata" / "runtime" / "piper" / "espeak-ng-data-1.6.0"
            self.assertEqual((staged / "phontab").read_bytes(), b"fixture")
            self.assertEqual(prepared, native_path)

    def test_piper_rejects_a_tampered_bundled_voice_before_loading(self) -> None:
        backend = engine.PiperSpeechBackend()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in engine.PiperSpeechBackend.RESOURCE_SHA256:
                (root / name).write_bytes(b"tampered")
            with mock.patch.object(engine, "resource_path", return_value=root):
                with self.assertRaises(engine.PiperSpeechUnavailable):
                    backend.synthesize("asked", "us", 1.0)

    def test_only_exact_microsoft_desktop_tokens_are_selected(self) -> None:
        class Token:
            def __init__(self, token_id: str, **attributes: str) -> None:
                self.Id = token_id
                self.attributes = attributes

            def GetAttribute(self, name: str) -> str:
                return self.attributes.get(name, "")

        impostor = Token(
            r"HKEY_LOCAL_MACHINE\Vendor\BritishVoice",
            Language="809;409",
            Name="Some British Voice",
            Vendor="Third Party",
        )
        hazel = Token(
            r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_EN-GB_HAZEL_11.0",
            Language="809",
            Name="Microsoft Hazel Desktop",
            Vendor="Microsoft",
        )
        tokens = SimpleNamespace(
            Count=2,
            Item=lambda index: (impostor, hazel)[index],
        )

        selected = engine.SpeechPlayer._find_system_voice(
            SimpleNamespace(GetVoices=lambda: tokens), "uk"
        )

        self.assertIs(selected, hazel)

    def test_other_microsoft_voice_is_safe_fallback_when_hazel_is_absent(self) -> None:
        attributes = {
            "Language": "809;409",
            "Name": "Microsoft Alternate British Desktop",
            "Vendor": "Microsoft",
        }
        microsoft = SimpleNamespace(
            Id=r"HKEY_LOCAL_MACHINE\Microsoft\AlternateBritish",
            GetAttribute=lambda name: attributes[name],
        )
        tokens = SimpleNamespace(Count=1, Item=lambda _index: microsoft)

        selected = engine.SpeechPlayer._find_system_voice(
            SimpleNamespace(GetVoices=lambda: tokens), "uk"
        )

        self.assertIs(selected, microsoft)

    def test_third_party_voice_with_same_lcid_is_never_system_fallback(self) -> None:
        attributes = {
            "Language": "409",
            "Name": "Microsoft Zira Desktop",
            "Vendor": "Third Party",
        }
        impostor = SimpleNamespace(
            Id=r"HKEY_LOCAL_MACHINE\Vendor\TTS_MS_EN-US_ZIRA_11.0",
            GetAttribute=lambda name: attributes[name],
        )
        tokens = SimpleNamespace(Count=1, Item=lambda _index: impostor)

        selected = engine.SpeechPlayer._find_system_voice(
            SimpleNamespace(GetVoices=lambda: tokens), "us"
        )

        self.assertIsNone(selected)

    def test_explicit_system_speech_does_not_wait_for_neural_prewarm(self) -> None:
        release_prewarm = threading.Event()
        system_spoke = threading.Event()
        system_completed = threading.Event()

        def report_speech_status(value: str) -> None:
            if value == "微软原版发音播放完成":
                system_completed.set()

        class Backend:
            @staticmethod
            def warm_up() -> None:
                release_prewarm.wait(1.0)

            @staticmethod
            def stop() -> None:
                pass

            @staticmethod
            def close() -> None:
                pass

        attributes = {
            "Language": "409",
            "Name": "Microsoft Zira Desktop",
            "Vendor": "Microsoft",
        }
        token = SimpleNamespace(
            Id=r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_EN-US_ZIRA_11.0",
            GetAttribute=lambda name: attributes[name],
        )

        class Voice:
            Voice = None
            Rate = 0

            @staticmethod
            def GetVoices() -> SimpleNamespace:
                return SimpleNamespace(Count=1, Item=lambda _index: token)

            @staticmethod
            def Speak(_text: str, flags: int) -> None:
                if flags == 3:
                    system_spoke.set()

            @staticmethod
            def WaitUntilDone(_milliseconds: int) -> bool:
                return True

        with mock.patch.object(engine, "PiperSpeechBackend", return_value=Backend()), mock.patch.object(
            engine, "KokoroSpeechBackend", return_value=Backend()
        ), mock.patch.object(engine.pythoncom, "CoInitialize"), mock.patch.object(
            engine.pythoncom, "CoUninitialize"
        ), mock.patch.object(engine.win32com.client, "Dispatch", return_value=Voice()):
            player = engine.SpeechPlayer(
                speech_status_callback=report_speech_status
            )
            try:
                player.speak("hello", "us", mode="system")
                self.assertTrue(system_spoke.wait(0.5))
                self.assertTrue(system_completed.wait(0.5))
                self.assertFalse(release_prewarm.is_set())
            finally:
                release_prewarm.set()
                player.stop(timeout_seconds=1.0)

    def test_cooperative_prewarm_yields_before_real_sample_for_user_ai(self) -> None:
        player = self.make_unstarted_player()
        player._readiness_lock = threading.Lock()
        player._backend_readiness = {"Piper": None, "Kokoro": True}
        user_pending = player._interactive_ai_event()
        user_pending.set()
        warm_started = threading.Event()

        class Backend:
            @staticmethod
            def warm_up(should_continue) -> None:
                warm_started.set()
                if not should_continue():
                    raise engine.BackgroundSpeechYield("user wins")

        worker = threading.Thread(
            target=player._prewarm_backend,
            args=("Piper", Backend()),
        )
        worker.start()
        self.assertFalse(warm_started.wait(0.08))

        user_pending.clear()
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertTrue(warm_started.is_set())
        self.assertTrue(player._backend_readiness["Piper"])

    def test_parallel_prewarm_final_status_is_always_last(self) -> None:
        player = self.make_unstarted_player()
        player._readiness_lock = threading.Lock()
        player._backend_readiness = {"Piper": None, "Kokoro": None}
        statuses: list[str] = []
        player.speech_status_callback = statuses.append
        barrier = threading.Barrier(2)

        class Backend:
            @staticmethod
            def warm_up() -> None:
                barrier.wait(timeout=1.0)

        workers = [
            threading.Thread(
                target=player._prewarm_backend, args=(label, Backend())
            )
            for label in ("Piper", "Kokoro")
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(1.0)

        self.assertEqual(statuses[-1], "AI 发音已全部就绪")
        self.assertLess(statuses.index("Piper 已就绪"), len(statuses) - 1)
        self.assertLess(statuses.index("Kokoro 已就绪"), len(statuses) - 1)

    def test_late_prewarm_status_cannot_overwrite_a_user_request(self) -> None:
        player = self.make_unstarted_player()
        player._readiness_lock = threading.Lock()
        player._backend_readiness = {"Piper": None, "Kokoro": None}
        statuses: list[str] = []
        player.speech_status_callback = statuses.append
        player._latest_request_id = 1

        player._mark_backend_readiness("Piper", True)
        player._mark_backend_readiness("Kokoro", True)

        self.assertEqual(statuses, [])
        self.assertEqual(player._backend_readiness, {"Piper": True, "Kokoro": True})

    def test_natural_completion_is_emitted_only_for_the_latest_request(self) -> None:
        player = self.make_unstarted_player()
        player.natural_requests = queue.Queue()
        statuses: list[str] = []
        player.speech_status_callback = statuses.append
        player._latest_request_id = 1
        with tempfile.TemporaryDirectory() as folder:
            audio_path = Path(folder) / "word.wav"
            audio_path.write_bytes(b"fixture")
            player.piper_backend = SimpleNamespace(
                synthesize=mock.Mock(return_value=(audio_path, 0.001)),
                play=mock.Mock(),
                stop=mock.Mock(),
                discard=lambda path: path.unlink(missing_ok=True),
            )
            player.natural_requests.put((1, "word", "us", "standard"))
            def stop_natural_loop() -> None:
                player.stop_event.set()
                player.natural_requests.put(None)

            stop_loop = threading.Timer(0.12, stop_natural_loop)
            stop_loop.start()
            try:
                player._natural_loop()
            finally:
                stop_loop.join(1.0)

        self.assertEqual(statuses[-1], "AI 发音播放完成")

    def test_superseded_natural_request_does_not_emit_completion(self) -> None:
        player = self.make_unstarted_player()
        player.natural_requests = queue.Queue()
        statuses: list[str] = []
        player.speech_status_callback = statuses.append
        player._latest_request_id = 1
        with tempfile.TemporaryDirectory() as folder:
            audio_path = Path(folder) / "word.wav"
            audio_path.write_bytes(b"fixture")
            supersede_workers: list[threading.Thread] = []

            def supersede(_path: Path) -> None:
                def mark_new_request() -> None:
                    with player._request_lock:
                        player._latest_request_id = 2

                worker = threading.Thread(target=mark_new_request)
                supersede_workers.append(worker)
                worker.start()

            player.piper_backend = SimpleNamespace(
                synthesize=mock.Mock(return_value=(audio_path, 0.001)),
                play=supersede,
                stop=mock.Mock(),
                discard=lambda path: path.unlink(missing_ok=True),
            )
            player.natural_requests.put((1, "word", "us", "standard"))
            def stop_natural_loop() -> None:
                player.stop_event.set()
                player.natural_requests.put(None)

            stop_loop = threading.Timer(0.12, stop_natural_loop)
            stop_loop.start()
            try:
                player._natural_loop()
            finally:
                stop_loop.join(1.0)
                for worker in supersede_workers:
                    worker.join(1.0)

        self.assertNotIn("AI 发音播放完成", statuses)

    def test_cancel_replaces_pending_speech_with_purge(self) -> None:
        player = self.make_unstarted_player()
        player.speak("first", "us")

        player.cancel()

        self.assertIs(player.requests.get_nowait(), engine.SpeechPlayer._PURGE)
        self.assertIs(
            player.natural_requests.get_nowait(), engine.SpeechPlayer._PURGE
        )

    def test_missing_voice_produces_visible_status(self) -> None:
        status_event = threading.Event()
        statuses: list[str] = []

        def report(value: str) -> None:
            statuses.append(value)
            status_event.set()

        voice = SimpleNamespace(GetVoices=lambda: SimpleNamespace(Count=0))
        with mock.patch.object(engine.pythoncom, "CoInitialize"), mock.patch.object(
            engine.pythoncom, "CoUninitialize"
        ), mock.patch.object(
            engine.win32com.client, "Dispatch", return_value=voice
        ), mock.patch.object(
            engine.PiperSpeechBackend,
            "synthesize",
            side_effect=engine.PiperSpeechUnavailable("test fallback"),
        ), mock.patch.object(
            engine.PiperSpeechBackend, "warm_up"
        ), mock.patch.object(
            engine.KokoroSpeechBackend, "warm_up"
        ):
            player = engine.SpeechPlayer(report)
            try:
                player.speak("hello", "uk")
                self.assertTrue(status_event.wait(1.0))
                deadline = time.monotonic() + 1.0
                while (
                    not any("英式" in value and "语音" in value for value in statuses)
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
            finally:
                player.stop(timeout_seconds=1.0)

        self.assertTrue(any("英式" in value and "语音" in value for value in statuses))

    def test_failed_purge_does_not_kill_the_only_speech_worker(self) -> None:
        player = self.make_unstarted_player()
        player.requests = queue.Queue()
        player._latest_request_id = 1
        player.requests.put((1, "first", "us", "system"))
        player.requests.put(engine.SpeechPlayer._PURGE)
        player.requests.put(None)

        attributes = {
            "Language": "409",
            "Name": "Microsoft Zira Desktop",
            "Vendor": "Microsoft",
        }
        token = SimpleNamespace(
            Id=r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_EN-US_ZIRA_11.0",
            GetAttribute=lambda name: attributes[name],
        )

        class PurgeFailingVoice:
            Rate = 0
            Voice = None

            @staticmethod
            def GetVoices() -> SimpleNamespace:
                return SimpleNamespace(Count=1, Item=lambda _index: token)

            @staticmethod
            def Speak(_text: str, flags: int) -> None:
                if flags == 2:
                    raise RuntimeError("purge failed")

            @staticmethod
            def WaitUntilDone(_milliseconds: int) -> bool:
                return False

        with mock.patch.object(engine.pythoncom, "CoInitialize"), mock.patch.object(
            engine.pythoncom, "CoUninitialize"
        ) as uninitialize, mock.patch.object(
            engine.win32com.client, "Dispatch", return_value=PurgeFailingVoice()
        ):
            player._speech_loop()

        uninitialize.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
