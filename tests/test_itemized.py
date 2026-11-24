from itemized import itemized_rows, looks_itemized

ITEMIZED = """Original Contract Sum: $1,250,000.00
Outlets - 40 ea @ 125.00 = 5,000.00
Wiring labor - 100 hr @ 75.00 = 7,500.00
New Contract Sum: $1,262,500.00"""

NOT_ITEMIZED = """Original contract: $840,000.00
Credit for deleted work: ($32,400.00)
Revised contract total: $807,600.00"""


def test_priced_rows_are_detected_and_summary_lines_ignored():
    assert len(itemized_rows(ITEMIZED)) == 2
    assert looks_itemized(ITEMIZED)


def test_summary_only_document_is_not_itemized():
    assert not looks_itemized(NOT_ITEMIZED)


def test_single_row_is_not_enough():
    assert not looks_itemized("Outlets - 40 ea @ 125.00 = 5,000.00")


AIA_ROWS = """Ref.    Description                                               Amount (USD)
PCO-014 Add emergency generator pad and conduit, Level 1 east yard 18,450.00
PCO-015 Revise lobby flooring from VCT to porcelain tile            6,200.00
PCO-016 Credit: delete exterior monument signage (owner-furnished) (3,150.00)
The original Contract Sum was                           $         1,850,000.00
The net change by previously authorized Change Orders   $          64,300.00
The Contract Sum prior to this Change Order was         $         1,914,300.00
The Contract Sum will be increased by this Change Order in the amount of $ 21,500.00
The new Contract Sum including this Change Order will be $        1,935,800.00
The Contract Time will be increased by                               9 days"""


def test_rows_with_a_single_trailing_amount_are_detected():
    rows = itemized_rows(AIA_ROWS)
    assert [r.split()[0] for r in rows] == ["PCO-014", "PCO-015", "PCO-016"]
    assert looks_itemized(AIA_ROWS)


def test_real_aia_sample_pdf_is_recognised_as_itemized_and_not_as_signed():
    from pathlib import Path
    from pdf_reader import read_document
    from confidence import has_signature_evidence
    p = Path(__file__).parent.parent / "samples" / "aia_style_g701.pdf"
    text = read_document(p).text
    assert looks_itemized(text)
    assert not has_signature_evidence(text)     # printed names and dates only, no '/s/'


def test_a_row_whose_name_contains_contract_is_still_a_row():
    text = "Materials - 10 ea @ 50.00 = 500.00\nSubcontractor markup - 1 ea @ 250.00 = 250.00"
    assert len(itemized_rows(text)) == 2 and looks_itemized(text)
