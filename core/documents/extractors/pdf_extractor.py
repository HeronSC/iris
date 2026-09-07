# File: core/documents/extractors/pdf_extractor.py

from __future__ import annotations

import logging
from pathlib import Path

from core.documents.models import ExtractedDocument


class PdfExtractor:
    name = "pdf"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".pdf"

    def extract(self, path: Path) -> ExtractedDocument:
        try:
            #! @allow-local-import
            from pypdf import PdfReader
        except Exception:
            return ExtractedDocument(
                text="",
                content_status="text_unavailable",
                extractor=self.name,
                error="Install pypdf for PDF extraction",
            )

        try:
            pypdf_logger = logging.getLogger("pypdf")
            previous_level = pypdf_logger.level
            pypdf_logger.setLevel(logging.ERROR)
            reader = PdfReader(str(path))
            fragments: list[str] = []
            for page in reader.pages[:50]:
                text = page.extract_text() or ""
                if text.strip():
                    fragments.append(text.strip())
            if not fragments:
                return ExtractedDocument(text="", content_status="text_unavailable", extractor=self.name, error="No extractable text")
            return ExtractedDocument(text="\n".join(fragments), content_status="indexed", extractor=self.name)
        except Exception as error:
            return ExtractedDocument(text="", content_status="error", extractor=self.name, error=str(error))
        finally:
            pypdf_logger = logging.getLogger("pypdf")
            pypdf_logger.setLevel(previous_level)

