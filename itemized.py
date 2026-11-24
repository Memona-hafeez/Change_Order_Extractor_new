"""Detect documents that list priced rows, so we can insist on line-item extraction.

Without line items the 'sum of line items == change amount' cross-check never runs
(this happened on the AIA-style sample), so a missing list on an itemized document is a silent gap.

Two row shapes are recognised:
  * quantity x unit price = total   (two or more decimal amounts on one line)
  * description ......... amount    (a line of words ending in one amount, e.g. "PCO-014 Add generator pad 18,450.00")
Lines that look like contract summary lines (contract sum, previous changes, total, ...) are ignored.
"""
import re

MONEY = re.compile(r"\d[\d,]*\.\d{2}\b")
AMOUNT_AT_END = re.compile(r"\(?\$?\s?\d[\d,]*\.\d{2}\)?\s*$")
# Whole words only: "Subcontractor markup" is a priced row, not a contract summary line.
SUMMARY_WORDS = re.compile(
    r"\b(?:contract|previous|prior|net change|this change|this co|total|adjusted|revised|original|balance|sum)\b",
    re.IGNORECASE)


def itemized_rows(text):
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s or SUMMARY_WORDS.search(s):
            continue
        qty_price_total = len(MONEY.findall(s)) >= 2
        described_amount = bool(AMOUNT_AT_END.search(s)) and len(re.findall(r"[A-Za-z]{3,}", s)) >= 2
        if qty_price_total or described_amount:
            rows.append(s)
    return rows


def looks_itemized(text, min_rows=2):
    return len(itemized_rows(text)) >= min_rows
