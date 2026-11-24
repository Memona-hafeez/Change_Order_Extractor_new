import re
from difflib import SequenceMatcher
from schema import MONEY_FIELDS

W_SELF, W_GROUND, W_AGREE, W_VALID = 0.20, 0.40, 0.25, 0.15
REVIEW_THRESHOLD = 0.70
ISSUE_CAP = 0.60
NUMERIC_DAY_FIELDS = ("schedule_change_days",)

# Printed names under a signature block are not signatures. We only trust explicit markers.
SLASH_S = re.compile(r"/s/", re.IGNORECASE)
SIGNED_WORD = re.compile(r"\b(?:e-?|digitally |electronically )?signed\b", re.IGNORECASE)
# 'signed' only counts when it states that something WAS signed. Boilerplate such as
# "NOT VALID UNTIL SIGNED BY THE OWNER" or "to be signed" or "Signed: ______" does not.
NOT_A_STATEMENT = {"until", "when", "once", "if", "unless", "before", "after", "upon", "be", "not", "yet",
                   "must", "shall", "cannot", "never", "without"}
BLANK_AFTER = re.compile(r"\s*[:\-]?\s*(?:_{3,}|\.{3,}|\[\s*\])")


def _norm(s):
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def evidence_score(evidence, source):
    """1.0 if the quoted evidence exists in the document verbatim, else the best partial overlap."""
    if not evidence:
        return 0.0
    e, s = _norm(evidence), _norm(source)
    if e in s:
        return 1.0
    m = SequenceMatcher(None, e, s, autojunk=False).find_longest_match(0, len(e), 0, len(s))
    return round(m.size / len(e), 2)


def number_in_text(value, text):
    """True if `value` appears in `text` as a whole number (not inside a longer number).

    '5' does not match '15' or '5.25'; '12500' matches '$12,500.00'. Commas are ignored.
    """
    if value is None or not text:
        return False
    flat = str(text).replace(",", "")
    v = abs(float(value))
    candidates = [f"{v:.2f}"]
    if v == int(v):
        candidates.append(f"{int(v)}")
    for c in candidates:
        pattern = r"(?<![\d.])" + re.escape(c) + r"(?!\d)(?!\.\d*[1-9])"
        if re.search(pattern, flat):
            return True
    return False


def number_grounding(value, evidence, source):
    """1.0 if the number is inside the quoted evidence, 0.5 if only elsewhere in the document, else 0.

    Checking the evidence span (not the whole document) matters for small numbers: '5' days is
    present somewhere in almost any document, so a whole-document check proves very little.
    """
    if number_in_text(value, evidence):
        return 1.0
    if number_in_text(value, source):
        return 0.5
    return 0.0


def has_signature_evidence(source):
    """True if the text contains '/s/' or a statement that something was signed.

    'unsigned', conditions ("not valid until signed", "to be signed") and blank lines ("Signed: ____") do not count.
    A handwritten signature image leaves no text, so a genuinely signed scan can still return False.
    """
    text = source or ""
    if SLASH_S.search(text):
        return True
    for m in SIGNED_WORD.finditer(text):
        before = re.findall(r"[a-z']+", text[max(0, m.start() - 40):m.start()].lower())[-3:]
        if any(w in NOT_A_STATEMENT for w in before):
            continue
        if BLANK_AFTER.match(text, m.end()):
            continue
        return True
    return False


def ocr_quality(evidence, ocr_words):
    """Mean OCR confidence (0-1) of the words in the evidence snippet. None when OCR was not used."""
    if ocr_words is None:
        return None
    if not evidence:
        return 0.0

    def tok(w):
        return re.sub(r"[^a-z0-9.]", "", str(w).lower())

    best = {}
    for word, conf in ocr_words:
        t = tok(word)
        if t:
            best[t] = max(best.get(t, 0.0), conf)
    confs = [best[t] for t in (tok(w) for w in str(evidence).split()) if t in best]
    if not confs:
        return 0.0
    return round(sum(confs) / len(confs) / 100.0, 3)


def field_confidence(field, value, evidence, self_conf, agreement, has_issue, source, ocr_q=None):
    if value is None:
        # Model says "not present": trust more if both runs agree
        return 0.8 if agreement else 0.3
    ground = evidence_score(evidence, source)
    if field in MONEY_FIELDS:
        ground = 0.5 * ground + 0.5 * number_grounding(value, evidence, source)
    elif field in NUMERIC_DAY_FIELDS and value != 0:
        ground = 0.5 * ground + 0.5 * number_grounding(value, evidence, source)
    score = (W_SELF * self_conf + W_GROUND * ground
             + W_AGREE * (1.0 if agreement else 0.0)
             + W_VALID * (0.0 if has_issue else 1.0))
    if ocr_q is not None:
        # Scanned input: shaky OCR on the evidence words lowers trust (hand-chosen factor 0.7..1.0).
        score *= 0.7 + 0.3 * ocr_q
    if has_issue:
        score = min(score, ISSUE_CAP)  # validation failure => always goes to human review
    return round(max(0.0, min(1.0, score)), 2)
