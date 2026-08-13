from __future__ import annotations

from pathlib import Path
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

from core.documents.models import ExtractedDocument


class DocxExtractor:
    name = "docx"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".docx"

    def extract(self, path: Path) -> ExtractedDocument:
        try:
            with ZipFile(path) as archive:
                xml_bytes = archive.read("word/document.xml")
            root = ET.fromstring(xml_bytes)
            texts = [node.text.strip() for node in root.iter() if node.tag.endswith("}t") and node.text and node.text.strip()]
            return ExtractedDocument(text="\n".join(texts), content_status="indexed", extractor=self.name)
        except KeyError:
            return ExtractedDocument(text="", content_status="text_unavailable", extractor=self.name, error="word/document.xml missing")
        except (BadZipFile, ET.ParseError, OSError) as error:
            return ExtractedDocument(text="", content_status="error", extractor=self.name, error=str(error))

