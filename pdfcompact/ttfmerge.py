"""Merge TrueType subset fonts that share an identical glyph-ID space.

Why this exists
---------------
Some PDF producers embed a *separate* font program for every page.  Each program
is a subset of the same original font: the ``loca`` table keeps the original
glyph-ID space (e.g. 22024 slots) while ``glyf`` only carries the outlines used
on that page, and every other table (head/hhea/maxp/hmtx/OS-2/prep) is
byte-identical between subsets.

Because the glyph IDs are preserved, the union of all subsets of one family is a
valid superset font whose glyph IDs are exactly the IDs the pages already
reference.  When the CID->GID map is /Identity, every page can share that single
merged program, which removes dozens of megabytes of duplicated tables.

This module is PDF-agnostic: it only works on byte strings.
"""

from __future__ import annotations

import hashlib
import struct

__all__ = ["FontError", "font_info", "merge_group", "build_ttf", "verify_merged"]

REQUIRED_TABLES = ("head", "hhea", "maxp", "hmtx", "loca", "glyf")
# Tables that must be byte-identical for two subsets to be mergeable.
SIGNATURE_TABLES = ("head", "hhea", "hmtx", "maxp", "OS/2", "prep", "fpgm", "cvt ", "cmap", "gasp", "post", "name")


class FontError(Exception):
    """Raised when a font cannot be parsed or two subsets do not match."""


def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from(">H", buf, off)[0]


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from(">I", buf, off)[0]


def parse_tables(font: bytes) -> dict[str, tuple[int, int]]:
    """Return {tag: (offset, length)} from a TrueType/OpenType file."""
    if len(font) < 12:
        raise FontError("font too short")
    if font[:4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
        raise FontError("unsupported sfnt version %r" % font[:4])
    count = _u16(font, 4)
    if not 4 <= count <= 64:
        raise FontError("implausible table count %d" % count)
    tables: dict[str, tuple[int, int]] = {}
    for index in range(count):
        entry = 12 + 16 * index
        if entry + 16 > len(font):
            raise FontError("truncated table directory")
        tag, _checksum, offset, length = struct.unpack_from(">4sIII", font, entry)
        tag = tag.decode("latin-1")
        if offset + length > len(font):
            raise FontError("table %s outside font" % tag)
        tables[tag] = (offset, length)
    return tables


def num_glyphs(font: bytes, tables: dict[str, tuple[int, int]]) -> int:
    if "maxp" not in tables:
        raise FontError("no maxp table")
    return _u16(font, tables["maxp"][0] + 4)


def read_loca(font: bytes, tables: dict[str, tuple[int, int]], glyphs: int) -> tuple[list[int], int]:
    """Return (offsets, index_to_loc_format)."""
    if "loca" not in tables or "head" not in tables:
        raise FontError("no loca/head table")
    offset, length = tables["loca"]
    fmt = struct.unpack_from(">h", font, tables["head"][0] + 50)[0]
    if fmt == 0:
        if length < 2 * (glyphs + 1):
            raise FontError("short loca is truncated")
        return [v * 2 for v in struct.unpack_from(">%dH" % (glyphs + 1), font, offset)], 0
    if fmt == 1:
        if length < 4 * (glyphs + 1):
            raise FontError("long loca is truncated")
        return list(struct.unpack_from(">%dI" % (glyphs + 1), font, offset)), 1
    raise FontError("bad indexToLocFormat %d" % fmt)


def _normalised_head(head: bytes) -> bytes:
    """Zero the fields a subsetter may rewrite without changing the font."""
    buf = bytearray(head)
    if len(buf) >= 28:
        buf[8:12] = b"\0\0\0\0"   # checkSumAdjustment
        buf[12:20] = b"\0" * 8    # created
        buf[20:28] = b"\0" * 8    # modified
    return bytes(buf)


def font_info(font: bytes) -> dict | None:
    """Describe a font, or return None when it is not a usable TrueType program."""
    try:
        tables = parse_tables(font)
        if any(tag not in tables for tag in REQUIRED_TABLES):
            return None
        glyphs = num_glyphs(font, tables)
        loca, fmt = read_loca(font, tables, glyphs)
        glyf_length = tables["glyf"][1]
        previous = 0
        for value in loca:
            if value < previous or value > glyf_length:
                return None
            previous = value

        parts = [str(glyphs), str(fmt)]
        for tag in SIGNATURE_TABLES:
            if tag in tables:
                off, length = tables[tag]
                data = font[off:off + length]
                if tag == "head":
                    data = _normalised_head(data)
                parts.append(tag + hashlib.md5(data).hexdigest())
            else:
                parts.append(tag + "-")
        return {
            "tables": tables,
            "num_glyphs": glyphs,
            "loca": loca,
            "loc_format": fmt,
            "signature": "|".join(parts),
            "blob": font,
        }
    except (FontError, struct.error):
        return None


def _same_glyph(a: bytes, b: bytes) -> bool:
    """Compare glyph programs, ignoring the trailing padding a subsetter adds."""
    if a == b:
        return True
    return abs(len(a) - len(b)) <= 3 and a.rstrip(b"\0") == b.rstrip(b"\0")


def merge_group(infos: list[dict]) -> tuple[bytes, dict]:
    """Union the ``glyf`` tables of several subsets of the same font.

    Returns ``(merged_font_bytes, statistics)``; raises :class:`FontError` when
    the subsets disagree about a glyph, which would mean they do not share a
    glyph-ID space and must not be merged.
    """
    if not infos:
        raise FontError("empty group")
    base = infos[0]
    glyphs = base["num_glyphs"]
    tables = base["tables"]
    union: dict[int, bytes] = {}
    conflicts = 0

    for info in infos:
        if info["num_glyphs"] != glyphs or info["signature"] != base["signature"]:
            raise FontError("incompatible fonts in group")
        blob = info["blob"]
        glyf_off, glyf_len = info["tables"]["glyf"]
        glyf = blob[glyf_off:glyf_off + glyf_len]
        loca = info["loca"]
        for gid in range(glyphs):
            start, end = loca[gid], loca[gid + 1]
            if end <= start:
                continue
            piece = glyf[start:end]
            known = union.get(gid)
            if known is None:
                union[gid] = piece
            elif not _same_glyph(known, piece):
                conflicts += 1
            elif len(piece) > len(known):
                union[gid] = piece
        if conflicts:
            raise FontError("glyph conflict in %d slots" % conflicts)

    glyf_out = bytearray()
    offsets = [0] * (glyphs + 1)
    for gid in range(glyphs):
        offsets[gid] = len(glyf_out)
        piece = union.get(gid)
        if piece:
            glyf_out += piece
            glyf_out += b"\0" * (len(glyf_out) % 4 and 4 - len(glyf_out) % 4)
    offsets[glyphs] = len(glyf_out)

    merged = build_ttf(base["blob"], tables, bytes(glyf_out), struct.pack(">%dI" % (glyphs + 1), *offsets))
    return merged, {
        "num_glyphs": glyphs,
        "glyphs_present": len(union),
        "glyf_bytes": len(glyf_out),
        "font_bytes": len(merged),
    }


def _checksum(data: bytes) -> int:
    if len(data) % 4:
        data += b"\0" * (4 - len(data) % 4)
    total = 0
    for (word,) in struct.iter_unpack(">I", data):
        total = (total + word) & 0xFFFFFFFF
    return total


def build_ttf(base_font: bytes, tables: dict[str, tuple[int, int]], glyf: bytes, loca: bytes) -> bytes:
    """Assemble a TrueType file from ``base_font`` tables plus new glyf/loca."""
    present: dict[str, bytearray] = {}
    for tag, (offset, length) in tables.items():
        if tag in ("glyf", "loca"):
            continue
        present[tag] = bytearray(base_font[offset:offset + length])
    if "head" not in present or len(present["head"]) < 54:
        raise FontError("missing or truncated head table")

    head = present["head"]
    struct.pack_into(">I", head, 8, 0)    # checkSumAdjustment, patched at the end
    struct.pack_into(">h", head, 50, 1)   # indexToLocFormat = long offsets
    present["glyf"] = bytearray(glyf)
    present["loca"] = bytearray(loca)

    tags = sorted(present)
    count = len(tags)
    entry_selector = 0
    while (1 << (entry_selector + 1)) <= count:
        entry_selector += 1
    search_range = (1 << entry_selector) * 16

    out = bytearray(struct.pack(">IHHHH", 0x00010000, count, search_range,
                                entry_selector, count * 16 - search_range))
    directory: list[tuple[str, int, int, int]] = []
    offset = 12 + 16 * count
    for tag in tags:
        data = bytes(present[tag])
        directory.append((tag, offset, len(data), _checksum(data)))
        offset += len(data) + (-len(data)) % 4

    for tag, toffset, length, checksum in directory:
        out += struct.pack(">4sIII", tag.encode("latin-1"), checksum, toffset, length)
    for tag, toffset, length, _checksum_value in directory:
        if len(out) != toffset:
            raise FontError("internal table offset mismatch")
        out += present[tag]
        out += b"\0" * ((-len(out)) % 4)

    adjustment = (0xB1B0AFBA - _checksum(bytes(out))) & 0xFFFFFFFF
    head_offset = next(entry[1] for entry in directory if entry[0] == "head")
    struct.pack_into(">I", out, head_offset + 8, adjustment)
    return bytes(out)


def verify_merged(merged: bytes, infos: list[dict]) -> None:
    """Round-trip check: every source glyph must be present at the same GID."""
    tables = parse_tables(merged)
    glyphs = num_glyphs(merged, tables)
    loca, _fmt = read_loca(merged, tables, glyphs)
    glyf_off, glyf_len = tables["glyf"]
    glyf = merged[glyf_off:glyf_off + glyf_len]

    for info in infos:
        src_tables = info["tables"]
        src_off, src_len = src_tables["glyf"]
        src_glyf = info["blob"][src_off:src_off + src_len]
        src_loca = info["loca"]
        for gid in range(glyphs):
            start, end = src_loca[gid], src_loca[gid + 1]
            if end <= start:
                continue
            start2, end2 = loca[gid], loca[gid + 1]
            if end2 <= start2 or glyf[start2:end2].rstrip(b"\0") != src_glyf[start:end].rstrip(b"\0"):
                raise FontError("glyph %d did not survive the merge" % gid)
