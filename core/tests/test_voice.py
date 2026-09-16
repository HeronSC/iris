# File: core/tests/test_voice.py

from __future__ import annotations

import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from core.voice import ConversationSession, Recorder, SentenceStreamer, Speaker, SpokenNotifier, StreamingVad, Transcriber, VoiceConfig, VoiceError, VoiceService, is_end_phrase, speech_text
from core.voice.text import interpret_yes_no


class SpeechTextTests(unittest.TestCase):
    def test_markdown_is_flattened_for_the_ear(self) -> None:
        text = "# Weather\n\nIt is **sunny** in [Boston](http://x.example) with `72F`.\n\n- wind low\n1. no rain\n\n```py\nprint(1)\n```\nSee https://example.com/x for more."
        self.assertEqual(speech_text(text), "Weather It is sunny in Boston with 72F. wind low no rain code omitted. See link for more.")

    def test_tables_are_dropped_and_long_text_stops_at_a_sentence(self) -> None:
        text = "| a | b |\n|---|---|\n| 1 | 2 |\nFirst sentence. Second sentence is longer. Third."
        self.assertEqual(speech_text(text, limit=45), "First sentence. Second sentence is longer.")
        self.assertEqual(speech_text(text, limit=40), "First sentence.")
        self.assertEqual(speech_text("word " * 50, limit=22), "word word word word")
        self.assertEqual(speech_text("", limit=10), "")


class VoiceConfigTests(unittest.TestCase):
    def test_defaults_and_models_path_sit_beside_the_data_folder(self) -> None:
        config = VoiceConfig.from_config({"memory_path": "E:\\AI\\Iris\\Data\\Memory"})
        self.assertEqual(config.models_path, Path("E:\\AI\\Iris\\Data\\Models"))
        self.assertEqual(config.hotkey, "ctrl+alt+t")
        self.assertEqual(config.whisper_model, "small.en")
        self.assertEqual(config.speak_replies, "voice")
        self.assertTrue(config.enabled)

    def test_section_values_are_read_and_bad_modes_fall_back(self) -> None:
        config = VoiceConfig.from_config({"memory_path": "D:\\Data\\Memory", "voice": {"enabled": False, "speak_replies": "loudly", "input_device": "3", "output_device": "Headset", "hotkey": "ctrl+alt+v"}})
        self.assertFalse(config.enabled)
        self.assertEqual(config.speak_replies, "voice")
        self.assertEqual(config.input_device, 3)
        self.assertEqual(config.output_device, "Headset")
        self.assertEqual(config.hotkey, "ctrl+alt+v")


class _FakeStream:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True


class RecorderTests(unittest.TestCase):
    def test_collects_mono_audio_until_stopped_and_caps_at_max_seconds(self) -> None:
        streams: list[_FakeStream] = []

        def factory(sample_rate, device, callback):
            stream = _FakeStream()
            streams.append(stream)
            return stream

        recorder = Recorder(sample_rate=100, max_seconds=1.0, stream_factory=factory)
        self.assertEqual(recorder.stop().shape, (0,))
        recorder.start()
        self.assertTrue(recorder.active)
        recorder._on_audio(np.ones((60, 1), dtype=np.float32), 60)
        recorder._on_audio(np.full((60, 2), 0.5, dtype=np.float32), 60)
        self.assertTrue(recorder.full)
        recorder._on_audio(np.ones((60, 1), dtype=np.float32), 60)
        audio = recorder.stop()
        self.assertEqual(audio.shape, (120,))
        self.assertEqual(float(audio[0]), 1.0)
        self.assertEqual(float(audio[-1]), 0.5)
        self.assertFalse(recorder.active)
        self.assertTrue(streams[0].closed)

    def test_a_missing_microphone_is_a_voice_error(self) -> None:
        def factory(sample_rate, device, callback):
            raise OSError("no default input device")

        recorder = Recorder(stream_factory=factory)
        with self.assertRaises(VoiceError):
            recorder.start()
        self.assertFalse(recorder.active)


class _FakeModel:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[int, dict]] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((int(np.asarray(audio).shape[0]), kwargs))
        segments = [SimpleNamespace(text=f" {word} ") for word in self.text.split("|")] if self.text else []
        return iter(segments), SimpleNamespace(language="en")


class TranscriberTests(unittest.TestCase):
    def test_short_audio_is_skipped_and_segments_are_joined(self) -> None:
        model = _FakeModel("hello|world")
        transcriber = Transcriber("small.en", model_factory=lambda *_: model)
        self.assertEqual(transcriber.transcribe(np.zeros(1000, dtype=np.float32)), "")
        self.assertFalse(transcriber.loaded)
        self.assertEqual(transcriber.transcribe(np.zeros(16000, dtype=np.float32)), "hello world")
        self.assertEqual(model.calls[-1][1]["language"], "en")
        self.assertTrue(model.calls[-1][1]["vad_filter"])

    def test_a_broken_gpu_falls_back_to_cpu_once(self) -> None:
        attempts: list[tuple[str, str]] = []

        def factory(name, device, compute_type, root):
            attempts.append((device, compute_type))
            if device != "cpu":
                return _BrokenModel()
            return _FakeModel("ok")

        transcriber = Transcriber("small.en", device="cuda", compute_type="float16", model_factory=factory)
        self.assertEqual(transcriber.transcribe(np.zeros(16000, dtype=np.float32)), "ok")
        self.assertEqual(attempts, [("cuda", "float16"), ("cpu", "int8")])
        self.assertEqual(transcriber.device_used, "cpu")
        transcriber.transcribe(np.zeros(16000, dtype=np.float32))
        self.assertEqual(len(attempts), 2)

    def test_cpu_failure_is_reported(self) -> None:
        def factory(*_):
            raise RuntimeError("model files missing")

        transcriber = Transcriber("small.en", device="cpu", model_factory=factory)
        with self.assertRaises(VoiceError):
            transcriber.transcribe(np.zeros(16000, dtype=np.float32))

    def test_other_sample_rates_are_resampled_to_16k(self) -> None:
        model = _FakeModel("ok")
        transcriber = Transcriber("small.en", model_factory=lambda *_: model)
        transcriber.transcribe(np.zeros(44100, dtype=np.float32), 44100)
        self.assertEqual(model.calls[-1][0], 16000)


class _BrokenModel:
    def transcribe(self, audio, **kwargs):
        raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")


class _FakeVoice:
    config = SimpleNamespace(sample_rate=22050)

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.spoken: list[str] = []

    def synthesize(self, text):
        self.spoken.append(text)
        for sentence in range(3):
            if self.gate is not None and sentence > 0:
                self.gate.wait(timeout=2.0)
            yield SimpleNamespace(sample_rate=22050, audio_int16_array=np.zeros(5000, dtype=np.int16))


class _FakePlayer:
    def __init__(self) -> None:
        self.blocks: list[int] = []
        self.closed = False

    def write(self, block) -> None:
        self.blocks.append(int(np.asarray(block).shape[0]))

    def close(self) -> None:
        self.closed = True


class SpeakerTests(unittest.TestCase):
    def test_downloads_a_missing_voice_once_and_plays_in_blocks(self) -> None:
        downloads: list[tuple[str, Path]] = []
        players: list[_FakePlayer] = []
        voice = _FakeVoice()
        root = Path("C:\\nowhere\\Models")
        speaker = Speaker(
            "en_US-lessac-medium",
            root,
            downloader=lambda name, folder: downloads.append((name, folder)),
            voice_loader=lambda path: voice,
            player_factory=lambda rate, device: players.append(_FakePlayer()) or players[-1],
        )
        self.assertEqual(speaker.model_path, root / "piper" / "en_US-lessac-medium.onnx")
        with unittest.mock.patch.object(Path, "exists", return_value=True), unittest.mock.patch.object(Path, "mkdir"):
            self.assertTrue(speaker.speak("Hello there.", wait=True))
        self.assertEqual(downloads, [])
        self.assertEqual(voice.spoken, ["Hello there."])
        self.assertEqual(sum(players[0].blocks), 15000)
        self.assertTrue(players[0].closed)
        self.assertFalse(speaker.speak("   "))

    def test_missing_voice_is_fetched_into_the_models_folder(self) -> None:
        downloads: list[str] = []
        speaker = Speaker("en_US-lessac-medium", Path("C:\\nowhere"), downloader=lambda name, folder: downloads.append(name), voice_loader=lambda path: _FakeVoice(), player_factory=lambda rate, device: _FakePlayer())
        with unittest.mock.patch.object(Path, "exists", return_value=False), unittest.mock.patch.object(Path, "mkdir"):
            speaker.load()
        self.assertEqual(downloads, ["en_US-lessac-medium"])

    def test_stop_interrupts_playback(self) -> None:
        gate = threading.Event()
        player = _FakePlayer()
        speaker = Speaker("v", Path("C:\\nowhere"), voice_loader=lambda path: _FakeVoice(gate), player_factory=lambda rate, device: player)
        with unittest.mock.patch.object(Path, "exists", return_value=True):
            speaker.speak("A long reply.")
        self.assertTrue(speaker.speaking)
        threading.Timer(0.1, gate.set).start()
        speaker.stop()
        for _ in range(50):
            if not speaker.speaking:
                break
            time.sleep(0.05)
        self.assertFalse(speaker.speaking)
        self.assertLess(sum(player.blocks), 15000)
        self.assertTrue(player.closed)


class _FakeSpeaker:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stopped = 0
        self.loaded = False

    @property
    def speaking(self) -> bool:
        return False

    def load(self) -> None:
        self.loaded = True

    def speak(self, text: str, *, wait: bool = False) -> bool:
        self.spoken.append(text)
        return True

    def stop(self) -> None:
        self.stopped += 1


class _FakeTranscriber:
    device_used = "cpu"

    def __init__(self, text: str = "turn on the lights") -> None:
        self.text = text
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def transcribe(self, audio, sample_rate=16000) -> str:
        return self.text if np.asarray(audio).shape[0] else ""


def _service(text: str = "turn on the lights", **section) -> VoiceService:
    config = {"memory_path": "C:\\nowhere\\Data\\Memory", "voice": section}
    recorder = Recorder(sample_rate=100, stream_factory=lambda rate, device, callback: _FakeStream())
    return VoiceService.from_config(config, recorder=recorder, transcriber=_FakeTranscriber(text), speaker=_FakeSpeaker())


class VoiceServiceTests(unittest.TestCase):
    def test_disabled_and_missing_dependencies_say_why(self) -> None:
        off = VoiceService.from_config({"memory_path": "C:\\x\\Memory", "voice": {"enabled": False}})
        self.assertFalse(off.available)
        self.assertIn("voice.enabled", off.problem)
        with self.assertRaises(VoiceError):
            off.start_listening()
        self.assertFalse(off.speak("hello"))
        with unittest.mock.patch("core.voice.service.missing_modules", return_value=["sounddevice"]):
            missing = VoiceService.from_config({"memory_path": "C:\\x\\Memory"})
        self.assertIn("sounddevice", missing.problem)

    def test_push_to_talk_round_trip(self) -> None:
        service = _service()
        self.assertTrue(service.available)
        service.start_listening()
        self.assertTrue(service.listening)
        self.assertEqual(service.speaker.stopped, 1)
        service.recorder._on_audio(np.ones((50, 1), dtype=np.float32), 50)
        self.assertEqual(service.stop_listening(), "turn on the lights")
        self.assertFalse(service.listening)
        service.start_listening()
        self.assertEqual(service.stop_listening(), "")

    def test_speaking_respects_mute_and_mode(self) -> None:
        service = _service()
        self.assertTrue(service.should_speak_reply(from_voice=True))
        self.assertFalse(service.should_speak_reply(from_voice=False))
        self.assertTrue(service.speak("**Done.** See `x`."))
        self.assertEqual(service.speaker.spoken, ["Done. See x."])
        service.muted = True
        self.assertFalse(service.speak("quiet"))
        self.assertFalse(service.should_speak_reply(from_voice=True))
        self.assertIn("muted", service.describe())
        always = _service(speak_replies="always")
        self.assertTrue(always.should_speak_reply(from_voice=False))
        never = _service(speak_replies="never")
        self.assertFalse(never.should_speak_reply(from_voice=True))

    def test_warm_up_loads_both_models(self) -> None:
        service = _service()
        service.warm_up(background=False)
        self.assertTrue(service.transcriber.loaded)
        self.assertTrue(service.speaker.loaded)
        self.assertIsNone(service.warm_error)
        self.assertIn("ctrl+alt+t", service.describe())

    def test_notifications_are_read_as_title_then_body(self) -> None:
        service = _service()
        notifier = SpokenNotifier(service)
        self.assertEqual(notifier.name, "voice")
        notifier.send(SimpleNamespace(title="Disk C: is low.", body="12% free on C:."))
        self.assertEqual(service.speaker.spoken, ["Disk C: is low. 12% free on C:."])


class _FakeVadSession:
    def __init__(self, probabilities: list[float]) -> None:
        self.probabilities = list(probabilities)
        self.calls = 0

    def run(self, outputs, feeds):
        self.calls += 1
        value = self.probabilities.pop(0) if self.probabilities else 0.0
        assert feeds["input"].shape == (1, 576)
        assert feeds["h"].shape == (1, 1, 128)
        return np.array([[value]], dtype=np.float32), feeds["h"] + 1, feeds["c"] + 1


class StreamingVadTests(unittest.TestCase):
    def test_keeps_state_between_frames_and_pads_short_ones(self) -> None:
        session = _FakeVadSession([0.1, 0.9])
        vad = StreamingVad(session_factory=lambda: session)
        self.assertAlmostEqual(vad.probability(np.zeros(512, dtype=np.float32)), 0.1)
        self.assertAlmostEqual(vad.probability(np.zeros(100, dtype=np.float32)), 0.9)
        self.assertEqual(float(vad._h[0, 0, 0]), 2.0)
        vad.reset()
        self.assertEqual(float(vad._h[0, 0, 0]), 0.0)
        self.assertEqual(session.calls, 2)


class PhraseTests(unittest.TestCase):
    def test_end_phrases_and_yes_no(self) -> None:
        self.assertTrue(is_end_phrase("That's all."))
        self.assertTrue(is_end_phrase("okay thanks, Iris"))
        self.assertTrue(is_end_phrase("Goodbye!"))
        self.assertFalse(is_end_phrase("That's all the milk we have, order more"))
        self.assertFalse(is_end_phrase("what time is it"))
        self.assertTrue(is_end_phrase("later", ("later",)))
        self.assertEqual(interpret_yes_no("Yes."), "yes")
        self.assertEqual(interpret_yes_no("yeah, go ahead"), "yes")
        self.assertEqual(interpret_yes_no("No, don't."), "no")
        self.assertEqual(interpret_yes_no("Cancel that"), "no")
        self.assertIsNone(interpret_yes_no("maybe tomorrow"))
        self.assertIsNone(interpret_yes_no(""))


class SentenceStreamerTests(unittest.TestCase):
    def test_speaks_sentence_by_sentence_and_flushes_the_rest(self) -> None:
        spoken: list[str] = []
        streamer = SentenceStreamer(lambda text: spoken.append(text) or True)
        for delta in ["It is **sunny**", " in Boston today. Highs", " near 72. Dr. Who", " is on later.\n\nBring a coat."]:
            streamer.feed(delta)
        self.assertEqual(spoken, ["It is sunny in Boston today.", "Highs near 72.", "Dr. Who is on later."])
        streamer.finish()
        self.assertEqual(spoken[-1], "Bring a coat.")
        self.assertTrue(streamer.started)

    def test_code_blocks_wait_until_closed_and_the_limit_holds(self) -> None:
        spoken: list[str] = []
        streamer = SentenceStreamer(lambda text: spoken.append(text) or True, limit=30)
        streamer.feed("Here is the code. ```py\nprint(1). done\n")
        self.assertEqual(spoken, [])
        streamer.feed("```\nThat prints one. And this sentence is past the limit.")
        streamer.finish()
        self.assertEqual(spoken[0], "Here is the code.")
        self.assertNotIn("past the limit", " ".join(spoken))
        streamer.reset()
        self.assertFalse(streamer.started)


class SpeakerQueueTests(unittest.TestCase):
    def test_enqueue_plays_in_order_and_speak_clears_the_queue(self) -> None:
        voice = _FakeVoice()
        player = _FakePlayer()
        speaker = Speaker("v", Path("C:\\nowhere"), voice_loader=lambda path: voice, player_factory=lambda rate, device: player)
        with unittest.mock.patch.object(Path, "exists", return_value=True):
            speaker.enqueue("First sentence.")
            speaker.enqueue("Second sentence.")
            speaker.enqueue("Third sentence.", wait=True)
        self.assertEqual(voice.spoken, ["First sentence.", "Second sentence.", "Third sentence."])
        self.assertFalse(speaker.speaking)
        gate = threading.Event()
        slow = _FakeVoice(gate)
        speaker = Speaker("v", Path("C:\\nowhere"), voice_loader=lambda path: slow, player_factory=lambda rate, device: _FakePlayer())
        with unittest.mock.patch.object(Path, "exists", return_value=True):
            speaker.enqueue("Slow one.")
            speaker.enqueue("Never played.")
            threading.Timer(0.1, gate.set).start()
            speaker.speak("Replacement.", wait=True)
        self.assertNotIn("Never played.", slow.spoken)
        self.assertEqual(slow.spoken[-1], "Replacement.")


class _FakeInput:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True


class _SessionVad:
    def __init__(self) -> None:
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def probability(self, frame) -> float:
        return 0.9 if float(np.max(np.abs(np.asarray(frame)))) > 0.5 else 0.05


class _SessionService:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.speaking = False
        self.stops = 0
        self.transcriber = self
        self.audio_seconds = 0.0

    def transcribe(self, audio, sample_rate=16000) -> str:
        self.audio_seconds = float(np.asarray(audio).shape[0]) / sample_rate
        return self.texts.pop(0) if self.texts else ""

    def stop_speaking(self) -> None:
        self.stops += 1
        self.speaking = False


def _drive(session: ConversationSession, vad: _SessionVad, *, voiced_frames: int, silent_frames: int) -> None:
    _ = vad
    for _index in range(voiced_frames):
        session.feed(np.ones(512, dtype=np.float32))
    for _index in range(silent_frames):
        session.feed(np.zeros(512, dtype=np.float32))


def _settle(session: ConversationSession, *, want_state: str | None = None, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if session._frames.empty() and (want_state is None or session.state == want_state):
            time.sleep(0.05)
            if session._frames.empty() and (want_state is None or session.state == want_state):
                return
        time.sleep(0.01)


class ConversationSessionTests(unittest.TestCase):
    def _session(self, service, vad, *, clock=None, idle_seconds: float = 20.0):
        heard: list[str] = []
        states: list[str] = []
        ended: list[str] = []
        stream = _FakeInput()
        session = ConversationSession(
            service,
            on_utterance=heard.append,
            on_state=states.append,
            on_end=ended.append,
            vad=vad,
            input_factory=lambda rate, device, callback: stream,
            clock=clock,
            end_silence_ms=64,
            idle_seconds=idle_seconds,
        )
        return session, heard, states, ended, stream

    def test_speech_then_silence_becomes_an_utterance(self) -> None:
        vad = _SessionVad()
        service = _SessionService(["what time is it"])
        session, heard, states, ended, stream = self._session(service, vad)
        session.start()
        self.assertTrue(stream.started)
        self.assertEqual(vad.resets, 1)
        _drive(session, vad, voiced_frames=10, silent_frames=5)
        _settle(session, want_state="waiting")
        self.assertEqual(heard, ["what time is it"])
        self.assertEqual(states[:4], ["waiting", "speaking", "transcribing", "waiting"])
        self.assertGreaterEqual(service.audio_seconds, 10 * 512 / 16000)
        session.stop()
        self.assertTrue(stream.closed)
        self.assertEqual(ended, ["stopped"])
        self.assertFalse(session.active)

    def test_end_phrase_and_idle_end_the_session(self) -> None:
        vad = _SessionVad()
        service = _SessionService(["thanks Iris"])
        session, heard, states, ended, stream = self._session(service, vad)
        session.start()
        _drive(session, vad, voiced_frames=5, silent_frames=5)
        session._thread.join(timeout=3.0)
        self.assertEqual(heard, [])
        self.assertEqual(ended, ["phrase"])
        self.assertFalse(session.active)
        now = [100.0]
        vad = _SessionVad()
        session, heard, states, ended, stream = self._session(_SessionService([]), vad, clock=lambda: now[0], idle_seconds=5.0)
        session.start()
        now[0] = 106.0
        session.feed(np.zeros(512, dtype=np.float32))
        session._thread.join(timeout=3.0)
        self.assertEqual(ended, ["idle"])

    def test_hold_drops_speech_and_barge_in_stops_playback(self) -> None:
        vad = _SessionVad()
        service = _SessionService(["ignored", "kept", "the answer"])
        session, heard, states, ended, stream = self._session(service, vad)
        session.start()
        session.hold()
        _drive(session, vad, voiced_frames=5, silent_frames=5)
        _settle(session, want_state="waiting")
        self.assertEqual(heard, [])
        session.resume()
        service.speaking = True
        _drive(session, vad, voiced_frames=3, silent_frames=0)
        _settle(session)
        self.assertEqual(service.stops, 0)
        _drive(session, vad, voiced_frames=6, silent_frames=5)
        _settle(session, want_state="waiting")
        self.assertEqual(service.stops, 1)
        self.assertEqual(heard, ["kept"])
        answers: list[str | None] = []
        asker = threading.Thread(target=lambda: answers.append(session.request_answer(timeout=3.0)))
        asker.start()
        time.sleep(0.1)
        _drive(session, vad, voiced_frames=5, silent_frames=5)
        asker.join(timeout=3.0)
        self.assertEqual(answers, ["the answer"])
        self.assertEqual(heard, ["kept"])
        session.stop()


def _conversation_ready(service: VoiceService) -> _FakeInput:
    stream = _FakeInput()
    original = service.session_factory
    service.session_factory = lambda svc, **kwargs: original(svc, input_factory=lambda rate, device, callback: stream, **kwargs)
    service.vad_factory = _SessionVad
    return stream


class ConversationServiceTests(unittest.TestCase):
    def test_start_needs_a_listener_then_runs_and_ends(self) -> None:
        service = _service()
        with self.assertRaises(VoiceError):
            service.start_conversation()
        heard: list[str] = []
        ended: list[str] = []
        service.on_utterance = heard.append
        service.on_conversation_end = ended.append
        _conversation_ready(service)
        session = service.start_conversation()
        self.assertTrue(service.in_conversation)
        self.assertIs(service.start_conversation(), session)
        self.assertIn("in a conversation", service.describe())
        self.assertTrue(service.end_conversation())
        self.assertFalse(service.in_conversation)
        self.assertEqual(ended, ["stopped"])
        self.assertFalse(service.end_conversation())
        self.assertIsNone(service.ask("Proceed?"))

    def test_voice_command(self) -> None:
        from core.assistant.voice_command import VoiceCommandHandler

        lines: list[str] = []
        service = _service()
        service.on_utterance = lambda text: None
        _conversation_ready(service)
        handler = VoiceCommandHandler(service, output=lambda text, role=None: lines.append(text))
        self.assertFalse(handler.handle("/voices", {}))
        self.assertTrue(handler.handle("/voice", {}))
        self.assertIn("Whisper", lines[-1])
        self.assertTrue(handler.handle("/voice start", {}))
        self.assertTrue(service.in_conversation)
        self.assertTrue(handler.handle("/voice stop", {}))
        self.assertFalse(service.in_conversation)
        self.assertTrue(handler.handle("/voice mute", {}))
        self.assertTrue(service.muted)
        self.assertTrue(handler.handle("/voice say hello there", {}))
        self.assertIn("Nothing was spoken", lines[-1])
        self.assertTrue(handler.handle("/voice unmute", {}))
        self.assertTrue(handler.handle("/voice say hello there", {}))
        self.assertEqual(service.speaker.spoken[-1], "hello there")
        self.assertTrue(handler.handle("/voice dance", {}))
        self.assertIn("Usage", lines[-1])


if __name__ == "__main__":
    unittest.main()
