"""Reading images for document memory: OCR by default, a vision model when enabled.

``read_image(data, name)`` returns the text to store for an image:

ocr     Text in the image (screenshots, scans, photographed pages), read on this machine by
        RapidOCR (pip install cognicore-env[ocr]). Free, offline, nothing leaves the machine.
vision  A description of what the image shows (diagrams, charts, photos), from a vision
        model. Off unless COGNICORE_VISION_PROVIDER is set; then the image is sent to that
        provider, which costs money per image. By default it runs only when OCR found little
        text (COGNICORE_VISION_WHEN=little_text), i.e. where OCR cannot help.

Both are behind one interface (``ImageReader``), so other OCR engines or vision providers
can be added without changing callers. An image with no readable text and no description
is refused rather than stored empty.

Settings (environment):
    COGNICORE_IMAGE_OCR          "0" turns OCR off (default on when RapidOCR is installed)
    COGNICORE_VISION_PROVIDER    "anthropic" to describe images with Claude (default: off)
    COGNICORE_VISION_MODEL       model name (default claude-haiku-5-5)
    COGNICORE_VISION_WHEN        little_text (default) | always
    ANTHROPIC_API_KEY            for the anthropic provider
"""

import base64
import io
import os
from dataclasses import dataclass
from typing import List, Optional

from cognicore.memory.documents import DocumentError

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
MAX_PIXELS = 40_000_000           # refuse decompression bombs before decoding
LITTLE_TEXT = 200                 # characters of OCR text below which vision is used (little_text)
DEFAULT_VISION_MODEL = "claude-haiku-5-5"
VISION_PROMPT = (
    "This image is being stored in a searchable memory. First transcribe every piece of text in it "
    "exactly. Then describe what it shows: for a chart, its type, axes, series and the values or trend; "
    "for a diagram, its parts and how they connect; for a photo, the subject and any details someone "
    "might later ask about. Be factual and concise. Do not add anything that is not in the image."
)
_MAGIC = {b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg", b"GIF8": "image/gif", b"BM": "image/bmp",
          b"II*\x00": "image/tiff", b"MM\x00*": "image/tiff"}


class ImageError(DocumentError):
    """An image that cannot be read: unsupported, too large or with nothing readable."""


@dataclass
class Reading:
    text: str
    readers: List[str]  # which readers produced it, e.g. ["ocr"] or ["ocr", "vision"]


def is_image(data: bytes, name: str = "") -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_SUFFIXES or media_type(data) is not None


def media_type(data: bytes) -> Optional[str]:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for magic, kind in _MAGIC.items():
        if data.startswith(magic):
            return kind
    return None


class ImageReader:
    name = "reader"

    def available(self) -> bool:
        raise NotImplementedError

    def read(self, data: bytes) -> str:
        raise NotImplementedError


class OcrReader(ImageReader):
    """RapidOCR: text lines in reading order. The engine loads once per process."""

    name = "ocr"
    _engine = None

    def available(self) -> bool:
        if os.environ.get("COGNICORE_IMAGE_OCR", "1").strip() == "0":
            return False
        try:
            import rapidocr  # noqa: F401
        except ImportError:
            return False
        return True

    def read(self, data: bytes) -> str:
        if OcrReader._engine is None:
            import logging

            from rapidocr import RapidOCR

            logging.getLogger("RapidOCR").setLevel(logging.WARNING)
            OcrReader._engine = RapidOCR()
        result = OcrReader._engine(data)
        return "\n".join(t.strip() for t in (result.txts or ()) if t and t.strip())


class AnthropicVisionReader(ImageReader):
    """Claude describes the image. Sends the image to Anthropic."""

    name = "vision"

    def __init__(self, client=None):
        self._client = client

    def available(self) -> bool:
        if os.environ.get("COGNICORE_VISION_PROVIDER", "").strip().lower() != "anthropic":
            return False
        if self._client is not None:
            return True
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def read(self, data: bytes) -> str:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        kind = media_type(data) or "image/png"
        if kind not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            data, kind = _as_png(data), "image/png"
        message = self._client.messages.create(
            model=os.environ.get("COGNICORE_VISION_MODEL", DEFAULT_VISION_MODEL),
            max_tokens=1024,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": kind,
                                             "data": base64.b64encode(data).decode("ascii")}},
                {"type": "text", "text": VISION_PROMPT},
            ]}],
        )
        return "\n".join(b.text for b in message.content if getattr(b, "type", "") == "text").strip()


def _as_png(data: bytes) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    with Image.open(io.BytesIO(data)) as img:
        img.convert("RGB").save(out, "PNG")
    return out.getvalue()


def check_size(data: bytes, name: str) -> None:
    """Refuse images too large to decode safely, before any reader decodes them."""
    try:
        from PIL import Image
    except ImportError:
        return  # readers that need Pillow are unavailable too
    try:
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
    except Exception as e:
        raise ImageError(f"{name} could not be read as an image ({type(e).__name__}).") from e
    if w * h > MAX_PIXELS:
        raise ImageError(f"{name} is {w}x{h} pixels; the limit is {MAX_PIXELS // 1_000_000} million pixels.")


def read_image(data: bytes, name: str, ocr: Optional[ImageReader] = None,
               vision: Optional[ImageReader] = None) -> Reading:
    """The text to store for an image (see the module docstring)."""
    ocr = ocr or OcrReader()
    vision = vision or AnthropicVisionReader()
    check_size(data, name)
    if not ocr.available() and not vision.available():
        raise ImageError("Reading images needs OCR (pip install cognicore-env[ocr]) or a vision model "
                         "(COGNICORE_VISION_PROVIDER).")
    parts, used = [], []
    text = ""
    if ocr.available():
        text = ocr.read(data)
        if text:
            parts.append(text)
            used.append(ocr.name)
    when = os.environ.get("COGNICORE_VISION_WHEN", "little_text").strip().lower()
    if vision.available() and (when == "always" or len(text) < LITTLE_TEXT):
        described = vision.read(data)
        if described:
            parts.append(("Image description:\n" if text else "") + described)
            used.append(vision.name)
    if not parts:
        raise ImageError(f"{name} has no text that could be read"
                         + ("" if vision.available() else "; describing images needs a vision model") + ".")
    return Reading("\n\n".join(parts), used)
