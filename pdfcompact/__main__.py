"""Command line interface: ``python -m pdfcompact input.pdf -o output.pdf``."""

from __future__ import annotations

import argparse
import sys

from .pipeline import Options, compress


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdfcompact",
        description="Shrink a PDF by merging duplicate fonts, CMaps and metadata, "
                    "rounding vector coordinates and repacking the object table.",
    )
    parser.add_argument("source", help="input PDF")
    parser.add_argument("-o", "--output", help="output PDF (default: <input>.compact.pdf)")
    parser.add_argument("--quality", type=int, default=75, help="JPEG quality for re-encoded images (default 75)")
    parser.add_argument("--max-dim", type=int, default=1200, help="largest image side in pixels (default 1200)")
    parser.add_argument("--small-dp", type=int, default=2, help="decimals for local coordinates (default 2)")
    parser.add_argument("--large-dp", type=int, default=1, help="decimals for absolute page coordinates (default 1)")
    parser.add_argument("--dp-threshold", type=float, default=10.0,
                        help="value above which --large-dp applies (default 10)")
    parser.add_argument("--no-fonts", action="store_true", help="skip TrueType subset merging")
    parser.add_argument("--no-images", action="store_true", help="skip image re-encoding")
    parser.add_argument("--keep-metadata", action="store_true", help="keep XMP metadata blobs")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the final summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output or (args.source.rsplit(".", 1)[0] + ".compact.pdf")
    if output == args.source:
        print("refusing to overwrite the input file", file=sys.stderr)
        return 2

    options = Options(
        quality=args.quality,
        max_dim=args.max_dim,
        small_decimals=args.small_dp,
        large_decimals=args.large_dp,
        large_threshold=args.dp_threshold,
        merge_fonts=not args.no_fonts,
        reencode_images=not args.no_images,
        keep_metadata=args.keep_metadata,
        verbose=not args.quiet,
    )
    try:
        result = compress(args.source, output, options)
    except FileNotFoundError:
        print("no such file: %s" % args.source, file=sys.stderr)
        return 1
    except Exception as error:
        print("failed: %s" % error, file=sys.stderr)
        return 1

    print("%s: %.2f MB -> %.2f MB (%.1f%% removed) in %.1fs"
          % (output, result["source_size"] / 1048576, result["target_size"] / 1048576,
             100 * (1 - result["ratio"]), result["seconds"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
