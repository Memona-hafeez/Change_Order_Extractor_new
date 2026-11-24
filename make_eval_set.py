"""Generate a larger synthetic evaluation set with ground truth, including scan-like degraded copies.

    python make_eval_set.py            # writes eval_set/dev and eval_set/test

Each document is rendered in one of four variants:
    text        normal PDF with a text layer
    scan_clean  rasterised, no text layer (forces the OCR path)
    scan_noisy  slight rotation, blur, noise, JPEG compression at 150 dpi
    scan_bad    stronger rotation, blur, noise, heavy JPEG at 110 dpi

The split is fixed by document index (every third document is TEST) so prompts and validators can be
tuned on DEV only. Ground truth is written to <split>/expected/<name>.json; the key "_rendered" holds
the exact strings printed on the page, used by ocr_check.py to measure OCR recall without any LLM.

Everything here is SYNTHETIC. It is cleaner than real faxes and photographs, so it is a regression
and OCR-robustness test, not a benchmark of real-world accuracy.
"""
import io
import json
import random
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

SEED = 42
N_DOCS = 30
VARIANTS = ["text", "scan_clean", "scan_noisy", "scan_bad"]
OUT = Path("eval_set")

PROJECTS = ["Riverside Medical Center - Phase 2", "Maple Street Townhomes", "Harbor View Warehouse",
            "Lakeside School Gym Renovation", "Oak Ridge Public Library", "Cedar Point Apartments",
            "Northgate Fire Station 4", "Willow Creek Elementary Addition", "Summit Ridge Office Park",
            "Pinecrest Senior Living", "Eastside Transit Hub", "Brookfield Distribution Center"]
OWNERS = ["Riverside Health Partners LLC", "Maple Dev Group", "Harbor Logistics Co", "Lakeside School District",
          "City of Oak Ridge", "Cedar Point Holdings", "Northgate Fire District", "Willow Creek USD",
          "Summit Ridge Partners", "Pinecrest Care Group", "Eastside Transit Authority", "Brookfield Realty Trust"]
CONTRACTORS = ["Apex Builders Inc.", "Brightline Construction", "Steelman Contractors LLC", "Tri-County Builders",
               "Ironwood Construction Company", "Redwood General Contracting", "Granite State Builders",
               "Blue Harbor Construction", "Keystone Contractors Inc.", "Lone Oak Builders"]
SIGNERS = ["John Smith", "Maria Lopez", "Priya Nair", "Daniel Okafor", "Susan Whitfield", "Tom Becker"]
SCOPES = ["Add two additional electrical outlets in each patient room on Level 3.",
          "Install additional fire suppression sprinkler heads in Zone B per fire marshal request.",
          "Replace specified vinyl flooring with polished concrete in the lobby.",
          "Relocate the main water line to avoid an unmarked utility found during excavation.",
          "Add a secondary scoreboard wiring run in the gymnasium.",
          "Upgrade rooftop HVAC unit capacity from 10 to 15 tons.",
          "Owner deleted the rooftop deck from the scope of work.",
          "Install additional blocking for wall-mounted equipment in exam rooms."]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


def money(v, style):
    neg = v < 0
    a = abs(v)
    s = f"${a:,.2f}" if style == "cents" else (f"${a:,.0f}" if style == "dollars" else f"{a:,.0f}")
    return f"({s})" if neg else s


def make_doc(rng, idx):
    orig = rng.randrange(400, 4000) * 1000
    prev = rng.choice([0, rng.randrange(5, 90) * 1000, None])
    credit = rng.random() < 0.2
    chg = rng.randrange(8, 200) * 250 * (-1 if credit else 1)
    new = orig + (prev or 0) + chg
    days = 0 if rng.random() < 0.3 else rng.randrange(2, 20) * (-1 if credit and rng.random() < 0.5 else 1)
    project = rng.choice(PROJECTS)
    year, mon, day = 2026, rng.randrange(1, 13), rng.randrange(13, 29)   # day > 12: never ambiguous
    signed = rng.choice([True, False, False]) if rng.random() < 0.8 else None
    return dict(idx=idx, orig=orig, prev=prev, chg=chg, new=new, days=days, project=project,
                owner=rng.choice(OWNERS), contractor=rng.choice(CONTRACTORS), no=rng.randrange(1, 40),
                year=year, mon=mon, day=day, signer=rng.choice(SIGNERS), signed=signed,
                scope="Owner deleted the rooftop deck from the scope of work." if credit else rng.choice(SCOPES),
                credit=credit)


def line_items(rng, chg):
    """2-3 priced rows whose totals add up exactly to the change amount."""
    n = rng.choice([2, 3])
    rows, remaining = [], chg
    names = rng.sample(["Materials", "Labor", "Equipment rental", "Permit fee", "Subcontractor markup"], n)
    for i, name in enumerate(names):
        if i == n - 1:
            total = remaining
        else:
            total = round(remaining * rng.choice([0.3, 0.4, 0.5]) / 5) * 5
        remaining -= total
        qty = rng.choice([1, 10, 20, 40, 100])
        rows.append((name, qty, total / qty, total))
    return rows


def render_text(d, rng):
    """Returns (text, expected, rendered). `rendered` = the strings as printed, for the OCR recall check."""
    tpl = d["idx"] % 3
    dt_styles = [f"{MONTHS[d['mon'] - 1]} {d['day']}, {d['year']}",
                 f"{d['year']}-{d['mon']:02d}-{d['day']:02d}",
                 f"{d['day']} {MONTHS[d['mon'] - 1][:3]} {d['year']}"]
    date_txt = dt_styles[rng.randrange(3)]
    exp = dict(project_name=d["project"], change_order_number=str(d["no"]),
               date=f"{d['year']}-{d['mon']:02d}-{d['day']:02d}", contractor=d["contractor"], owner=d["owner"],
               original_contract_sum=d["orig"], previous_changes_total=d["prev"], change_amount=d["chg"],
               new_contract_sum=d["new"], schedule_change_days=d["days"], signed=d["signed"])
    rend = dict(project_name=d["project"], change_order_number=str(d["no"]), date=date_txt,
                contractor=d["contractor"], owner=d["owner"])
    sign_line = {True: f"Approved: {d['signer']}  Signed: /s/ {d['signer']}",
                 False: "Signature: ____________ (unsigned)",
                 None: f"Prepared by: {d['signer']}"}[d["signed"]]
    if d["signed"] is None:
        exp["signed"] = None

    if tpl == 0:   # formal
        m = lambda v: money(v, "cents")
        lines = [f"CHANGE ORDER NO. {d['no']:03d}", f"Project: {d['project']}", f"Date: {date_txt}",
                 f"Owner: {d['owner']}", f"Contractor: {d['contractor']}", "", "Description of Change:", d["scope"], ""]
        lines += [f"Original Contract Sum: {m(d['orig'])}"]
        if d["prev"] is not None:
            lines += [f"Net change by previous Change Orders: {m(d['prev'])}"]
        lines += [f"This Change Order: {m(d['chg'])}", f"New Contract Sum: {m(d['new'])}", "",
                  f"Contract time change: {d['days']} days"]
        if not d["credit"] and rng.random() < 0.6:
            lines += ["", "Line items:"] + [f"{n} - {q} ea @ {u:,.2f} = {t:,.2f}" for n, q, u, t in line_items(rng, d["chg"])]
        lines += ["", sign_line]
        rend.update(original_contract_sum=m(d["orig"]), change_amount=m(d["chg"]), new_contract_sum=m(d["new"]))
    elif tpl == 1:  # abbreviated labels
        m = lambda v: money(v, "dollars")
        dl = f"{abs(d['days'])} calendar days" + (" (decrease)" if d["days"] < 0 else "")
        lines = [f"CO # {d['no']}", f"Job: {d['project']}", f"Dated {date_txt}", f"Owner : {d['owner']}",
                 f"GC : {d['contractor']}", f"Scope: {d['scope']}", f"Original contract   {m(d['orig'])}"]
        if d["prev"] is not None:
            lines += [f"Prior approved COs   {m(d['prev'])}"]
        lines += [f"THIS CO   {m(d['chg'])}", f"Adjusted contract sum   {m(d['new'])}", f"Time extension: {dl}", sign_line]
        rend.update(original_contract_sum=m(d["orig"]), change_amount=m(d["chg"]), new_contract_sum=m(d["new"]))
    else:           # email style
        m = lambda v: money(v, "plain")
        cost = money(d["chg"], "dollars")
        time_txt = "No time impact." if d["days"] == 0 else (
            f"Adds {d['days']} days." if d["days"] > 0 else f"Reduces the schedule by {abs(d['days'])} days.")
        sign_txt = {True: f"Signed: /s/ {d['signer']}", False: "Not yet signed by the owner.",
                    None: ""}[d["signed"]]
        lines = [f"Subject: CO {d['no']} - {d['project'].split(' - ')[0]}",
                 f"Hi team, attaching CO {d['no']} for {d['project']} dated {date_txt}.",
                 f"Owner is {d['owner']}, contractor {d['contractor']}.",
                 f"{d['scope']} Cost is {cost}.",
                 f"Contract was {m(d['orig'])} and now becomes {m(d['new'])}. {time_txt}", sign_txt]
        exp["previous_changes_total"] = None
        rend.update(original_contract_sum=m(d["orig"]), change_amount=cost, new_contract_sum=m(d["new"]))
    return "\n".join(lines), exp, rend


def write_text_pdf(path, text):
    c = canvas.Canvas(str(path), pagesize=letter)
    y = 740
    for line in text.split("\n"):
        c.setFont("Helvetica", 12)
        c.drawString(54, y, line)
        y -= 18
        if y < 60:
            c.showPage()
            y = 740
    c.save()


def degrade(img, variant, rng):
    if variant == "scan_clean":
        return img
    p = dict(scan_noisy=dict(rot=1.5, blur=0.8, sigma=12, q=45, scale=150 / 200),
             scan_bad=dict(rot=3.0, blur=1.3, sigma=25, q=30, scale=110 / 200))[variant]
    w, h = img.size
    img = img.resize((int(w * p["scale"]), int(h * p["scale"])), Image.LANCZOS)
    img = img.rotate(rng.uniform(-p["rot"], p["rot"]), expand=False, fillcolor=255, resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(p["blur"]))
    arr = np.asarray(img, dtype=np.float32)
    nrng = np.random.default_rng(rng.randrange(10**9))
    arr = np.clip(arr + nrng.normal(0, p["sigma"], arr.shape), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=p["q"])
    return Image.open(io.BytesIO(buf.getvalue())).convert("L")


def render_variant(text_pdf, out_pdf, variant, rng):
    if variant == "text":
        out_pdf.write_bytes(Path(text_pdf).read_bytes())
        return
    img = pdfium.PdfDocument(str(text_pdf))[0].render(scale=200 / 72).to_pil().convert("L")
    degrade(img, variant, rng).save(str(out_pdf), "PDF", resolution=150)


def main():
    rng = random.Random(SEED)
    for split in ("dev", "test"):
        (OUT / split / "expected").mkdir(parents=True, exist_ok=True)
    tmp = OUT / "_tmp.pdf"
    counts = {"dev": 0, "test": 0}
    for i in range(N_DOCS):
        d = make_doc(rng, i)
        text, exp, rend = render_text(d, rng)
        variant = VARIANTS[i % len(VARIANTS)]
        split = "test" if i % 3 == 2 else "dev"
        name = f"co_{i:02d}_{variant}"
        write_text_pdf(tmp, text)
        render_variant(tmp, OUT / split / f"{name}.pdf", variant, rng)
        exp["_rendered"] = rend
        (OUT / split / "expected" / f"{name}.json").write_text(json.dumps(exp, indent=2))
        counts[split] += 1
    tmp.unlink()
    print(f"wrote {counts['dev']} dev and {counts['test']} test documents to {OUT}/")


if __name__ == "__main__":
    main()
