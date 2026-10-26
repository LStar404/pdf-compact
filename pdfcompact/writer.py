"""Small, careful PDF 1.7 writer.

The object graph comes from pypdf and is only *read* here; it is emitted through
this module so that we can

* re-deflate every stream at level 9,
* pack all non-stream objects into compressed /ObjStm containers,
* replace the classic cross-reference table with an /XRef stream,
* drop every object that is no longer reachable.

Object numbers are preserved: references inside the graph stay valid, and new
objects created by the caller use numbers above the original maximum.
"""

from __future__ import annotations

import struct
import zlib

from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    ByteStringObject,
    DictionaryObject,
    FloatObject,
    IndirectObject,
    NameObject,
    NullObject,
    NumberObject,
    StreamObject,
    TextStringObject,
)

__all__ = ["RawStream", "WriterStats", "reachable", "write_pdf"]

IMAGE_FILTERS = frozenset({"/DCTDecode", "/JPXDecode", "/CCITTFaxDecode", "/JBIG2Decode", "/RunLengthDecode"})
NAME_UNSAFE = frozenset("#%()<>[]{} ")


class RawStream:
    """A stream whose body is already encoded, with optional extra dictionary keys."""

    __slots__ = ("data", "filter", "extra")

    def __init__(self, data: bytes, filter: str | None = None, extra: dict | None = None):
        self.data = data
        self.filter = filter
        self.extra = extra or {}


class WriterStats(dict):
    pass


# --------------------------------------------------------------------- values

def _name_bytes(name) -> bytes:
    text = str(name)
    if text.startswith("/"):
        text = text[1:]
    out = bytearray(b"/")
    for char in text:
        code = ord(char)
        if code < 33 or code > 126 or char in NAME_UNSAFE:
            out += b"#" + ("%02X" % code).encode()
        else:
            out += char.encode("latin-1")
    return bytes(out)


def _string_bytes(content: bytes) -> bytes:
    if not content:
        return b"()"
    if all(32 <= byte <= 126 or byte in (9, 10, 13) for byte in content) and not any(
        byte in content for byte in (0x28, 0x29, 0x5C)
    ):
        return b"(" + content + b")"
    return b"<" + content.hex().upper().encode() + b">"


def _text_string_bytes(value: TextStringObject) -> bytes:
    raw = getattr(value, "original_bytes", None)
    if raw is None:
        try:
            raw = value.get_original_bytes()
        except Exception:
            raw = None
    if raw is not None:
        return _string_bytes(raw)
    try:
        return _string_bytes(str(value).encode("latin-1"))
    except UnicodeEncodeError:
        return _string_bytes(b"\xfe\xff" + str(value).encode("utf-16-be"))


def serialize(value, alive: set[int]) -> bytes:
    """Serialize one PDF value; ``alive`` guards against dangling references."""
    if isinstance(value, IndirectObject):
        if value.idnum not in alive:
            raise ValueError("dangling reference to object %d" % value.idnum)
        return b"%d %d R" % (value.idnum, value.generation)
    if isinstance(value, DictionaryObject):
        parts = [b"<<"]
        for key, item in dict.items(value):
            parts.append(_name_bytes(key))
            parts.append(serialize(item, alive))
        parts.append(b">>")
        return b" ".join(parts)
    if isinstance(value, (ArrayObject, list)):
        return b"[" + b" ".join(serialize(item, alive) for item in value) + b"]"
    if isinstance(value, NameObject):
        return _name_bytes(value)
    if isinstance(value, BooleanObject):
        return b"true" if value else b"false"
    if isinstance(value, NullObject):
        return b"null"
    if isinstance(value, NumberObject):
        return str(int(value)).encode()
    if isinstance(value, FloatObject):
        text = repr(float(value))
        return (text[:-2] if text.endswith(".0") else text).encode()
    if isinstance(value, TextStringObject):
        return _text_string_bytes(value)
    if isinstance(value, ByteStringObject):
        return _string_bytes(value.original_bytes)
    if isinstance(value, bytes):
        return _string_bytes(value)
    if isinstance(value, str):
        return _string_bytes(value.encode("latin-1", "replace"))
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, int):
        return str(value).encode()
    if isinstance(value, float):
        text = repr(value)
        return (text[:-2] if text.endswith(".0") else text).encode()
    raise TypeError("cannot serialize %r" % (value,))


# ---------------------------------------------------------------- reachability

def _walk(value, stack: list[int], seen: set[int]) -> None:
    if isinstance(value, IndirectObject):
        if value.idnum not in seen:
            stack.append(value.idnum)
    elif isinstance(value, RawStream):
        for item in value.extra.values():
            _walk(item, stack, seen)
    elif isinstance(value, DictionaryObject):
        for item in dict.values(value):
            _walk(item, stack, seen)
    elif isinstance(value, (ArrayObject, list)):
        for item in value:
            _walk(item, stack, seen)


def reachable(objects: dict[int, object], roots: list[int]) -> set[int]:
    seen: set[int] = set()
    stack = list(roots)
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        if oid not in objects:
            raise ValueError("missing object %d" % oid)
        seen.add(oid)
        _walk(objects[oid], stack, seen)
    return seen


# ------------------------------------------------------------------- streams

def _filters(stream) -> list[str]:
    value = dict.get(stream, "/Filter")
    if isinstance(value, IndirectObject):
        value = value.get_object()
    if value is None:
        return []
    if isinstance(value, (ArrayObject, list)):
        return [str(item) for item in value]
    return [str(value)]


def prepare_stream(stream, level: int = 9) -> tuple[DictionaryObject, bytes]:
    """Return (dictionary without /Length, encoded body)."""
    if isinstance(stream, RawStream):
        dictionary = DictionaryObject()
        for key, value in stream.extra.items():
            dictionary[NameObject(key)] = value
        if stream.filter:
            dictionary[NameObject("/Filter")] = NameObject(stream.filter)
        return dictionary, bytes(stream.data)

    filters = _filters(stream)
    dictionary = DictionaryObject()
    for key, value in dict.items(stream):
        if key in ("/Length", "/Filter", "/DecodeParms"):
            continue
        dictionary[key] = value

    if len(filters) == 1 and filters[0] in IMAGE_FILTERS:
        return dictionary, bytes(stream._data)

    decoded = stream.get_data()
    packed = zlib.compress(decoded, level)
    if len(packed) < len(decoded):
        dictionary[NameObject("/Filter")] = NameObject("/FlateDecode")
        return dictionary, packed
    return dictionary, decoded


# -------------------------------------------------------------------- writer

def write_pdf(path: str, objects: dict[int, object], root_oid: int, info_oid: int | None = None,
              level: int = 9, objstm_max_objects: int = 600,
              objstm_max_bytes: int = 3 * 1024 * 1024) -> WriterStats:
    """Write ``objects`` to ``path`` as a PDF 1.7 file with object streams."""
    roots = [root_oid] + ([info_oid] if info_oid is not None else [])
    alive = reachable(objects, roots)

    stream_ids = [oid for oid in sorted(alive) if isinstance(objects[oid], (StreamObject, RawStream))]
    plain_ids = [oid for oid in sorted(alive) if oid not in set(stream_ids)]

    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}

    def emit(oid: int, body: bytes) -> None:
        offsets[oid] = len(out)
        out.extend(b"%d 0 obj\n" % oid)
        out.extend(body)
        out.extend(b"\nendobj\n")

    for oid in stream_ids:
        dictionary, body = prepare_stream(objects[oid], level)
        dictionary[NameObject("/Length")] = NumberObject(len(body))
        emit(oid, serialize(dictionary, alive) + b"\nstream\n" + body + b"\nendstream")

    objstm_of: dict[int, tuple[int, int]] = {}
    next_oid = max(alive) + 1
    batches: list[list[tuple[int, bytes]]] = []
    batch: list[tuple[int, bytes]] = []
    batch_size = 0
    for oid in plain_ids:
        body = serialize(objects[oid], alive)
        batch.append((oid, body))
        batch_size += len(body) + 12
        if len(batch) >= objstm_max_objects or batch_size >= objstm_max_bytes:
            batches.append(batch)
            batch, batch_size = [], 0
    if batch:
        batches.append(batch)

    for chunk in batches:
        stream_number = next_oid
        next_oid += 1
        header_parts: list[bytes] = []
        body_parts: list[bytes] = []
        offset = 0
        for index, (oid, body) in enumerate(chunk):
            header_parts.append(b"%d %d" % (oid, offset))
            body_parts.append(body + b"\n")
            offset += len(body) + 1
            objstm_of[oid] = (stream_number, index)
        header = b" ".join(header_parts) + b"\n"
        payload = header + b"".join(body_parts)
        packed = zlib.compress(payload, level)
        dictionary = DictionaryObject()
        dictionary[NameObject("/Type")] = NameObject("/ObjStm")
        dictionary[NameObject("/N")] = NumberObject(len(chunk))
        dictionary[NameObject("/First")] = NumberObject(len(header))
        dictionary[NameObject("/Filter")] = NameObject("/FlateDecode")
        dictionary[NameObject("/Length")] = NumberObject(len(packed))
        emit(stream_number, serialize(dictionary, alive) + b"\nstream\n" + packed + b"\nendstream")

    xref_oid = next_oid
    size = xref_oid + 1
    free_ids = [oid for oid in range(1, size) if oid not in offsets and oid not in objstm_of]
    free_next = {oid: (free_ids[i + 1] if i + 1 < len(free_ids) else 0) for i, oid in enumerate(free_ids)}

    rows = bytearray(struct.pack(">BIH", 0, free_ids[0] if free_ids else 0, 65535))
    for oid in range(1, size):
        if oid in offsets:
            rows += struct.pack(">BIH", 1, offsets[oid], 0)
        elif oid in objstm_of:
            stream_number, index = objstm_of[oid]
            rows += struct.pack(">BIH", 2, stream_number, index)
        else:
            rows += struct.pack(">BIH", 0, free_next[oid], 0)

    packed_rows = zlib.compress(bytes(rows), level)
    xref_offset = len(out)
    dictionary = DictionaryObject()
    dictionary[NameObject("/Type")] = NameObject("/XRef")
    dictionary[NameObject("/Size")] = NumberObject(size)
    dictionary[NameObject("/W")] = ArrayObject([NumberObject(1), NumberObject(4), NumberObject(2)])
    dictionary[NameObject("/Index")] = ArrayObject([NumberObject(0), NumberObject(size)])
    dictionary[NameObject("/Root")] = IndirectObject(root_oid, 0, None)
    if info_oid is not None:
        dictionary[NameObject("/Info")] = IndirectObject(info_oid, 0, None)
    dictionary[NameObject("/Filter")] = NameObject("/FlateDecode")
    dictionary[NameObject("/Length")] = NumberObject(len(packed_rows))

    out.extend(b"%d 0 obj\n" % xref_oid)
    out.extend(serialize(dictionary, alive))
    out.extend(b"\nstream\n" + packed_rows + b"\nendstream\nendobj\n")
    out.extend(b"startxref\n%d\n%%%%EOF\n" % xref_offset)

    with open(path, "wb") as handle:
        handle.write(out)

    return WriterStats(objects=len(alive), streams=len(stream_ids), plain=len(plain_ids),
                       objstms=len(batches), size=len(out), free=len(free_ids))
