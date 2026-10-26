# How `pdfcompact` works

This document explains each stage, why it is safe, and what the numbers were on
the file the tool was tuned for: a 387-page, 122.64 MB Chinese technical book
whose pages are a mix of vector text outlines, embedded images and per-page font
subsets.

Everything below is generic; no content from that book is included here.

---

## 0. Reading the file

`pypdf` parses the document, then every indirect object is pulled into a plain
dict `oid -> object`. From that point on the object graph is treated as a tree of
dictionaries, arrays, names, numbers, strings and streams; pypdf is not asked to
write anything.

Object numbers are **preserved** throughout the pipeline. That matters because
references live inside objects: as long as an object keeps its number, a
reference stays valid, and objects that are no longer referenced can simply be
omitted from the output.

## 1. Deduplicating identical streams

Producers that assemble a PDF from many single-page PDFs often copy per-page
resources instead of sharing them. The usual suspects:

* an `/ICCBased` profile per image,
* a `/ToUnicode` CMap per font usage,
* the same CFF program dozens of times.

Each stream is hashed by its **decoded** bytes, together with its filter chain
and dictionary (minus `/Length`), and duplicates are mapped onto the first
occurrence. All references are then rewritten. On the test file this removed
4 560 stream objects, mostly the ~3 750 nearly identical `/ToUnicode` CMaps that
accounted for about 50 MB.

## 2. Merging TrueType subset fonts

This is the largest win when it applies, so it is worth being precise about why
it is safe.

A subsetter used by this kind of producer keeps the **original glyph-ID space**:

* `maxp.numGlyphs` is the original count (22 024, not the handful actually used);
* `loca` has an entry for every original glyph ID, pointing at either a real
  outline or an empty slot;
* `hmtx`, `head`, `hhea`, `OS/2`, `prep` are copied verbatim from the original;
* `glyf` only contains the outlines used on that page.

Consequently the same character has the same glyph ID in every subset of a
family, and the union of the subsets is a font that is valid for all of them.

`ttfmerge` therefore:

1. groups programs by family name (the `ABCDEF+` subset prefix is stripped) and
   by a signature of every table that must match byte for byte;
2. refuses any group whose subsets disagree — a single glyph conflict means the
   glyph-ID spaces differ and the merge would silently corrupt text;
3. unions the `glyf` pieces by glyph ID, rebuilds `loca` (long format, 4-byte
   aligned glyphs), recomputes table checksums and `head.checkSumAdjustment`;
4. verifies the result by re-parsing it and comparing **every** source glyph
   against the merged program at the same GID.

Because the glyph IDs are unchanged and the CID→GID map is `/Identity`, every
page can keep its own `FontDescriptor` and `/W` array while pointing
`/FontFile2` at the same merged program.

On the test file: 1 028 distinct programs in 27 groups collapsed to 16 programs,
28.8 MB → 0.8 MB (compressed).

If a font fails any check, the group is skipped and logged; the document stays
correct, just larger.

## 3. Dropping XMP metadata

Page-level `/Metadata` streams carry XMP packets — RDF/XML, often tens of
kilobytes each. They have no effect on rendering, text selection or bookmarks, so
they are removed (`--keep-metadata` keeps them). 3.4 MB in the test file.

## 4. Rounding content-stream coordinates

Content streams are tokenized, not regex-replaced: literal strings, hex strings,
names and comments are copied verbatim, so nothing inside a `(text)` or
`<hex>`operand is touched. Streams that contain inline images (`BI … ID … EI`)
are skipped entirely, because rounding inside binary image data would corrupt
them.

Real numbers are reformatted with fewer decimals:

* `|v| < 10` → 2 decimals: glyph outlines, colours, unit-square coordinates;
* `|v| >= 10` → 1 decimal: absolute page coordinates, text positions.

0.01 pt is 1/7200 inch, 0.1 pt is ~0.035 mm. Trailing zeros are removed as well,
which makes the deflate stage happier: on the test file 54.96 MB of verbose
content deflated to 11.16 MB, versus 16.9 MB before rounding.

`--dp-threshold` controls where the switch happens, if you want to be more or less
aggressive.

## 5. Re-encoding images (conservatively)

Each image is decoded, and then judged:

| Situation | Action |
| --- | --- |
| `/Indexed`, `/Lab`, `/DeviceN`, image masks, soft masks | left untouched |
| `/Separation` (spot ink) | colour space preserved; lossless PNG only if it is smaller |
| photo-like | JPEG candidate; accepted only if **≥ 20 % smaller** than the original |
| grayscale line art | JPEG rejected as above; lossless PNG if smaller |

The threshold matters: re-encoding an already well-compressed bar code or line
figure at low quality makes it *bigger* and blurrier. Keeping the original is the
right answer there. On the test file only 23 of 256 images were worth
re-encoding.

## 6. Writing a PDF 1.7 file

The output uses the modern container features:

* every stream is re-deflated at level 9;
* all non-stream objects go into `/ObjStm` containers (compressed, batched by
  count and size), which removes the 4 MB of plain dictionary text;
* a single `/XRef` stream replaces the classic table, including a proper free
  list for the objects that were dropped.

Before writing, reachability is recomputed from the trailer's `/Root` and
`/Info`; unreachable objects (merged-away fonts, deduplicated streams, metadata)
are never emitted. While serializing, every reference is checked against that
reachable set — a dangling reference raises instead of producing a broken file.

---

## Verification methodology

The tool was validated on the 387-page test document with four independent checks:

1. **Structure** — same page count, identical `MediaBox`/`CropBox` on all pages,
   same image count, all embedded TrueType programs re-parse cleanly.
2. **Content operators** — per page, the multiset of drawing operators
   (`m l c re f S q Q BT ET Tj TJ Do …`) must be identical. This catches any
   structural damage while ignoring the rounded numbers.
3. **Text** — `extract_text()` compared page by page. 57 pages differ by a single
   space, an artefact of pypdf's spacing heuristic reacting to a 0.01 pt shift;
   no page differs in actual characters.
4. **Rendering** — pages were rendered with a real PDF engine before and after,
   then compared pixel-wise and inspected at 2× zoom: text sharpness, spot-colour
   figures, gray backdrops and a bar code on the back cover all survived.

## Known limitations

* Text that was converted to outlines by the producer cannot be re-flowed or
  searched; that is how the file already was, the tool neither fixes nor worsens
  it.
* Files with signed content, incremental updates or encryption are out of scope.
* If every page uses a *different* glyph-ID space (some producers re-number per
  subset), font merging is skipped by design — check the log for
  `skipping font family`.
