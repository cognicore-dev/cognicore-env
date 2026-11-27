"""Document memory: add a file once, recall only the parts that answer a question.

A document (PDF, plain text or Markdown) is split into sections of about SECTION_TOKENS
tokens, never across pages, and each section is stored as a memory:

    text       "page <n>:\\n<section text>"     (text files: "part <n>:")
    category   "document:<name>"                (recall shows it next to each result)
    session_id "doc:<doc_id>"                   (all sections of one document, in order)
    metadata   doc_id, document (name), page, section, sha256

so recall finds the relevant sections in any mode and shows the document and page, and the
whole document never has to be pasted into a chat. The name stays out of the section text
because search indexes the text: a file name repeated in every section would make every
section match its words. Adding the same file again adds nothing
(same doc_id, from the content's SHA-256). Removing a document deletes all its sections.

PDF support needs ``pypdf`` (pip install cognicore-env[docs]). Scanned PDFs and images have
no extractable text; they are refused with a clear message rather than stored empty.
"""

import hashlib
import io
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from cognicore.memory.base import MemoryEntry, MemoryScope

SECTION_TOKENS = 300
MAX_BYTES = 25 * 1024 * 1024
MAX_PAGES = 1000
MAX_SECTIONS = 5000
CATEGORY = "document"  # stored as "document:<name>"
SOURCE = "documents"
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".text"}
PDF_SUFFIXES = {".pdf"}

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_BLANK_LINES = re.compile(r"\n\s*\n")


class DocumentError(ValueError):
    """A document that cannot be added: unsupported, too large, unreadable or empty."""


@dataclass
class AddedDocument:
    doc_id: str
    name: str
    sections: int
    pages: int
    already_present: bool = False


def _tokens(text: str) -> int:
    return (len(text) + 3) // 4


# --- reading --------------------------------------------------------------------------------

def read_pages(data: bytes, name: str) -> List[Tuple[Optional[int], str]]:
    """(page number or None, text) for each page of the document *data* named *name*."""
    if len(data) > MAX_BYTES:
        raise DocumentError(f"{name} is {len(data) // (1024 * 1024)} MB; the limit is {MAX_BYTES // (1024 * 1024)} MB.")
    suffix = Path(name).suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return [(None, data.decode("utf-8", errors="replace"))]
    if suffix in PDF_SUFFIXES or data[:5] == b"%PDF-":
        return _pdf_pages(data, name)
    raise DocumentError(f"{name}: unsupported file type. Supported: PDF, .txt, .md.")


def _pdf_pages(data: bytes, name: str) -> List[Tuple[Optional[int], str]]:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise DocumentError("Reading PDFs needs pypdf: pip install cognicore-env[docs]") from e
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentError(f"{name} is password protected.")
        if len(reader.pages) > MAX_PAGES:
            raise DocumentError(f"{name} has {len(reader.pages)} pages; the limit is {MAX_PAGES}.")
        return [(i + 1, page.extract_text() or "") for i, page in enumerate(reader.pages)]
    except DocumentError:
        raise
    except Exception as e:  # pypdf raises many types on damaged files
        raise DocumentError(f"{name} could not be read as a PDF ({type(e).__name__}).") from e


# --- splitting ------------------------------------------------------------------------------

def _clean(text: str) -> str:
    # NFKC turns PDF ligatures ("ﬁeld" with U+FB01) and other compatibility forms into plain
    # letters, so the words match what people search for.
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # words hyphenated across lines
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _blocks(text: str) -> List[str]:
    """Paragraphs; when a page has none (common in PDF text), its lines."""
    paragraphs = [p.strip() for p in _BLANK_LINES.split(text) if p.strip()]
    if len(paragraphs) > 1:
        return paragraphs
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def _pieces(block: str, limit: int) -> List[str]:
    """*block*, cut at sentence ends (or, failing that, spaces) into parts of at most *limit* tokens."""
    if _tokens(block) <= limit:
        return [block]
    out, current = [], ""
    for sentence in _SENTENCE_END.split(block):
        while _tokens(sentence) > limit:  # one very long sentence: cut at a space near the limit
            cut = sentence.rfind(" ", 0, limit * 4) if " " in sentence[: limit * 4] else limit * 4
            cut = cut if cut > 0 else limit * 4
            if current:
                out.append(current)
                current = ""
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        candidate = f"{current} {sentence}".strip()
        if current and _tokens(candidate) > limit:
            out.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        out.append(current)
    return [p for p in out if p]


def split_sections(pages: Sequence[Tuple[Optional[int], str]],
                   target: int = SECTION_TOKENS) -> List[Tuple[Optional[int], str]]:
    """(page, text) sections of about *target* tokens. Blocks are kept whole when they fit,
    joined in reading order, and never cross a page."""
    sections: List[Tuple[Optional[int], str]] = []
    for page, raw in pages:
        current: List[str] = []
        size = 0
        for block in _blocks(_clean(raw)):
            for piece in _pieces(block, target):
                t = _tokens(piece)
                if current and size + t > target:
                    sections.append((page, "\n".join(current)))
                    current, size = [], 0
                current.append(piece)
                size += t + 1
        if current:
            sections.append((page, "\n".join(current)))
    return sections


# --- storing --------------------------------------------------------------------------------

def _doc_session(doc_id: str) -> str:
    return f"doc:{doc_id}"


def _existing_sections(backend, doc_id: str) -> int:
    with backend._get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE session_id = ? AND source_component = ? "
            "AND state NOT IN ('archived', 'deleted')", (_doc_session(doc_id), SOURCE)).fetchone()[0]


def add_document(backend, data: bytes, name: str, replace: bool = False) -> AddedDocument:
    """Split the document into sections and store them. Same content twice adds nothing."""
    name = Path(name).name or "document"
    sha = hashlib.sha256(data).hexdigest()
    doc_id = sha[:12]
    existing = _existing_sections(backend, doc_id)
    if existing and not replace:
        return AddedDocument(doc_id, name, existing, 0, already_present=True)

    pages = read_pages(data, name)
    sections = split_sections(pages)
    if not sections:
        raise DocumentError(f"{name} has no text to store. Scanned PDFs and images need text recognition, "
                            "which is not supported yet.")
    if len(sections) > MAX_SECTIONS:
        raise DocumentError(f"{name} splits into {len(sections)} sections; the limit is {MAX_SECTIONS}.")
    if existing:
        remove_document(backend, doc_id)

    entries = []
    for i, (page, text) in enumerate(sections, start=1):
        where = f"page {page}" if page is not None else f"part {i}"
        entries.append(MemoryEntry(
            text=f"{where}:\n{text}",
            category=f"{CATEGORY}:{name[:80]}",
            scope=MemoryScope.GLOBAL,
            session_id=_doc_session(doc_id),
            sequence_id=i,
            metadata={"doc_id": doc_id, "document": name, "page": page, "section": i, "sha256": sha},
            source_component=SOURCE,
            creation_reason="document added",
        ))
    backend.store_many(entries)
    return AddedDocument(doc_id, name, len(entries), len(pages))


def add_file(backend, path: Union[str, Path], name: Optional[str] = None, replace: bool = False) -> AddedDocument:
    path = Path(path)
    if not path.is_file():
        raise DocumentError(f"{path} is not a file.")
    if path.stat().st_size > MAX_BYTES:
        raise DocumentError(f"{path.name} is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    return add_document(backend, path.read_bytes(), name or path.name, replace=replace)


def list_documents(backend) -> List[Dict[str, Any]]:
    """One row per stored document: doc_id, name, sections, pages, added (timestamp)."""
    with backend._get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id, COUNT(*), MIN(timestamp), MIN(metadata_json) FROM memory_entries "
            "WHERE session_id LIKE 'doc:%' AND source_component = ? AND state NOT IN ('archived', 'deleted') "
            "GROUP BY session_id ORDER BY MIN(timestamp) DESC", (SOURCE,)).fetchall()
        out = []
        for session_id, count, added, meta_json in rows:
            meta = json.loads(meta_json or "{}")
            pages = conn.execute(
                "SELECT COUNT(DISTINCT json_extract(metadata_json, '$.page')) FROM memory_entries "
                "WHERE session_id = ?", (session_id,)).fetchone()[0]
            out.append({"doc_id": session_id[4:], "name": meta.get("document", "?"), "sections": count,
                        "pages": pages, "added": added})
    return out


def remove_document(backend, doc_id: str) -> int:
    """Delete every section of the document; returns how many were deleted."""
    with backend._get_conn() as conn:
        cur = conn.execute("DELETE FROM memory_entries WHERE session_id = ? AND source_component = ?",
                           (_doc_session(doc_id), SOURCE))
        return cur.rowcount


# --- command line ---------------------------------------------------------------------------

USAGE = """usage: cognicore docs add FILE [FILE ...] [--replace]
       cognicore docs list
       cognicore docs remove DOC_ID

Adds documents (PDF, .txt, .md) to your local CogniCore memory, which Claude Code, Codex and
the local MCP server recall from. Only the sections that match a question are recalled."""


def main(argv: Sequence[str]) -> int:
    from cognicore.hooks import _backend

    argv = list(argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    action, rest = argv[0], argv[1:]
    backend = _backend()
    if action == "add":
        replace = "--replace" in rest
        files = [a for a in rest if a != "--replace"]
        if not files:
            print(USAGE)
            return 2
        failed = 0
        for f in files:
            try:
                d = add_file(backend, f, replace=replace)
            except DocumentError as e:
                print(f"  skipped: {e}")
                failed += 1
                continue
            if d.already_present:
                print(f"  {d.name}: already added ({d.sections} sections, id {d.doc_id})")
            else:
                print(f"  added {d.name}: {d.sections} sections from {d.pages} page(s), id {d.doc_id}")
        return 1 if failed else 0
    if action == "list":
        docs = list_documents(backend)
        if not docs:
            print("  No documents added yet.")
        for d in docs:
            print(f"  {d['doc_id']}  {d['name']}  ({d['sections']} sections, {d['pages']} page(s))")
        return 0
    if action == "remove" and len(rest) == 1:
        n = remove_document(backend, rest[0].strip())
        print(f"  removed {n} section(s)" if n else f"  no document with id {rest[0]}")
        return 0 if n else 1
    print(USAGE)
    return 2
