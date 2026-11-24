import json
from pathlib import Path
import pytest
import review_logic as rl
from schema import FIELDS

DOC = "CHANGE ORDER NO. 003\nOwner: Riverside Health\n  Partners LLC\nThis Change Order: $12,500.00\n<b>x</b>"


def result(**over):
    """A result shaped like pipeline output: 12,500 change, one flagged field (owner)."""
    vals = {"project_name": "Riverside Medical Center", "change_order_number": "003", "date": "2026-03-14",
            "contractor": "Apex Builders Inc.", "owner": "Riverside Health", "description_of_change": "outlets",
            "original_contract_sum": 1250000.0, "previous_changes_total": 0.0, "change_amount": 12500.0,
            "new_contract_sum": 1262500.0, "schedule_change_days": 5, "signed": True}
    vals.update(over)
    fields = {f: {"value": vals[f], "confidence": 0.95, "evidence": None, "issues": []} for f in FIELDS}
    fields["owner"]["confidence"] = 0.52
    return {"source_file": "co.txt", "input": {"ocr_used": False, "mean_ocr_confidence": None},
            "fields": fields, "line_items": [], "document_issues": [], "needs_review": ["owner"]}


# ---- evidence + highlighting
def test_find_evidence_tolerates_different_whitespace():
    s, e = rl.find_evidence_span(DOC, "Owner: Riverside Health Partners LLC")
    assert DOC[s:e].startswith("Owner:") and DOC[s:e].endswith("LLC")


def test_find_evidence_missing_or_empty_is_none():
    assert rl.find_evidence_span(DOC, "Contractor: Zenith") is None
    assert rl.find_evidence_span(DOC, None) is None
    assert rl.find_evidence_span(DOC, "") is None


def test_find_evidence_falls_back_to_case_insensitive():
    assert rl.find_evidence_span(DOC, "this change order: $12,500.00") is not None


def test_highlight_escapes_document_html():
    out = rl.highlight_html(DOC, [rl.find_evidence_span(DOC, "$12,500.00")])
    assert "<b>x</b>" not in out and "&lt;b&gt;x&lt;/b&gt;" in out
    assert "<mark>$12,500.00</mark>" in out


def test_highlight_merges_overlapping_spans():
    out = rl.highlight_html("abcdefgh", [(1, 4), (3, 6)])
    assert out.count("<mark>") == 1 and "<mark>bcdef</mark>" in out


# ---- display round trip
def test_to_display_round_trips_through_validation():
    r = result(change_amount=-32400.0, new_contract_sum=1217600.0)
    clean, issues = rl.revalidate(r, {})
    assert clean["change_amount"] == -32400.0 and clean["signed"] is True and issues == {}


def test_to_display_values():
    assert rl.to_display(None) == "" and rl.to_display(True) == "true" and rl.to_display(5) == "5"
    assert rl.to_display(12500.0) == "12500.00"


# ---- revalidation after edits
def test_edit_that_breaks_the_arithmetic_is_reported_on_the_money_fields():
    _, issues = rl.revalidate(result(), {"change_amount": "99,999.00"})
    for f in ("original_contract_sum", "change_amount", "new_contract_sum"):
        assert any("!=" in m for m in issues[f])


def test_unparseable_edit_is_reported():
    _, issues = rl.revalidate(result(), {"change_amount": "see attached"})
    assert "change_amount" in issues


def test_clearing_a_field_makes_it_null():
    clean, _ = rl.revalidate(result(), {"owner": "  "})
    assert clean["owner"] is None


def test_changes_list_only_contains_real_changes():
    r = result()
    assert rl.compute_changes(r, {}) == []
    # "12500" parses to the same number as the stored 12500.0: not a change
    assert rl.compute_changes(r, {"change_amount": "12500"}) == []
    ch = rl.compute_changes(r, {"owner": "Riverside Health Partners LLC"})
    assert ch == [{"field": "owner", "from": "Riverside Health", "to": "Riverside Health Partners LLC"}]


# ---- approval rules
def test_flagged_field_must_be_confirmed_or_edited():
    r = result()
    ok, reasons = rl.can_approve(r, {}, confirmed=[], issues_acknowledged=False, approver="Memona")
    assert not ok and "owner" in reasons[0]
    assert rl.can_approve(r, {}, ["owner"], False, "Memona")[0]
    assert rl.can_approve(r, {"owner": "Riverside Health Partners LLC"}, [], False, "Memona")[0]


def test_approver_name_is_required():
    ok, reasons = rl.can_approve(result(), {}, ["owner"], False, "  ")
    assert not ok and "name" in reasons[0]


def test_open_validation_issue_needs_explicit_acknowledgement():
    r = result()
    edited = {"change_amount": "99,999.00"}
    ok, reasons = rl.can_approve(r, edited, ["owner"], False, "Memona")
    assert not ok and any("validation issues" in x for x in reasons)
    assert rl.can_approve(r, edited, ["owner"], True, "Memona")[0]


def test_build_approval_refuses_when_rules_are_not_met():
    with pytest.raises(ValueError):
        rl.build_approval(result(), {}, "Memona", confirmed=[], issues_acknowledged=False)


def test_build_approval_record_contents():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 16, 10, 30, tzinfo=timezone.utc)
    rec = rl.build_approval(result(), {"owner": "Riverside Health Partners LLC"}, "Memona",
                            confirmed=[], issues_acknowledged=False, now=now)
    assert rec["approved_at"] == "2026-11-16T10:30:00Z" and rec["approved_by"] == "Memona"
    assert rec["fields"]["owner"] == "Riverside Health Partners LLC"
    assert rec["fields"]["change_amount"] == 12500.0
    assert [c["field"] for c in rec["changes"]] == ["owner"]
    assert rec["confirmed_without_change"] == []
    assert rec["needs_review_at_extraction"] == ["owner"]
    assert rec["original_confidence"]["owner"] == 0.52


def test_confirmed_without_change_is_recorded():
    rec = rl.build_approval(result(), {}, "Memona", confirmed=["owner"], issues_acknowledged=False)
    assert rec["changes"] == [] and rec["confirmed_without_change"] == ["owner"]


def test_acknowledged_issues_are_stored_in_the_record():
    rec = rl.build_approval(result(), {"change_amount": "99,999.00"}, "Memona", ["owner"], True)
    assert "change_amount" in rec["open_issues_acknowledged"]


# ---- saving
def test_save_never_overwrites(tmp_path):
    rec = rl.build_approval(result(), {}, "Memona", ["owner"], False)
    p1 = rl.save_approval(rec, tmp_path)
    p2 = rl.save_approval(rec, tmp_path)           # same second, same source file
    assert p1 != p2 and p1.exists() and p2.exists()
    assert json.loads(p1.read_text())["source_file"] == "co.txt"


def test_save_creates_the_folder(tmp_path):
    rec = rl.build_approval(result(), {}, "Memona", ["owner"], False)
    assert rl.save_approval(rec, tmp_path / "new" / "approved").exists()


# ---- ordering + locating files
def test_field_order_puts_flagged_fields_first():
    order = rl.field_order(result())
    assert order[0] == "owner" and sorted(order) == sorted(FIELDS)


def test_find_source_file(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "co.txt").write_text("x")
    assert rl.find_source_file({"source_file": "co.txt"}, [tmp_path]).name == "co.txt"
    assert rl.find_source_file({"source_file": "nope.pdf"}, [tmp_path]) is None


# ---- real pipeline output files (committed in output/) load and approve cleanly
@pytest.mark.parametrize("p", sorted((Path(__file__).parent.parent / "output").glob("*.json")))
def test_real_output_files_can_go_through_review(p):
    r = rl.load_result(p)
    clean, _ = rl.revalidate(r, {})
    assert set(clean) >= set(FIELDS)
    ok, _ = rl.can_approve(r, {}, confirmed=r["needs_review"], issues_acknowledged=True, approver="t")
    assert ok
