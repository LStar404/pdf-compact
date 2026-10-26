"""Conservative image re-encoding helpers.

Photos are worth re-encoding; line art, barcodes, screenshots and spot-colour
artwork are not.  The caller decides (see :mod:`pdfcompact.pipeline`); this
module only turns image streams into PIL images and back.
"""

from __future__ import annotations

import io

from PIL import Image

__all__ = ["decode_image", "encode_jpeg", "encode_png", "scaled_size"]

Image.MAX_IMAGE_PIXELS = None

JPEG_FILTERS = ("/DCTDecode", "/JPXDecode")


def decode_image(raw: bytes, filters: list[str], colorspace, width: int, height: int,
                 bits: int, decoded: bytes | None) -> Image.Image | None:
    """Decode a PDF image stream to a PIL image, or return None if unsupported."""
    try:
        if len(filters) == 1 and filters[0] in JPEG_FILTERS:
            image = Image.open(io.BytesIO(raw))
            image.load()
            return image
        if filters not in ([], ["/FlateDecode"]):
            return None
        if bits != 8 or decoded is None or width <= 0 or height <= 0:
            return None
        pixels = decoded
        channels = len(pixels) // (width * height)
        if isinstance(colorspace, tuple):
            family, components = colorspace[0], (colorspace[1] if len(colorspace) > 1 else 0)
            if family == "/ICCBased":
                mode = {1: "L", 3: "RGB", 4: "CMYK"}.get(components)
                if mode and {"L": 1, "RGB": 3, "CMYK": 4}[mode] == channels:
                    return Image.frombytes(mode, (width, height), pixels)
            return None
        mode = {"/DeviceGray": "L", "/DeviceRGB": "RGB", "/DeviceCMYK": "CMYK"}.get(colorspace)
        if mode and {"L": 1, "RGB": 3, "CMYK": 4}[mode] == channels:
            return Image.frombytes(mode, (width, height), pixels)
        return None
    except Exception:
        return None


def scaled_size(size: tuple[int, int], max_dim: int) -> tuple[int, int]:
    width, height = size
    if max(width, height) <= max_dim:
        return width, height
    scale = max_dim / float(max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def _prepare(image: Image.Image, max_dim: int, keep_mode: bool) -> tuple[Image.Image, str]:
    mode = image.mode
    if keep_mode and mode in ("L", "CMYK"):
        pass
    elif mode == "CMYK":
        pass
    elif mode in ("L", "1", "LA"):
        image = image.convert("L")
    else:
        image = image.convert("RGB")
    width, height = scaled_size(image.size, max_dim)
    if (width, height) != image.size:
        image = image.resize((width, height), Image.LANCZOS)
    target = "/DeviceCMYK" if image.mode == "CMYK" else "/DeviceGray" if image.mode == "L" else "/DeviceRGB"
    return image, target


def encode_jpeg(image: Image.Image, max_dim: int = 1200, quality: int = 75) -> tuple[bytes, str]:
    """Downscale and encode as JPEG. Returns (bytes, pdf_colour_space_name)."""
    prepared, colorspace = _prepare(image, max_dim, keep_mode=False)
    buffer = io.BytesIO()
    prepared.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue(), colorspace


def encode_png(image: Image.Image, max_dim: int | None = None) -> tuple[bytes, str]:
    """Encode losslessly (PNG + FlateDecode). CMYK is converted to RGB."""
    prepared = image
    if max_dim is not None:
        width, height = scaled_size(prepared.size, max_dim)
        if (width, height) != prepared.size:
            prepared = prepared.resize((width, height), Image.LANCZOS)
    if prepared.mode == "CMYK":
        prepared = prepared.convert("RGB")
    elif prepared.mode not in ("L", "RGB"):
        prepared = prepared.convert("L" if prepared.mode in ("1", "LA") else "RGB")
    buffer = io.BytesIO()
    prepared.save(buffer, format="PNG", optimize=True, compress_level=9)
    colorspace = "/DeviceGray" if prepared.mode == "L" else "/DeviceRGB"
    return buffer.getvalue(), colorspace
