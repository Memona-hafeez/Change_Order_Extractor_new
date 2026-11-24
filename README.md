# Change Order Extractor

Reads construction change orders (text PDFs, scanned PDFs, photos) and turns them into **validated, structured data with a
confidence score and a quoted source for every field**. A person reviews whatever the system is unsure about, approves the
result, and the approved change order is sent to **QuickBooks Online** as an Estimate.

```
PDF / scan / text -> OCR if needed -> LLM extraction (x2) -> validation -> confidence -> human review -> QuickBooks
```

```jsonc
"change_amount": {
  "value": -32400.0,
  "confidence": 0.93,
  "evidence": "Credit for deleted work: ($32,400.00)",
  "issues": []
}
```

> **Status.** Extraction, OCR, review/approval, tests and the app are working. QuickBooks login is verified against a sandbox
> company; sending Estimates is implemented and unit-tested with a fake HTTP layer, and is being verified end to end.
> The full LLM accuracy evaluation on the scanned set has not been run yet. See [Limitations](#limitations).
> A written walk-through of the design is in [REPORT.md](REPORT.md).

## Features

- **Scanned input.** Documents without a text layer are rendered at 300 dpi and read with Tesseract, which returns a confidence
  per word. Poor scans lower field confidence, and a badly read document goes to review as a whole.
- **Evidence for every field.** The model must quote the text it used. The quote is checked against the document.
- **Code does the maths.** The LLM copies values exactly as written. Parsing, signs, dates and arithmetic are plain Python.
- **Cross-checks.** Contract-sum arithmetic, line-item totals, distinct party names, ambiguous dates.
- **Confidence from several signals**, not just the model's own claim (grounding, agreement between runs, validation, OCR quality).
- **Human review and approval.** Flagged fields must be edited or confirmed, edits are re-validated, every approval is saved with
  the corrections made. Nothing reaches QuickBooks without an approval record.
- **QuickBooks Online integration** (sandbox): OAuth with automatic token refresh, dry-run mode, duplicate protection.
- **One-screen app** (Streamlit): extract, review, send, dashboard.
- **147 automated tests** and a CI workflow. No API key or network needed to run them.

## Quick start

Requirements: Python 3.10+, the Tesseract OCR program, a Groq API key (for new extractions).

```bash
# Tesseract: Windows -> installer from https://github.com/UB-Mannheim/tesseract/wiki
#            Linux   -> sudo apt install tesseract-ocr      macOS -> brew install tesseract
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
cp .env.example .env                                      # then fill it in (see below)
python -m streamlit run app.py
```

`.env`:

```
GROQ_API_KEY=...                                   # default model: openai/gpt-oss-120b
QB_CLIENT_ID=...                                   # Intuit developer dashboard -> your app -> Keys (Development)
QB_CLIENT_SECRET=...
QB_REDIRECT_URI=http://localhost:8000/callback     # must also be listed in the app's Redirect URIs
QB_ENVIRONMENT=sandbox
# TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe   # only if Tesseract is not on PATH
```

Never commit `.env` or `.qb_tokens.json` (both are in `.gitignore`).

### Connect QuickBooks (once)

```bash
python qb_auth.py connect           # browser login, saves .qb_tokens.json (use --manual if the browser callback fails)
python quickbooks_client.py hello   # prints the connected company name
```

### Command line

```bash
python pipeline.py samples/some.pdf                       # extract one file -> JSON
python post_to_quickbooks.py approved/<file>.json --dry-run
python post_to_quickbooks.py approved/<file>.json --create-item
pytest                                                    # tests
```

## The app

| Tab | What it does |
|---|---|
| 1. Extract | Upload a PDF, scan (PNG/JPG) or text file, or open a saved result. |
| 2. Review and approve | Each field with its confidence, evidence and issues; the source text with the evidence highlighted; edit or confirm flagged fields; approve with your name. |
| 3. Send to QuickBooks | Pick an approved record, choose a customer, run a dry run or create the Estimate in the sandbox. |
| 4. Dashboard | Approved and pending change orders, original and revised contract value per project, outstanding invoices with days overdue (live from QuickBooks). |

## How it works

| Step | File | What it does |
|---|---|---|
| Read | `pdf_reader.py` | pdfplumber (layout preserved); Tesseract OCR for scans and images, with per-word confidence. |
| Select | `chunker.py` | Short documents pass through; long ones keep the chunks with the most dollar and keyword density. |
| Extract | `extract.py` | One call returns `{value, evidence, confidence}` per field plus line items. Run twice (temperature 0.0 and 0.3). If the page has priced rows but no line items came back (`itemized.py`), a focused second call asks for them. |
| Validate | `validate.py` | Normalises values (`$12,500.00` to `12500.0`, `(3,200)` to `-3200.0`, dates to ISO) and runs the cross-checks. |
| Score | `confidence.py` | Per-field confidence from the signals below. Fields under 0.70 go to `needs_review`. |
| Review | `review_logic.py` | Approval rules, re-validation of edits, audit record. No UI code, so it is testable. |
| Send | `qb_mapping.py`, `post_to_quickbooks.py`, `quickbooks_client.py`, `qb_auth.py` | Maps an approved record to an Estimate and posts it. |

### Confidence

| Signal | Weight | Meaning |
|---|---|---|
| Grounding | 0.40 | Is the quoted evidence really in the source? Numbers must appear in the evidence (whole-number match). |
| Agreement | 0.25 | Do two runs give the same normalised value? |
| Self-reported | 0.20 | The model's own confidence. |
| Validation | 0.15 | No failed cross-check. Any field with an issue is capped at 0.60. |
| OCR (scans) | factor 0.7 to 1.0 | Score is multiplied by `0.7 + 0.3 * q` (q = mean word confidence in the evidence). If the whole scan averages below 0.80, every field is capped at 0.65 and goes to review. |

`signed` is capped unless the text has `/s/` or a statement that something was signed. A printed name is not a signature.

### QuickBooks mapping

| Change order | QuickBooks Estimate |
|---|---|
| Owner | Customer (must already exist, or pass `--customer`) |
| Change order number `CO-012` | `DocNumber` `CO-12` (also used to detect duplicates) |
| Change amount, description | One line, or one line per item when the items add up to the change amount |
| Date | `TxnDate` (left out if not a valid date) |
| Approver, time, corrections | `PrivateNote` |
| Status | `Pending` (an Estimate does not post to the ledger) |

Safety: sandbox only unless `--allow-production`; an existing Estimate with the same `DocNumber` or an entry in `qb_posted.json`
blocks a second post; a 401 refreshes the token once; 429 backs off; a failed POST is never retried blindly.

## Evaluation

Synthetic data only. Degradation (blur, noise, rotation, JPEG) is simulated, not real faxes or phone photos.

**Early run, 6 documents x 11 fields** (before the OCR, grounding, line-item and `signed` changes; not re-run since):
97 to 100% field accuracy across three runs, wrong answers scored lower than right ones (avg 0.30 to 0.53 vs 0.95 to 0.96).
Six mostly synthetic files that I tuned against: a sanity check, not a benchmark.

**OCR recall, 30 generated documents (no LLM involved)**, Windows, Tesseract 5.4.0:

| Variant | DEV key values recovered | TEST key values recovered | Mean OCR confidence (dev / test) |
|---|---|---|---|
| text layer | 48/48 | 16/16 | n/a |
| scan_clean | 40/40 | 23/24 | 0.93 / 0.95 |
| scan_noisy | 32/32 | 24/24 | 0.94 / 0.96 |
| scan_bad | 11/40 (28%) | 2/16 (12%) | 0.71 / 0.55 |

OCR is reliable up to moderate degradation and collapses on heavily degraded scans, which is why a poorly read document is sent
to review as a whole instead of being trusted field by field.

## Limitations

- **No end-to-end accuracy number for scans yet.** `python eval.py eval_set/dev` (full pipeline, needs an API key) has not been run.
- **The 0.80 OCR gate was chosen on synthetic scans.** TEST per-document values were looked at during diagnosis, so TEST is no longer a clean hold-out for that threshold.
- **Confidence weights are hand-set**, not fitted. Two runs of the same model share blind spots, so agreement is not independence.
- **Real documents are untested**: faxes, phone photos, stamps, handwriting, multi-column tables.
- **Signature images** leave no text, so a genuinely signed scan can be flagged for review unnecessarily.
- **QuickBooks**: behaviour of credit (negative) amounts, item and income-account setup and DocNumber settings must be confirmed per company. Sandbox only; there is no production hardening.
- **Cost**: two LLM calls per document (three when line items are re-requested).

## Roadmap

1. Run the full evaluation on the scanned set and report accuracy per input type and auto-accepted vs reviewed error rates.
2. Replace the second same-model run with a different model for a more independent agreement signal.
3. Hand-label real change orders; fit and calibrate the confidence weights and the OCR gate.
4. Detect signature images; compare Tesseract with a vision model on degraded scans.
5. Payments: Stripe, as a later extension. Retainage and lien waivers are out of scope for now.

## Project layout

```
app.py                  Streamlit app (extract, review, send, dashboard)
pipeline.py             orchestration + CLI              extract.py       prompt + LLM calls
pdf_reader.py           PDF / OCR / text input           validate.py      normalisation + cross-checks
chunker.py              long-document selection          confidence.py    per-field scoring
itemized.py             priced-row detection             schema.py        pydantic model
review_logic.py         approval rules + audit record    qb_auth.py       OAuth + token store
qb_mapping.py           record -> Estimate               quickbooks_client.py   API client
post_to_quickbooks.py   safe posting CLI                 eval.py, ocr_check.py  evaluation
make_samples.py, make_eval_set.py   synthetic data       tests/           pytest (LLM and HTTP mocked)
```
