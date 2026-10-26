# pdfcompact

Shrink PDFs by removing **structural redundancy** instead of destroying content.

Most "compress PDF" tools re-render pages to JPEG, which degrades text and turns
selectable text into pictures. `pdfcompact` works on the file's internal
structure instead: it finds duplicated font programs, duplicated character maps,
dead metadata and over-precise vector coordinates, and repacks what is left into
a modern PDF 1.7 container with object streams.

Text stays text, outlines stay vector, images keep their original pixels unless
re-encoding is genuinely worth it.

---

## What it actually removes

The interesting savings are rarely "images are too big". On the file this was
written for, the breakdown looked like this:

| Redundancy | Size | What `pdfcompact` does |
| --- | --- | --- |
| One TrueType subset embedded **per page** (1561 programs, 27 families) | ~29 MB | Unions the subsets per family into one program per font (→ 0.8 MB) |
| Thousands of near-identical `/ToUnicode` CMaps | ~50 MB | Keeps one copy of each distinct stream |
| Per-page XMP metadata blobs | 3.4 MB | Removes them (rendering is unaffected) |
| Duplicate CFF font programs (507 copies) | 3.9 MB | Deduplicates |
| Vector coordinates written with 5–7 decimals | 16.9 MB | Rounds to 0.01 pt (0.1 pt for absolute page coordinates) |
| Uncompressed dictionary / xref data | 4.2 MB | Packs into compressed object streams |

Result on that file: **122.64 MB → 17.99 MB (−85.3 %)** in 74 s, 387/387 pages,
610/610 bookmarks and every page box preserved.

---

## How it works

```
input.pdf
   │
   ├─ 1. dedupe identical streams ......... /ToUnicode, ICC profiles, CFF programs
   ├─ 2. merge TrueType subsets ........... per family, preserving glyph IDs
   ├─ 3. drop XMP metadata
   ├─ 4. round content-stream numbers .... 2 decimals local, 1 decimal absolute
   ├─ 5. re-encode images conservatively . photos only, line art untouched
   │
   └─ write: object streams + xref stream, Flate level 9
```

Details, including why merging per-page subsets is safe, are in
[docs/how-it-works.md](docs/how-it-works.md).

---

## Install

```bash
pip install -r requirements.txt
```

Only two dependencies: [`pypdf`](https://pypi.org/project/pypdf/) and
[`Pillow`](https://pypi.org/project/Pillow/).

## Usage

```bash
# simplest form
python -m pdfcompact book.pdf -o book.small.pdf

# quieter, and a little more aggressive on vector precision
python -m pdfcompact book.pdf -o book.small.pdf --quiet --dp-threshold 3

# only do the structural work, leave images and fonts alone
python -m pdfcompact book.pdf -o book.small.pdf --no-fonts --no-images
```

As a library:

```python
from pdfcompact import Options, compress

report = compress("book.pdf", "book.small.pdf", Options(quality=75, max_dim=1200))
print(report["source_size"], "->", report["target_size"])
```

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `-o`, `--output` | `<input>.compact.pdf` | output path |
| `--quality` | `75` | JPEG quality when an image is re-encoded |
| `--max-dim` | `1200` | longest image side after downscaling |
| `--small-dp` | `2` | decimals for local coordinates (glyph outlines, colours) |
| `--large-dp` | `1` | decimals for absolute page coordinates |
| `--dp-threshold` | `10` | value above which `--large-dp` applies |
| `--no-fonts` | off | skip TrueType subset merging |
| `--no-images` | off | skip image re-encoding |
| `--keep-metadata` | off | keep XMP blobs |
| `-q`, `--quiet` | off | only print the summary line |

---

## Quality safeguards

* **Font merging is verified, not hoped for.** Every glyph of every source
  subset is checked against the merged program by glyph ID; if a family shows
  *any* glyph conflict it is left untouched (`glyph conflict` in the log).
  Merging is only attempted when the CID→GID map is identity-like, i.e. when the
  subsets really do share one glyph-ID space.
* **Coordinates are rounded far below the visible threshold.** 0.01 pt is
  1/7200 inch; 0.1 pt is about 0.035 mm.
* **Images are judged one by one.** A JPEG candidate is accepted only when it is
  at least 20 % smaller than the original. Spot-colour (`/Separation`) images keep
  their colour space and are re-encoded losslessly or not at all, so bar codes,
  line art and ink illustrations do not get smeared or inverted.
* **Nothing is silently dropped.** Objects that become unreachable are removed,
  but every reference is checked while writing; a dangling reference aborts the
  run instead of producing a broken file.

Recommended sanity check for a real book:

```bash
python -c "from pypdf import PdfReader as R; a,b=R('book.pdf'),R('book.small.pdf'); \
print(len(a.pages), len(b.pages), a.pages[10].extract_text()==b.pages[10].extract_text())"
```

---

## When to use it

Good fit:

* files produced by merging many smaller PDFs (every page brings its own fonts);
* CJK books where each page embeds a subset of the same font family;
* vector-heavy documents (matplotlib/TikZ figures, text converted to outlines);
* anything where you want the text to remain selectable.

Poor fit:

* already-optimised files from a careful producer (there will be little duplicate
  structure left to remove);
* files whose size is a handful of large photographs — see `--quality`,
  `--max-dim`;
* files you do not have the right to modify or redistribute.

`pdfcompact` does not decrypt, does not OCR and does not rasterise.

---

## 中文说明

`pdfcompact` 通过消除 PDF 的**结构性冗余**来压缩文件，而不是把页面重新渲染成图片：
合并“每页各嵌一份”的同族字体子集、去掉重复的字符映射与元数据、把矢量坐标收敛到
0.01 pt、按需压缩图片，最后用对象流重新打包成 PDF 1.7。文字仍是文字，图形仍是矢量。

```bash
pip install -r requirements.txt
python -m pdfcompact 书.pdf -o 书.small.pdf
```

在 387 页的中文技术书上实测：122.64 MB → 17.99 MB（压缩 85.3%），74 秒完成，
页数、书签、页面尺寸、文字提取结果与绘图指令全部保持一致。

---

## License

MIT — see [LICENSE](LICENSE).

This repository contains **no book, no PDF and no text from any publication**.
It is a tool; only run it on files you own or are allowed to modify.
