"""Document memory: add a file once, recall the sections that answer a question."""

import pytest

from cognicore.memory import documents as docs
from cognicore.memory.base import MemoryEntry
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend

pytest.importorskip("pypdf")


def make_pdf(pages):
    """A minimal valid PDF with one text line per entry in each page's list of lines."""
    objs = []

    def add(body):
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages_id = len(objs) + 1 + 2 * len(pages)  # pages object comes after each page + its content
    kids = []
    for lines in pages:
        ops = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
        for ln in lines:
            safe = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops += [f"({safe}) Tj", "T*"]
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                        b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (pages_id, content, font)))
    assert add(b"<< /Type /Pages /Kids [%s] /Count %d >>"
               % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))) == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, catalog, xref)
    return bytes(out)


@pytest.fixture
def backend(tmp_path):
    return SQLiteMemoryBackend(str(tmp_path / "m.db"))


GUIDE = make_pdf([
    ["Employee handbook", "Holidays: staff get 24 days of paid leave each year.",
     "Leave requests go to your manager two weeks ahead."],
    ["Expenses", "Travel is reimbursed within 30 days of submitting receipts.",
     "Meals over 50 dollars need a manager's approval."],
])


# --- splitting ------------------------------------------------------------------------------

def test_sections_respect_size_and_pages_and_keep_all_text():
    para = " ".join(f"Sentence number {i} talks about topic {i % 7}." for i in range(400))
    pages = [(1, para), (2, "Short page two.\n\nSecond paragraph of page two.")]
    sections = docs.split_sections(pages, target=120)
    assert all(docs._tokens(t) <= 120 for _, t in sections)
    assert [p for p, _ in sections] == sorted(p for p, _ in sections)  # in order, page by page
    assert {p for p, _ in sections} == {1, 2}
    assert " ".join(t for p, t in sections if p == 1).split() == para.split()  # nothing lost or reordered
    assert sections[-1] == (2, "Short page two.\nSecond paragraph of page two.")


def test_one_huge_word_is_still_cut():
    sections = docs.split_sections([(None, "x" * 5000)], target=100)
    assert all(docs._tokens(t) <= 100 for _, t in sections)
    assert "".join(t for _, t in sections) == "x" * 5000


def test_pdf_ligatures_become_plain_letters():
    assert docs.split_sections([(1, "The ﬁeld is eﬀective")]) == [(1, "The field is effective")]


def test_hyphenated_line_breaks_are_joined():
    assert docs.split_sections([(1, "reimburse-\nment within days")]) == [(1, "reimbursement within days")]


# --- adding, recalling, removing ---------------------------------------------------------------

def test_pdf_sections_cite_document_and_page(backend):
    d = docs.add_document(backend, GUIDE, "handbook.pdf")
    assert (d.pages, d.already_present) == (2, False) and d.sections >= 2
    hits = backend.search("travel reimbursed receipts", top_k=1)
    assert hits[0].entry.text.startswith("page 2:")
    assert "30 days" in hits[0].entry.text
    assert hits[0].entry.category == "document:handbook.pdf"
    assert "handbook" not in hits[0].entry.text  # the name is shown via the category, not searched
    assert hits[0].entry.metadata["doc_id"] == d.doc_id and hits[0].entry.metadata["page"] == 2


def test_text_and_markdown_files(backend):
    d = docs.add_document(backend, b"# Notes\n\nThe deploy runs at 02:00 UTC.\n\nRollback with revert.", "ops.md")
    assert d.sections == 1 and d.pages == 1
    hit = backend.search("deploy UTC", top_k=1)[0]
    assert hit.entry.text.startswith("part 1:") and hit.entry.category == "document:ops.md"


def test_same_file_twice_adds_nothing_and_replace_replaces(backend):
    first = docs.add_document(backend, GUIDE, "handbook.pdf")
    again = docs.add_document(backend, GUIDE, "renamed.pdf")
    assert again.already_present and again.doc_id == first.doc_id
    assert backend.count() == first.sections
    replaced = docs.add_document(backend, GUIDE, "handbook.pdf", replace=True)
    assert not replaced.already_present and backend.count() == first.sections


def test_list_and_remove_only_touch_that_document(backend):
    backend.store(MemoryEntry(text="unrelated note about travel"))
    d = docs.add_document(backend, GUIDE, "handbook.pdf")
    other = docs.add_document(backend, b"Office opens at nine.", "office.txt")
    listed = {x["doc_id"]: x for x in docs.list_documents(backend)}
    assert listed[d.doc_id]["name"] == "handbook.pdf" and listed[d.doc_id]["pages"] == 2
    assert listed[other.doc_id]["name"] == "office.txt"
    assert docs.remove_document(backend, d.doc_id) == d.sections
    assert [x["doc_id"] for x in docs.list_documents(backend)] == [other.doc_id]
    texts = [r.entry.text for r in backend.search("travel", top_k=10)]
    assert "unrelated note about travel" in texts and not any("30 days" in t for t in texts)
    assert docs.remove_document(backend, "nope") == 0


# --- refusals -----------------------------------------------------------------------------------

def test_unsupported_too_large_empty_and_damaged(backend, monkeypatch):
    with pytest.raises(docs.DocumentError, match="unsupported"):
        docs.add_document(backend, b"\x89PNG\r\n", "photo.png")
    with pytest.raises(docs.DocumentError, match="no text"):
        docs.add_document(backend, make_pdf([[]]), "scan.pdf")
    with pytest.raises(docs.DocumentError, match="could not be read"):
        docs.add_document(backend, b"%PDF-1.4 garbage", "broken.pdf")
    monkeypatch.setattr(docs, "MAX_BYTES", 10)
    with pytest.raises(docs.DocumentError, match="limit"):
        docs.add_document(backend, GUIDE, "handbook.pdf")
    assert backend.count() == 0


def test_page_limit(backend, monkeypatch):
    monkeypatch.setattr(docs, "MAX_PAGES", 1)
    with pytest.raises(docs.DocumentError, match="pages"):
        docs.add_document(backend, GUIDE, "handbook.pdf")


def test_missing_pypdf_explains_how_to_install(backend, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_pypdf(name, *a, **kw):
        if name == "pypdf":
            raise ImportError("no pypdf")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_pypdf)
    with pytest.raises(docs.DocumentError, match=r"cognicore-env\[docs\]"):
        docs.add_document(backend, GUIDE, "handbook.pdf")
    assert docs.add_document(backend, b"plain text still works", "a.txt").sections == 1


# --- command line -----------------------------------------------------------------------------

def test_cli_add_list_remove(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path / "data"))
    pdf = tmp_path / "handbook.pdf"
    pdf.write_bytes(GUIDE)
    assert docs.main(["add", str(pdf)]) == 0
    assert "added handbook.pdf" in capsys.readouterr().out
    assert docs.main(["add", str(pdf)]) == 0
    assert "already added" in capsys.readouterr().out
    assert docs.main(["add", str(tmp_path / "missing.pdf")]) == 1
    assert docs.main(["list"]) == 0
    out = capsys.readouterr().out
    doc_id = [ln.split()[0] for ln in out.splitlines() if "handbook.pdf" in ln][0]
    assert docs.main(["remove", doc_id]) == 0
    assert docs.main(["list"]) == 0 and "No documents" in capsys.readouterr().out
    assert docs.main([]) == 0 and docs.main(["bogus"]) == 2
