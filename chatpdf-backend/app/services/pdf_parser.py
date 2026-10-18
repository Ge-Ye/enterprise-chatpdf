# app/services/pdf_parser.py
import pdfplumber
from typing import List

def extract_text_by_page(path: str) -> List[str]:
    pages = []
    with pdfplumber.open(path) as pdf:
        for p in pdf.pages:
            text = p.extract_text()
            if text:
                pages.append(text)
    return pages
