"""
Prepare the Warhammer 40,000 Core Rules PDF for the RAG assignment.

Problems with the raw PDF (and what this script does about them):
  1. Two columns + sidebar boxes -> plain extraction interleaves them.
     Fix: read text block by block and order blocks column by column, top to bottom.
  2. Some pages are artwork, diagrams or example datasheets with no rules.
     Fix: skip a fixed list of non-rule pages (see SKIP_PAGES).
  3. Noise: page numbers, red step-callout digits, "CORE RULES | ..." footers,
     vertical decorative text, tiny thumbnail text, control-character glyphs.
     Fix: filter these at the line/span level.
  4. Sidebars (rule exceptions) and "■" summary boxes get merged into the main rule.
     Fix: split the text into sections at headings, so each sidebar becomes its own
     section, and keep summary boxes as separate chunks tagged type="summary".
  5. Lore/flavour italics under headings add noise for retrieval.
     Fix: move them to a separate "flavour" field instead of the chunk text.

Outputs (written to ./output):
  warhammer_chunks.jsonl  - one chunk per line, ready to embed
  warhammer_clean.md      - the cleaned document, readable, for checking by eye
  preprocessing_stats.json - numbers used in preprocessing_report.md

Usage:
  pip install pymupdf
  python prepare_warhammer.py
"""

import json
import re
import statistics
from pathlib import Path

import pymupdf

HERE = Path(__file__).parent
PDF_PATH = HERE / "warhammer40000_core&key_corerules_eng_24.09-5xfayxjekm.pdf"
OUT_DIR = HERE / "output"

# Pages (1-based) that hold no rules text worth retrieving.
SKIP_PAGES = {
    1: "cover",
    2: "artwork, no text",
    4: "overview of how the book is laid out (meta, not rules)",
    27: "weapon illustration page (only weapon names)",
    31: "full-page diagram (only callout numbers)",
    37: "example datasheet diagram (labels and stat lines, no rules)",
    40: "artwork, no text",
    50: "example battlefield map caption",
    51: "example battlefield map caption",
    52: "example battlefield map caption",
}

# Text the PDF does not contain as text. In the Wound Roll table (p.21) the required D6
# results are drawn as vector graphics, so only the "+" signs are extractable. The values
# below are the standard Wound Roll table; check them against page 21 of the PDF.
MANUAL_FIXES = {
    "Strength is TWICE (or more than twice) the Toughness.":
        "Strength is TWICE (or more than twice) the Toughness: wound on 2+.",
    "Strength is GREATER than the Toughness.":
        "Strength is GREATER than the Toughness: wound on 3+.",
    "Strength is EQUAL to the Toughness.":
        "Strength is EQUAL to the Toughness: wound on 4+.",
    "Strength is LESS than the Toughness.":
        "Strength is LESS than the Toughness: wound on 5+.",
    "Strength is HALF (or less than half) the Toughness.":
        "Strength is HALF (or less than half) the Toughness: wound on 6+.",
}

FOOTER_COLOR = 0x7C7D79      # grey "CORE RULES | SECTION" footer
SUMMARY_FILL = (0.846, 0.855, 0.817)   # background colour of the "■" summary boxes
TITLE_MIN_SIZE = 17           # headings this big are page/topic titles (parents of sub-headings)
COLUMN_GAP = 40              # min gap (pt) between block left edges that starts a new column
EXAMPLE_MAX_SIZE = 7.0       # worked-example captions on diagrams use small Conduit text
MAX_CHUNK_WORDS = 250         # split long sections into chunks of about this size
MIN_CHUNK_WORDS = 8           # drop fragments shorter than this
FLAVOUR_MAX_WORDS = 40


# ---------------------------------------------------------------- span helpers

def is_bold(span):
    f = span["font"]
    return "Bold" in f or "ExtraBold" in f or "Black" in f


def is_italic(span):
    f = span["font"]
    return f.endswith("-It") or "Ital" in f


def clean_text(s):
    s = re.sub(r"[\x00-\x08\x0b-\x1f]", "", s)   # control glyphs (bullets/arrows in the Conduit font)
    s = s.replace(" ", " ")
    s = re.sub(r"●\s*", "• ", s)
    return s


def line_text(spans):
    return clean_text("".join(s["text"] for s in spans)).strip()


def classify_line(spans, text):
    """Return 'heading', 'flavour_candidate', 'example' or 'body'."""
    visible = [s for s in spans if s["text"].strip()]
    size = max(s["size"] for s in visible)
    if (size >= 10 and all(is_bold(s) or "Medium" in s["font"] for s in visible)
            and len(text) < 70 and len(re.findall(r"[A-Za-z]", text)) >= 3):
        return "heading"
    if all(s["font"].startswith("Conduit") for s in visible) and size <= EXAMPLE_MAX_SIZE + 0.05:
        return "example"
    if all(is_italic(s) for s in visible):
        return "flavour_candidate"
    return "body"


# ---------------------------------------------------------------- page reading

def page_footer(page):
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                if s["color"] == FOOTER_COLOR and "|" in s["text"]:
                    return s["text"].split("|", 1)[1].strip().title()
    return None


def ordered_blocks(page):
    """Text blocks sorted column by column, then top to bottom.

    Columns are found per page by clustering the blocks' left edges: a gap of more than
    COLUMN_GAP points between neighbouring left edges starts a new column. This handles the
    2-column + sidebar pages, the stratagem pages and the 3-box "Muster your army" pages.
    """
    blocks = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
    xs = sorted({round(b["bbox"][0]) for b in blocks})
    starts = [x for prev, x in zip(xs, xs[1:]) if x - prev > COLUMN_GAP]
    col = lambda b: sum(b["bbox"][0] >= s - 0.5 for s in starts)
    blocks.sort(key=lambda b: (col(b), b["bbox"][1]))
    return blocks


def summary_boxes(page):
    """Rectangles of the grey-green '■' summary boxes on this page."""
    boxes = []
    for d in page.get_drawings():
        fill = d.get("fill")
        if fill and all(abs(a - b) < 0.01 for a, b in zip(fill, SUMMARY_FILL)):
            boxes.append(d["rect"])
    return boxes


def box_of(bbox, boxes):
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    for i, r in enumerate(boxes):
        if r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1:
            return i
    return None


def page_lines(page):
    """Yield (block_index, kind, text) for the useful lines on a page.

    All lines inside one summary box are gathered (in top-to-bottom order) and yielded
    once as kind='summary', at the point where the box is first reached. PyMuPDF often
    splits a box across several blocks, so this keeps each bullet in one piece.
    """
    boxes = summary_boxes(page)
    lines = []
    for bi, b in enumerate(ordered_blocks(page)):
        for l in b["lines"]:
            if abs(l["dir"][1]) > 0.1:                         # vertical decorative text
                continue
            spans = [s for s in l["spans"]
                     if s["size"] >= 5                          # thumbnail / mini-diagram text
                     and s["color"] != FOOTER_COLOR]            # footer
            if not spans:
                continue
            text = line_text(spans)
            if not text or re.fullmatch(r"[\d\s]+", text):      # page numbers, callout digits
                continue
            if len(text) <= 8 and not re.search(r"[A-Za-z]{3}", text) and not text.endswith((".", ",")):
                continue                                        # diagram labels: 1½", A, B, or
            if re.match(r"\d+ ", text) and all(s["color"] == 0xFFFFFF for s in spans):
                continue                                        # numbered labels on diagrams ("5 FIGHT PHASE")
            lines.append((bi, spans, text, box_of(l["bbox"], boxes), l["bbox"]))

    emitted = set()
    for bi, spans, text, box, bbox in lines:
        size = max(s["size"] for s in spans)
        if box is None:
            yield bi, classify_line(spans, text), text, bbox[1], size
        elif box not in emitted:
            emitted.add(box)
            in_box = sorted((ln for ln in lines if ln[3] == box), key=lambda ln: (round(ln[4][1]), ln[4][0]))
            yield bi, "summary", join_lines([ln[2] for ln in in_box]), bbox[1], size


# ---------------------------------------------------------------- sectioning

def join_lines(lines):
    out = ""
    for t in lines:
        if out.endswith("-") and len(out) > 1 and out[-2].isalpha() and t[:1].islower():
            out = out[:-1] + t                                  # re-join hyphenated word
        elif out:
            out += " " + t
        else:
            out = t
    out = out.replace("■", "\n■ ").strip()
    return re.sub(r"[ \t]+", " ", out)


def build_sections(doc):
    sections = []
    current = None
    section_name = "Introduction"

    parent = None                     # last page/topic title (e.g. "RUINS"), shown before sub-headings

    def new_section(heading, page_no):
        return {"heading": heading, "section": section_name, "page_start": page_no,
                "page_end": page_no, "flavour": [], "paras": [], "summary": [], "examples": [],
                "is_title": False, "parent": None}

    for page in doc:
        pno = page.number + 1
        if pno in SKIP_PAGES:
            continue
        footer = page_footer(page)
        if footer and footer != section_name:
            parent = None                                       # new chapter: forget the old title
        section_name = footer or section_name
        if current is None:
            current = new_section("Introduction", pno)

        buf, buf_key, buf_italic = [], None, True               # lines of the current paragraph

        def add_para(target, text):
            # PyMuPDF sometimes splits one paragraph over several blocks: re-join when the
            # previous piece stops mid-sentence and the new one continues in lower case.
            ends_mid = target and (re.search(r"\b(the|a|an|of|to|and|or|in|on|with|by|for)$", target[-1])
                                   or (not re.search(r"[.!?:)\"”]$", target[-1]) and text[:1].islower()))
            if ends_mid:
                target[-1] += " " + text
            else:
                target.append(text)

        def flush():
            nonlocal buf, buf_key, buf_italic
            if buf:
                text = join_lines(buf)
                if buf_key[1] == "example":
                    if len(text.split()) >= 6:                  # skip short diagram labels
                        add_para(current["examples"], text)
                elif buf_italic and not current["paras"] and len(text.split()) <= FLAVOUR_MAX_WORDS:
                    current["flavour"].append(text)              # lore line right under a heading
                else:
                    add_para(current["paras"], text)
            buf, buf_key, buf_italic = [], None, True

        last_heading_y = None
        for bi, kind, text, y, size in page_lines(page):
            if kind == "heading":
                flush()
                empty = not (current["paras"] or current["summary"] or current["examples"])
                if empty and last_heading_y is not None and 0 < y - last_heading_y < 25:
                    current["heading"] += " " + text            # heading wrapped onto 2 lines
                    if current["is_title"]:
                        parent = current["heading"]
                else:
                    sections.append(current)
                    is_title = size >= TITLE_MIN_SIZE
                    if is_title:
                        parent = text
                    current = new_section(text, pno)
                    current["is_title"] = is_title
                    current["parent"] = None if is_title else parent
                last_heading_y = y
                continue
            if kind == "summary":
                flush()
                current["summary"].append(text)
                current["page_end"] = pno
                continue
            # a new paragraph starts at each block, or when switching between text and example
            key = (bi, "example" if kind == "example" else "text")
            if buf_key is not None and key != buf_key:
                flush()
            buf_key = key
            buf_italic = buf_italic and kind == "flavour_candidate"
            buf.append(text)
            current["page_end"] = pno
        flush()

    sections.append(current)
    return sections


# ---------------------------------------------------------------- chunking

def split_long(paras, max_words):
    """Group paragraphs into pieces of at most ~max_words, splitting long paragraphs at sentences."""
    pieces, cur = [], []
    units = []
    for p in paras:
        if len(p.split()) > max_words:
            units.extend(re.split(r"(?<=[.!?])\s+(?=[A-Z■])", p))
        else:
            units.append(p)
    for u in units:
        if cur and len(" ".join(cur).split()) + len(u.split()) > max_words:
            pieces.append(cur)
            cur = []
        cur.append(u)
    if cur:
        if pieces and len(" ".join(cur).split()) < 40:
            pieces[-1].extend(cur)                              # don't leave a tiny tail chunk
        else:
            pieces.append(cur)
    return pieces


def make_chunks(sections):
    chunks = []
    for sec in sections:
        heading = sec["heading"].strip()
        if sec["parent"] and sec["parent"].lower() not in heading.lower():
            heading = f"{sec['parent'].strip()} › {heading}"
        base = {"section": sec["section"], "heading": heading,
                "page_start": sec["page_start"], "page_end": sec["page_end"],
                "flavour": " ".join(sec["flavour"])}
        paras = sec["paras"]
        if paras and paras[0].strip().lower() == heading.lower():
            paras = paras[1:]                                   # heading repeated as first line
        for part_no, piece in enumerate(split_long(paras, MAX_CHUNK_WORDS), 1):
            body = "\n".join(piece)
            if len(body.split()) < MIN_CHUNK_WORDS:
                continue
            chunks.append({**base, "type": "rule", "part": part_no,
                           "text": f"{heading}\n{body}"})
        for kind, label in (("summary", "summary"), ("examples", "example")):
            body = "\n".join(sec[kind]).strip()
            if len(body.split()) >= MIN_CHUNK_WORDS:
                chunks.append({**base, "type": label, "part": 1,
                               "text": f"{heading} ({label})\n{body}"})
    for c in chunks:
        for old, new in MANUAL_FIXES.items():
            if old in c["text"]:
                c["text"] = c["text"].replace(old, new)
                c["manual_fix"] = True
    for i, c in enumerate(chunks):
        c["chunk_id"] = f"wh-{i:03d}"
        c["n_words"] = len(c["text"].split())
    return chunks


# ---------------------------------------------------------------- quality check

def normalize(s):
    s = re.sub(r"[■•\x00-\x1f]", " ", s).replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", s).strip().lower()


def sentence_integrity(chunks, naive_text):
    """Share of sentences (>= 12 words) from the cleaned chunks that also appear intact in
    plain PyMuPDF page.get_text() output. A broken sentence means the naive extraction
    spliced other text (usually a sidebar) into the middle of it."""
    naive = normalize(naive_text)
    total = intact = 0
    broken_examples = []
    for c in chunks:
        if c["type"] != "rule" or c.get("manual_fix"):
            continue
        body = c["text"].split("\n", 1)[1]
        for sent in re.split(r"(?<=[.!?])\s+", body):
            if len(sent.split()) < 12:
                continue
            total += 1
            if normalize(sent) in naive:
                intact += 1
            elif len(broken_examples) < 5:
                broken_examples.append({"chunk_id": c["chunk_id"], "sentence": sent})
    return total, intact, broken_examples


# ---------------------------------------------------------------- main

def main():
    OUT_DIR.mkdir(exist_ok=True)
    doc = pymupdf.open(PDF_PATH)

    sections = build_sections(doc)
    chunks = make_chunks(sections)

    with open(OUT_DIR / "warhammer_chunks.jsonl", "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    with open(OUT_DIR / "warhammer_clean.md", "w", encoding="utf-8") as f:
        f.write("# Warhammer 40,000 Core Rules (cleaned for RAG)\n\n")
        last_section = None
        for c in chunks:
            if c["section"] != last_section:
                f.write(f"\n## {c['section']}\n\n")
                last_section = c["section"]
            pages = (f"p.{c['page_start']}" if c["page_start"] == c["page_end"]
                     else f"pp.{c['page_start']}-{c['page_end']}")
            f.write(f"### [{c['chunk_id']}] {c['type'].upper()} · {pages}\n\n{c['text']}\n\n")

    # naive baseline: plain text extraction of the same kept pages, no block ordering
    naive_text = "\n".join(p.get_text() for p in doc if p.number + 1 not in SKIP_PAGES)
    total, intact, broken = sentence_integrity(chunks, naive_text)

    words = [c["n_words"] for c in chunks]
    stats = {
        "pdf_pages": doc.page_count,
        "pages_skipped": {str(k): v for k, v in SKIP_PAGES.items()},
        "pages_kept": doc.page_count - len(SKIP_PAGES),
        "sections": len(sections),
        "chunks_total": len(chunks),
        "chunks_rule": sum(c["type"] == "rule" for c in chunks),
        "chunks_summary": sum(c["type"] == "summary" for c in chunks),
        "chunks_example": sum(c["type"] == "example" for c in chunks),
        "chunk_words_min": min(words),
        "chunk_words_median": statistics.median(words),
        "chunk_words_max": max(words),
        "corpus_words": sum(words),
        "sentence_integrity": {
            "sentences_checked": total,
            "intact_in_naive_extraction": intact,
            "broken_in_naive_extraction": total - intact,
            "broken_examples": broken,
        },
    }
    with open(OUT_DIR / "preprocessing_stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

    print(json.dumps({k: v for k, v in stats.items() if k != "pages_skipped"},
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
