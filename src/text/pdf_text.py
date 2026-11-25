"""Read the text layer and the tables of a digital PDF with pymupdf.

`read_pdf` returns the text and the tables of every page, and says whether the
PDF is a scan. A scanned PDF has no text layer to read, so the Flow's `load` step
stops and hands the shipment to a person (the optional PaddleOCR fallback is
issue #29).

A PDF counts as scanned when its pages hold fewer than `min_chars_per_page`
characters on average (`[text] min_chars_per_page` in settings.toml). The average
keeps one short page, such as the back of a B/L, from flagging the whole file.
Pages with little text are still listed in `low_text_pages`, so a scan stapled
into a digital PDF is visible.

Only pymupdf and pydantic are used; nothing leaves the computer.
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
