"""Content sniffing. Clients lie about Content-Type, so the file's own bytes decide."""

import codecs
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import UnsupportedMediaError

ALLOWED_EXTENSIONS = {".pdf": "pdf", ".docx": "docx", ".txt": "txt", ".md": "md", ".markdown": "md"}
MIME_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
    "md": "text/markdown",
}


@dataclass(frozen=True, slots=True)
class DetectedType:
    file_type: str
    mime: str


def sanitize_filename(name: str | None) -> str:
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip(" .")
    if not name:
        name = "untitled.txt"
    stem, dot, ext = name.rpartition(".")
    if dot and len(name) > 255:
        name = stem[: 250 - len(ext)] + "." + ext
    return name[:255]


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        # incremental decoder tolerates a multibyte char cut at the end of the sample
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
        return True
    except UnicodeDecodeError:
        return False


def detect_type(path: Path, filename: str) -> DetectedType:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedMediaError(
            f"Unsupported file extension '{ext or '(none)'}'",
            details={"allowed": sorted(ALLOWED_EXTENSIONS)},
        )
    expected = ALLOWED_EXTENSIONS[ext]
    with path.open("rb") as fh:
        head = fh.read(8192)

    if head.startswith(b"%PDF-"):
        actual = "pdf"
    elif head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as zf:
                actual = "docx" if "word/document.xml" in zf.namelist() else "zip"
        except zipfile.BadZipFile:
            actual = "zip"
    elif _looks_like_text(head):
        actual = expected if expected in ("txt", "md") else "txt"
    else:
        actual = "binary"

    if actual != expected:
        raise UnsupportedMediaError(
            f"File content does not match its extension: extension says '{expected}', content looks like '{actual}'",
            details={"extension": ext, "detected": actual},
        )
    return DetectedType(actual, MIME_TYPES[actual])
