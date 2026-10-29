import io
import uuid
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import UnsupportedMediaError
from app.schemas.ai import DocumentAnalysis
from app.services.analysis import parse_json_model
from app.services.chat import extract_citations, strip_citations
from app.services.chunking import chunk_pages
from app.services.extraction import Page, clean_text
from app.services.file_validation import detect_type, sanitize_filename
from app.services.llm.pricing import cost_usd
from app.services.retrieval import RetrievedChunk, or_tsquery
from app.services.tokens import count_tokens


# ---------------------------------------------------------------- chunking
def test_chunks_respect_size_and_keep_page_provenance():
    pages = [Page(i, f"Page {i} paragraph. " * 60) for i in range(1, 6)]
    chunks = chunk_pages(pages, size=120, overlap=20)
    assert len(chunks) > 5
    assert all(c.token_count <= 120 + 5 for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert chunks[0].page_start == 1 and chunks[-1].page_end == 5


def test_chunks_overlap_carries_previous_tail():
    paras = [Page(1, "\n\n".join(f"Paragraph number {i} has some words." for i in range(40)))]
    chunks = chunk_pages(paras, size=60, overlap=20)
    prev, nxt = chunks[0].content.split("\n\n"), chunks[1].content.split("\n\n")
    carried = [p for p in nxt if p in prev]
    assert carried and carried == prev[-len(carried) :] and nxt[: len(carried)] == carried


def test_oversized_sentence_is_split_by_words():
    chunks = chunk_pages([Page(None, "word " * 2000)], size=100, overlap=10)
    assert len(chunks) >= 15
    assert all(c.token_count <= 110 for c in chunks)


def test_overlap_must_be_smaller_than_size():
    with pytest.raises(ValueError):
        chunk_pages([Page(1, "x")], size=10, overlap=10)


def test_clean_text_dehyphenates_and_collapses_whitespace():
    assert clean_text("inter-\nnational   law\n\n\n\nend") == "international law\n\nend"


# ---------------------------------------------------------------- validation
def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_detects_pdf_and_text(tmp_path):
    assert detect_type(_write(tmp_path, "a.pdf", b"%PDF-1.4 ..."), "a.pdf").file_type == "pdf"
    assert detect_type(_write(tmp_path, "a.md", "# héllo".encode()), "a.md").file_type == "md"


def test_detects_docx_by_zip_structure(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<w/>")
    assert detect_type(_write(tmp_path, "a.docx", buf.getvalue()), "a.docx").file_type == "docx"


@pytest.mark.parametrize(
    "name,data",
    [("a.pdf", b"not a pdf at all"), ("a.docx", b"PK\x03\x04garbage"), ("a.txt", b"\x00\x01\x02binary"), ("a.exe", b"MZ")],
)
def test_rejects_mismatched_or_unsupported(tmp_path, name, data):
    with pytest.raises(UnsupportedMediaError):
        detect_type(_write(tmp_path, name, data), name)


def test_sanitize_filename_strips_paths_and_control_chars():
    assert sanitize_filename("../../etc/passwd\x00.txt") == "passwd.txt"
    assert sanitize_filename("C:\\Users\\x\\report.pdf") == "report.pdf"
    assert sanitize_filename("") == "untitled.txt"
    assert len(sanitize_filename("a" * 400 + ".pdf")) <= 255


# ---------------------------------------------------------------- citations & retrieval helpers
def _hit(i: int) -> RetrievedChunk:
    return RetrievedChunk(uuid.uuid4(), uuid.uuid4(), f"doc{i}.pdf", i, f"content {i}", i, i + 1, 10, 0.1, 1, None)


def test_extract_citations_dedupes_and_ignores_unknown_ids():
    sources = {"S1": _hit(1), "S2": _hit(2)}
    cits = extract_citations("A [S2]. B [S1, S2]. C [S9].", sources)
    assert [c["source_id"] for c in cits] == ["S2", "S1"]
    assert cits[0]["pages"] == "2-3"


def test_strip_citations():
    assert strip_citations("Fact [S1][S2] and more [S3, S4].") == "Fact  and more ."


def test_or_tsquery_sanitises_input():
    assert or_tsquery("What's the notice (period)?!") == "what | the | notice | period"
    assert or_tsquery("&&|!") is None


# ---------------------------------------------------------------- AI output parsing
def test_parse_json_model_handles_fences_and_normalises():
    raw = '```json\n{"title":"T","summary":"S","category":"LEGAL","tags":["Q3 Revenue","q3 revenue","x"]}\n```'
    a = parse_json_model(raw, DocumentAnalysis)
    assert a.category == "legal"
    assert a.tags == ["q3-revenue", "x"]


def test_unknown_category_falls_back_to_other():
    assert DocumentAnalysis(title="t", summary="s", category="astrology").category == "other"


def test_parse_json_model_rejects_non_json():
    with pytest.raises(ValueError):
        parse_json_model("sorry, I cannot do that", DocumentAnalysis)


# ---------------------------------------------------------------- pricing / tokens
def test_cost_uses_longest_prefix_match():
    assert cost_usd("gpt-4o-mini-2024-07-18", 1_000_000, 0) == pytest.approx(0.15)
    assert cost_usd("gpt-4o-2024-08-06", 1_000_000, 0) == pytest.approx(2.50)
    assert cost_usd("unknown-model", 1000, 1000) == 0.0


def test_count_tokens():
    assert count_tokens("") == 0
    assert 1 <= count_tokens("hello world") <= 4


# ---------------------------------------------------------------- config
def test_openai_provider_without_key_fails_at_startup():
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(_env_file=None, llm_provider="openai", openai_api_key=None)
    assert Settings(_env_file=None, llm_provider="fake").llm_provider == "fake"
