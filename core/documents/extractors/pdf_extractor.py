# File: core/documents/extractors/pdf_extractor.py

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.documents.models import ExtractedDocument

logger = logging.getLogger(__name__)


class PdfExtractor:

    name = "pdf"

    def __init__(self, ocr: Any | None = None, *, max_pages: int = 200, ocr_max_pages: int = 20, ocr_dpi: int = 200) -> None:
        self.ocr = ocr
        self.max_pages = max_pages
        self.ocr_max_pages = ocr_max_pages
        self.ocr_dpi = ocr_dpi

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".pdf"

    def extract(self, path: Path) -> ExtractedDocument:
        try:
            #! @allow-local-import
            import pymupdf
        except Exception:
            return ExtractedDocument(text="", content_status="text_unavailable", extractor=self.name, error="Install pymupdf for PDF extraction")

        try:
            pymupdf.TOOLS.mupdf_display_errors(False)
        except Exception:
            pass

        try:
            document = pymupdf.open(str(path))
        except Exception as error:
            return ExtractedDocument(text="", content_status="error", extractor=self.name, error=str(error))

        try:
            fragments: list[str] = []
            without_text: list[int] = []
            page_count = min(len(document), self.max_pages)
            for index in range(page_count):
                try:
                    text = document[index].get_text("text") or ""
                except Exception as error:
                    logger.debug("PDF page %d of %s unreadable: %s", index + 1, path, error)
                    text = ""
                if text.strip():
                    fragments.append(f"[page {index + 1}]\n{text.strip()}")
                else:
                    without_text.append(index)

            ocr_used = 0
            ocr_error: str | None = None
            if without_text and self.ocr is not None:
                for index in without_text[: self.ocr_max_pages]:
                    try:
                        image = document[index].get_pixmap(dpi=self.ocr_dpi).tobytes("png")
                        recognised = self.ocr.read(image)
                    except Exception as error:
                        ocr_error = str(error)
                        logger.warning("OCR failed on page %d of %s: %s", index + 1, path, error)
                        break
                    if recognised and recognised.strip():
                        fragments.append(f"[page {index + 1}] (ocr)\n{recognised.strip()}")
                        ocr_used += 1
                fragments.sort(key=_page_number)

            if not fragments:
                detail = "No extractable text"
                if without_text and self.ocr is None:
                    detail += " (no OCR reader configured)"
                elif ocr_error:
                    detail += f" (OCR failed: {ocr_error})"
                return ExtractedDocument(text="", content_status="text_unavailable", extractor=self.name, error=detail)
            note = None
            if len(document) > self.max_pages:
                note = f"Only the first {self.max_pages} of {len(document)} pages were read"
            return ExtractedDocument(text="\n\n".join(fragments), content_status="indexed", extractor=self.name, error=note)
        except Exception as error:
            return ExtractedDocument(text="", content_status="error", extractor=self.name, error=str(error))
        finally:
            document.close()


def _page_number(fragment: str) -> int:
    header = fragment.split("]", 1)[0]
    try:
        return int(header.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0
