# app/services/pdf_parser.py
import re
from statistics import median
from typing import List, Sequence, Tuple

import pdfplumber
from app.services.chunker import clean_pdf_text

_HEADER_DATE = re.compile(r"^\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}.*$")
_URL_LINE = re.compile(r"^https?://\S+.*$", re.IGNORECASE)
_PAGE_COUNT = re.compile(r"^(?:\d+\s*/\s*\d+|Page\s+\d+\s+of\s+\d+)$", re.IGNORECASE)
_PRIVATE_USE = re.compile(r"[\ue000-\uf8ff]")
_BULLET = re.compile(r"^\s*(?:[•●▪◦·\uf0b7*\-–])\s*(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)、]\s+")
_NAV_NOISE = ("Seller Central Help", "Policies, agreements, and guidelines")
_TERMINAL = re.compile(r"[.!?。！？,，;；:]\s*$")


def clean_page_text(text: str) -> str:
    """Remove common browser-print margins and repair PDF extraction noise."""
    text = text or ""
    text = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[A-Za-z])", "", text)
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]

    def is_margin(line: str) -> bool:
        return bool(_HEADER_DATE.match(line) or _URL_LINE.match(line) or _PAGE_COUNT.match(line) or re.fullmatch(r"\d+", line))

    while lines and (not lines[0] or is_margin(lines[0])):
        lines.pop(0)
    while lines and (not lines[-1] or is_margin(lines[-1])):
        lines.pop()
    nav_indexes = set()
    for index in range(min(5, len(lines))):
        line = lines[index].casefold()
        if all(phrase.casefold() in line for phrase in _NAV_NOISE):
            nav_indexes.add(index)
        elif index + 1 < len(lines):
            next_line = lines[index + 1].casefold()
            if any(phrase.casefold() in line for phrase in _NAV_NOISE) and any(
                phrase.casefold() in next_line for phrase in _NAV_NOISE
            ):
                nav_indexes.update((index, index + 1))
    lines = [line for index, line in enumerate(lines) if index not in nav_indexes]
    return clean_pdf_text("\n".join(_PRIVATE_USE.sub(" ", line) for line in lines))


def _extract_lines(page) -> List[dict]:
    """Collect line text and font metrics from pdfplumber word records."""
    try:
        words = page.extract_words(extra_attrs=["size", "fontname"], x_tolerance=2, y_tolerance=3)
    except (TypeError, ValueError, KeyError):
        words = []
    if not words:
        text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
        return [{"text": line, "size": None, "fontname": "", "x0": 0.0} for line in text.splitlines()]

    words.sort(key=lambda word: (float(word.get("top", 0)), float(word.get("x0", 0))))
    grouped: List[List[dict]] = []
    tops: List[float] = []
    for word in words:
        top = float(word.get("top", 0))
        if not grouped or abs(top - tops[-1]) > 3:
            grouped.append([word])
            tops.append(top)
        else:
            grouped[-1].append(word)

    lines = []
    for row in grouped:
        row.sort(key=lambda word: float(word.get("x0", 0)))
        sizes = [float(word["size"]) for word in row if word.get("size") is not None]
        fonts = [str(word.get("fontname", "")) for word in row]
        lines.append({
            "text": " ".join(str(word.get("text", "")) for word in row).strip(),
            "size": sum(sizes) / len(sizes) if sizes else None,
            "fontname": " ".join(fonts),
            "x0": min(float(word.get("x0", 0)) for word in row),
        })
    return lines


def _format_page_lines(lines: List[dict]) -> str:
    sizes = [line["size"] for line in lines if line.get("size")]
    body_size = median(sizes) if sizes else None
    body_left = min((line["x0"] for line in lines), default=0.0)
    output: List[str] = []
    previous_was_colon = False
    in_list = False

    for line_index, item in enumerate(lines):
        text = _PRIVATE_USE.sub(" ", str(item.get("text", ""))).strip()
        if not text:
            continue
        if line_index in (0, len(lines) - 1) and (
            _HEADER_DATE.match(text)
            or _URL_LINE.match(text)
            or _PAGE_COUNT.match(text)
            or re.fullmatch(r"\d+", text)
        ):
            continue
        bullet = _BULLET.match(text)
        numbered = bool(_NUMBERED.match(text))
        indentation = float(item.get("x0", body_left)) > body_left + 8
        short_open_line = len(text) <= 100 and not _TERMINAL.search(text)
        if bullet:
            text = "- " + bullet.group(1).strip()
            in_list = True
        elif numbered:
            in_list = True
        elif previous_was_colon and short_open_line:
            text = "- " + text
            in_list = True
        elif in_list and (short_open_line or indentation):
            text = "- " + text
            in_list = True
        else:
            in_list = False

        clean_text = text[2:].strip() if text.startswith("- ") else text
        is_list = text.startswith("- ") or numbered
        valid_title_shape = (
            3 <= len(clean_text) <= 90
            and not clean_text.endswith((".", "。", ",", "，", ";", "；"))
            and not is_list
        )
        font_name = str(item.get("fontname", "")).casefold()
        bold = any(marker in font_name for marker in ("bold", "semibold", "medi", "heavy"))
        size = item.get("size")
        true_title = valid_title_shape and not previous_was_colon and (
            (body_size is not None and size is not None and size >= body_size * 1.12)
            or bold
            or (body_size is None and not previous_was_colon and not in_list and clean_text.istitle())
        )
        if true_title:
            if output and output[-1]:
                output.append("")
            output.append(f"## {clean_text}")
            output.append("")
            in_list = False
        else:
            output.append(text)
        previous_was_colon = text.endswith((":", "："))
    return "\n".join(output)


def combine_pages(pages: Sequence[str]) -> Tuple[str, List[Tuple[int, int, int]]]:
    """Join cleaned page text into one stream and retain source page ranges."""
    parts: List[str] = []
    page_ranges: List[Tuple[int, int, int]] = []
    offset = 0
    previous = ""
    for page_num, raw_page in enumerate(pages):
        page_text = clean_page_text(raw_page)
        if not page_text:
            continue
        if parts:
            first_line = page_text.splitlines()[0]
            page_separator = "\n\n" if re.search(r"[.!?。！？:：;；]$", previous.strip()) else " "
            if first_line.startswith(("## ", "- ")) or _NUMBERED.match(first_line) or (
                len(first_line) <= 85
                and (first_line.istitle() or first_line.isupper() or first_line.endswith((":", "：")))
            ):
                page_separator = "\n\n"
            parts.append(page_separator)
            offset += len(page_separator)
        start = offset
        parts.append(page_text)
        offset += len(page_text)
        page_ranges.append((start, offset, page_num))
        previous = page_text
    return "".join(parts), page_ranges


def extract_text_by_page(path: str) -> List[str]:
    """Backward-compatible per-page extraction with margin and glyph cleanup."""
    with pdfplumber.open(path) as pdf:
        return [clean_page_text(_format_page_lines(_extract_lines(page))) for page in pdf.pages]


def extract_document_text(path: str) -> Tuple[str, List[Tuple[int, int, int]]]:
    """Extract a full PDF text stream and the character range belonging to each page."""
    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            pages.append(_format_page_lines(_extract_lines(page)))
    return combine_pages(pages)
