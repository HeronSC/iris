# File: core/assistant/voice_command.py

from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.voice.errors import VoiceError

USAGE = "Usage: /voice | /voice start | /voice stop | /voice wake [on|off] | /voice mute | /voice unmute | /voice say <text>"


class VoiceCommandHandler:
    def __init__(self, voice: Any, output: OutputSink | None = None) -> None:
        self.voice = voice
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        text = user_input.strip()
        lowered = text.lower()
        if lowered != "/voice" and not lowered.startswith("/voice "):
            return False
        parts = text.split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "status"
        if action == "status":
            emit_output(self.output, self.voice.describe())
            return True
        if not self.voice.available:
            emit_output(self.output, self.voice.problem or "Voice is not available.")
            return True
        if action == "start":
            try:
                self.voice.start_conversation()
            except VoiceError as error:
                emit_output(self.output, str(error))
                return True
            emit_output(self.output, "Conversation on. Just talk; say \"that's all\" when you are done.")
            return True
        if action in {"stop", "end"}:
            ended = self.voice.end_conversation("stopped")
            self.voice.stop_speaking()
            emit_output(self.output, "Conversation ended." if ended else "No conversation was running.")
            return True
        if action == "wake":
            wanted = parts[2].strip().lower() if len(parts) > 2 else ("off" if self.voice.wake_mode else "on")
            if wanted in {"on", "start"}:
                try:
                    self.voice.start_wake()
                except VoiceError as error:
                    emit_output(self.output, str(error))
                    return True
                emit_output(self.output, "Wake word on. Say \"Iris\" to start; /voice wake off to stop listening for it.")
                return True
            if wanted in {"off", "stop"}:
                emit_output(self.output, "Wake word off." if self.voice.stop_wake() else "The wake word was not on.")
                return True
            emit_output(self.output, "Usage: /voice wake [on|off]")
            return True
        if action in {"mute", "unmute"}:
            self.voice.muted = action == "mute"
            if self.voice.muted:
                self.voice.stop_speaking()
            emit_output(self.output, "Voice muted." if self.voice.muted else "Voice unmuted.")
            return True
        if action == "say":
            phrase = parts[2].strip() if len(parts) > 2 else ""
            if not phrase:
                emit_output(self.output, "Usage: /voice say <text>")
                return True
            spoken = self.voice.speak(phrase)
            emit_output(self.output, "Speaking." if spoken else "Nothing was spoken (muted, or voice is unavailable).")
            return True
        emit_output(self.output, USAGE)
        return True
