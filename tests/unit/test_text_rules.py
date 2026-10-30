"""Text detection, decoding, BOM handling and newline reporting."""

from __future__ import annotations

import codecs

import pytest

from filekeeper.core.errors import BinaryContentRefusedError, ValidationError
from filekeeper.core.safety.textdata import (
    decode_bytes,
    detect_newline,
    encode_text,
    looks_binary,
)


def test_nul_byte_marks_content_binary() -> None:
    assert looks_binary(b"abc\x00def") is True
    assert looks_binary(b"text") is False


def test_binary_content_is_refused() -> None:
    with pytest.raises(BinaryContentRefusedError):
        decode_bytes(b"\x89PNG\x00\x01")


def test_invalid_utf8_is_refused() -> None:
    with pytest.raises(BinaryContentRefusedError):
        decode_bytes(b"caf\xe9\n")


def test_explicit_encoding_is_honoured() -> None:
    text, profile = decode_bytes(b"caf\xe9\n", encoding="latin-1")
    assert text == "café\n"
    assert profile.encoding == "latin-1"


def test_unknown_encoding_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        decode_bytes(b"text", encoding="not-a-codec")


def test_utf8_bom_is_detected_stripped_and_restored() -> None:
    text, profile = decode_bytes(codecs.BOM_UTF8 + b"hello\n")
    assert text == "hello\n"
    assert profile.has_bom is True
    assert encode_text(text, encoding=profile.encoding, has_bom=True).startswith(codecs.BOM_UTF8)


def test_crlf_is_detected_and_never_normalised() -> None:
    text, profile = decode_bytes(b"one\r\ntwo\r\n")
    assert profile.newline == "\r\n"
    assert text == "one\r\ntwo\r\n"
    assert encode_text(text, encoding=profile.encoding, has_bom=False) == b"one\r\ntwo\r\n"


def test_newline_detection_prefers_the_dominant_convention() -> None:
    assert detect_newline("a\nb\nc\r\n") == "\n"
    assert detect_newline("a\r\nb\r\n") == "\r\n"
    assert detect_newline("a\rb\r") == "\r"
    assert detect_newline("no newline") == ""


def test_partial_decode_tolerates_a_truncated_multibyte_character() -> None:
    # "é" is two bytes; a bounded read can stop between them.
    truncated = b"h\xc3"

    with pytest.raises(BinaryContentRefusedError):
        decode_bytes(truncated)

    text, _ = decode_bytes(truncated, partial=True)
    assert text == "h"
