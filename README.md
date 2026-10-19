# Warhammer 40,000 Core Rules: cleaned for the RAG assignment

This folder turns the Warhammer 40,000 Core Rules PDF into clean, retrieval-ready chunks for the
"Baseline vs. Re-Ranking" RAG assignment.

## Files

| File | What it is |
|---|---|
| `prepare_warhammer.py` | Main script: PDF → cleaned chunks |
| `compare_extraction.py` | Measures how much naive extraction breaks the text, compared with the cleaned chunks |
| `requirements.txt` | `pymupdf` (the only dependency) |
| `output/warhammer_chunks.jsonl` | **The corpus to embed**: one chunk per line |
| `output/warhammer_clean.md` | The same chunks as a readable document, for checking by eye |
| `output/preprocessing_stats.json` | Chunk counts, sizes, skipped pages |
| `output/extraction_comparison.json` | Sentence-integrity results for the naive extractors |

## Run

```bash
pip install -r requirements.txt
python prepare_warhammer.py
python compare_extraction.py
```

## Using the chunks in the notebook

```python
import json
chunks = [json.loads(l) for l in open("output/warhammer_chunks.jsonl", encoding="utf-8")]
texts = [c["text"] for c in chunks]          # embed these with the bi-encoder
```

Each chunk has these fields:

| Field | Meaning |
|---|---|
| `chunk_id` | `wh-000` … `wh-293`. Use it in rank-shift tables ("wh-091 moved from #7 to #1") |
| `type` | `rule` (main text), `summary` (the ■ quick-reference boxes) or `example` (worked examples printed on diagrams) |
| `heading` | e.g. `SHOOTING PHASE › BIG GUNS NEVER TIRE`, `RUINS › BENEFIT OF COVER` |
| `section` | chapter, from the page footer (e.g. `Shooting Phase`) |
| `page_start`, `page_end` | PDF pages the chunk comes from |
| `text` | the heading plus the chunk body. This is what to embed |
| `flavour` | lore text kept out of `text` (not needed for retrieval) |
| `manual_fix` | present on the one chunk whose values were added by hand (see Limitations) |

Index all three types. The `summary` chunks restate nearby rules in short form, so they act as
natural near-duplicate distractors, which is useful for the keyword-trap failure queries.

## What was wrong with the PDF, and what the script does

| Problem | Fix |
|---|---|
| Two columns plus sidebar boxes, so plain extraction interleaves them (`pdftotext -layout` breaks **66.5%** of sentences, `pdftotext` **19.6%**) | Read text block by block and find columns per page by clustering the blocks' left edges. Read each column top to bottom. |
| Sidebar exceptions (e.g. *Big Guns Never Tire*) merged into the main rule | Split into sections at headings, so each sidebar is its own chunk |
| ■ summary boxes split across blocks, with bullets cut in half | Find the boxes by their grey-green background rectangle and keep the whole box together as a `summary` chunk |
| Non-rule pages: cover, artwork, weapon illustrations, example datasheet, battlefield maps | Skipped (10 pages, reasons listed in `SKIP_PAGES` / `preprocessing_stats.json`) |
| Noise: 46 page footers, 199 digit-only lines (page numbers, step callouts), 209 control-character glyphs, diagram labels (`1½"`, `A`, `B`), vertical decorative text | All filtered: **0** footers, **0** digit-only lines and **0** control glyphs remain |
| Repeated sub-headings with no context (six "BENEFIT OF COVER" sections, one per terrain type) | Sub-headings carry their parent title: `CRATERS AND RUBBLE › BENEFIT OF COVER`, `RUINS › BENEFIT OF COVER` |
| Wound Roll table values (2+…6+) are drawn as vector graphics, so no extractor can read them | Restored through a small `MANUAL_FIXES` table (flagged with `manual_fix: true`) |

## Result

| | |
|---|---|
| PDF pages | 60 (50 kept) |
| Chunks | **294**: 217 rule, 66 summary, 11 example |
| Chunk size | median 62 words, max 299 |
| Corpus | about 23k words |

Sentence integrity: each of the 644 longer sentences in the cleaned rule chunks was checked
against the naive extractions of the same pages.

| Extractor | Sentences broken |
|---|---|
| `pdftotext -layout` | 428 / 644 (66.5%) |
| `pdftotext` (default) | 126 / 644 (19.6%) |
| PyMuPDF `page.get_text()` | 2 / 644 (0.3%) |

PyMuPDF's plain text keeps sentences whole, but it is still one undivided stream per page. It
contains all the footers, callout digits and glyph noise listed above, keeps summary boxes and
sidebars mixed into the main rule, and has no headings or sections to chunk by. The script uses
PyMuPDF and adds the layout and structure handling on top.

## Good chunks for failure queries

- `SHOOTING PHASE › Locked in Combat` (general ban) vs. `SHOOTING PHASE › BIG GUNS NEVER TIRE` (Monster/Vehicle exception)
- "A unit cannot shoot … in the same turn that it Advanced" vs. `WEAPON ABILITIES › ASSAULT` (exception)
- `PISTOL`: another exception to the Engagement Range ban
- `HAZARDOUS`: nested exception (Character/Monster/Vehicle take 3 mortal wounds instead)
- `NORMAL` / `ADVANCE` / `FALL BACK MOVES`: near-duplicate wording (keyword trap)
- `… › BENEFIT OF COVER` × 6 terrain types: near-duplicates that differ only by terrain

## Limitations

1. **Wound Roll values were added by hand.** Check chunk `wh-098` against page 21 of the PDF.
2. Long sections are split at about 250 words, so a few rules span two chunks (e.g. *Big Guns Never Tire*: `wh-091` is the rule, `wh-092` its Designer's Note).
3. On page 49 (*Example Battlefields*, `wh-241`), one intro sentence runs into a map caption. Minor, since that page holds no rules.
4. A few parent labels are imperfect (e.g. `ARMIES › KEYWORDS` on page 5, where the true parent is *Core Concepts*). It doesn't affect the rule text.
5. The PDF is © Games Workshop. Keep it out of a public repo; link to it instead.
