import pytest
from validate import parse_money, parse_date, canon, validate, to_bool, to_int


@pytest.mark.parametrize("raw,expected", [
    ("$12,500.00", 12500.0),
    ("(3,200)", -3200.0),
    ("($32,400.00)", -32400.0),
    ("-1,000", -1000.0),
    ("615,000", 615000.0),
    (None, None),
    ("", None),
    ("n/a", None),
    ("1.2.3", None),
])
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


def test_parse_date_iso_and_words():
    assert parse_date("2026-03-14") == ("2026-03-14", False)
    assert parse_date("March 14, 2026") == ("2026-03-14", False)
    assert parse_date("June 5 2026") == ("2026-06-05", False)


def test_parse_date_ambiguous_is_flagged_and_assumes_mm_dd():
    assert parse_date("04/03/2026") == ("2026-04-03", True)


def test_parse_date_unambiguous_slash():
    assert parse_date("13/03/2026") == ("2026-03-13", False)   # 13 cannot be a month
    assert parse_date("03/03/2026") == ("2026-03-03", False)   # same day and month


def test_parse_date_garbage():
    assert parse_date("sometime soon") == (None, False)


def test_helpers():
    assert to_int("5 days") == 5
    assert to_int("decrease of 3 days") == 3
    assert to_bool("signed") is True and to_bool("unsigned") is False and to_bool("maybe") is None


def test_canon_makes_values_comparable():
    assert canon("change_amount", "($32,400.00)") == canon("change_amount", "-32400")
    assert canon("change_order_number", "CO-012") == canon("change_order_number", "12")
    assert canon("project_name", "Lakeside School  Gym!") == "lakeside school gym"


def base_raw(**over):
    raw = {"project_name": "Riverside Clinic", "change_order_number": "3", "date": "March 14, 2026",
           "contractor": "Apex Builders", "owner": "Riverside Health", "description_of_change": "outlets",
           "original_contract_sum": "$1,000", "previous_changes_total": None,
           "change_amount": "$100", "new_contract_sum": "$1,100",
           "schedule_change_days": "5", "signed": "true"}
    raw.update(over)
    return raw


def test_validate_clean_document_has_no_issues():
    clean, items, issues = validate(base_raw(), [])
    assert issues == {}
    assert clean["change_amount"] == 100.0 and clean["signed"] is True
    assert clean["date"] == "2026-03-14"


def test_validate_flags_contract_sum_mismatch_on_all_three_fields():
    _, _, issues = validate(base_raw(new_contract_sum="$1,500"), [])
    for f in ("original_contract_sum", "change_amount", "new_contract_sum"):
        assert "original + change != new contract sum" in issues[f]


def test_validate_accepts_previous_changes_in_the_sum():
    raw = base_raw(previous_changes_total="$50", new_contract_sum="$1,150")
    _, _, issues = validate(raw, [])
    assert "new_contract_sum" not in issues


def test_validate_credit_is_negative_and_checks_out():
    raw = base_raw(change_amount="($100)", new_contract_sum="$900")
    clean, _, issues = validate(raw, [])
    assert clean["change_amount"] == -100.0 and issues == {}


def test_validate_line_item_arithmetic():
    ok = [{"description": "a", "quantity": "2", "unit_price": "50", "total": "100"}]
    bad = [{"description": "a", "quantity": "2", "unit_price": "50", "total": "120"}]
    assert "line_items" not in validate(base_raw(), ok)[2]
    assert "line_items" in validate(base_raw(), bad)[2]


def test_validate_sum_of_line_items_must_match_change_amount():
    items = [{"description": "a", "quantity": None, "unit_price": None, "total": "60"},
             {"description": "b", "quantity": None, "unit_price": None, "total": "30"}]
    _, _, issues = validate(base_raw(), items)          # 90 != 100
    assert "sum of line items != change amount" in issues["change_amount"]
    items[1]["total"] = "40"
    assert "change_amount" not in validate(base_raw(), items)[2]


def test_validate_owner_and_contractor_must_differ():
    _, _, issues = validate(base_raw(owner="Apex Builders"), [])
    assert "owner" in issues and "contractor" in issues


def test_validate_ambiguous_date_is_flagged():
    _, _, issues = validate(base_raw(date="04/03/2026"), [])
    assert any("ambiguous" in m for m in issues["date"])


def test_validate_unparseable_amount_is_flagged():
    _, _, issues = validate(base_raw(change_amount="see attached"), [])
    assert "change_amount" in issues
