"""Text detection, decoding and encoding rules.

Rules this module enforces, and that the documentation repeats:

* A file containing a NUL byte in its first 8 KiB is treated as binary. Text
  operations refuse it instead of returning mojibake.
* Decoding is strict. Undecodable bytes are an error unless the caller names an
  encoding explicitly.
* A UTF-8 byte-order mark is preserved on rewrite.
* Line endings are *never* normalised. Content is decoded and re-encoded as-is;
  the detected convention is reported, not applied.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from typing import Final

from ..errors import BinaryContentRefusedError, ValidationError

BINARY_SAMPLE_SIZE: Final = 8192
_BOMS: Final[tuple[tuple[bytes, str], ...]] = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
)


@dataclass(frozen=True, slots=True)
class TextProfile:
    """How a file's bytes decode, and what its lines look like."""

    encoding: str
    newline: str
    has_bom: bool


def looks_binary(data: bytes) -> bool:
    """True when the sample contains NUL, the cheapest reliable heuristic."""

    return b"\x00" in data[:BINARY_SAMPLE_SIZE]


def detect_bom(data: bytes) -> tuple[str, bool] | None:
    """Return ``(encoding, has_bom)`` when the data starts with a known BOM."""

    # Longest first: UTF-32 LE shares its first two bytes with UTF-16 LE.
    for bom, encoding in sorted(_BOMS, key=lambda item: len(item[0]), reverse=True):
        if data.startswith(bom):
            return encoding, True
    return None


def detect_newline(text: str) -> str:
    """Dominant newline convention: ``\\r\\n``, ``\\n``, ``\\r``, or ``""``."""

    crlf = text.count("\r\n")
    lone_cr = text.count("\r") - crlf
    lone_lf = text.count("\n") - crlf
    counts = {"\r\n": crlf, "\n": lone_lf, "\r": lone_cr}
    best = max(counts, key=lambda key: counts[key])
    return best if counts[best] > 0 else ""


def decode_bytes(
    data: bytes,
    *,
    encoding: str | None = None,
    partial: bool = False,
    path: str = "",
) -> tuple[str, TextProfile]:
    """Decode bytes as text, or refuse them with BINARY_CONTENT_REFUSED.

    ``partial`` tolerates a truncated trailing multi-byte sequence, which is what
    a bounded read produces.
    """

    if encoding is None:
        if looks_binary(data):
            raise BinaryContentRefusedError(
                "binary content cannot be read as text",
                details={"path": path, "reason": "nul_byte"},
            )
        bom = detect_bom(data)
        if bom is not None:
            detected, has_bom = bom
            encoding = detected
        else:
            detected, has_bom = "utf-8", False
            encoding = detected
    else:
        has_bom = encoding == "utf-8" and data.startswith(codecs.BOM_UTF8)

    try:
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        text = decoder.decode(data, final=not partial)
    except LookupError as exc:
        raise ValidationError(
            f"unknown text encoding: {encoding}", details={"encoding": encoding}
        ) from exc
    except UnicodeDecodeError as exc:
        raise BinaryContentRefusedError(
            f"content is not valid {encoding}; pass an explicit encoding to force a read",
            details={"path": path, "encoding": encoding, "byte_offset": exc.start},
        ) from exc

    if encoding == "utf-8" and text.startswith("\ufeff"):
        text = text[1:]
        has_bom = True
    elif encoding in {"utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"} and text.startswith(
        "\ufeff"
    ):
        text = text[1:]

    return text, TextProfile(encoding=encoding, newline=detect_newline(text), has_bom=has_bom)


def encode_text(text: str, *, encoding: str, has_bom: bool) -> bytes:
    """Encode text back with the same convention it was read with."""

    try:
        payload = text.encode(encoding)
    except (LookupError, UnicodeEncodeError) as exc:
        raise ValidationError(
            f"content cannot be encoded as {encoding}: {exc}",
            details={"encoding": encoding},
        ) from exc
    if has_bom and encoding == "utf-8":
        return codecs.BOM_UTF8 + payload
    return payload
