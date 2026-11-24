import sys
import json
from pathlib import Path
from schema import FIELDS
from pdf_reader import read_document, IMAGE_SUFFIXES
from extract import call_llm, call_llm_line_items
from validate import validate, canon
from confidence import (field_confidence, REVIEW_THRESHOLD, has_signature_evidence, ocr_quality)
from itemized import looks_itemized

# Document-level OCR gate. Chosen on the DEV split only: on synthetic scans, readable ones had a mean word
# confidence >= 0.89 and the badly degraded ones 0.64-0.73 (where only ~12-50% of key values survived OCR).
# 0.80 sits in the gap. Real scans will differ, so treat it as a starting point, not a calibrated value.
LOW_OCR_QUALITY = 0.80
LOW_OCR_CAP = 0.65              # below the review threshold: every field of a badly read scan goes to a human
SUFFIXES = (".pdf", ".txt") + IMAGE_SUFFIXES


def _node(run, f):
    n = run.get(f)
    return n if isinstance(n, dict) else {"value": n, "evidence": None, "confidence": 0.5}


def _num(x, default=0.5):
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return default


def process(path):
    doc = read_document(path)
    text = doc.text
    run1 = call_llm(text, temperature=0.0)
    run2 = call_llm(text, temperature=0.3)   # second opinion for agreement check

    line_items = run1.get("line_items") or []
    doc_notes = []
    if not line_items and looks_itemized(text):
        # The document lists priced rows but the main call returned none: ask once more, focused,
        # so the sum-of-line-items cross-check can run.
        line_items = call_llm_line_items(text)
        doc_notes.append("line items were missing from the main extraction and were requested separately"
                         if line_items else "document looks itemized but no line items could be extracted")

    raw = {f: _node(run1, f).get("value") for f in FIELDS}
    clean, items, issues = validate(raw, line_items)

    # Printed names under a signature block are not signatures: no marker => cap via the issue path.
    if clean.get("signed") is True and not has_signature_evidence(text):
        issues.setdefault("signed", []).append(
            "no signature marker ('/s/' or 'signed') found; a printed name is not a signature")

    mean_ocr = doc.mean_ocr_confidence
    low_ocr = False
    if doc.ocr_used:
        doc_notes.append(f"scanned input read with OCR (mean word confidence {mean_ocr:.2f})")
        low_ocr = mean_ocr < LOW_OCR_QUALITY
        if low_ocr:
            doc_notes.append("low OCR quality: every field is sent to review; check against the original")

    fields = {}
    for f in FIELDS:
        n1, n2 = _node(run1, f), _node(run2, f)
        agreement = canon(f, n1.get("value")) == canon(f, n2.get("value"))
        ocr_q = ocr_quality(n1.get("evidence"), doc.ocr_words) if doc.ocr_used else None
        conf = field_confidence(
            f, clean.get(f), n1.get("evidence"), _num(n1.get("confidence")),
            agreement, f in issues, text, ocr_q=ocr_q)
        field_issues = list(issues.get(f, []))
        if low_ocr:
            conf = min(conf, LOW_OCR_CAP)
            field_issues.append("low OCR quality on this scan")
        fields[f] = {"value": clean.get(f), "confidence": conf,
                     "evidence": n1.get("evidence"), "issues": field_issues}

    return {
        "source_file": Path(path).name,
        "input": {"ocr_used": doc.ocr_used, "mean_ocr_confidence": mean_ocr},
        "fields": fields,
        "line_items": items,
        "document_issues": issues.get("line_items", []) + issues.get("_document", []) + doc_notes,
        "needs_review": [f for f, d in fields.items() if d["confidence"] < REVIEW_THRESHOLD],
    }


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
    files = sorted(p for p in ([target] if target.is_file() else target.glob("*"))
                   if p.suffix.lower() in SUFFIXES)
    Path("output").mkdir(exist_ok=True)
    for f in files:
        print(f"Processing {f.name} ...")
        result = process(f)
        (Path("output") / f"{f.stem}.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
