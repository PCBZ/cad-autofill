"""Try the PDF text reader on a folder of PDFs.

    python tools/check_pdf_text.py inbox/<shipment>

Prints, for every PDF, the page count, whether it looks scanned, and the characters
and tables on each page. The full text and tables are written to
output/pdf_text_check/<name>.txt (ignored by git) so you can compare them with the
PDF by eye. Nothing is sent anywhere.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.text.pdf_text import PdfReadError, read_pdf  # noqa: E402

OUT_DIR = Path("output/pdf_text_check")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="folder with PDFs")
    parser.add_argument("--min-chars", type=int, default=50, help="min_chars_per_page (default 50)")
    args = parser.parse_args()

    pdfs = sorted(args.folder.glob("*.pdf"))
    if not pdfs:
        print(f"no PDFs in {args.folder}", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for pdf in pdfs:
        try:
            result = read_pdf(pdf, min_chars_per_page=args.min_chars)
        except PdfReadError as e:
            print(f"\n=== {pdf.name}: ERROR {e}")
            continue
        print(
            f"\n=== {pdf.name}: {len(result.pages)} pages, scanned={result.scanned}, "
            f"low-text pages={result.low_text_pages}"
        )
        lines = []
        for page in result.pages:
            print(f"-- page {page.number}: {page.chars} chars, {len(page.tables)} tables")
            lines.append(f"##### page {page.number}\n{page.text}")
            for i, table in enumerate(page.tables, 1):
                print(f"   table {i}: {len(table.rows)} rows x {max(map(len, table.rows))} cols")
                lines.append(f"##### page {page.number}, table {i}")
                lines += [" | ".join(row) for row in table.rows]
        (OUT_DIR / f"{pdf.stem}.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nfull text written to {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
