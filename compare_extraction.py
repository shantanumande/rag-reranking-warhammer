"""
Measure how badly naive PDF extraction breaks the rules text, compared with the cleaned chunks.

For every sentence of 12+ words in the cleaned rule chunks, check whether it appears intact
(whitespace-normalised) in a naive extraction of the same pages. A sentence that is not
intact was split up by the naive extractor, usually by sidebar or diagram text.

Requires output/warhammer_chunks.jsonl (run prepare_warhammer.py first).
pdftotext (poppler / xpdf) is optional; it is skipped if not installed.

Usage:
  python compare_extraction.py
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf

import prepare_warhammer as pw


def pdftotext_pages(args):
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.txt"
        subprocess.run([exe, "-enc", "UTF-8", *args, str(pw.PDF_PATH), str(out)], check=True)
        return out.read_text(encoding="utf-8", errors="replace").split("\f")


def main():
    chunks = [json.loads(l) for l in open(pw.OUT_DIR / "warhammer_chunks.jsonl", encoding="utf-8")]
    doc = pymupdf.open(pw.PDF_PATH)

    extractions = {"PyMuPDF page.get_text()": [p.get_text() for p in doc]}
    for name, args in [("pdftotext (default)", []), ("pdftotext -layout", ["-layout"])]:
        pages = pdftotext_pages(args)
        if pages is not None:
            extractions[name] = pages

    results = {}
    for name, pages in extractions.items():
        kept = "\n".join(p for i, p in enumerate(pages, 1) if i not in pw.SKIP_PAGES)
        total, intact, _ = pw.sentence_integrity(chunks, kept)
        results[name] = {"sentences_checked": total, "broken": total - intact,
                         "broken_pct": round(100 * (total - intact) / total, 1)}
        print(f"{name:26s} {total - intact:4d}/{total} sentences broken ({results[name]['broken_pct']}%)")

    with open(pw.OUT_DIR / "extraction_comparison.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
