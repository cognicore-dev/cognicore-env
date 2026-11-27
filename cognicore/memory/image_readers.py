"""Reading images for document memory: OCR by default, a vision model when enabled.

``read_image(data, name)`` returns the text to store for an image:

ocr     Text in the image (screenshots, scans, photographed pages), read on this machine by
        Tesseract when it is installed (the hosted server image has it), else by RapidOCR
        (pip install cognicore-env[ocr], no system package needed). Free, offline, nothing
        leaves the machine.
vision  A description of what the image shows (diagrams, charts, photos), from a vision
        model. Off unless COGNICORE_VISION_PROVIDER is set; then the image is sent to that
        provider, which costs money per image. By default it runs only when OCR found little
        text (COGNICORE_VISION_WHEN=little_text), i.e. where OCR cannot help.

Both are behind one interface (``ImageReader``), so other OCR engines or vision providers
can be added without changing callers. An image with no readable text and no description
is refused rather than stored empty.

Settings (environment):
    COGNICORE_IMAGE_OCR          "0" turns OCR off (default on when an engine is installed)
    COGNICORE_OCR_ENGINE         auto (default: Tesseract, else RapidOCR) | tesseract | rapidocr
    COGNICORE_OCR_LANG           Tesseract language(s), e.g. eng or eng+deu (default eng)
    COGNICORE_VISION_PROVIDER    "anthropic" to describe images with Claude (default: off)
    COGNICORE_VISION_MODEL       model name (default claude-haiku-5-5)
    COGNICORE_VISION_WHEN        little_text (default) | always
    ANTHROPIC_API_KEY            for the anthropic provider
"""

import base64
import io
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

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


def _tesseract_path() -> Optional[str]:
    import shutil

    return shutil.which(os.environ.get("COGNICORE_TESSERACT", "tesseract"))


def _rapidocr_installed() -> bool:
    try:
        import rapidocr  # noqa: F401
    except ImportError:
        return False
    return True


def ocr_engine() -> Optional[str]:
    """The OCR engine to use: COGNICORE_OCR_ENGINE, else Tesseract when installed, else RapidOCR."""
    if os.environ.get("COGNICORE_IMAGE_OCR", "1").strip() == "0":
        return None
    wanted = os.environ.get("COGNICORE_OCR_ENGINE", "auto").strip().lower()
    if wanted in ("auto", "tesseract") and _tesseract_path():
        return "tesseract"
    if wanted in ("auto", "rapidocr") and _rapidocr_installed():
        return "rapidocr"
    return None


class OcrReader(ImageReader):
    """Text lines in reading order, from Tesseract (a system package, used on the hosted server)
    or RapidOCR (pip only: pip install cognicore-env[ocr]). RapidOCR loads once per process."""

    name = "ocr"
    _rapid = None
    TIMEOUT = 60

    def available(self) -> bool:
        return ocr_engine() is not None

    def read(self, data: bytes) -> str:
        engine = ocr_engine()
        if engine == "tesseract":
            return self._tesseract(data)
        if engine == "rapidocr":
            return self._rapidocr(data)
        return ""

    def _tesseract(self, data: bytes) -> str:
        import subprocess

        # stdin to stdout: no temporary files (the hosted server's filesystem is read-only)
        try:
            lang = os.environ.get("COGNICORE_OCR_LANG", "eng")
            done = subprocess.run([_tesseract_path(), "stdin", "stdout", "-l", lang],  # nosec B603 - fixed argv
                                  input=data, capture_output=True, timeout=self.TIMEOUT, check=False)
        except subprocess.TimeoutExpired as e:
            raise ImageError(f"Reading the image's text took longer than {self.TIMEOUT} seconds.") from e
        text = done.stdout.decode("utf-8", errors="replace")
        return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())

    def _rapidocr(self, data: bytes) -> str:
        if OcrReader._rapid is None:
            import logging

            from rapidocr import RapidOCR

            logging.getLogger("RapidOCR").setLevel(logging.WARNING)
            OcrReader._rapid = RapidOCR()
        result = OcrReader._rapid(data)
        return "\n".join(t.strip() for t in (result.txts or ()) if t and t.strip())


class AnthropicVisionReader(ImageReader):
    """Claude describes the image. Sends the image to Anthropic."""

    name = "vision"

    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(self, client=None):
        # *client*: anything with messages.create(**request) (the anthropic SDK, or a test fake).
        # Without one, the Messages API is called directly with httpx: no SDK to install.
        self._client = client

    def available(self) -> bool:
        if os.environ.get("COGNICORE_VISION_PROVIDER", "").strip().lower() != "anthropic":
            return False
        return self._client is not None or bool(os.environ.get("ANTHROPIC_API_KEY"))

    def read(self, data: bytes) -> str:
        kind = media_type(data) or "image/png"
        if kind not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            converted = _as_png(data)
            if converted is None:
                return ""  # BMP or TIFF without Pillow: OCR still reads it; no description
            data, kind = converted, "image/png"
        request = {
            "model": os.environ.get("COGNICORE_VISION_MODEL", DEFAULT_VISION_MODEL),
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": kind,
                                             "data": base64.b64encode(data).decode("ascii")}},
                {"type": "text", "text": VISION_PROMPT},
            ]}],
        }
        if self._client is not None:
            message = self._client.messages.create(**request)
            blocks = [{"type": getattr(b, "type", ""), "text": getattr(b, "text", "")} for b in message.content]
        else:
            import httpx

            resp = httpx.post(self.API_URL, json=request, timeout=60.0, headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": self.API_VERSION})
            if resp.status_code != 200:
                raise ImageError(f"The vision model could not describe the image (HTTP {resp.status_code}).")
            blocks = resp.json().get("content") or []
        return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def _as_png(data: bytes) -> Optional[bytes]:
    """PNG bytes for a BMP or TIFF (for the vision API), or None when Pillow is not installed."""
    try:
        from PIL import Image
    except ImportError:
        return None
    out = io.BytesIO()
    with Image.open(io.BytesIO(data)) as img:
        img.convert("RGB").save(out, "PNG")
    return out.getvalue()


def _u16(b: bytes, big: bool) -> int:
    return int.from_bytes(b[:2], "big" if big else "little")


def _u32(b: bytes, big: bool) -> int:
    return int.from_bytes(b[:4], "big" if big else "little")


def image_size(data: bytes) -> Optional[Tuple[int, int]]:
    """(width, height) read from the file header, without decoding the image, or None.

    Covers PNG, GIF, BMP, WebP, JPEG and TIFF, the formats the readers accept.
    """
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
            return _u32(data[16:], True), _u32(data[20:], True)
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return _u16(data[6:], False), _u16(data[8:], False)
        if data.startswith(b"BM"):
            return abs(int.from_bytes(data[18:22], "little", signed=True)), \
                abs(int.from_bytes(data[22:26], "little", signed=True))
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8 ":
                return _u16(data[26:], False) & 0x3FFF, _u16(data[28:], False) & 0x3FFF
            if chunk == b"VP8L":
                bits = _u32(data[21:], False)
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
            return None
        if data.startswith(b"\xff\xd8"):
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # markers without a length
                    i += 2
                    continue
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # start of frame
                    return _u16(data[i + 7:], True), _u16(data[i + 5:], True)
                i += 2 + _u16(data[i + 2:], True)
            return None
        if data[:4] in (b"II*\x00", b"MM\x00*"):
            big = data[:2] == b"MM"
            ifd = _u32(data[4:], big)
            width = height = None
            for n in range(_u16(data[ifd:], big)):
                entry = data[ifd + 2 + 12 * n: ifd + 14 + 12 * n]
                tag, kind = _u16(entry, big), _u16(entry[2:], big)
                value = _u16(entry[8:], big) if kind == 3 else _u32(entry[8:], big)
                if tag == 256:
                    width = value
                elif tag == 257:
                    height = value
            return (width, height) if width and height else None
    except (IndexError, ValueError):
        return None
    return None


def check_size(data: bytes, name: str) -> None:
    """Refuse images too large to decode safely, before any reader decodes them."""
    size = image_size(data)
    if size is None:
        raise ImageError(f"{name} could not be read as an image.")
    w, h = size
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
