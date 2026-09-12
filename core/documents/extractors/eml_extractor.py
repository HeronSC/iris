# File: core/documents/extractors/eml_extractor.py

from __future__ import annotations

import html
import re
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path

from core.documents.models import ExtractedDocument

HEADER_ORDER = ("From", "To", "Cc", "Date", "Subject")
TAG_PATTERN = re.compile(r"<[^>]+>")
SPACE_PATTERN = re.compile(r"[ \t]+")
BLANK_PATTERN = re.compile(r"\n{3,}")


def email_text(message: EmailMessage) -> str:
    lines = [f"{name}: {message.get(name)}" for name in HEADER_ORDER if message.get(name)]
    body = message.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        content = body.get_content()
        text = strip_html(content) if body.get_content_subtype() == "html" else content
    attachments = [item.get_filename() for item in message.iter_attachments() if item.get_filename()]
    if attachments:
        lines.append("Attachments: " + ", ".join(attachments))
    return "\n".join(lines) + "\n\n" + text.strip() + "\n"


def strip_html(markup: str) -> str:
    without_blocks = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    with_breaks = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", without_blocks)
    with_cells = re.sub(r"(?i)</t[dh]>", " ", with_breaks)
    plain = html.unescape(TAG_PATTERN.sub("", with_cells)).replace("\xa0", " ")
    plain = SPACE_PATTERN.sub(" ", plain)
    return BLANK_PATTERN.sub("\n\n", "\n".join(line.strip() for line in plain.splitlines()))


class EmlExtractor:
    name = "eml"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".eml"

    def extract(self, path: Path) -> ExtractedDocument:
        try:
            message = message_from_bytes(path.read_bytes(), policy=policy.default)
        except OSError as error:
            return ExtractedDocument(text="", content_status="error", extractor=self.name, error=str(error))
        try:
            text = email_text(message)
        except (LookupError, UnicodeError, ValueError, TypeError) as error:
            return ExtractedDocument(text="", content_status="error", extractor=self.name, error=f"Could not decode the message: {error}")
        return ExtractedDocument(text=text, content_status="indexed", extractor=self.name)


__all__ = ["EmlExtractor", "email_text", "strip_html"]
