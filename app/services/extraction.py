import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import ExtractionError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Page:
    number: int | None  # 1-based for paginated formats, None otherwise
    text: str


def clean_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # de-hyphenate words broken across lines
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_pdf(path: Path) -> list[Page]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractionError("PDF is password protected", code="encrypted_pdf")
        pages = []
        for i, page in enumerate(reader.pages, start=1):
            try:
                pages.append(Page(i, clean_text(page.extract_text() or "")))
            except Exception as exc:  # one bad page shouldn't sink the document
                logger.warning("pdf page extraction failed", extra={"page": i, "error": str(exc)})
                pages.append(Page(i, ""))
        return pages
    except ExtractionError:
        raise
    except (PdfReadError, ValueError, KeyError) as exc:
        raise ExtractionError(f"Could not parse PDF: {exc}", code="corrupt_file") from exc


def _extract_docx(path: Path) -> list[Page]:
    import docx
    from docx.opc.exceptions import PackageNotFoundError

    try:
        d = docx.Document(str(path))
    except (PackageNotFoundError, KeyError, ValueError) as exc:
        raise ExtractionError(f"Could not parse DOCX: {exc}", code="corrupt_file") from exc
    parts: list[str] = []
    for p in d.paragraphs:
        if not p.text.strip():
            continue
        style = (p.style.name or "").lower() if p.style is not None else ""
        parts.append(f"## {p.text}" if style.startswith("heading") else p.text)
    for table in d.tables:
        rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
        parts.append("\n".join(rows))
    return [Page(None, clean_text("\n\n".join(parts)))]


def _extract_text(path: Path) -> list[Page]:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    return [Page(None, clean_text(text))]


_EXTRACTORS = {"pdf": _extract_pdf, "docx": _extract_docx, "txt": _extract_text, "md": _extract_text}


def extract(path: Path, file_type: str) -> list[Page]:
    if not path.exists():
        raise ExtractionError("Stored file is missing", code="file_missing")
    pages = _EXTRACTORS[file_type](path)
    if sum(len(p.text) for p in pages) < 20:
        raise ExtractionError(
            "No extractable text found (the file may be scanned images; OCR is not supported)",
            code="no_text",
        )
    return pages
