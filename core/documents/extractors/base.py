from __future__ import annotations

from pathlib import Path
from typing import Protocol

from core.documents.models import ExtractedDocument


class DocumentExtractor(Protocol):
    name: str

    def supports(self, path: Path) -> bool:
        ...

    def extract(self, path: Path) -> ExtractedDocument:
        ...

