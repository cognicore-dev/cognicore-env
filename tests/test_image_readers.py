"""Images in document memory: OCR by default, a vision model only when enabled."""

import base64
import io
from types import SimpleNamespace

import pytest

from cognicore.memory import documents as docs
from cognicore.memory import image_readers as ir
from cognicore.memory.sqlite_backend import SQLiteMemoryBackend

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw, ImageFont  # noqa: E402


def png(lines=("Invoice INV-2026-0412", "Total due: 1,250.00 USD"), size=(900, 200), fmt="PNG"):
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=36)
    for i, line in enumerate(lines):
        draw.text((30, 30 + 70 * i), line, fill="black", font=font)
    out = io.BytesIO()
    img.save(out, fmt)
    return out.getvalue()


class Fake(ir.ImageReader):
    def __init__(self, name, text, on=True):
        self.name, self.text, self.on, self.calls = name, text, on, 0

    def available(self):
        return self.on

    def read(self, data):
        self.calls += 1
        return self.text


@pytest.fixture(autouse=True)
def no_real_vision(monkeypatch):
    for var in ("COGNICORE_VISION_PROVIDER", "COGNICORE_VISION_WHEN", "COGNICORE_IMAGE_OCR"):
        monkeypatch.delenv(var, raising=False)


# --- choosing readers ---------------------------------------------------------------------------

def test_ocr_text_is_enough_vision_not_called():
    ocr, vision = Fake("ocr", "x" * 300), Fake("vision", "a chart")
    r = ir.read_image(png(), "a.png", ocr, vision)
    assert r.text == "x" * 300 and r.readers == ["ocr"] and vision.calls == 0


def test_vision_describes_images_with_little_text():
    ocr, vision = Fake("ocr", "Q1 Q2"), Fake("vision", "A bar chart: revenue rises each quarter.")
    r = ir.read_image(png(), "chart.png", ocr, vision)
    assert r.readers == ["ocr", "vision"]
    assert r.text == "Q1 Q2\n\nImage description:\nA bar chart: revenue rises each quarter."


def test_vision_always_when_configured(monkeypatch):
    monkeypatch.setenv("COGNICORE_VISION_WHEN", "always")
    vision = Fake("vision", "a scanned letter")
    assert ir.read_image(png(), "a.png", Fake("ocr", "x" * 300), vision).readers == ["ocr", "vision"]


def test_vision_is_off_by_default():
    reader = ir.AnthropicVisionReader(client=object())
    assert not reader.available()  # a client alone does not switch it on; the operator must


def test_nothing_available_or_nothing_read():
    with pytest.raises(ir.ImageError, match=r"cognicore-env\[ocr\]"):
        ir.read_image(png(), "a.png", Fake("ocr", "", on=False), Fake("vision", "", on=False))
    with pytest.raises(ir.ImageError, match="no text that could be read; describing images needs a vision model"):
        ir.read_image(png(), "a.png", Fake("ocr", ""), Fake("vision", "", on=False))


def test_huge_and_broken_images_are_refused(monkeypatch):
    monkeypatch.setattr(ir, "MAX_PIXELS", 1000)
    with pytest.raises(ir.ImageError, match="million pixels"):
        ir.read_image(png(), "big.png", Fake("ocr", "x"), Fake("vision", "y"))
    with pytest.raises(ir.ImageError, match="could not be read as an image"):
        ir.read_image(b"\x89PNG\r\n broken", "broken.png", Fake("ocr", "x"), Fake("vision", "y"))
    assert issubclass(ir.ImageError, docs.DocumentError)


def test_media_types():
    assert ir.media_type(png()) == "image/png"
    assert ir.media_type(png(fmt="JPEG")) == "image/jpeg"
    assert ir.media_type(png(fmt="WEBP")) == "image/webp"
    assert ir.is_image(b"anything", "scan.TIFF") and not ir.is_image(b"%PDF-1.4", "a.pdf")


# --- the Claude vision reader -------------------------------------------------------------------

class FakeClaude:
    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="A pie chart with three slices.")])


def test_anthropic_reader_sends_the_image_and_prompt(monkeypatch):
    monkeypatch.setenv("COGNICORE_VISION_PROVIDER", "anthropic")
    client = FakeClaude()
    reader = ir.AnthropicVisionReader(client=client)
    assert reader.available()
    assert reader.read(png(fmt="BMP")) == "A pie chart with three slices."
    call = client.calls[0]
    assert call["model"] == ir.DEFAULT_VISION_MODEL
    image, prompt = call["messages"][0]["content"]
    assert image["source"]["media_type"] == "image/png"  # BMP is converted to a format the API takes
    assert base64.b64decode(image["source"]["data"])[:4] == b"\x89PNG"
    assert prompt["text"] == ir.VISION_PROMPT
    monkeypatch.setenv("COGNICORE_VISION_MODEL", "claude-sonnet-5-5")
    reader.read(png())
    assert client.calls[1]["model"] == "claude-sonnet-5-5"


# --- images as documents ------------------------------------------------------------------------

@pytest.fixture
def backend(tmp_path):
    return SQLiteMemoryBackend(str(tmp_path / "m.db"))


def test_image_document_with_fake_readers(backend, monkeypatch):
    monkeypatch.setattr(ir, "OcrReader", lambda: Fake("ocr", "Whiteboard: ship the billing fix on Friday"))
    monkeypatch.setattr(ir, "AnthropicVisionReader", lambda: Fake("vision", "", on=False))
    d = docs.add_document(backend, png(), "whiteboard.png")
    hit = backend.search("billing fix Friday", top_k=1)[0]
    assert hit.entry.category == "document:whiteboard.png" and "ship the billing fix" in hit.entry.text
    assert d.sections == 1


def test_scanned_pdf_without_ocr_is_refused_with_how_to_fix(backend, monkeypatch):
    pytest.importorskip("pypdf")
    monkeypatch.setenv("COGNICORE_IMAGE_OCR", "0")
    scan = io.BytesIO()
    Image.open(io.BytesIO(png())).save(scan, "PDF")
    with pytest.raises(docs.DocumentError, match=r"cognicore-env\[ocr\]"):
        docs.add_document(backend, scan.getvalue(), "scan.pdf")


# --- real OCR (runs where the ocr extra is installed) ---------------------------------------

def _need_ocr():
    if ir.ocr_engine() is None:
        pytest.skip("no OCR engine installed (Tesseract, or the ocr extra)")


def test_real_ocr_reads_an_image(backend):
    _need_ocr()
    docs.add_document(backend, png(), "invoice.png")
    hit = backend.search("INV-2026-0412", top_k=1)[0]
    assert "Invoice INV-2026-0412" in hit.entry.text and "1,250.00 USD" in hit.entry.text


def test_real_ocr_reads_a_scanned_pdf(backend):
    _need_ocr()
    pytest.importorskip("pypdf")
    scan = io.BytesIO()
    Image.open(io.BytesIO(png(("Contract 77-B", "Renewal date: 1 March 2027")))).save(scan, "PDF")
    d = docs.add_document(backend, scan.getvalue(), "contract-scan.pdf")
    assert d.pages == 1
    hit = backend.search("renewal date contract", top_k=1)[0]
    assert hit.entry.text.startswith("page 1:") and "1 March 2027" in hit.entry.text


def test_anthropic_reader_calls_the_api_directly_without_the_sdk(monkeypatch):
    import httpx

    monkeypatch.setenv("COGNICORE_VISION_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not ir.AnthropicVisionReader().available()  # no key, no vision
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    sent = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        sent.update(url=url, json=json, headers=headers)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "A line chart rising to 40."}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    reader = ir.AnthropicVisionReader()
    assert reader.available() and reader.read(png()) == "A line chart rising to 40."
    assert sent["url"] == "https://api.anthropic.com/v1/messages"
    assert sent["headers"] == {"x-api-key": "test-key", "anthropic-version": "2023-06-01"}
    assert sent["json"]["model"] == ir.DEFAULT_VISION_MODEL
    assert sent["json"]["messages"][0]["content"][0]["source"]["media_type"] == "image/png"

    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(529, json={}))
    with pytest.raises(ir.ImageError, match="HTTP 529"):
        reader.read(png())


# --- choosing the OCR engine --------------------------------------------------------------------

@pytest.mark.parametrize("setting,tesseract,rapid,expected", [
    ("auto", True, True, "tesseract"), ("auto", False, True, "rapidocr"), ("auto", False, False, None),
    ("rapidocr", True, True, "rapidocr"), ("tesseract", False, True, None),
])
def test_engine_choice(monkeypatch, setting, tesseract, rapid, expected):
    monkeypatch.setenv("COGNICORE_OCR_ENGINE", setting)
    monkeypatch.setattr(ir, "_tesseract_path", lambda: "/usr/bin/tesseract" if tesseract else None)
    monkeypatch.setattr(ir, "_rapidocr_installed", lambda: rapid)
    assert ir.ocr_engine() == expected
    monkeypatch.setenv("COGNICORE_IMAGE_OCR", "0")
    assert ir.ocr_engine() is None


def test_tesseract_is_run_without_temporary_files(monkeypatch):
    import subprocess

    calls = []

    def fake_run(argv, input=None, capture_output=None, timeout=None, check=None):
        calls.append((argv, input))
        return SimpleNamespace(stdout=b"Invoice INV-7\n\n  Total 12 USD  \n", returncode=0)

    monkeypatch.setenv("COGNICORE_OCR_ENGINE", "tesseract")
    monkeypatch.setattr(ir, "_tesseract_path", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ir.OcrReader().read(b"img") == "Invoice INV-7\nTotal 12 USD"
    assert calls == [(["/usr/bin/tesseract", "stdin", "stdout", "-l", "eng"], b"img")]

    def slow_run(*a, **k):
        raise subprocess.TimeoutExpired("tesseract", 60)
    monkeypatch.setattr(subprocess, "run", slow_run)
    with pytest.raises(ir.ImageError, match="longer than 60 seconds"):
        ir.OcrReader().read(b"img")
