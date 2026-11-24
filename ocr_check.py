"""Measure OCR quality on the generated eval set WITHOUT calling any LLM (no API key needed).

    python ocr_check.py eval_set/dev
    python ocr_check.py eval_set/test

For each document it reads the file through the real input path (text layer or OCR) and checks how many of
the printed key values (names, dates, amounts) are recovered. Amounts are compared as numbers, so
"$12,500.00" and "12500" count as the same; names must appear verbatim after whitespace/case normalisation.
This isolates the OCR step: it says how much of the document the LLM can even see correctly.
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from pdf_reader import read_document
from confidence import number_in_text
from validate import parse_money

MONEY_KEYS = {"original_contract_sum", "change_amount", "new_contract_sum"}
VARIANT = re.compile(r"_(text|scan_clean|scan_noisy|scan_bad)$")


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def found(key, printed, text):
    if key in MONEY_KEYS:
        return number_in_text(parse_money(printed), text)
    return norm(printed) in norm(text)


def main(folder, per_doc=False):
    folder = Path(folder)
    stats = defaultdict(lambda: {"ok": 0, "n": 0, "conf": [], "docs": 0})
    misses = []
    for pdf in sorted(folder.glob("*.pdf")):
        exp = json.loads((folder / "expected" / f"{pdf.stem}.json").read_text())
        doc = read_document(pdf)
        v = VARIANT.search(pdf.stem).group(1)
        s = stats[v]
        s["docs"] += 1
        if doc.mean_ocr_confidence is not None:
            s["conf"].append(doc.mean_ocr_confidence)
        doc_ok = 0
        for key, printed in exp["_rendered"].items():
            s["n"] += 1
            if found(key, printed, doc.text):
                s["ok"] += 1
                doc_ok += 1
            else:
                misses.append((pdf.name, key, printed))
        if per_doc and doc.ocr_used:
            n = len(exp["_rendered"])
            print(f"  {pdf.stem:22s} mean OCR conf {doc.mean_ocr_confidence:.2f}   recovered {doc_ok}/{n}")
    print(f"{'variant':12s} {'docs':>4s} {'key values recovered':>22s} {'mean OCR conf':>14s}")
    tot_ok = tot_n = 0
    for v in ("text", "scan_clean", "scan_noisy", "scan_bad"):
        s = stats.get(v)
        if not s:
            continue
        tot_ok += s["ok"]
        tot_n += s["n"]
        conf = f"{sum(s['conf']) / len(s['conf']):.2f}" if s["conf"] else "n/a (text layer)"
        print(f"{v:12s} {s['docs']:4d} {s['ok']:>8d}/{s['n']:<4d} = {s['ok'] / s['n']:>4.0%} {conf:>14s}")
    print(f"{'ALL':12s} {'':4s} {tot_ok:>8d}/{tot_n:<4d} = {tot_ok / tot_n:>4.0%}")
    if misses:
        print("\nValues not recovered:")
        for m in misses:
            print("  ", m)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0] if args else "eval_set/dev", per_doc="--per-doc" in sys.argv)
