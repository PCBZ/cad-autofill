"""Read the text and tables of a digital PDF with pymupdf.

A PDF is `scanned` when its pages average fewer than `min_chars_per_page` characters
(`[text]` in settings.toml): there is no text layer to read, so a person takes over.
`low_text_pages` lists pages under the threshold, even when the PDF as a whole is fine.
"""

from pathlib import Path

import pymupdf
from pydantic import BaseModel


class PdfReadError(Exception):
    """The file cannot be read as a PDF (corrupt, encrypted, or no pages)."""


class PdfTable(BaseModel):
    rows: list[list[str]]  # cell text, empty string for an empty or merged cell


class PdfPage(BaseModel):
    number: int  # 1-based, as a person counts pages
    text: str
    chars: int  # characters other than whitespace
    tables: list[PdfTable] = []


class PdfText(BaseModel):
    path: str
    pages: list[PdfPage]
    scanned: bool
    low_text_pages: list[int] = []  # page numbers with fewer than min_chars_per_page characters


def read_pdf(path: str | Path, *, min_chars_per_page: int) -> PdfText:
    path = Path(path)
    try:
        doc = pymupdf.open(path)
    except Exception as e:  # pymupdf raises several types for missing, corrupt or non-PDF files
        raise PdfReadError(f"cannot open {path.name} as a PDF: {e}") from e
    with doc:
        if doc.needs_pass:
            raise PdfReadError(f"{path.name} is password protected")
        if doc.page_count == 0:
            raise PdfReadError(f"{path.name} has no pages")
        pages = [_read_page(page) for page in doc]
    average = sum(p.chars for p in pages) / len(pages)
    return PdfText(
        path=str(path),
        pages=pages,
        scanned=average < min_chars_per_page,
        low_text_pages=[p.number for p in pages if p.chars < min_chars_per_page],
    )


def _read_page(page: pymupdf.Page) -> PdfPage:
    # Table detection and cell reads use different coordinate frames on a page with
    # /Rotate set. Reading the page unrotated (in memory only) keeps them the same.
    page.set_rotation(0)
    text = page.get_text("text", sort=True)
    tables = [_read_table(page, table) for table in page.find_tables().tables]
    return PdfPage(
        number=page.number + 1,
        text=text,
        chars=len("".join(text.split())),
        tables=[t for t in tables if t.rows],
    )


def _read_table(page: pymupdf.Page, table: pymupdf.table.Table) -> PdfTable:
    # Each cell is read from its own area of the text layer rather than with
    # Table.extract(), which can run words together in small print.
    rows = [
        [" ".join(page.get_textbox(cell).split()) if cell else "" for cell in row.cells]
        for row in table.rows
    ]
    return PdfTable(rows=rows)
