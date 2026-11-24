# Change-Order Extraction Pipeline

Turns messy construction change-order PDFs or plain text into **validated JSON with a
confidence score, supporting evidence and validation issues for every field**, and tells you
which fields a human should check.

```jsonc
"change_amount": {
  "value": -32400.0,
  "confidence": 0.93,
  "evidence": "Credit for deleted work: ($32,400.00)",
  "issues": []
}
```

## Quick start

```bash
sudo apt install tesseract-ocr   # for scanned PDFs / images (macOS: brew install tesseract)
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env            # add GROQ_API_KEY (default model: openai/gpt-oss-120b)
python make_samples.py          # builds 5 synthetic samples + expected JSON
python pipeline.py samples      # extract all samples -> output/*.json
python pipeline.py some.pdf     # one file (text PDF, scanned PDF, image or .txt)
python eval.py                  # field accuracy + review workload vs samples/expected

pytest                          # 75 tests, no API key needed (the LLM is mocked)
python make_eval_set.py         # 30 synthetic docs incl. scan-like copies -> eval_set/dev, eval_set/test
python ocr_check.py eval_set/dev   # OCR quality on that set, also without any API key
python eval.py eval_set/dev     # full pipeline on the DEV split (needs the API key)
```

## Approach

```
PDF / scan / text -> read (OCR if needed) -> select relevant chunks -> LLM (x2) -> validate -> score -> JSON
```

| Step | File | What it does |
|---|---|---|
| Read | `pdf_reader.py` | pdfplumber with `layout=True` to keep label/value columns aligned. If a PDF has no text layer (or the input is an image), pages are rendered at 300 dpi and read with Tesseract, which also returns a confidence for every word. |
| Select | `chunker.py` | Short docs pass through. Long docs (>12k chars) are split into overlapping chunks and the highest keyword/dollar-density chunks are kept. |
| Extract | `extract.py` | One LLM call returns `{value, evidence, confidence}` per field plus line items. Prompt rules: evidence must be a verbatim quote, never guess or calculate, "unsigned" (false) differs from "no mention" (null). Called twice (temperature 0.0 and 0.3). If the document has priced rows but the model returned no line items (`itemized.py`), a second focused call asks for them so the sum check can run. |
| Validate | `validate.py` | Deterministic code, not the LLM, normalizes values: `$12,500.00` -> `12500.0`, `(3,200)` -> `-3200.0`, dates -> ISO. Then cross-checks (below). |
| Score | `confidence.py` | Per-field confidence from four signals plus an OCR factor for scans (below). Fields under 0.70 go to `needs_review`. |

### Design decisions

1. **The LLM copies, code computes.** The model is told to return values exactly as written.
   Parsing, sign handling and arithmetic are done in plain Python, so a language model never
   has to do maths or reformat dates.
2. **Abstain rather than guess.** Missing or ambiguous fields come back `null` with low
   confidence and land in `needs_review`. A silent wrong value is worse than a flagged blank.
3. **Confidence is built mostly from signals the model cannot fake.**

   | Signal | Weight | Meaning |
   |---|---|---|
   | Grounding | 0.40 | Is the quoted evidence really in the source (fuzzy match)? Money and day-count fields must also contain the number: full credit if it is inside the quoted evidence, half credit if it only appears elsewhere in the document (whole-number match, so `5` does not match `15` or `5.25`). |
   | Agreement | 0.25 | Do two independent runs give the same normalized value? |
   | Self-reported | 0.20 | The model's own confidence (deliberately the smallest-but-one weight). |
   | Validation | 0.15 | No failed cross-check. Any field with an issue is **capped at 0.60**. |
   | OCR (scans only) | factor 0.7-1.0 | Score is multiplied by `0.7 + 0.3 * q`, where `q` is the mean Tesseract confidence of the words in the field's evidence. If the whole scan averages below 0.80, every field is capped at 0.65 and goes to review. |

   `signed` is also capped (via the validation path) unless the text contains `/s/` or the word "signed".

4. **Cross-field validation catches errors a single field cannot show.**
   - `original (+ previous) + this change == new contract sum`
   - line item `qty * unit_price == total`, and `sum(line items) == change amount`
   - owner / contractor / project name must not collapse to the same string
   - ambiguous dates (`04/03/2026`) are parsed as MM/DD, flagged, and sent to review
   - final pydantic schema check

## Results

`python eval.py` over 6 documents x 11 scored fields (free-text description is not scored):
5 synthetic samples covering a clean PDF, a credit with an ambiguous date, OCR-style noise
(`0RDER`, non-standard labels), an email-style text file and a draft with missing values, plus
one PDF imitating the AIA G701 form layout (AI-generated, not a scan of a real project).

| Run | Accuracy | Avg conf. correct | Avg conf. wrong | Wrong but confident (>=0.7) |
|---|---|---|---|---|
| 1 | 65/66 (98%) | 0.96 | 0.30 | 0 |
| 2 | 64/66 (97%) | 0.95 | 0.53 | 1 |
| 3 | 66/66 (100%) | 0.95 | n/a | 0 |

What the runs showed:
- Wrong answers consistently scored lower than right ones, so the confidence ordering is useful.
- **Results vary between runs** (LLM sampling), so I report the range, not the best run.
- Run 1's miss: sample3 labels the base amount "Prev. contract sum" instead of "original".
  The model abstained (`null`, 0.30) rather than guessing, which is the intended behaviour. I then
  added a prompt rule for this label variant, which is why run 3 passes.
- Run 2's miss was the dangerous kind: an email calls the project "Lakeside School gym" in the
  subject and "Lakeside School Gym Renovation" in the body; the model picked the short form at 0.75.

**Honest caveat:** six documents, mostly synthetic, and I tuned the prompt and validators while
looking at these same files. 97-100% is a sanity check for the pipeline, not a benchmark of real-world accuracy.
These runs were made **before** the OCR / grounding / line-item / `signed` changes below and have not been re-run.

### OCR on scanned input (no LLM involved)

`make_eval_set.py` generates 30 synthetic change orders (3 layouts, credits, line items, signed / unsigned)
in four variants, and `ocr_check.py` measures how many of the printed key values (names, dates, amounts) survive reading.
Amounts are compared as numbers; names must appear verbatim.

| Variant | What it is | DEV recovered | TEST recovered | Mean OCR conf (dev / test) |
|---|---|---|---|---|
| text | normal PDF with text layer | 48/48 (100%) | 16/16 (100%) | n/a |
| scan_clean | rasterised, no text layer | 40/40 (100%) | 23/24 (96%) | 0.93 / 0.95 |
| scan_noisy | rotation, blur, noise, JPEG at 150 dpi | 32/32 (100%) | 24/24 (100%) | 0.95 / 0.96 |
| scan_bad | stronger degradation at 110 dpi | 14/40 (35%) | 3/16 (19%) | 0.69 / 0.55 |

- OCR is reliable up to moderate degradation and **collapses on heavily degraded scans**: only about a fifth to a third of the key
  values survive. That is why a badly read scan is not trusted field by field; the whole document goes to review.
- The 0.80 gate was picked on the DEV split (readable scans averaged >= 0.89, bad ones 0.64-0.73). **I looked at the TEST
  per-document numbers while diagnosing this, so TEST is no longer a clean held-out set for that threshold.**
- Not yet run: `python eval.py eval_set/dev` (the full LLM pipeline on these files). It needs an API key and the free-tier rate limit
  makes it slow, so there is no end-to-end accuracy number for the scanned set yet.

## Failure modes

Known and not fixed unless stated.

**Input**

| Failure | Behaviour |
|---|---|
| Scanned / image-only PDF | Read with Tesseract. Fine on clean and moderately noisy synthetic scans, poor on heavily degraded ones (see above), which are sent to review. Real faxes, phone photos, stamps and handwriting are untested. |
| OCR reading order | Tesseract is run line by line (`--psm 6`); multi-column or table layouts can interleave columns. |
| Ambiguous date (`04/03/2026`) | Assumes MM/DD, flags it, caps confidence at 0.60 so it gets reviewed. |
| Multi-column layouts | Value can be read from the wrong column (e.g. checkbox text `OWNER X` taken as a name). Prompt rules and the distinct-parties check reduce this, not eliminate it. |
| Long documents | Keyword chunk selection could drop the page that holds the totals. Only applies above 12k chars, untested. |
| Unseen label wording | "Prev. contract sum", "Revised sum", etc. Handled for the cases I saw; new variants will tend to return `null` and be flagged. |

**Checks that are weaker than they look**

| Failure | Detail |
|---|---|
| Number grounding | Fixed to check the quoted evidence with whole-number matching. Remaining gap: a number inside the *right* snippet can still be the *wrong* number, and the check cannot tell. |
| Line items | The AIA-style sample used to return none, so the sum check never ran. A focused second call now fills them in when the page has priced rows, but the "looks itemized" test is a regex heuristic and can miss unusual layouts. Not yet measured on the LLM. |
| Same entity, several spellings | Seen in eval (project name above). A "use the most complete name in the body" prompt rule was added. Not yet measured. |
| Disagreement alone does not force review | If every other signal is perfect, two runs that disagree still score 0.74, above the 0.70 threshold. Free-text fields disagree often, so a hard cap would flag them constantly. |

**Confidence**

| Failure | Detail |
|---|---|
| `signed` | Printed names under a signature block used to score 1.0. Now capped at 0.60 unless the text has `/s/` or "signed". A signature *image* on a scan is not detected, so a genuinely signed scan can be flagged for review unnecessarily. |
| Weights are hand-set | 0.20 / 0.40 / 0.25 / 0.15 were chosen by reasoning, not fitted. With 66 data points they cannot be calibrated. |
| Agreement is not independence | Both runs use the same model and share blind spots, so agreeing does not prove correctness. |

**Practical**

| Failure | Detail |
|---|---|
| Cost / latency | Two LLM calls per document. Fine here, wasteful at volume. |
| Free-tier rate limits | `eval.py` sleeps 15 s between files. |
| Sample realism | All data is synthetic. Degradation is simulated (blur, noise, rotation, JPEG), not real faxes or photos. |
| Re-tuning | Changes in this round (grounding, `signed`, line items, OCR factor) were unit-tested with a mocked LLM but their effect on real model output has not been measured. |

## What I would do next

1. Run `python eval.py eval_set/dev` and `eval_set/test` with the real model, and report accuracy per input variant plus the
   auto-accepted vs reviewed error rates that `eval.py` prints.
2. Replace the second same-model run with a different model for a more independent agreement signal.
3. Test on real, hand-labelled change orders, then fit and calibrate the confidence weights and the OCR gate
   (reliability curve) instead of hand-setting them.
4. Detect signature images on scans, and compare Tesseract with a vision model for the degraded cases.
5. Push approved extractions into an accounting system (QuickBooks sandbox) behind a human review step.

## Layout

```
pipeline.py     orchestration, CLI        extract.py     prompt + LLM calls
pdf_reader.py   PDF / OCR / text input    validate.py    normalisation + cross-checks
chunker.py      long-document selection   confidence.py  per-field scoring
itemized.py     priced-row detection      schema.py      pydantic model, fields
eval.py         accuracy + review load    ocr_check.py   OCR recall, no LLM
make_samples.py hand-made samples         make_eval_set.py  30 synthetic docs, dev/test split
tests/          pytest, LLM mocked        samples/ eval_set/  inputs + expected/ ground truth
```
