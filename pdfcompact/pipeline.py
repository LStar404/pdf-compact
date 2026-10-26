"""Compression pipeline: find and remove structural redundancy in a PDF.

The steps are independent and each one is safe to skip:

1. **dedupe streams** - producers often emit thousands of identical /ToUnicode
   CMaps, ICC profiles or CFF programs; one copy is enough.
2. **merge TrueType programs** - per-page font subsets of the same family share a
   glyph-ID space, so they can be unioned into a single program (see
   :mod:`pdfcompact.ttfmerge`).
3. **drop page-level XMP** - metadata blobs are large and irrelevant to rendering.
4. **round content-stream coordinates** - see :mod:`pdfcompact.content_opt`.
5. **re-encode images conservatively** - see :mod:`pdfcompact.images`.

Finally :mod:`pdfcompact.writer` repacks everything with object streams.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import zlib

from pypdf import PdfReader
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NumberObject,
    StreamObject,
)

from . import content_opt, images, ttfmerge
from .writer import RawStream, write_pdf

__all__ = ["Options", "compress"]

SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")


class Options:
    def __init__(self, quality: int = 75, max_dim: int = 1200, small_decimals: int = 2,
                 large_decimals: int = 1, large_threshold: float = 10.0,
                 merge_fonts: bool = True, reencode_images: bool = True,
                 keep_metadata: bool = False, verbose: bool = True):
        self.quality = quality
        self.max_dim = max_dim
        self.small_decimals = small_decimals
        self.large_decimals = large_decimals
        self.large_threshold = large_threshold
        self.merge_fonts = merge_fonts
        self.reencode_images = reencode_images
        self.keep_metadata = keep_metadata
        self.verbose = verbose


class Reporter:
    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self.start = time.time()

    def __call__(self, message: str) -> None:
        if self.verbose:
            print("[%6.1fs] %s" % (time.time() - self.start, message), flush=True)


# ------------------------------------------------------------------- helpers

def _raw_get(dictionary, key, default=None):
    try:
        return dict.get(dictionary, key, default)
    except Exception:
        return default


def _resolve(value):
    if isinstance(value, IndirectObject):
        if getattr(value, "pdf", None) is None:
            return None
        try:
            return value.get_object()
        except Exception:
            return None
    return value


def _as_int(value, default=0):
    value = _resolve(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _filters(stream) -> list[str]:
    value = _raw_get(stream, "/Filter")
    if isinstance(value, IndirectObject):
        value = _resolve(value)
    if value is None:
        return []
    if isinstance(value, (ArrayObject, list)):
        return [str(item) for item in value]
    return [str(value)]


def _stream_bytes(stream) -> bytes:
    if isinstance(stream, RawStream):
        return stream.data
    try:
        return stream.get_data()
    except Exception:
        return bytes(stream._data)


def _walk(objects: dict[int, object], callback) -> None:
    """Run ``callback(container)`` for every dictionary/array in the graph."""
    seen: set[int] = set()
    stack = [(None, obj) for obj in objects.values()]
    while stack:
        key, value = stack.pop()
        if isinstance(value, (DictionaryObject, ArrayObject)):
            if id(value) in seen:
                continue
            seen.add(id(value))
            callback(value)
            children = dict.values(value) if isinstance(value, DictionaryObject) else value
            for child in list(children):
                if isinstance(child, (DictionaryObject, ArrayObject)):
                    stack.append((None, child))


def _rewrite_refs(objects: dict[int, object], mapping: dict[int, int]) -> int:
    """Point every reference at its canonical object (mapping: old -> new)."""
    if not mapping:
        return 0
    count = 0
    seen: set[int] = set()

    def fix(container):
        nonlocal count
        if id(container) in seen:
            return
        seen.add(id(container))
        if isinstance(container, DictionaryObject):
            for key in list(dict.keys(container)):
                value = dict.get(container, key)
                if isinstance(value, IndirectObject):
                    target = mapping.get(value.idnum)
                    if target is not None:
                        container[key] = IndirectObject(target, value.generation, value.pdf)
                        count += 1
                elif isinstance(value, (DictionaryObject, ArrayObject)):
                    fix(value)
        else:
            for index, value in enumerate(list(container)):
                if isinstance(value, IndirectObject):
                    target = mapping.get(value.idnum)
                    if target is not None:
                        container[index] = IndirectObject(target, value.generation, value.pdf)
                        count += 1
                elif isinstance(value, (DictionaryObject, ArrayObject)):
                    fix(value)

    for value in objects.values():
        if isinstance(value, (DictionaryObject, ArrayObject)):
            fix(value)
    return count


def _colorspace(value, objects) -> str | tuple:
    if isinstance(value, IndirectObject):
        value = _resolve(value)
    if isinstance(value, NameObject) or (isinstance(value, str) and value.startswith("/")):
        return str(value)
    if isinstance(value, (ArrayObject, list)) and len(value) >= 2:
        family = str(value[0])
        if family == "/ICCBased":
            profile = _resolve(value[1])
            components = 3
            if isinstance(profile, DictionaryObject):
                components = _as_int(_raw_get(profile, "/N", 3), 3)
            return ("/ICCBased", components)
        if family == "/Separation":
            return ("/Separation", _colorspace(value[2], objects) if len(value) > 2 else "")
        if family in ("/DeviceN", "/Indexed", "/Lab"):
            return (family,)
        if family in ("/CalRGB", "/CalGray"):
            return (family, 3 if family == "/CalRGB" else 1)
    return "?"


# ------------------------------------------------------------------- pipeline

def _load(path: str, report: Reporter):
    reader = PdfReader(path)
    report("parsed %d pages" % len(reader.pages))
    size = int(reader.trailer["/Size"])
    objects: dict[int, object] = {}
    for oid in range(1, size):
        try:
            objects[oid] = reader.get_object(IndirectObject(oid, 0, reader))
        except Exception:
            continue
    root_oid = reader.trailer.raw_get("/Root").idnum
    info_oid = reader.trailer.raw_get("/Info").idnum if "/Info" in reader.trailer else None
    report("loaded %d objects" % len(objects))
    return objects, root_oid, info_oid


def _content_ids(objects: dict[int, object]) -> set[int]:
    ids: set[int] = set()
    for obj in objects.values():
        if not isinstance(obj, DictionaryObject) or str(_raw_get(obj, "/Type")) != "/Page":
            continue
        contents = _raw_get(obj, "/Contents")
        if isinstance(contents, IndirectObject):
            ids.add(contents.idnum)
        else:
            contents = _resolve(contents)
            if isinstance(contents, (ArrayObject, list)):
                ids.update(item.idnum for item in contents if isinstance(item, IndirectObject))
    return ids


def _dedupe(objects: dict[int, object], report: Reporter) -> dict[int, int]:
    canonical: dict[tuple, int] = {}
    mapping: dict[int, int] = {}
    for oid in sorted(objects):
        obj = objects[oid]
        if not isinstance(obj, StreamObject):
            continue
        payload = _stream_bytes(obj)
        extras = []
        for key, value in sorted(dict.items(obj), key=lambda item: str(item[0])):
            if key in ("/Length", "/Filter", "/DecodeParms", "/Length1", "/Length2", "/Length3"):
                continue
            extras.append((str(key), "ref:%d" % value.idnum if isinstance(value, IndirectObject) else str(value)))
        key = (hashlib.md5(payload).digest(), tuple(_filters(obj)), tuple(extras))
        if key in canonical:
            mapping[oid] = canonical[key]
        else:
            canonical[key] = oid
    for duplicate in mapping:
        objects.pop(duplicate, None)
    report("dedupe: %d duplicate stream objects dropped" % len(mapping))
    return mapping


def _font_groups(objects: dict[int, object]):
    family_of: dict[int, str] = {}
    for obj in objects.values():
        if not isinstance(obj, DictionaryObject):
            continue
        program = _raw_get(obj, "/FontFile2")
        if not isinstance(program, IndirectObject):
            continue
        name = _raw_get(obj, "/FontName")
        family = SUBSET_PREFIX.sub("", str(_resolve(name))) if name is not None else "?"
        family_of[program.idnum] = family
    for obj in objects.values():
        if not isinstance(obj, DictionaryObject):
            continue
        base = _raw_get(obj, "/BaseFont")
        descriptor = _raw_get(obj, "/FontDescriptor")
        if base is None or not isinstance(descriptor, IndirectObject):
            continue
        resolved = _resolve(descriptor)
        if not isinstance(resolved, DictionaryObject):
            continue
        program = _raw_get(resolved, "/FontFile2")
        if isinstance(program, IndirectObject):
            family_of[program.idnum] = SUBSET_PREFIX.sub("", str(_resolve(base)))

    groups: dict[tuple[str, str], list[int]] = {}
    infos: dict[int, dict] = {}
    for oid, family in family_of.items():
        obj = objects.get(oid)
        if not isinstance(obj, StreamObject):
            continue
        info = ttfmerge.font_info(_stream_bytes(obj))
        if info is None:
            continue
        infos[oid] = info
        groups.setdefault((family, info["signature"]), []).append(oid)
    return groups, infos


def _merge_fonts(objects: dict[int, object], groups, infos, report: Reporter) -> dict[int, int]:
    mapping: dict[int, int] = {}
    before = 0
    after = 0
    merged_families = 0
    for (family, _signature), oids in sorted(groups.items(), key=lambda item: -len(item[1])):
        if len(oids) < 2:
            continue
        before += sum(len(objects[oid]._data) for oid in oids if isinstance(objects.get(oid), StreamObject))
        try:
            merged, stats = ttfmerge.merge_group([infos[oid] for oid in oids])
            ttfmerge.verify_merged(merged, [infos[oid] for oid in oids])
        except Exception as error:  # keep the originals when anything is off
            report("  skipping font family %s (%d programs): %s" % (family, len(oids), error))
            continue
        keep = oids[0]
        extra = {str(key): value for key, value in dict.items(objects[keep])
                 if key not in ("/Length", "/Filter", "/DecodeParms")}
        extra["/Length1"] = NumberObject(len(merged))
        objects[keep] = RawStream(zlib.compress(merged, 9), "/FlateDecode", extra)
        for other in oids[1:]:
            objects.pop(other, None)
            mapping[other] = keep
        merged_families += 1
        after += len(zlib.compress(merged, 6))
        report("  merged %-28s %4d programs -> 1 (%d glyphs, %.0f KB)"
               % (family, len(oids), stats["glyphs_present"], stats["font_bytes"] / 1024))
    if merged_families:
        report("fonts: %d families, %.1f MB -> %.1f MB" % (merged_families, before / 1048576, after / 1048576))
    return mapping


def _strip_metadata(objects: dict[int, object], report: Reporter) -> None:
    removed = 0
    freed = 0
    for obj in list(objects.values()):
        if not isinstance(obj, DictionaryObject):
            continue
        metadata = _raw_get(obj, "/Metadata")
        if metadata is None:
            continue
        if isinstance(metadata, IndirectObject):
            stream = objects.pop(metadata.idnum, None)
            if isinstance(stream, StreamObject):
                freed += len(stream._data)
        dict.__delitem__(obj, "/Metadata")
        removed += 1
    report("metadata: %d XMP blobs removed (%.2f MB)" % (removed, freed / 1048576))


def _optimize_content(objects: dict[int, object], ids: set[int], options: Options, report: Reporter) -> None:
    before = 0
    after = 0
    for oid in sorted(ids):
        obj = objects.get(oid)
        if obj is None:
            continue
        data = _stream_bytes(obj)
        before += len(data)
        rounded, changed = content_opt.optimize_content(
            data, options.small_decimals, options.large_decimals, options.large_threshold
        )
        packed = zlib.compress(rounded, 9)
        after += len(packed)
        if changed:
            objects[oid] = RawStream(packed, "/FlateDecode")
    report("content: %.2f MB -> %.2f MB (%d streams)"
           % (before / 1048576, after / 1048576, len(ids)))


def _optimize_images(objects: dict[int, object], options: Options, report: Reporter) -> None:
    reencoded = 0
    kept = 0
    skipped = 0
    before = 0
    after = 0

    for oid in sorted(objects):
        obj = objects.get(oid)
        if not isinstance(obj, StreamObject) or str(_raw_get(obj, "/Subtype")) != "/Image":
            continue
        if any(key in obj for key in ("/ImageMask", "/SMask", "/Mask")):
            skipped += 1
            continue
        width = _as_int(_raw_get(obj, "/Width"))
        height = _as_int(_raw_get(obj, "/Height"))
        bits = _as_int(_raw_get(obj, "/BitsPerComponent", 8), 8)
        if width * height < 400:
            skipped += 1
            continue
        colorspace = _colorspace(_raw_get(obj, "/ColorSpace"), objects)
        if colorspace in ("/Indexed",) or (isinstance(colorspace, tuple) and colorspace[0] in ("/Indexed", "/DeviceN", "/Lab")):
            skipped += 1
            continue

        filters = _filters(obj)
        raw = bytes(obj._data)
        decoded = None
        if not any(name in ("/DCTDecode", "/JPXDecode") for name in filters):
            decoded = _stream_bytes(obj)
        image = images.decode_image(raw, filters, colorspace, width, height, bits, decoded)
        if image is None:
            skipped += 1
            continue

        before += len(raw)
        spot = isinstance(colorspace, tuple) and colorspace[0] == "/Separation"
        candidate = None  # (data, filter, colorspace_value, width, height)
        try:
            if spot:
                if image.mode in ("L", "1"):
                    data, _ = images.encode_png(image)
                    if len(data) < 0.9 * len(raw):
                        candidate = (data, "/FlateDecode", _raw_get(obj, "/ColorSpace"), image.size[0], image.size[1])
            else:
                data, colorspace_name = images.encode_jpeg(image, options.max_dim, options.quality)
                new_size = images.scaled_size(image.size, options.max_dim)
                if len(data) < 0.8 * len(raw):
                    candidate = (data, "/DCTDecode", NameObject(colorspace_name), new_size[0], new_size[1])
                elif image.mode in ("L", "1"):
                    data, _ = images.encode_png(image)
                    if len(data) < 0.9 * len(raw):
                        candidate = (data, "/FlateDecode", NameObject("/DeviceGray"), image.size[0], image.size[1])
        except Exception:
            candidate = None

        if candidate is None:
            after += len(raw)
            kept += 1
            continue

        data, filter_name, colorspace_value, out_width, out_height = candidate
        extra = {str(key): value for key, value in dict.items(obj)
                 if key not in ("/Length", "/Filter", "/DecodeParms", "/Decode", "/ColorSpace",
                                "/Width", "/Height", "/BitsPerComponent", "/SMask", "/Mask")}
        extra["/Width"] = NumberObject(out_width)
        extra["/Height"] = NumberObject(out_height)
        extra["/BitsPerComponent"] = NumberObject(8)
        extra["/ColorSpace"] = colorspace_value
        objects[oid] = RawStream(data, filter_name, extra)
        after += len(data)
        reencoded += 1

    report("images: %d re-encoded, %d left alone, %d skipped (%.2f MB -> %.2f MB)"
           % (reencoded, kept, skipped, before / 1048576, after / 1048576))


def compress(source: str, target: str, options: Options | None = None) -> dict:
    """Compress ``source`` into ``target`` and return a report dictionary."""
    options = options or Options()
    report = Reporter(options.verbose)
    started = time.time()

    objects, root_oid, info_oid = _load(source, report)
    content_ids = _content_ids(objects)

    dedupe_map = _dedupe(objects, report)
    _rewrite_refs(objects, dedupe_map)
    content_ids = _content_ids(objects)

    groups, infos = _font_groups(objects)
    report("font programs: %d in %d families" % (sum(len(v) for v in groups.values()), len(groups)))
    if options.merge_fonts:
        font_map = _merge_fonts(objects, groups, infos, report)
        _rewrite_refs(objects, font_map)
        content_ids = _content_ids(objects)

    if not options.keep_metadata:
        _strip_metadata(objects, report)

    _optimize_content(objects, content_ids, options, report)

    if options.reencode_images:
        _optimize_images(objects, options, report)

    report("writing %s" % target)
    stats = write_pdf(target, objects, root_oid, info_oid)
    source_size = os.path.getsize(source)
    target_size = os.path.getsize(target)
    report("wrote %.2f MB (%.1f%% of %.2f MB), %d objects, %d object streams"
           % (target_size / 1048576, 100.0 * target_size / source_size,
              source_size / 1048576, stats["objects"], stats["objstms"]))

    return {
        "source": source,
        "target": target,
        "source_size": source_size,
        "target_size": target_size,
        "ratio": target_size / source_size,
        "objects": stats["objects"],
        "seconds": time.time() - started,
    }
