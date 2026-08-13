from __future__ import annotations

import csv
from pathlib import Path

from core.documents.models import ExtractedDocument


class CsvExtractor:
    name = "csv"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".csv"

    def extract(self, path: Path) -> ExtractedDocument:
        rows: list[str] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                reader = csv.reader(handle)
                row_count = 0
                for row in reader:
                    rows.append(" | ".join(row))
                    row_count = row_count + 1
                    if row_count >= 2000:
                        break
            return ExtractedDocument(text="\n".join(rows), content_status="indexed", extractor=self.name)
        except OSError as error:
            return ExtractedDocument(text="", content_status="error", extractor=self.name, error=str(error))

