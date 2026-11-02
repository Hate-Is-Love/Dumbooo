"""Reading text out of documents: plain text and code, Word (.docx) and PDF.

PDFs are read with ``pypdf``; .docx files are ZIP archives of XML and are
read with the standard library. Scanned PDFs contain no text layer, which is
reported honestly instead of returning an empty summary.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml",
                   ".ini", ".cfg", ".conf", ".xml", ".html", ".htm", ".css", ".py", ".pyi", ".js", ".ts", ".tsx",
                   ".jsx", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sql", ".sh",
                   ".ps1", ".bat", ".cmd", ".r", ".m", ".kt", ".swift", ".dart", ".lua", ".gitignore", ".env.example",
                   ".tex", ".srt", ".ipynb", ".vue", ".scss", ".less", ".gradle", ".properties", ".dockerfile"}
CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs",
                   ".rb", ".php", ".sql", ".kt", ".swift", ".dart", ".lua", ".html", ".css", ".vue", ".scss", ".r"}
DOC_EXTENSIONS = {".pdf", ".docx"}


class DocumentError(ValueError):
    pass


def readable(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in TEXT_EXTENSIONS or suffix in DOC_EXTENSIONS or path.name.lower() in (
        "readme", "license", "makefile", "dockerfile", "requirements.txt")


def _docx_text(path: Path, max_chars: int) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise DocumentError(f"{path.name} is not a valid Word document.") from exc
    root = ElementTree.fromstring(xml)
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = []
    for para in root.iter(f"{ns}p"):
        text = "".join(t.text or "" for t in para.iter(f"{ns}t"))
        if text.strip():
            paragraphs.append(text)
        if sum(len(p) for p in paragraphs) > max_chars:
            break
    return "\n".join(paragraphs)[:max_chars]


def _pdf_text(path: Path, max_chars: int, max_pages: int = 60) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise DocumentError("Reading PDFs needs the 'pypdf' package.") from exc
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise DocumentError(f"{path.name} is password-protected.") from exc
        pages = len(reader.pages)
        out = []
        for page in reader.pages[:max_pages]:
            out.append(page.extract_text() or "")
            if sum(len(p) for p in out) > max_chars:
                break
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f"{path.name} could not be read as a PDF ({exc.__class__.__name__}).") from exc
    text = re.sub(r"[ \t]+\n", "\n", "\n".join(out)).strip()
    return text[:max_chars], pages


def extract_text(path: Path, max_chars: int = 60000) -> dict:
    """``{"text", "pages"?, "truncated"}``; raises ``DocumentError`` for unsupported or unreadable files."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text, pages = _pdf_text(path, max_chars + 1)
        if not text.strip():
            raise DocumentError(f"{path.name} has no text layer (it is probably a scanned image), so I can't "
                                "read it without OCR.")
        return {"text": text[:max_chars], "pages": pages, "truncated": len(text) > max_chars}
    if suffix == ".docx":
        text = _docx_text(path, max_chars + 1)
        return {"text": text[:max_chars], "truncated": len(text) > max_chars}
    if not readable(path):
        raise DocumentError(f"I can't read {suffix or 'this kind of'} files as text.")
    with open(path, "rb") as fh:
        raw = fh.read(max_chars * 4 + 1)
    if b"\x00" in raw[:4096]:
        raise DocumentError(f"{path.name} looks like a binary file, not text.")
    text = raw.decode("utf-8", errors="replace")
    return {"text": text[:max_chars], "truncated": len(text) > max_chars}
