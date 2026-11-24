import pytest
import post_to_quickbooks as p


class FakeClient:
    def __init__(self, customers=None, items=None, estimates=None):
        self.data = {"Customer": customers if customers is not None else [{"Id": "7"}],
                     "Item": items if items is not None else [{"Id": "9"}],
                     "Estimate": estimates or [], "Account": [{"Id": "1"}]}
        self.created = []

    def query_list(self, sql, entity):
        return self.data[entity]

    def create(self, entity, body):
        self.created.append((entity, body))
        return {"Id": "100", "DocNumber": body.get("DocNumber"), "TotalAmt": 1}


def rec():
    return {"source_file": "x.pdf", "approved_by": "M", "approved_at": "t",
            "fields": {"project_name": "P", "change_order_number": "CO-1", "date": "2026-01-02",
                       "owner": "Acme", "change_amount": 10.0, "description_of_change": "d"},
            "line_items": [], "changes": [], "open_issues_acknowledged": {}}


def go(client, tmp_path, **kw):
    return p.post(rec(), client, ledger_path=tmp_path / "led.json", **kw)


def test_dry_run_sends_nothing(tmp_path):
    c = FakeClient()
    out = go(c, tmp_path, dry_run=True)
    assert c.created == [] and out["created"] is None and out["payload"]["DocNumber"] == "CO-1"
    assert not (tmp_path / "led.json").exists()


def test_post_creates_and_records_ledger(tmp_path):
    c = FakeClient()
    out = go(c, tmp_path)
    assert c.created[0][0] == "Estimate" and out["created"]["id"] == "100"
    with pytest.raises(p.PostError, match="already posted"):
        go(c, tmp_path)
    assert len(c.created) == 1


def test_existing_estimate_blocks(tmp_path):
    c = FakeClient(estimates=[{"Id": "5"}])
    with pytest.raises(p.PostError, match="already exists"):
        go(c, tmp_path)
    assert c.created == []


def test_customer_not_found_or_ambiguous(tmp_path):
    with pytest.raises(p.PostError, match="not found"):
        go(FakeClient(customers=[]), tmp_path)
    with pytest.raises(p.PostError, match="refusing to guess"):
        go(FakeClient(customers=[{"Id": "1"}, {"Id": "2"}]), tmp_path)


def test_missing_item_needs_flag(tmp_path):
    c = FakeClient(items=[])
    with pytest.raises(p.PostError, match="--create-item"):
        go(c, tmp_path)
    go(c, tmp_path, create_item=True)
    assert [e for e, _ in c.created] == ["Item", "Estimate"]


def test_production_guard(tmp_path):
    c = FakeClient()
    with pytest.raises(p.PostError, match="sandbox"):
        go(c, tmp_path, environment="production")
    assert c.created == []
