import os
import json
import time
from dotenv import load_dotenv
from schema import FIELDS, FIELD_HELP
from chunker import select_relevant

load_dotenv()
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

_client = None


def get_client():
    """Create the Groq client lazily so tests and validators import without an API key."""
    global _client
    if _client is None:
        from groq import Groq
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set. Copy .env.example to .env and add your key.")
        _client = Groq(api_key=key)
    return _client


field_lines = "\n".join(f'- "{f}": {FIELD_HELP[f]}' for f in FIELDS)

SYSTEM = f"""You are an information extraction engine for construction change orders.
Return ONLY one valid JSON object (JSON, no markdown, no commentary).

For EACH field below return an object: {{"value": ..., "evidence": "...", "confidence": 0.0-1.0}}
- "evidence" must be an EXACT copy of the shortest snippet from the document that supports the value. Never paraphrase it.
- For money and day-count fields the evidence snippet MUST contain the number itself exactly as written.
- If the document does not state the field, use {{"value": null, "evidence": null, "confidence": 0}}.
- NEVER guess and NEVER calculate values that are not written in the document.
- "confidence" is your honest certainty. Lower it if the text is ambiguous, garbled or has several candidate values.

Layout notes:
- The text comes from a PDF or an OCR scan; labels and values can be on separate lines or in different columns. A value usually sits directly below or beside its label. OCR text may contain character mistakes (0 for O, l for 1): copy exactly what you see.
- "owner" and "contractor" must be company or person names, never a project or building name. If the owner's name appears only in a signature block (e.g. "OWNER (Firm name)" followed by a name), use that.
- Checkbox labels such as "OWNER X" are not names.
- "project_name": when the project is named more than once with different wording (for example a short form in an email subject and a longer form in the body), use the most complete name from the body.
- "signed" is true only if the document shows signatures, "/s/", or signer names with dates in the signature area. In that case quote one signer line as evidence.
- "signed" is false ONLY if the document visibly shows an unsigned or blank signature line, or says it is unsigned. If the document says nothing about signatures or approval, "signed" must be null (not false).
- "description_of_change": include ALL listed change items, joined with "; ".

Fields:
{field_lines}

Also return "line_items": a list of objects with keys description, quantity, unit_price, total
(copied as written; empty list if the document has no itemized lines). If the document lists
priced rows (quantity, unit price, line total) you MUST list every row.
"""

LINE_ITEMS_SYSTEM = """You extract itemized price rows from a construction change order.
Return ONLY one JSON object: {"line_items": [{"description": ..., "quantity": ..., "unit_price": ..., "total": ...}]}.
Copy values exactly as written. Include every priced row (quantity, unit price, line total) and
nothing else: no summary rows such as contract sums, totals of previous change orders or the new contract sum.
If there are no priced rows return {"line_items": []}."""


def _chat(system, text, temperature, retries):
    user = f'DOCUMENT:\n"""\n{text}\n"""'
    for attempt in range(retries):
        try:
            resp = get_client().chat.completions.create(
                model=MODEL,
                temperature=temperature,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
            return json.loads(resp.choices[0].message.content)
        except RuntimeError:
            raise  # missing API key: retrying will not help
        except Exception as e:
            wait = 10 * (attempt + 1)
            print(f"  LLM error ({e}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError("LLM failed after retries")


def call_llm(text, temperature=0.0, retries=5):
    return _chat(SYSTEM, select_relevant(text), temperature, retries)


def call_llm_line_items(text, retries=5):
    """Focused second pass used only when the document looks itemized but the main call returned no rows."""
    out = _chat(LINE_ITEMS_SYSTEM, select_relevant(text), 0.0, retries)
    items = out.get("line_items") if isinstance(out, dict) else None
    return items if isinstance(items, list) else []
