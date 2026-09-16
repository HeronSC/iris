# File: core/tests/test_voice.py

from __future__ import annotations

import threading
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from core.voice import Recorder, Speaker, SpokenNotifier, Transcriber, VoiceConfig, VoiceError, VoiceService, speech_text


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
        self.assertEqual(config.hotkey, "ctrl+alt+space")
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
        speaker._thread.join(timeout=2.0)
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
        self.assertIn("ctrl+alt+space", service.describe())

    def test_notifications_are_read_as_title_then_body(self) -> None:
        service = _service()
        notifier = SpokenNotifier(service)
        self.assertEqual(notifier.name, "voice")
        notifier.send(SimpleNamespace(title="Disk C: is low.", body="12% free on C:."))
        self.assertEqual(service.speaker.spoken, ["Disk C: is low. 12% free on C:."])


if __name__ == "__main__":
    unittest.main()
