import shutil
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from pdf_reader import read_document, read_input, Document

LINES = ["CHANGE ORDER NO. 014", "Project: Harbor View Warehouse", "Owner: Harbor Logistics Co",
         "Original Contract Sum: $2,100,000.00", "This Change Order: $18,750.00",
         "New Contract Sum: $2,118,750.00", "Contract time change: 10 days", "Approved: Maria Lopez (signed)"]

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def text_pdf(path):
    c = canvas.Canvas(str(path), pagesize=letter)
    y = 720
    for line in LINES:
        c.setFont("Helvetica", 14)
        c.drawString(72, y, line)
        y -= 26
    c.save()


def scanned_pdf(src, dst):
    """Rasterise a text PDF and save it as an image-only PDF (no text layer), like a scan."""
    import pypdfium2 as pdfium
    img = pdfium.PdfDocument(str(src))[0].render(scale=200 / 72).to_pil().convert("L")
    img.save(str(dst), "PDF", resolution=200)


def test_plain_text_input(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello change order")
    d = read_document(p)
    assert d.text == "hello change order" and not d.ocr_used and d.mean_ocr_confidence is None


def test_text_pdf_does_not_use_ocr(tmp_path):
    p = tmp_path / "t.pdf"
    text_pdf(p)
    d = read_document(p)
    assert not d.ocr_used
    assert "$18,750.00" in d.text
    assert read_input(p) == d.text


@needs_tesseract
def test_scanned_pdf_falls_back_to_ocr_and_keeps_the_numbers(tmp_path):
    src, scan = tmp_path / "t.pdf", tmp_path / "scan.pdf"
    text_pdf(src)
    scanned_pdf(src, scan)
    d = read_document(scan)
    assert d.ocr_used and d.ocr_words
    assert 0.5 < d.mean_ocr_confidence <= 1.0
    flat = d.text.replace(",", "")
    assert "18750.00" in flat and "2118750.00" in flat
    assert "Harbor" in d.text


@needs_tesseract
def test_image_input_is_ocred(tmp_path):
    import pypdfium2 as pdfium
    src = tmp_path / "t.pdf"
    text_pdf(src)
    png = tmp_path / "t.png"
    pdfium.PdfDocument(str(src))[0].render(scale=200 / 72).to_pil().convert("L").save(png)
    d = read_document(png)
    assert d.ocr_used and "Harbor" in d.text


@needs_tesseract
def test_pipeline_marks_scanned_input(tmp_path, monkeypatch):
    import pipeline
    src, scan = tmp_path / "t.pdf", tmp_path / "scan.pdf"
    text_pdf(src)
    scanned_pdf(src, scan)
    run = {f: {"value": None, "evidence": None, "confidence": 0} for f in pipeline.FIELDS}
    run["change_amount"] = {"value": "$18,750.00", "evidence": "This Change Order: $18,750.00", "confidence": 0.9}
    run["line_items"] = []
    monkeypatch.setattr(pipeline, "call_llm", lambda text, temperature=0.0: dict(run))
    res = pipeline.process(scan)
    assert res["input"]["ocr_used"] is True
    assert any("OCR" in m for m in res["document_issues"])
    assert res["fields"]["change_amount"]["value"] == 18750.0
