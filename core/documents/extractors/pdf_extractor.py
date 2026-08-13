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
            from pypdf import PdfReader  # type: ignore
        except Exception:
            return ExtractedDocument(
                text="",
                content_status="text_unavailable",
                extractor=self.name,
                error="Install pypdf for PDF extraction",
            )

        try:
            # pypdf can emit noisy parser warnings for partially malformed PDFs.
            # We still want to attempt extraction and report hard failures, but
            # avoid flooding the interactive scan output with warning lines.
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

