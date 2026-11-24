"""Change Order Extractor: one screen for the whole flow.

    streamlit run app.py

Tabs: 1 Extract  ->  2 Review & approve  ->  3 Send to QuickBooks (sandbox)  ->  4 Dashboard.
All the real logic lives in the existing modules (pipeline, review_logic, qb_mapping, post_to_quickbooks);
this file is only the screen.
"""
import json
from datetime import date
from pathlib import Path

import streamlit as st

from review_logic import (can_approve, build_approval, save_approval, field_order, to_display,
                          find_source_file, find_evidence_span, highlight_html, load_result)
from schema import FIELDS

st.set_page_config(page_title="Change Order Extractor", layout="wide")
UPLOADS, APPROVED, OUTPUT, SAMPLES = Path("uploads"), Path("approved"), Path("output"), Path("samples")


# ------------------------------------------------------------------ small helpers
def days_overdue(due_date, today=None):
    """Whole days past the due date (0 if not due yet or no date)."""
    try:
        y, m, d = (int(x) for x in str(due_date).split("-"))
        return max(0, ((today or date.today()) - date(y, m, d)).days)
    except (ValueError, TypeError):
        return 0


def revised_contract(rec):
    f = rec["fields"]
    if f.get("new_contract_sum") is not None:
        return f["new_contract_sum"]
    if f.get("original_contract_sum") is not None and f.get("change_amount") is not None:
        return f["original_contract_sum"] + (f.get("previous_changes_total") or 0) + f["change_amount"]
    return None


def approved_records():
    out = []
    for p in sorted(APPROVED.glob("*.json")) if APPROVED.exists() else []:
        try:
            out.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            pass
    return out


def money(x):
    return "-" if x is None else f"${x:,.2f}"


def qb_client():
    from quickbooks_client import build_client
    from qb_auth import load_config
    cfg = load_config()
    return build_client(cfg), cfg


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Status")
    try:
        from qb_auth import TokenStore
        t = TokenStore().load()
    except Exception:
        t = None
    st.write("QuickBooks: " + (f"connected (realm {t.get('realm_id')}, {t.get('environment')})" if t
                               else "not connected. Run `python qb_auth.py connect` once."))
    import os
    from dotenv import load_dotenv
    load_dotenv()
    st.write("Groq key: " + ("set" if os.environ.get("GROQ_API_KEY") else "missing (needed only for new extractions)"))

st.markdown("""
<style>
#MainMenu, footer {visibility: hidden;}
.block-container {padding-top: 1.6rem; max-width: 1300px;}
div[data-testid="stMetric"] {background: rgba(128,128,128,0.08); border: 1px solid rgba(128,128,128,0.25);
    border-radius: 10px; padding: 12px 16px;}
.steps {display:flex; gap:8px; margin: 6px 0 14px 0; flex-wrap: wrap;}
.step {padding: 6px 14px; border-radius: 999px; font-size: 0.85rem; border: 1px solid rgba(128,128,128,0.35);}
.step.done {background:#1b7f4b; color:white; border-color:#1b7f4b;}
.step.now {border-color:#2f6fed; color:#2f6fed; font-weight:600;}
.badge {padding: 2px 10px; border-radius: 999px; font-size: 0.78rem; font-weight:600; color:white;}
.b-ok {background:#1b7f4b;} .b-warn {background:#c77700;} .b-bad {background:#c0392b;}
</style>
""", unsafe_allow_html=True)

st.title("Change Order Extractor")
st.caption("Scan or PDF in, structured change order out. A person approves it, then it goes to QuickBooks. "
           "Nothing is sent without approval.")


def stepper():
    have = bool(st.session_state.get("result"))
    appr = bool(st.session_state.get("approved_path"))
    sent = bool(st.session_state.get("sent"))
    steps = [("1 Extract", have), ("2 Review & approve", appr), ("3 Send to QuickBooks", sent)]
    cur = next((i for i, (_, d) in enumerate(steps) if not d), len(steps))
    html_ = "".join(f'<span class="step {"done" if d else ("now" if i == cur else "")}">{n}</span>'
                    for i, (n, d) in enumerate(steps))
    st.markdown(f'<div class="steps">{html_}</div>', unsafe_allow_html=True)


def conf_badge(c, flagged):
    cls = "b-bad" if flagged else ("b-ok" if c >= 0.85 else "b-warn")
    return f'<span class="badge {cls}">{c:.2f}</span>'


stepper()
tab_extract, tab_review, tab_send, tab_dash = st.tabs(["1. Extract", "2. Review & approve", "3. Send to QuickBooks", "4. Dashboard"])

# ------------------------------------------------------------------ 1. extract
with tab_extract:
    left, right = st.columns(2)
    with left:
        st.subheader("New document")
        up = st.file_uploader("PDF, scan (PNG/JPG) or text", type=["pdf", "png", "jpg", "jpeg", "txt"])
        if st.button("Extract", disabled=up is None, type="primary"):
            UPLOADS.mkdir(exist_ok=True)
            path = UPLOADS / up.name
            path.write_bytes(up.getvalue())
            with st.spinner("Reading and extracting (OCR is used for scans)..."):
                try:
                    from pipeline import process
                    st.session_state["result"] = process(str(path))
                    st.session_state["source_path"] = str(path)
                    st.success("Done. Go to the Review tab.")
                except RuntimeError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Extraction failed: {e}")
    with right:
        st.subheader("Or open a saved result")
        saved = sorted(OUTPUT.glob("*.json")) if OUTPUT.exists() else []
        pick = st.selectbox("Saved extraction results", saved, format_func=lambda p: p.name,
                            index=None, placeholder="choose one")
        if st.button("Open", disabled=pick is None):
            res = load_result(pick)
            st.session_state["result"] = res
            src = find_source_file(res, [SAMPLES, UPLOADS])
            st.session_state["source_path"] = str(src) if src else None
            st.success("Opened. Go to the Review tab.")

# ------------------------------------------------------------------ 2. review
with tab_review:
    res = st.session_state.get("result")
    if not res:
        st.info("Extract or open a document first.")
    else:
        sk = res["source_file"]
        inp = res.get("input") or {}
        st.subheader(sk)
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Input", "Scan (OCR)" if inp.get("ocr_used") else "Text / PDF")
        k2.metric("OCR confidence", f"{inp['mean_ocr_confidence']:.2f}" if inp.get("mean_ocr_confidence") is not None else "n/a")
        k3.metric("Fields flagged", f"{len(res['needs_review'])} of {len(FIELDS)}")
        k4.metric("Line items", len(res.get("line_items") or []))
        if inp.get("ocr_used"):
            st.warning(f"Scanned document, OCR used (mean confidence {inp.get('mean_ocr_confidence')}).")
        for n in res.get("document_issues") or []:
            st.warning(n)
        if res["needs_review"]:
            st.error("Needs review: " + ", ".join(res["needs_review"]))
        else:
            st.success("No field was flagged.")
        form, doc = st.columns([3, 2])
        edited, confirmed = {}, []
        with form:
            for f in field_order(res):
                node = res["fields"][f]
                flagged = f in res["needs_review"]
                c1, c2, c3 = st.columns([3, 1, 1])
                edited[f] = c1.text_input(f"{f}{'  (review)' if flagged else ''}", to_display(node["value"]),
                                          key=f"{sk}_{f}")
                c2.markdown("confidence<br>" + conf_badge(node["confidence"], flagged), unsafe_allow_html=True)
                if flagged:
                    if c3.checkbox("confirm", key=f"{sk}_{f}_ok"):
                        confirmed.append(f)
                if node.get("evidence"):
                    st.caption("evidence: " + node["evidence"])
                for msg in node.get("issues") or []:
                    st.caption("issue: " + msg)
            if res.get("line_items"):
                st.write("Line items")
                st.dataframe(res["line_items"])
        with doc:
            st.write("Source text")
            try:
                from pdf_reader import read_document
                text = read_document(st.session_state["source_path"]).text if st.session_state.get("source_path") else None
            except Exception:
                text = None
            if text:
                spans = [find_evidence_span(text, res["fields"][f].get("evidence")) for f in FIELDS]
                st.markdown(highlight_html(text, spans), unsafe_allow_html=True)
            else:
                st.caption("Original document not found next to the app, so no highlighting.")

        from review_logic import revalidate, compute_changes
        done_ = {c["field"] for c in compute_changes(res, edited)} | set(confirmed)
        reviewed = len([f for f in res["needs_review"] if f in done_])
        if res["needs_review"]:
            st.progress(reviewed / len(res["needs_review"]), text=f"{reviewed} of {len(res['needs_review'])} flagged fields reviewed")
        _, issues = revalidate(res, edited)
        ack = False
        if issues:
            st.warning("Open validation issues: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in issues.items()))
            ack = st.checkbox("I have seen these issues and still approve", key=f"{sk}_ack")
        approver = st.text_input("Your name (approver)", key="approver")
        ok, reasons = can_approve(res, edited, confirmed, ack, approver)
        if reasons:
            st.caption("Not ready: " + "; ".join(reasons))
        if st.button("Approve", disabled=not ok, type="primary"):
            rec = build_approval(res, edited, approver, confirmed, ack)
            path = save_approval(rec, APPROVED)
            st.session_state["approved_path"] = str(path)
            st.success(f"Approved and saved: {path}. Go to the Send tab.")

# ------------------------------------------------------------------ 3. send
with tab_send:
    recs = approved_records()
    if not recs:
        st.info("No approved change orders yet.")
    else:
        names = [p.name for p, _ in recs]
        default = names.index(Path(st.session_state["approved_path"]).name) \
            if st.session_state.get("approved_path") and Path(st.session_state["approved_path"]).name in names else len(names) - 1
        chosen = st.selectbox("Approved record", names, index=default)
        rec = dict(recs)[APPROVED / chosen] if (APPROVED / chosen) in dict(recs) else recs[default][1]
        customer = st.text_input("QuickBooks customer (leave empty to use the owner on the document)",
                                 help="In the sandbox, try an existing sample customer such as: Cool Cars")
        dry = st.checkbox("Dry run (show what would be sent, send nothing)", value=True)
        mk_item = st.checkbox("Create the 'Change Order' item if it is missing", value=True)
        if st.button("Send to QuickBooks sandbox", type="primary"):
            from post_to_quickbooks import post, PostError
            from qb_mapping import MappingError
            from qb_auth import QBAuthError
            from quickbooks_client import QuickBooksError
            try:
                client, cfg = qb_client()
                out = post(rec, client, environment=cfg["environment"], dry_run=dry,
                           customer=customer.strip() or None, create_item=mk_item)
                for w in out["warnings"]:
                    st.warning(w)
                st.json(out["payload"])
                if out["created"]:
                    st.session_state["sent"] = True
                    st.success(f"Created Estimate {out['created']['doc_number']} (QuickBooks id {out['created']['id']}).")
                else:
                    st.info("Dry run: nothing was sent.")
            except (PostError, MappingError, QBAuthError, QuickBooksError) as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Unexpected error: {e}")

# ------------------------------------------------------------------ 4. dashboard
with tab_dash:
    recs = approved_records()
    approved_src = {Path(r["source_file"]).stem for _, r in recs}
    pending = []
    for p in sorted(OUTPUT.glob("*.json")) if OUTPUT.exists() else []:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("needs_review") and Path(r["source_file"]).stem not in approved_src:
            pending.append(r["source_file"])
    total_approved = sum((r["fields"].get("change_amount") or 0) for _, r in recs)

    st.subheader("Change orders")
    a, b, c = st.columns(3)
    a.metric("Approved", len(recs))
    b.metric("Pending review", len(pending))
    c.metric("Approved change value", money(total_approved))
    rows = {}
    for _, r in recs:
        f = r["fields"]
        k = f.get("project_name") or "(no project)"
        row = rows.setdefault(k, {"Project": k, "Approved COs": 0, "Approved total": 0.0,
                                  "Original contract": f.get("original_contract_sum"), "Revised contract": None})
        row["Approved COs"] += 1
        row["Approved total"] += f.get("change_amount") or 0
        row["Revised contract"] = revised_contract(r)
    if rows:
        st.dataframe(list(rows.values()), hide_index=True)
    elif not recs:
        st.caption("Approve a change order to see it here.")
    if pending:
        with st.expander(f"Waiting for review ({len(pending)})"):
            for n in pending:
                st.write("- " + n)

    st.subheader("Receivables (live from QuickBooks)")
    if st.button("Load from QuickBooks", key="load_qb"):
        try:
            client, _ = qb_client()
            inv = client.query_list("SELECT * FROM Invoice WHERE Balance > '0' MAXRESULTS 100", "Invoice")
            est = client.query_list("SELECT * FROM Estimate ORDERBY MetaData.CreateTime DESC MAXRESULTS 20", "Estimate")
            st.session_state["qb_data"] = {"inv": inv, "est": est}
        except Exception as e:
            st.error(str(e))
    data = st.session_state.get("qb_data")
    if data:
        table = [{"Customer": i.get("CustomerRef", {}).get("name"), "Invoice": i.get("DocNumber"),
                  "Balance": i.get("Balance") or 0, "Due": i.get("DueDate"),
                  "Days overdue": days_overdue(i.get("DueDate"))} for i in data["inv"]]
        table.sort(key=lambda r: -r["Days overdue"])
        overdue = [r for r in table if r["Days overdue"] > 0]
        x, y, z = st.columns(3)
        x.metric("Outstanding", money(sum(r["Balance"] for r in table)))
        y.metric("Overdue invoices", len(overdue))
        z.metric("Oldest overdue", f"{max((r['Days overdue'] for r in table), default=0)} days")
        if table:
            by_cust = {}
            for r in table:
                by_cust[r["Customer"] or "?"] = by_cust.get(r["Customer"] or "?", 0) + r["Balance"]
            st.write("Outstanding by customer")
            st.bar_chart(by_cust)
            st.dataframe(table, hide_index=True)
        st.write("Estimates in QuickBooks (change orders created here are numbered CO-...)")
        st.dataframe([{"Customer": e.get("CustomerRef", {}).get("name"), "Number": e.get("DocNumber"),
                       "Total": e.get("TotalAmt"), "Status": e.get("TxnStatus")} for e in data["est"]], hide_index=True)