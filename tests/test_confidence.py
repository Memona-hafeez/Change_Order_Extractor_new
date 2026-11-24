import pytest
from confidence import (number_in_text, number_grounding, has_signature_evidence, ocr_quality,
                        field_confidence, evidence_score, REVIEW_THRESHOLD)

DOC = "Original Contract Sum: $1,250,000.00\nThis Change Order: $12,500.00\nContract time change: 15 days"


@pytest.mark.parametrize("value,text,expected", [
    (12500, "This Change Order: $12,500.00", True),
    (12500.0, "total 12500", True),
    (5, "Time change: 15 days", False),          # '5' must not match inside 15
    (5, "Fee $5.25", False),                      # ...or the start of 5.25
    (5, "Time change: 5 days", True),
    (12500, "Sum $125,000.00", False),            # not inside a bigger number
    (-32400, "Credit ($32,400.00)", True),        # sign ignored
    (None, "anything", False),
    (5, None, False),
])
def test_number_in_text_uses_whole_numbers(value, text, expected):
    assert number_in_text(value, text) is expected


def test_number_grounding_prefers_the_evidence_span():
    assert number_grounding(12500, "This Change Order: $12,500.00", DOC) == 1.0
    assert number_grounding(12500, "This Change Order", DOC) == 0.5     # number only elsewhere in doc
    assert number_grounding(99999, "This Change Order", DOC) == 0.0


def test_small_number_in_the_wrong_place_only_gets_half_credit():
    # '15' appears in the document, but the evidence the model quoted does not contain it.
    assert number_grounding(15, "Contract time", DOC) == 0.5


@pytest.mark.parametrize("text,expected", [
    ("Signed: /s/ John Smith", True),
    ("Approved by John Smith (signed)", True),
    ("Digitally signed by A. Lee", True),
    ("Signature: ____________ (unsigned)", False),   # 'unsigned' is not a marker
    ("Approved by: John Smith, Owner Rep.", False),  # a printed name is not a signature
    ("", False),
])
def test_has_signature_evidence(text, expected):
    assert has_signature_evidence(text) is expected


def test_evidence_score_exact_and_partial():
    assert evidence_score("This Change Order: $12,500.00", DOC) == 1.0
    assert 0 < evidence_score("This Change Order: $99,999.00", DOC) < 1.0
    assert evidence_score(None, DOC) == 0.0


def test_ocr_quality_averages_evidence_word_confidences():
    words = [("This", 96), ("Change", 90), ("Order:", 94), ("$12,500.00", 50)]
    q = ocr_quality("This Change $12,500.00", words)
    assert q == pytest.approx((96 + 90 + 50) / 3 / 100, abs=1e-3)
    assert ocr_quality("x", None) is None          # no OCR used
    assert ocr_quality(None, words) == 0.0
    assert ocr_quality("not in doc", words) == 0.0


def test_perfect_money_field_scores_high_and_is_not_reviewed():
    c = field_confidence("change_amount", 12500.0, "This Change Order: $12,500.00", 0.95, True, False, DOC)
    assert c >= REVIEW_THRESHOLD


def test_validation_issue_caps_confidence_at_review_level():
    c = field_confidence("change_amount", 12500.0, "This Change Order: $12,500.00", 1.0, True, True, DOC)
    assert c <= 0.60 < REVIEW_THRESHOLD


def test_disagreement_lowers_confidence():
    a = field_confidence("owner", "X", "Owner: X", 0.9, True, False, "Owner: X")
    b = field_confidence("owner", "X", "Owner: X", 0.9, False, False, "Owner: X")
    assert b < a


def test_null_value_trusted_only_when_runs_agree():
    assert field_confidence("owner", None, None, 0, True, False, DOC) == 0.8
    assert field_confidence("owner", None, None, 0, False, False, DOC) == 0.3


def test_bad_ocr_on_the_evidence_lowers_confidence():
    args = ("change_amount", 12500.0, "This Change Order: $12,500.00", 0.95, True, False, DOC)
    clean_scan = field_confidence(*args, ocr_q=0.97)
    shaky_scan = field_confidence(*args, ocr_q=0.40)
    assert shaky_scan < clean_scan <= field_confidence(*args)


def test_zero_day_change_is_not_penalised_by_number_grounding():
    c = field_confidence("schedule_change_days", 0, "No time impact", 0.9, True, False, "No time impact")
    assert c >= REVIEW_THRESHOLD


@pytest.mark.parametrize("text,expected", [
    ("NOT VALID UNTIL SIGNED BY THE ARCHITECT, CONTRACTOR AND OWNER.", False),   # AIA boilerplate
    ("This change order will be signed by both parties.", False),
    ("Not yet signed by the district", False),
    ("Once signed, work may proceed.", False),
    ("Signed: ______________", False),                                            # blank signature line
    ("Signed by the owner on March 3", True),
    ("The document has been signed by the owner.", True),
    ("Approved: Maria Lopez, Steelman (signed)", True),
    ("NOT VALID UNTIL SIGNED BY THE OWNER.\nSigned: /s/ Dr. Helen Ortiz", True),  # a real marker still wins
])
def test_signature_evidence_ignores_boilerplate_and_blank_lines(text, expected):
    assert has_signature_evidence(text) is expected
