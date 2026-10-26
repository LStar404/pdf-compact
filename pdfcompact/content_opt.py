"""Tighten PDF content streams by rounding coordinates.

Vector-heavy PDFs (particularly ones where text was converted to outlines) store
millions of coordinates with five to seven decimals.  Rounding to 0.01 pt is
visually lossless and removes a surprising amount of entropy from the deflated
stream; rounding the large absolute page coordinates to 0.1 pt as well helps
further without being noticeable.

Everything that is not a number (strings, hex strings, names, comments) is copied
byte for byte, so the stream stays semantically identical.
"""

from __future__ import annotations

import re

__all__ = ["optimize_content", "is_safe_content"]

NUMBER = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")
DELIMITERS = frozenset(b"()<>[]{}/%")
WHITESPACE = frozenset(b"\x00\t\n\x0c\r ")
# Sequences that would mean binary inline-image data is present; rounding inside
# one of those would corrupt the page, so such streams are left untouched.
INLINE_IMAGE_MARKERS = (b" BI\n", b" BI ", b"\nBI\n", b"\r\nBI\r\n", b"\nBI ")


def is_safe_content(data: bytes) -> bool:
    return not any(marker in data for marker in INLINE_IMAGE_MARKERS)


def _format(value: float, decimals: int) -> bytes:
    text = ("%.*f" % (decimals, value)).rstrip("0").rstrip(".")
    if text in ("", "-", "-0"):
        text = "0"
    return text.encode("ascii")


def optimize_content(data: bytes, small_decimals: int = 2, large_decimals: int = 1,
                     large_threshold: float = 10.0) -> tuple[bytes, bool]:
    """Round real numbers inside a PDF content stream.

    Numbers with ``abs(value) >= large_threshold`` (absolute page coordinates)
    get ``large_decimals`` decimals, smaller ones (glyph outlines, colours) get
    ``small_decimals``.  Returns ``(stream, changed)``.
    """
    if not is_safe_content(data):
        return data, False

    out = bytearray()
    changed = False
    index = 0
    length = len(data)

    while index < length:
        byte = data[index]

        if byte == 0x28:  # '(' literal string
            cursor = index + 1
            depth = 1
            while cursor < length:
                char = data[cursor]
                if char == 0x5C:  # backslash escape
                    cursor += 2
                    continue
                if char == 0x28:
                    depth += 1
                elif char == 0x29:
                    depth -= 1
                    if depth == 0:
                        cursor += 1
                        break
                cursor += 1
            out += data[index:cursor]
            index = cursor
            continue

        if byte == 0x3C:  # '<' hex string or '<<' dictionary
            if data[index:index + 2] == b"<<":
                out += b"<<"
                index += 2
                continue
            end = data.find(b">", index + 1)
            end = length if end < 0 else end + 1
            out += data[index:end]
            index = end
            continue

        if byte == 0x2F:  # '/' name
            cursor = index + 1
            while cursor < length and data[cursor] not in DELIMITERS and data[cursor] not in WHITESPACE:
                cursor += 1
            out += data[index:cursor]
            index = cursor
            continue

        if byte == 0x25:  # '%' comment
            end = data.find(b"\n", index)
            end = length if end < 0 else end
            out += data[index:end]
            index = end
            continue

        if (0x30 <= byte <= 0x39) or (
            byte in (0x2B, 0x2D, 0x2E)
            and index + 1 < length
            and (0x30 <= data[index + 1] <= 0x39 or data[index + 1] == 0x2E)
        ):
            match = NUMBER.match(data, index)
            if match:
                token = match.group(0)
                index = match.end()
                if b"." not in token:
                    out += token
                    continue
                try:
                    value = float(token)
                except ValueError:
                    out += token
                    continue
                decimals = large_decimals if abs(value) >= large_threshold else small_decimals
                replacement = _format(value, decimals)
                if len(replacement) <= len(token):
                    changed = changed or replacement != token
                    out += replacement
                else:
                    out += token
                continue

        out.append(byte)
        index += 1

    return bytes(out), changed
