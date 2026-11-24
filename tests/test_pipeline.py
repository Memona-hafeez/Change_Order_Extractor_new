"""End-to-end pipeline tests with the LLM mocked: they need no API key and no network."""
import copy
import pytest
import pipeline

DOC = """CHANGE ORDER NO. 003
Project: Riverside Medical Center - Phase 2
Date: March 14, 2026
Owner: Riverside Health Partners LLC
Contractor: Apex Builders Inc.

Description of Change:
Add two additional electrical outlets in each patient room on Level 3.

Original Contract Sum: $1,250,000.00
Net change by previous Change Orders: $0.00
This Change Order: $12,500.00
New Contract Sum: $1,262,500.00

Contract time change: 5 days

Line items:
Outlets - 40 ea @ 125.00 = 5,000.00
Wiring labor - 100 hr @ 75.00 = 7,500.00

Approved by: John Smith, Owner Rep.  Signed: /s/ John Smith"""


def node(value, evidence, conf=0.95):
    return {"value": value, "evidence": evidence, "confidence": conf}


def good_run():
    return {
        "project_name": node("Riverside Medical Center - Phase 2", "Project: Riverside Medical Center - Phase 2"),
        "change_order_number": node("003", "CHANGE ORDER NO. 003"),
        "date": node("March 14, 2026", "Date: March 14, 2026"),
        "contractor": node("Apex Builders Inc.", "Contractor: Apex Builders Inc."),
        "owner": node("Riverside Health Partners LLC", "Owner: Riverside Health Partners LLC"),
        "description_of_change": node("Add two additional electrical outlets in each patient room on Level 3.",
                                     "Add two additional electrical outlets in each patient room on Level 3."),
        "original_contract_sum": node("$1,250,000.00", "Original Contract Sum: $1,250,000.00"),
        "previous_changes_total": node("$0.00", "Net change by previous Change Orders: $0.00"),
        "change_amount": node("$12,500.00", "This Change Order: $12,500.00"),
        "new_contract_sum": node("$1,262,500.00", "New Contract Sum: $1,262,500.00"),
        "schedule_change_days": node("5", "Contract time change: 5 days"),
        "signed": node("true", "Signed: /s/ John Smith"),
        "line_items": [
            {"description": "Outlets", "quantity": "40", "unit_price": "125.00", "total": "5,000.00"},
            {"description": "Wiring labor", "quantity": "100", "unit_price": "75.00", "total": "7,500.00"},
        ],
    }


@pytest.fixture
def doc_file(tmp_path):
    def make(text=DOC):
        p = tmp_path / "co.txt"
        p.write_text(text)
        return p
    return make


@pytest.fixture
def mock_llm(monkeypatch):
    """Install fake LLM calls. Returns a dict of call counters."""
    state = {"main": 0, "items": 0, "run1": good_run(), "run2": None, "items_result": []}

    def fake_main(text, temperature=0.0):
        state["main"] += 1
        run = state["run1"] if temperature == 0.0 else (state["run2"] or state["run1"])
        return copy.deepcopy(run)

    def fake_items(text):
        state["items"] += 1
        return copy.deepcopy(state["items_result"])

    monkeypatch.setattr(pipeline, "call_llm", fake_main)
    monkeypatch.setattr(pipeline, "call_llm_line_items", fake_items)
    return state


def test_clean_document_has_normalised_values_and_nothing_to_review(doc_file, mock_llm):
    res = pipeline.process(doc_file())
    f = res["fields"]
    assert f["change_amount"]["value"] == 12500.0
    assert f["new_contract_sum"]["value"] == 1262500.0
    assert f["date"]["value"] == "2026-03-14"
    assert f["schedule_change_days"]["value"] == 5
    assert f["signed"]["value"] is True
    assert res["needs_review"] == []
    assert res["document_issues"] == []
    assert res["input"] == {"ocr_used": False, "mean_ocr_confidence": None}
    assert mock_llm["items"] == 0                       # line items were already returned


def test_every_field_carries_confidence_evidence_and_issues(doc_file, mock_llm):
    res = pipeline.process(doc_file())
    for name, d in res["fields"].items():
        assert set(d) == {"value", "confidence", "evidence", "issues"}, name
        assert 0.0 <= d["confidence"] <= 1.0


def test_wrong_arithmetic_sends_the_three_money_fields_to_review(doc_file, mock_llm):
    mock_llm["run1"]["new_contract_sum"] = node("$1,300,000.00", "New Contract Sum: $1,262,500.00")
    res = pipeline.process(doc_file())
    for f in ("original_contract_sum", "change_amount", "new_contract_sum"):
        assert f in res["needs_review"]
        assert any("!=" in m for m in res["fields"][f]["issues"])


def test_printed_name_without_signature_marker_is_not_trusted_as_signed(doc_file, mock_llm):
    unsigned_text = DOC.replace("Signed: /s/ John Smith", "")       # only a printed name remains
    mock_llm["run1"]["signed"] = node("true", "Approved by: John Smith, Owner Rep.", 1.0)
    res = pipeline.process(doc_file(unsigned_text))
    assert "signed" in res["needs_review"]
    assert res["fields"]["signed"]["confidence"] <= 0.60
    assert any("signature marker" in m for m in res["fields"]["signed"]["issues"])


def test_real_signature_marker_keeps_signed_trusted(doc_file, mock_llm):
    res = pipeline.process(doc_file())
    assert "signed" not in res["needs_review"]
    assert res["fields"]["signed"]["issues"] == []


def test_missing_line_items_on_an_itemized_document_are_requested_again(doc_file, mock_llm):
    mock_llm["run1"]["line_items"] = []
    mock_llm["items_result"] = good_run()["line_items"]
    res = pipeline.process(doc_file())
    assert mock_llm["items"] == 1
    assert len(res["line_items"]) == 2
    assert any("requested separately" in m for m in res["document_issues"])
    assert "change_amount" not in res["needs_review"]    # 5,000 + 7,500 == 12,500


def test_requested_line_items_that_do_not_add_up_are_caught(doc_file, mock_llm):
    mock_llm["run1"]["line_items"] = []
    bad = good_run()["line_items"]
    bad[1]["total"] = "9,000.00"                          # 5,000 + 9,000 != 12,500
    mock_llm["items_result"] = bad
    res = pipeline.process(doc_file())
    assert "change_amount" in res["needs_review"]


def test_document_without_priced_rows_does_not_trigger_the_extra_call(doc_file, mock_llm):
    text = DOC.replace("Outlets - 40 ea @ 125.00 = 5,000.00\n", "").replace(
        "Wiring labor - 100 hr @ 75.00 = 7,500.00\n", "")
    mock_llm["run1"]["line_items"] = []
    pipeline.process(doc_file(text))
    assert mock_llm["items"] == 0


def test_disagreeing_runs_lower_the_confidence_of_that_field(doc_file, mock_llm):
    mock_llm["run2"] = good_run()
    mock_llm["run2"]["owner"] = node("Riverside Hospital", "Owner: Riverside Health Partners LLC")
    agree = pipeline.process(doc_file())
    mock_llm["run2"] = None
    res_same = pipeline.process(doc_file())
    assert agree["fields"]["owner"]["confidence"] < res_same["fields"]["owner"]["confidence"]


def test_evidence_that_is_not_in_the_document_lowers_confidence(doc_file, mock_llm):
    honest = pipeline.process(doc_file())["fields"]["contractor"]["confidence"]
    mock_llm["run1"]["contractor"] = node("Apex Builders Inc.", "Contractor of record: Zenith Corp", 0.99)
    made_up = pipeline.process(doc_file())["fields"]["contractor"]["confidence"]
    assert made_up < honest - 0.10


def test_missing_field_comes_back_null_and_is_trusted_when_runs_agree(doc_file, mock_llm):
    mock_llm["run1"]["schedule_change_days"] = node(None, None, 0)
    res = pipeline.process(doc_file())
    assert res["fields"]["schedule_change_days"]["value"] is None
    assert res["fields"]["schedule_change_days"]["confidence"] == 0.8


def _scan_of(text, word_conf):
    from pdf_reader import Document
    return Document(text, [(w, word_conf) for w in text.split()])


def test_readable_scan_is_marked_but_not_forced_into_review(doc_file, mock_llm, monkeypatch):
    monkeypatch.setattr(pipeline, "read_document", lambda p: _scan_of(DOC, 95))
    res = pipeline.process(doc_file())
    assert res["input"]["ocr_used"] and res["input"]["mean_ocr_confidence"] == 0.95
    assert res["needs_review"] == []
    assert any("OCR" in m for m in res["document_issues"])


def test_badly_read_scan_sends_every_field_to_review(doc_file, mock_llm, monkeypatch):
    monkeypatch.setattr(pipeline, "read_document", lambda p: _scan_of(DOC, 62))
    res = pipeline.process(doc_file())
    assert set(res["needs_review"]) == set(res["fields"])
    assert all(d["confidence"] <= pipeline.LOW_OCR_CAP for d in res["fields"].values())
    assert any("low OCR quality" in m for m in res["document_issues"])
    assert all("low OCR quality on this scan" in d["issues"] for d in res["fields"].values())


def test_ocr_gate_sits_between_the_dev_clusters():
    # dev scans: readable >= 0.89, badly degraded 0.64-0.73
    assert 0.73 < pipeline.LOW_OCR_QUALITY < 0.89
