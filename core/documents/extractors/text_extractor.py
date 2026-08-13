from __future__ import annotations

from pathlib import Path

from core.documents.models import ExtractedDocument


TEXT_FILE_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".cs",
    ".al",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".yml",
    ".yaml",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sql",
    ".ps1",
    ".psm1",
    ".psd1",
    ".bat",
    ".cmd",
    ".sh",
    ".ini",
    ".cfg",
    ".conf",
    ".toml",
    ".csproj",
    ".sln",
}


class TextExtractor:
    name = "text"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in TEXT_FILE_EXTENSIONS

    def extract(self, path: Path) -> ExtractedDocument:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            return ExtractedDocument(text=text, content_status="indexed", extractor=self.name)
        except OSError as error:
            return ExtractedDocument(text="", content_status="error", extractor=self.name, error=str(error))

