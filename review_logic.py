"""Logic behind the human review step (no UI code, so it can be tested without Streamlit).

Flow: load an extraction result -> a person looks at the fields, edits what is wrong, confirms the
fields the system flagged -> the edited values are validated again -> an approval record is saved.

Rules enforced here, not in the UI:
  * every field in `needs_review` must be edited or explicitly confirmed before approval,
  * edited values go through the same validate() cross-checks as the original extraction,
  * if any validation issue is still open, the reviewer must acknowledge it explicitly,
  * an approval file is never overwritten.
"""
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from schema import FIELDS
from validate import validate


# ----------------------------------------------------------------------------- loading

def load_result(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def find_source_file(result, search_dirs):
    """Locate the original document named in the result (so the screen can show it)."""
    name = result.get("source_file")
    for d in search_dirs:
        for p in Path(d).rglob(name or "\0"):
            if p.is_file():
                return p
    return None


def field_order(result):
    """Fields to review first (lowest confidence first), then the rest in schema order."""
    flagged = sorted(result["needs_review"], key=lambda f: result["fields"][f]["confidence"])
    return flagged + [f for f in FIELDS if f not in flagged]


# ----------------------------------------------------------------------------- display

def to_display(value):
    """Turn a stored value into the text shown in an input box (and parsed back by validate())."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def find_evidence_span(text, evidence):
    """(start, end) of the evidence inside the document, tolerant of different whitespace; None if absent."""
    tokens = (evidence or "").split()
    if not tokens or not text:
        return None
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    m = re.search(pattern, text) or re.search(pattern, text, re.IGNORECASE)
    return (m.start(), m.end()) if m else None


def highlight_html(text, spans):
    """Escape the whole document first, then wrap the given (start, end) spans in <mark>."""
    spans = sorted(s for s in spans if s)
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out, pos = [], 0
    for s, e in merged:
        out.append(html.escape(text[pos:s]))
        out.append("<mark>" + html.escape(text[s:e]) + "</mark>")
        pos = e
    out.append(html.escape(text[pos:]))
    return '<div style="white-space:pre-wrap;font-family:monospace;font-size:0.85rem">' + "".join(out) + "</div>"


# ----------------------------------------------------------------------------- editing + validation

def _same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) \
            and not isinstance(b, bool):
        return abs(a - b) < 0.005
    return a == b


def revalidate(result, edited):
    """Run the normal validation on the reviewer's values.

    `edited` maps field -> text typed by the reviewer; fields not in it keep their extracted value.
    Returns (clean_values, issues) where issues is validate()'s dict {field: [messages]}.
    """
    raw = {}
    for f in FIELDS:
        text = edited[f] if f in edited else to_display(result["fields"][f]["value"])
        raw[f] = text.strip() if isinstance(text, str) and text.strip() != "" else None
    clean, _items, issues = validate(raw, result.get("line_items") or [])
    return clean, issues


def compute_changes(result, edited):
    """List of {field, from, to} for fields whose value really changed (after parsing)."""
    clean, _ = revalidate(result, edited)
    changes = []
    for f in FIELDS:
        before, after = result["fields"][f]["value"], clean.get(f)
        if not _same(before, after):
            changes.append({"field": f, "from": before, "to": after})
    return changes


def can_approve(result, edited, confirmed, issues_acknowledged, approver):
    """(ok, reasons). The UI uses this to enable the Approve button and to explain why it is disabled."""
    reasons = []
    if not (approver or "").strip():
        reasons.append("enter your name")
    changed = {c["field"] for c in compute_changes(result, edited)}
    pending = [f for f in result["needs_review"] if f not in set(confirmed) and f not in changed]
    if pending:
        reasons.append("flagged fields not reviewed yet: " + ", ".join(pending))
    _, issues = revalidate(result, edited)
    if issues and not issues_acknowledged:
        reasons.append("validation issues are still open: " + ", ".join(sorted(issues)))
    return (not reasons), reasons


# ----------------------------------------------------------------------------- approval record

def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def build_approval(result, edited, approver, confirmed, issues_acknowledged, now=None):
    """Build the approval record. Raises ValueError if the approval rules are not met."""
    ok, reasons = can_approve(result, edited, confirmed, issues_acknowledged, approver)
    if not ok:
        raise ValueError("cannot approve: " + "; ".join(reasons))
    clean, issues = revalidate(result, edited)
    when = (now or _now()).isoformat().replace("+00:00", "Z")
    return {
        "source_file": result["source_file"],
        "approved_by": approver.strip(),
        "approved_at": when,
        "fields": clean,
        "line_items": result.get("line_items") or [],
        "changes": compute_changes(result, edited),
        "confirmed_without_change": sorted(
            f for f in confirmed if f in result["needs_review"]
            and f not in {c["field"] for c in compute_changes(result, edited)}),
        "needs_review_at_extraction": list(result["needs_review"]),
        "original_confidence": {f: result["fields"][f]["confidence"] for f in FIELDS},
        "open_issues_acknowledged": issues,
        "input": result.get("input"),
    }


def save_approval(record, folder="approved"):
    """Write approved/<source>__<timestamp>.json. Never overwrites an existing file."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stem = Path(record["source_file"]).stem
    stamp = record["approved_at"].replace("-", "").replace(":", "").replace("Z", "")
    path, n = folder / f"{stem}__{stamp}.json", 1
    while path.exists():
        n += 1
        path = folder / f"{stem}__{stamp}_{n}.json"
    with open(path, "x", encoding="utf-8") as fh:     # "x": fail instead of overwriting
        json.dump(record, fh, indent=2)
    return path
