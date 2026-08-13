from __future__ import annotations

from pathlib import Path
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

from core.documents.models import ExtractedDocument


class ExcelExtractor:
    name = "excel"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in {".xlsx", ".xls"}

    def extract(self, path: Path) -> ExtractedDocument:
        if path.suffix.lower() == ".xls":
            return ExtractedDocument(text="", content_status="text_unavailable", extractor=self.name, error="Legacy .xls extraction not implemented")

        try:
            with ZipFile(path) as archive:
                names = archive.namelist()
                workbook_xml = archive.read("xl/workbook.xml")
                workbook_root = ET.fromstring(workbook_xml)
                lines: list[str] = []

                sheet_names = [
                    node.attrib.get("name", "")
                    for node in workbook_root.iter()
                    if node.tag.endswith("}sheet") and node.attrib.get("name")
                ]
                if sheet_names:
                    lines.append("Sheets: " + ", ".join(sheet_names))

                shared_strings: list[str] = []
                if "xl/sharedStrings.xml" in names:
                    shared_xml = archive.read("xl/sharedStrings.xml")
                    shared_root = ET.fromstring(shared_xml)
                    for node in shared_root.iter():
                        if node.tag.endswith("}t") and node.text:
                            shared_strings.append(node.text.strip())

                for entry in names:
                    if not entry.startswith("xl/worksheets/") or not entry.endswith(".xml"):
                        continue
                    sheet_xml = archive.read(entry)
                    sheet_root = ET.fromstring(sheet_xml)
                    lines.append(f"Worksheet: {Path(entry).stem}")
                    cell_values: list[str] = []
                    for node in sheet_root.iter():
                        if not node.tag.endswith("}c"):
                            continue
                        value_node = None
                        for child in node:
                            if child.tag.endswith("}v"):
                                value_node = child
                                break
                        if value_node is None or value_node.text is None:
                            continue
                        raw_value = value_node.text.strip()
                        if node.attrib.get("t") == "s":
                            try:
                                index = int(raw_value)
                                if 0 <= index < len(shared_strings):
                                    raw_value = shared_strings[index]
                            except ValueError:
                                pass
                        if raw_value:
                            cell_values.append(raw_value)
                        if len(cell_values) >= 1000:
                            break
                    if cell_values:
                        lines.append(" ".join(cell_values))

            return ExtractedDocument(text="\n".join(lines), content_status="indexed", extractor=self.name)
        except KeyError:
            return ExtractedDocument(text="", content_status="text_unavailable", extractor=self.name, error="Workbook XML missing")
        except (BadZipFile, ET.ParseError, OSError) as error:
            return ExtractedDocument(text="", content_status="error", extractor=self.name, error=str(error))

