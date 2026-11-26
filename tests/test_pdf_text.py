"""pdf_text.py on PDFs built with pymupdf: text, tables, and scanned-PDF detection."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf

from src.text.pdf_text import PdfReadError, read_pdf

MIN_CHARS = 50
INVOICE_LINES = [
    "COMMERCIAL INVOICE No. INV-2026-0042",
    "Vendor: Shenzhen Fasteners Co., Ltd.",
    "Purchaser: Acme Imports Inc., Vancouver BC",
    "Currency: USD   Incoterms: FOB Shanghai",
    "Total: 2,500.00",
]
TABLE = [
    ["Description", "HS Code", "Qty", "Unit price", "Amount"],
    ["Bolts M8", "7318.15", "100", "2.50", "250.00"],
    ["Nuts M8", "7318.16", "900", "2.50", "2,250.00"],
]


def add_text_page(doc: pymupdf.Document, lines: list[str]) -> pymupdf.Page:
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((72, 72 + 18 * i), line, fontsize=11)
    return page


def add_grid(page: pymupdf.Page, rows: list[list[str]], top: float = 250) -> None:
    col_w, row_h = 90, 22
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            rect = pymupdf.Rect(
                50 + c * col_w, top + r * row_h, 50 + (c + 1) * col_w, top + (r + 1) * row_h
            )
            page.draw_rect(rect, width=0.8)
            page.insert_text((rect.x0 + 4, rect.y1 - 6), text, fontsize=9)


def make_scan(source: pymupdf.Document) -> pymupdf.Document:
    """An image-only copy of `source`: every page rendered to a picture."""
    scan = pymupdf.open()
    for page in source:
        new = scan.new_page()
        new.insert_image(new.rect, pixmap=page.get_pixmap(dpi=100))
    return scan


class PdfTextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def save(self, doc: pymupdf.Document, name: str = "doc.pdf", **kwargs) -> Path:
        path = self.dir / name
        doc.save(path, **kwargs)
        doc.close()
        return path

    def invoice(self) -> pymupdf.Document:
        doc = pymupdf.open()
        add_grid(add_text_page(doc, INVOICE_LINES), TABLE)
        return doc

    def test_text_per_page(self):
        doc = pymupdf.open()
        add_text_page(doc, ["BILL OF LADING MAEU123456789 page one, enough characters here"])
        add_text_page(
            doc, ["Page two carries the container numbers, the gross weights and the seal numbers"]
        )
        result = read_pdf(self.save(doc), min_chars_per_page=MIN_CHARS)
        self.assertEqual([p.number for p in result.pages], [1, 2])
        self.assertIn("MAEU123456789", result.pages[0].text)
        self.assertIn("container numbers", result.pages[1].text)
        self.assertFalse(result.scanned)
        self.assertEqual(result.low_text_pages, [])

    def test_invoice_text_and_table(self):
        result = read_pdf(self.save(self.invoice()), min_chars_per_page=MIN_CHARS)
        (page,) = result.pages
        self.assertIn("INV-2026-0042", page.text)
        (table,) = page.tables
        self.assertEqual(table.rows, TABLE)

    def test_rotated_pages_give_the_same_table_and_text(self):
        for rotation in (90, 180, 270):
            with self.subTest(rotation=rotation):
                doc = self.invoice()
                doc[0].set_rotation(rotation)
                (page,) = read_pdf(self.save(doc), min_chars_per_page=MIN_CHARS).pages
                self.assertIn("INV-2026-0042", page.text)
                self.assertEqual([t.rows for t in page.tables], [TABLE])

    def test_empty_cells_become_empty_strings(self):
        doc = pymupdf.open()
        add_grid(
            add_text_page(doc, INVOICE_LINES), [["a", "b", "c"], ["d", "", "f"], ["g", "h", "i"]]
        )
        (table,) = read_pdf(self.save(doc), min_chars_per_page=MIN_CHARS).pages[0].tables
        self.assertEqual(table.rows, [["a", "b", "c"], ["d", "", "f"], ["g", "h", "i"]])

    def test_page_without_table_has_none(self):
        doc = pymupdf.open()
        add_text_page(doc, INVOICE_LINES)
        result = read_pdf(self.save(doc), min_chars_per_page=MIN_CHARS)
        self.assertEqual(result.pages[0].tables, [])

    def test_image_only_pdf_is_flagged_as_scanned(self):
        path = self.save(make_scan(self.invoice()))
        result = read_pdf(path, min_chars_per_page=MIN_CHARS)
        self.assertTrue(result.scanned)
        self.assertEqual(result.low_text_pages, [1])
        self.assertEqual(result.pages[0].chars, 0)

    def test_blank_pdf_is_flagged_as_scanned(self):
        doc = pymupdf.open()
        doc.new_page()
        self.assertTrue(read_pdf(self.save(doc), min_chars_per_page=MIN_CHARS).scanned)

    def test_one_short_page_does_not_flag_the_whole_pdf(self):
        doc = pymupdf.open()
        add_text_page(doc, INVOICE_LINES)
        add_text_page(doc, ["Terms"])
        result = read_pdf(self.save(doc), min_chars_per_page=MIN_CHARS)
        self.assertFalse(result.scanned)
        self.assertEqual(result.low_text_pages, [2])

    def test_scan_stapled_into_a_digital_pdf_is_listed(self):
        doc = pymupdf.open()
        add_text_page(doc, INVOICE_LINES)
        scan = make_scan(self.invoice())
        doc.insert_pdf(scan)
        scan.close()
        result = read_pdf(self.save(doc), min_chars_per_page=MIN_CHARS)
        self.assertFalse(result.scanned)
        self.assertEqual(result.low_text_pages, [2])

    def test_threshold_comes_from_the_caller(self):
        doc = pymupdf.open()
        add_text_page(doc, ["about forty characters of text here ok"])
        path = self.save(doc)
        self.assertTrue(read_pdf(path, min_chars_per_page=200).scanned)
        self.assertFalse(read_pdf(path, min_chars_per_page=10).scanned)

    def test_whitespace_does_not_count_as_text(self):
        doc = pymupdf.open()
        add_text_page(doc, ["a b c d e f g h i j"])
        self.assertEqual(read_pdf(self.save(doc), min_chars_per_page=MIN_CHARS).pages[0].chars, 10)

    def test_not_a_pdf(self):
        path = self.dir / "fake.pdf"
        path.write_text("this is not a pdf")
        with self.assertRaises(PdfReadError):
            read_pdf(path, min_chars_per_page=MIN_CHARS)

    def test_missing_file(self):
        with self.assertRaises(PdfReadError):
            read_pdf(self.dir / "missing.pdf", min_chars_per_page=MIN_CHARS)

    def test_password_protected(self):
        doc = pymupdf.open()
        add_text_page(doc, INVOICE_LINES)
        path = self.save(
            doc,
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="owner",
            user_pw="user",
        )
        with self.assertRaisesRegex(PdfReadError, "password"):
            read_pdf(path, min_chars_per_page=MIN_CHARS)


if __name__ == "__main__":
    unittest.main()
