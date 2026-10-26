"""pdfcompact - shrink PDFs by removing structural redundancy.

Public entry points:

``compress(source, target, options)``
    Run the whole pipeline programmatically.

``python -m pdfcompact input.pdf -o output.pdf``
    Command line interface.
"""

from .pipeline import Options, compress

__version__ = "0.1.0"
__all__ = ["Options", "compress", "__version__"]
