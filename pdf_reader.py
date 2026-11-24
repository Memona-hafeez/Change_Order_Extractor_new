"""Input reading: text PDFs, plain text, and scanned PDFs / images via OCR.

read_document() returns the text plus, when OCR was used, a per-word confidence list that the
confidence scorer uses as an extra signal. read_input() keeps the old text-only interface.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import pdfplumber

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")
OCR_DPI = 300
MIN_TEXT_CHARS = 30


@dataclass
class Document:
    text: str
    ocr_words: Optional[List[Tuple[str, float]]] = None  # (word, tesseract conf 0-100) if OCR was used

    @property
    def ocr_used(self):
        return self.ocr_words is not None

    @property
    def mean_ocr_confidence(self):
        """Mean word confidence on a 0-1 scale, or None when no OCR was used."""
        if not self.ocr_words:
            return None
        return round(sum(c for _, c in self.ocr_words) / len(self.ocr_words) / 100.0, 3)


def _find_tesseract(pytesseract):
    """Use TESSERACT_CMD from the environment, or the usual Windows install folders, when it is not on PATH."""
    import os
    import shutil
    if shutil.which("tesseract"):
        return
    candidates = [os.environ.get("TESSERACT_CMD", ""),
                  r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                  r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"]
    for path in candidates:
        if path and os.path.isfile(path):
            pytesseract.pytesseract.tesseract_cmd = path
            return


def ocr_image(img):
    """OCR one PIL image. Returns (text, [(word, conf), ...]). Lines are kept so labels and values stay together."""
    try:
        import pytesseract
        from pytesseract import Output
    except ImportError as e:
        raise RuntimeError("OCR needs pytesseract (pip install pytesseract) and the Tesseract binary") from e
    _find_tesseract(pytesseract)
    try:
        data = pytesseract.image_to_data(img, output_type=Output.DICT, config="--psm 6")
    except pytesseract.TesseractNotFoundError as e:
        raise RuntimeError(
            "Tesseract is not installed or not on PATH. Windows: install it from "
            "https://github.com/UB-Mannheim/tesseract/wiki (default folder), then restart the terminal. "
            "Linux: apt install tesseract-ocr. You can also set TESSERACT_CMD in .env to the full path of tesseract.exe."
        ) from e

    lines, words, order = {}, [], []
    for i, raw in enumerate(data["text"]):
        word = (raw or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append(word)
        words.append((word, conf))
    text = "\n".join(" ".join(lines[k]) for k in order)
    return text, words


def _ocr_pdf(path):
    try:
        import pypdfium2 as pdfium
    except ImportError as e:
        raise RuntimeError("Scanned PDFs need pypdfium2 (pip install pypdfium2)") from e
    pdf = pdfium.PdfDocument(str(path))
    texts, words = [], []
    for page in pdf:
        img = page.render(scale=OCR_DPI / 72).to_pil().convert("L")
        t, w = ocr_image(img)
        texts.append(t)
        words.extend(w)
    return "\n".join(texts).strip(), words


def read_document(path) -> Document:
    p = Path(path)
    suffix = p.suffix.lower()

    if suffix in IMAGE_SUFFIXES:
        from PIL import Image
        text, words = ocr_image(Image.open(p).convert("L"))
        if len(text) < MIN_TEXT_CHARS:
            raise ValueError(f"{p.name}: OCR found almost no text")
        return Document(text, words)

    if suffix == ".pdf":
        pages = []
        with pdfplumber.open(p) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text(layout=True) or "")
        text = "\n".join(pages).strip()
        if len(text) >= MIN_TEXT_CHARS:
            return Document(text)
        # No text layer: treat as a scan and OCR it.
        text, words = _ocr_pdf(p)
        if len(text) < MIN_TEXT_CHARS:
            raise ValueError(f"{p.name}: no text layer and OCR found almost no text")
        return Document(text, words)

    return Document(p.read_text(encoding="utf-8", errors="ignore"))


def read_input(path):
    """Text only (kept for backward compatibility)."""
    return read_document(path).text