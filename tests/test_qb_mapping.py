import pytest
import qb_mapping as m


def rec(**over):
    f = {"project_name": "Riverside Tower", "change_order_number": "CO-012", "date": "2026-03-04",
         "owner": "Acme Holdings", "change_amount": 1500.0, "description_of_change": "Add outlets"}
    f.update(over)
    return {"source_file": "x.pdf", "approved_by": "Memona", "approved_at": "2026-11-18T10:00:00Z",
            "fields": f, "line_items": [], "changes": [], "open_issues_acknowledged": {}}


def test_escape_quote_and_backslash():
    assert m.escape_qb("O'Brien \\ Co") == "O\\'Brien \\\\ Co"
    assert "O\\'Brien" in m.customer_query("O'Brien")


@pytest.mark.parametrize("raw,exp", [("CO-012", "CO-12"), ("003", "CO-3"), ("PCO-014", "CO-14"), ("A", "CO-A")])
def test_doc_number(raw, exp):
    assert m.doc_number(raw) == exp


def test_doc_number_missing_and_long():
    with pytest.raises(m.MappingError):
        m.doc_number(None)
    assert len(m.doc_number("X" * 50)) == m.MAX_DOCNUMBER


def test_single_line_estimate():
    b = m.build_estimate(rec(), "7", "9")
    assert b["CustomerRef"] == {"value": "7"} and b["DocNumber"] == "CO-12" and b["TxnDate"] == "2026-03-04"
    assert len(b["Line"]) == 1 and b["Line"][0]["Amount"] == 1500.0
    assert b["Line"][0]["SalesItemLineDetail"]["ItemRef"] == {"value": "9"}


def test_line_items_used_only_when_they_sum():
    r = rec()
    r["line_items"] = [{"description": "a", "quantity": 2, "unit_price": 500, "total": 1000},
                       {"description": "b", "quantity": None, "unit_price": None, "total": 500}]
    b = m.build_estimate(r, "7", "9")
    assert [l["Amount"] for l in b["Line"]] == [1000, 500]
    assert b["Line"][0]["SalesItemLineDetail"]["Qty"] == 2 and "Qty" not in b["Line"][1]["SalesItemLineDetail"]
    r["line_items"][1]["total"] = 400            # no longer adds up -> one summary line
    assert len(m.build_estimate(r, "7", "9")["Line"]) == 1


def test_missing_required_fields():
    for over in ({"owner": None}, {"change_amount": None}, {"change_order_number": None}):
        with pytest.raises(m.MappingError):
            m.build_estimate(rec(**over), "7", "9")


def test_bad_date_left_out_and_warned():
    r = rec(date="March 4")
    assert "TxnDate" not in m.build_estimate(r, "7", "9")
    assert any("date" in w for w in m.warnings_for(r))


def test_credit_warns_and_keeps_sign():
    r = rec(change_amount=-200.0)
    assert m.build_estimate(r, "7", "9")["Line"][0]["Amount"] == -200.0
    assert any("negative" in w for w in m.warnings_for(r))


def test_private_note_lists_corrections():
    r = rec()
    r["changes"] = [{"field": "owner", "from": "A", "to": "B"}]
    n = m.build_estimate(r, "7", "9")["PrivateNote"]
    assert "Memona" in n and "owner" in n and "x.pdf" in n
