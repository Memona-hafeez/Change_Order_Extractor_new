"""Turn an approved change-order record into a QuickBooks Estimate payload. Pure functions, no network.

Mapping decisions (written down so they can be argued with):
  * We act as the CONTRACTOR's books: the owner is the QuickBooks Customer.
  * The document is an ESTIMATE (status Pending). Estimates are non-posting in QuickBooks, so a mistake does not
    touch the ledger; a person can later convert it to an invoice. (Check this in your sandbox.)
  * DocNumber = "CO-<number>", which is also what we use to detect duplicates.
  * One line for the whole change, or one line per item when the line items add up exactly to the change amount.
  * A credit (negative change) is sent as a negative line amount. Whether a given company accepts that is not
    verified; the dry run warns about it.
"""
import re

MAX_DOCNUMBER = 21        # QuickBooks DocNumber length limit (verify in your company)
MAX_TEXT = 4000


class MappingError(Exception):
    pass


def escape_qb(value):
    """Escape a value for a QuickBooks query string literal: backslash and single quote."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def customer_query(name):
    return f"SELECT * FROM Customer WHERE DisplayName = '{escape_qb(name)}'"


def item_query(name):
    return f"SELECT * FROM Item WHERE Name = '{escape_qb(name)}'"


def estimate_query(doc_number):
    return f"SELECT * FROM Estimate WHERE DocNumber = '{escape_qb(doc_number)}'"


INCOME_ACCOUNT_QUERY = "SELECT * FROM Account WHERE AccountType = 'Income' MAXRESULTS 1"


def doc_number(co_number):
    text = (str(co_number).strip() if co_number is not None else "")
    if not text:
        raise MappingError("change order number is missing, so no DocNumber can be built")
    digits = re.search(r"\d+", text)
    base = f"CO-{int(digits.group())}" if digits else "CO-" + re.sub(r"[^A-Za-z0-9]+", "", text)
    return base[:MAX_DOCNUMBER]


def _money(x):
    return round(float(x), 2)


def _line_items_usable(items, change):
    if not items or any(i.get("total") is None for i in items):
        return False
    return abs(sum(i["total"] for i in items) - change) <= 0.01


def check_ready(record):
    """Raise MappingError if the record cannot be turned into an estimate."""
    f = record.get("fields") or {}
    if not (f.get("owner") or "").strip():
        raise MappingError("owner is missing: it is used as the QuickBooks customer")
    if f.get("change_amount") is None:
        raise MappingError("change amount is missing")
    doc_number(f.get("change_order_number"))


def customer_name(record, override=None):
    return (override or record["fields"]["owner"]).strip()


def warnings_for(record):
    f = record["fields"]
    out = []
    if f["change_amount"] < 0:
        out.append("negative amount (credit): check in the sandbox how your company handles negative estimate lines")
    if f["change_amount"] == 0:
        out.append("change amount is 0")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.get("date") or ""):
        out.append("no valid date: TxnDate will be left out")
    if record.get("open_issues_acknowledged"):
        out.append("approved with open validation issues: " + ", ".join(sorted(record["open_issues_acknowledged"])))
    if len(doc_number(f.get("change_order_number"))) == MAX_DOCNUMBER:
        out.append("DocNumber was cut to 21 characters")
    return out


def private_note(record):
    changed = [c["field"] for c in record.get("changes") or []]
    note = f"Created from {record['source_file']}. Approved by {record['approved_by']} at {record['approved_at']}."
    if changed:
        note += " Reviewer corrected: " + ", ".join(changed) + "."
    return note[:MAX_TEXT]


def build_estimate(record, customer_id, item_id):
    """The JSON body for POST /estimate."""
    check_ready(record)
    f = record["fields"]
    change = _money(f["change_amount"])
    default_desc = (f.get("description_of_change") or f"Change Order {f['change_order_number']}").strip()[:MAX_TEXT]
    items = record.get("line_items") or []
    lines = []
    if _line_items_usable(items, change):
        for it in items:
            detail = {"ItemRef": {"value": str(item_id)}}
            q, u, t = it.get("quantity"), it.get("unit_price"), it["total"]
            if q is not None and u is not None and abs(q * u - t) <= 0.01:
                detail.update({"Qty": q, "UnitPrice": u})
            lines.append({"DetailType": "SalesItemLineDetail", "Amount": _money(t),
                          "Description": (it.get("description") or default_desc)[:MAX_TEXT],
                          "SalesItemLineDetail": detail})
    else:
        lines.append({"DetailType": "SalesItemLineDetail", "Amount": change, "Description": default_desc,
                      "SalesItemLineDetail": {"ItemRef": {"value": str(item_id)}}})
    body = {"CustomerRef": {"value": str(customer_id)}, "DocNumber": doc_number(f["change_order_number"]),
            "TxnStatus": "Pending", "PrivateNote": private_note(record), "Line": lines}
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.get("date") or ""):
        body["TxnDate"] = f["date"]
    return body
