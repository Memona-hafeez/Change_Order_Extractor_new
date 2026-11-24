"""Evaluate the pipeline against ground truth.

    python eval.py                      # the 6 hand-made samples in samples/
    python eval.py eval_set/dev         # tune on DEV only
    python eval.py eval_set/test        # run TEST once, when you want a final number

Besides field accuracy it reports what matters for a human-in-the-loop system: of the fields that
were auto-accepted (confidence >= REVIEW_THRESHOLD), how many were wrong, and how many fields went
to review. File names ending in _text / _scan_clean / _scan_noisy / _scan_bad are also grouped by variant.
"""
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from difflib import SequenceMatcher
from pipeline import process, SUFFIXES
from validate import canon
from schema import FIELDS
from confidence import REVIEW_THRESHOLD

SKIP = {"description_of_change"}  # free text, not scored
EVAL_FIELDS = [f for f in FIELDS if f not in SKIP]
VARIANT = re.compile(r"_(text|scan_clean|scan_noisy|scan_bad)$")


def match(f, exp, got):
    e, g = canon(f, exp), canon(f, got)
    if e is None or g is None:
        return e == g
    if isinstance(e, str):
        return SequenceMatcher(None, e, g).ratio() >= 0.85
    if isinstance(e, float):
        return abs(e - g) < 0.01
    return e == g


def main(folder):
    folder = Path(folder)
    correct = {f: 0 for f in EVAL_FIELDS}
    total = {f: 0 for f in EVAL_FIELDS}
    by_variant = defaultdict(lambda: [0, 0])
    conf_ok, conf_bad, dangerous = [], [], []
    accepted = accepted_wrong = flagged = flagged_wrong = 0

    files = sorted(p for p in folder.glob("*") if p.suffix.lower() in SUFFIXES)
    for p in files:
        exp_path = folder / "expected" / f"{p.stem}.json"
        if not exp_path.exists():
            print(f"SKIP {p.name}: no expected JSON in {folder}/expected/")
            continue
        exp = json.loads(exp_path.read_text())
        print("Evaluating", p.name)
        try:
            res = process(p)
        except Exception as e:
            print(f"  FAILED: {e}")
            continue
        m = VARIANT.search(p.stem)
        for f in EVAL_FIELDS:
            got = res["fields"][f]
            ok = match(f, exp.get(f), got["value"])
            total[f] += 1
            correct[f] += ok
            (conf_ok if ok else conf_bad).append(got["confidence"])
            if m:
                by_variant[m.group(1)][0] += ok
                by_variant[m.group(1)][1] += 1
            if got["confidence"] >= REVIEW_THRESHOLD:
                accepted += 1
                accepted_wrong += (not ok)
            else:
                flagged += 1
                flagged_wrong += (not ok)
            if not ok:
                print(f"  WRONG {f}: got {got['value']!r}, expected {exp.get(f)!r}, conf {got['confidence']}")
                if got["confidence"] >= REVIEW_THRESHOLD:
                    dangerous.append((p.name, f, got["value"], exp.get(f), got["confidence"]))
        time.sleep(15)  # free-tier rate limits (8000 tokens/min)

    print("\n=== Field accuracy ===")
    for f in EVAL_FIELDS:
        print(f"{f:26s} {correct[f]}/{total[f]}")
    all_c, all_t = sum(correct.values()), sum(total.values())
    if all_t:
        print(f"\nOVERALL: {all_c}/{all_t} = {all_c / all_t:.0%}")
    if by_variant:
        print("\n=== By input variant ===")
        for v, (c, t) in sorted(by_variant.items()):
            print(f"{v:12s} {c}/{t} = {c / t:.0%}")
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else "n/a"
    print(f"\nAvg confidence when correct: {avg(conf_ok)} | when wrong: {avg(conf_bad)}")
    if all_t:
        print(f"\n=== Review workload (threshold {REVIEW_THRESHOLD}) ===")
        print(f"Auto-accepted fields: {accepted}/{all_t} ({accepted / all_t:.0%}); "
              f"wrong among them: {accepted_wrong} ({accepted_wrong / max(accepted, 1):.1%})")
        print(f"Sent to review:       {flagged}/{all_t} ({flagged / all_t:.0%}); "
              f"of which actually wrong: {flagged_wrong}")
    print("\nWrong but confident (>=0.7), the dangerous cases:")
    for d in dangerous:
        print("  ", d)
    if not dangerous:
        print("   none")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "samples")
