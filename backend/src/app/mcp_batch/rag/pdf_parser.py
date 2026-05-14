"""PDF text extraction via PyMuPDF (CTR-0076, PRP-0037).

Extracts text from each page with page-level metadata.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pymupdf

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def extract_pages(file_path: Path) -> list[dict]:
    """Extract text and metadata from PDF pages.

    Args:
        file_path: Path to the PDF file.

    Returns:
        List of dicts with keys: text, page, source.
        Pages with no extractable text are skipped.
    """
    doc = pymupdf.open(str(file_path))
    pages: list[dict] = []
    total_pages = 0
    try:
        total_pages = len(doc)
        for page_num in range(total_pages):
            page = doc[page_num]
            text = page.get_text()
            if text and text.strip():
                pages.append(
                    {
                        "text": text,
                        "page": page_num + 1,  # 1-based page numbers
                        "source": file_path.name,
                    }
                )
    finally:
        doc.close()

    logger.info("Extracted %d pages with text from %s (%d total pages)", len(pages), file_path.name, total_pages)
    return pages
