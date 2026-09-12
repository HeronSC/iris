# File: core/documents/ocr.py

from __future__ import annotations

import base64
import io
import logging
from typing import Any, Protocol

from core.llm.models import ChatMessage, LLMRequest

logger = logging.getLogger(__name__)

VISION_PROMPT = (
    "Transcribe every piece of text in this image exactly as written, top to bottom, "
    "left to right. Keep line breaks. Output only the transcribed text, nothing else. "
    "If there is no text, output nothing."
)


class OcrReader(Protocol):
    name: str

    def read(self, image_png: bytes) -> str:
        ...


class WindowsOcr:

    name = "windows-ocr"

    def __init__(self, language: str = "en") -> None:
        self.language = language

    @staticmethod
    def available(language: str = "en") -> bool:
        try:
            #! @allow-local-import
            import pymupdf
            #! @allow-local-import
            from winocr import Language, OcrEngine

            return bool(OcrEngine.is_language_supported(Language(language)))
        except ImportError:
            return False

    def read(self, image_png: bytes) -> str:
        #! @allow-local-import
        import asyncio

        #! @allow-local-import
        import pymupdf
        #! @allow-local-import
        import winocr

        pixmap = pymupdf.Pixmap(image_png)
        if pixmap.colorspace is None or pixmap.colorspace.n != 3:
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
        if not pixmap.alpha:
            pixmap = pymupdf.Pixmap(pixmap, 1)
        pending = winocr.recognize_bytes(bytes(pixmap.samples), pixmap.width, pixmap.height, self.language)
        result = winocr.picklify(asyncio.run(winocr.to_coroutine(pending)))
        return _result_text(result)


class VisionOcr:

    name = "vision-model"

    def __init__(self, llm_client: Any, *, task: str = "vision", prompt: str = VISION_PROMPT) -> None:
        self.llm_client = llm_client
        self.task = task
        self.prompt = prompt

    def read(self, image_png: bytes) -> str:
        self._require_vision_model()
        request = LLMRequest(
            messages=(ChatMessage(role="user", content=self.prompt, images=(image_png,)),),
            task=self.task,
            think=False,
        )
        response = self.llm_client.chat(request)
        return (response.content or "").strip()

    def _require_vision_model(self) -> None:
        model_for = getattr(self.llm_client, "model_for", None)
        capabilities = getattr(self.llm_client, "capabilities", None)
        if not callable(model_for) or not callable(capabilities):
            return
        model = model_for(self.task)
        known = capabilities(model)
        if known and "vision" not in known:
            raise RuntimeError(f"{model} has no vision capability")
        if not known:
            raise RuntimeError(f"{model} is not available for vision")


class OcrService:

    name = "ocr"

    def __init__(self, readers: list[Any]) -> None:
        self.readers = [reader for reader in readers if reader is not None]
        self.last_reader: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.readers)

    def read(self, image_png: bytes) -> str:
        errors: list[str] = []
        for reader in self.readers:
            try:
                text = reader.read(image_png)
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                errors.append(f"{getattr(reader, 'name', reader.__class__.__name__)}: {error}")
                logger.warning("OCR reader %s failed: %s", getattr(reader, "name", reader), error)
                continue
            if text and text.strip():
                self.last_reader = getattr(reader, "name", None)
                return text
        if errors and len(errors) == len(self.readers):
            raise RuntimeError("; ".join(errors))
        self.last_reader = None
        return ""


def _result_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        text = result.get("text")
        if isinstance(text, str) and text.strip():
            return text
        lines = result.get("lines") or []
        return "\n".join(str(line.get("text", "")) if isinstance(line, dict) else str(line) for line in lines)
    text = getattr(result, "text", None)
    return str(text) if text else ""


def png_to_base64(image_png: bytes) -> str:
    return base64.b64encode(image_png).decode("ascii")
