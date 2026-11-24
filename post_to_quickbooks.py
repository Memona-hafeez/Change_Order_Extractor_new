"""Post one approved change order to QuickBooks as a draft-like Estimate.

    python post_to_quickbooks.py approved/<file>.json --dry-run     # show what would be sent, send nothing
    python post_to_quickbooks.py approved/<file>.json               # create it in the SANDBOX company
    options: --customer "Name"   use this QuickBooks customer instead of the document's owner
             --create-item       create the "Change Order" service item if it does not exist
             --allow-production  required when QB_ENVIRONMENT=production (do not use for the demo)

Safety: one document per change order. Before creating, we look for an existing Estimate with the same
DocNumber and check a local ledger (qb_posted.json), so running twice never creates two.
"""
import json
import sys
from pathlib import Path

import qb_mapping as m
from qb_auth import QBAuthError
from quickbooks_client import QuickBooksError

ITEM_NAME = "Change Order"
LEDGER = "qb_posted.json"


class PostError(Exception):
    pass


def load_ledger(path=LEDGER):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_ledger(data, path=LEDGER):
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def find_customer(client, name):
    rows = client.query_list(m.customer_query(name), "Customer")
    if not rows:
        raise PostError(f"customer {name!r} not found in QuickBooks. Create it there, or pass --customer with an existing name.")
    if len(rows) > 1:
        raise PostError(f"{len(rows)} customers are called {name!r}; refusing to guess.")
    return rows[0]["Id"]


def find_or_create_item(client, create):
    rows = client.query_list(m.item_query(ITEM_NAME), "Item")
    if rows:
        return rows[0]["Id"]
    if not create:
        raise PostError(f"service item {ITEM_NAME!r} does not exist. Re-run with --create-item (or create it in QuickBooks).")
    accounts = client.query_list(m.INCOME_ACCOUNT_QUERY, "Account")
    if not accounts:
        raise PostError("no income account found to attach the new item to")
    body = {"Name": ITEM_NAME, "Type": "Service", "IncomeAccountRef": {"value": accounts[0]["Id"]}}
    return client.create("Item", body)["Id"]


def post(record, client, environment="sandbox", dry_run=False, customer=None, create_item=False,
         allow_production=False, ledger_path=LEDGER):
    if environment != "sandbox" and not allow_production:
        raise PostError("refusing to post outside the sandbox without --allow-production")
    m.check_ready(record)
    number = m.doc_number(record["fields"]["change_order_number"])
    ledger = load_ledger(ledger_path)
    key = f"{record['fields'].get('project_name')}|{number}"
    if key in ledger:
        raise PostError(f"already posted ({number}, QuickBooks id {ledger[key]['estimate_id']}); nothing sent")
    existing = client.query_list(m.estimate_query(number), "Estimate")
    if existing:
        raise PostError(f"an Estimate with DocNumber {number} already exists in QuickBooks (id {existing[0]['Id']}); nothing sent")
    customer_id = find_customer(client, m.customer_name(record, customer))
    if dry_run:
        # in a dry run we do not create the item; use a placeholder id so the payload can be shown
        rows = client.query_list(m.item_query(ITEM_NAME), "Item")
        item_id = rows[0]["Id"] if rows else "<item id>"
    else:
        item_id = find_or_create_item(client, create_item)
    body = m.build_estimate(record, customer_id, item_id)
    out = {"warnings": m.warnings_for(record), "payload": body, "created": None}
    if dry_run:
        return out
    est = client.create("Estimate", body)
    ledger[key] = {"estimate_id": est["Id"], "doc_number": number, "source": record["source_file"]}
    save_ledger(ledger, ledger_path)
    out["created"] = {"id": est["Id"], "doc_number": est.get("DocNumber"), "total": est.get("TotalAmt")}
    return out


def main(argv):
    from quickbooks_client import build_client
    from qb_auth import load_config
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return
    opt = lambda n: argv[argv.index(n) + 1] if n in argv else None
    path = next(a for a in args if a.endswith(".json"))
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg = load_config()
    result = post(record, build_client(cfg), environment=cfg["environment"], dry_run="--dry-run" in argv,
                  customer=opt("--customer"), create_item="--create-item" in argv,
                  allow_production="--allow-production" in argv)
    for w in result["warnings"]:
        print("WARNING:", w)
    print(json.dumps(result["payload"], indent=2))
    print("DRY RUN: nothing was sent." if result["created"] is None else f"CREATED: {result['created']}")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (PostError, m.MappingError, QBAuthError, QuickBooksError) as e:
        sys.exit(f"ERROR: {e}")
