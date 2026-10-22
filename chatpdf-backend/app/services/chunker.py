# app/services/chunker.py
from typing import List, Optional, Sequence, Tuple
import uuid
import re

_LIST_START = re.compile(r"^\s*(?:[-*•●▪◦·]|\d+[.)、])\s+")
_REAL_HEADER = re.compile(r"^\s*##\s+(.+?)\s*$")
_TERMINAL = re.compile(r"[.!?。！？]\s*$")
_SPLIT_SEPARATORS = ("\n\n", "\n- ", "\n", "。", ". ", "；", "; ", "，", ", ", " ")


def clean_pdf_text(text: str) -> str:
    """Join extraction-induced line wraps while retaining headings, paragraphs and lists."""
    text = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[A-Za-z])", "", text)
    text = re.sub(r"[\ue000-\uf8ff]", " ", text)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output: List[str] = []
    for raw in lines:
        indent = len(raw) - len(raw.lstrip())
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            if output and output[-1] != "":
                output.append("")
            continue
        if not output or output[-1] == "":
            output.append(line)
            continue
        previous = output[-1]
        if indent and _LIST_START.match(previous) and not _LIST_START.match(line):
            output.append("  " + line)
            continue
        if (
            line.startswith("## ")
            or previous.startswith("## ")
            or _TERMINAL.search(previous)
            or _LIST_START.match(line)
            or _LIST_START.match(previous)
        ):
            output.append(line)
        else:
            output[-1] = previous + " " + line
    return "\n".join(output).strip()


def _split_long(text: str, limit: int) -> List[str]:
    if len(text) <= limit:
        return [text]
    for separator in _SPLIT_SEPARATORS:
        boundary = text.rfind(separator, 1, limit + 1)
        if boundary < 0:
            continue
        cut = boundary if separator.startswith("\n") else boundary + len(separator)
        left, right = text[:cut].strip(), text[cut:].strip()
        if left and right:
            return _split_long(left, limit) + _split_long(right, limit)
    boundary = text.rfind(" ", 1, limit + 1)
    if boundary < 0:
        return [text]
    return _split_long(text[:boundary].strip(), limit) + _split_long(text[boundary + 1:].strip(), limit)


def _page_for_offset(start: int, page_ranges: Optional[Sequence[Tuple[int, int, int]]]) -> int:
    if not page_ranges:
        return 0
    for range_start, range_end, page_num in page_ranges:
        if range_start <= start < range_end:
            return page_num
    return page_ranges[-1][2]


def _is_list_line(line: str) -> bool:
    return bool(_LIST_START.match(line))


def _blocks(lines: List[str]) -> List[str]:
    """Build paragraph and colon-led list blocks without classifying short prose as headings."""
    result: List[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line.endswith((":", "：")):
            block = [line]
            index += 1
            in_list = False
            while index < len(lines):
                candidate = lines[index].rstrip()
                if not candidate.strip():
                    # Blank lines inside an enumerated block are tolerated.
                    lookahead = index + 1
                    while lookahead < len(lines) and not lines[lookahead].strip():
                        lookahead += 1
                    if lookahead < len(lines) and _is_list_line(lines[lookahead]):
                        index = lookahead
                        continue
                    break
                if _is_list_line(candidate):
                    block.append(candidate.strip())
                    in_list = True
                    index += 1
                    continue
                if in_list and candidate[:1].isspace():
                    block.append(candidate.strip())
                    index += 1
                    continue
                break
            result.append("\n".join(block))
            continue
        if _is_list_line(line):
            block = [line]
            index += 1
            while index < len(lines):
                candidate = lines[index].rstrip()
                if _is_list_line(candidate):
                    block.append(candidate.strip())
                    index += 1
                elif candidate.strip() and candidate[:1].isspace():
                    block.append(candidate.strip())
                    index += 1
                elif not candidate.strip():
                    lookahead = index + 1
                    while lookahead < len(lines) and not lines[lookahead].strip():
                        lookahead += 1
                    if lookahead < len(lines) and _is_list_line(lines[lookahead]):
                        index = lookahead
                    else:
                        break
                else:
                    break
            result.append("\n".join(block))
            continue

        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip() and not _is_list_line(lines[index]) and not lines[index].strip().endswith((":", "：")):
            paragraph.append(lines[index].strip())
            index += 1
        result.append(" ".join(paragraph))
    return result


def _split_locked_list(block: str, limit: int) -> List[str]:
    lines = block.splitlines()
    if len(lines) <= 1 or not lines[0].endswith((":", "：")):
        return _split_long(block, limit)
    groups = [lines[0]]
    for line in lines[1:]:
        if _is_list_line(line):
            groups.append(line)
        elif groups:
            groups[-1] += "\n" + line
    chunks: List[str] = []
    current = ""
    for group in groups:
        candidate = f"{current}\n{group}" if current else group
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = group
    if current:
        chunks.append(current)
    return chunks


def _split_sections(text: str) -> List[Tuple[str, List[str]]]:
    sections: List[Tuple[str, List[str]]] = []
    title = ""
    body: List[str] = []
    for line in text.splitlines():
        match = _REAL_HEADER.match(line)
        if match:
            if title or body:
                sections.append((title, body))
            title = match.group(1).strip()
            body = []
        else:
            body.append(line)
    if title or body:
        sections.append((title, body))
    return sections


def chunk_text(
    text: str,
    chunk_size: int = 900,
    overlap: int = 180,
    *,
    page_ranges: Optional[Sequence[Tuple[int, int, int]]] = None,
    doc_id: Optional[str] = None,
) -> List[dict]:
    """Split only at explicit parser headings and protect colon-led list groups."""
    text = clean_pdf_text(text)
    if not text:
        return []
    chunk_size = max(1, chunk_size)
    overlap = max(0, min(overlap, chunk_size - 1))
    sections = _split_sections(text)
    cores: List[Tuple[str, str]] = []
    for title, lines in sections:
        header = f"## {title}" if title else ""
        blocks = _blocks(lines)
        if not blocks and header:
            cores.append((header, header))
            continue
        current = header
        current_anchor = header or (blocks[0] if blocks else "")
        for block in blocks:
            colon_locked = block.splitlines()[0].endswith((":", "："))
            if colon_locked and len(block) > 1600:
                pieces = _split_locked_list(block, 1600)
            elif not colon_locked and len(block) > chunk_size:
                pieces = _split_long(block, chunk_size)
            else:
                pieces = [block]
            for piece in pieces:
                candidate = f"{current}\n{piece}" if current else piece
                locked_list = colon_locked
                may_expand = bool(header and current == header) or locked_list
                if len(candidate) <= chunk_size or (may_expand and len(candidate) <= 1600):
                    current = candidate
                    if not current_anchor:
                        current_anchor = piece
                else:
                    if current:
                        cores.append((current, current_anchor))
                    continuation = f"## {title} (续)" if title else ""
                    current = f"{continuation}\n{piece}" if continuation else piece
                    current_anchor = piece
        if current:
            cores.append((current, current_anchor))

    chunks: List[dict] = []
    cursor = 0
    for index, (core, anchor) in enumerate(cores):
        core_start = text.find(anchor, cursor) if anchor else cursor
        if core_start < 0:
            core_start = cursor
        core_end = min(len(text), core_start + len(anchor))
        cursor = core_end
        chunk = core
        meta = {
            "chunk_index": index,
            "page": _page_for_offset(core_start, page_ranges),
            "start": core_start,
            "end": core_end,
        }
        if doc_id:
            meta["doc_id"] = str(doc_id)
        chunks.append({"id": str(uuid.uuid4()), "text": chunk, "meta": meta})
    return chunks
