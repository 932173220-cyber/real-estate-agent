import re
from pathlib import Path

def normalize_text(text: str) -> str:
    text = text.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[\t ]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()

def split_text(text: str, *, target_chars: int = 450, overlap_chars: int = 70) -> list[str]:
    """Structure-aware splitting with a bounded character overlap."""
    text = normalize_text(text)
    if not text:
        return []
    if target_chars < 200 or not 0 <= overlap_chars < target_chars // 2:
        raise ValueError("Invalid chunk size or overlap")

    pieces = [piece.strip() for piece in re.split(r"(?<=[。！？；.!?])|\n+", text) if piece.strip()]
    if not pieces:
        pieces = [text]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        while len(piece) > target_chars:
            if current:
                chunks.append(current.strip())
                current = current[-overlap_chars:]
            take = target_chars - len(current)
            current = (current + piece[:take]).strip()
            chunks.append(current)
            current = current[-overlap_chars:]
            piece = piece[take:]
        separator = "" if not current else " "
        if len(current) + len(separator) + len(piece) <= target_chars:
            current = current + separator + piece
        else:
            chunks.append(current.strip())
            prefix = current[-overlap_chars:] if overlap_chars else ""
            current = (prefix + (" " if prefix else "") + piece).strip()
    if current.strip():
        chunks.append(current.strip())
    result: list[str] = []
    for chunk in chunks:
        if chunk and (not result or chunk != result[-1]):
            result.append(chunk)
    return result

def read_pages(path: Path) -> list[tuple[int, str]]:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise RuntimeError("缺少 pypdf，无法解析 PDF") from exc
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception as exc:
                    raise RuntimeError("PDF 已加密，无法读取") from exc
            pages = []
            for number, page in enumerate(reader.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception:  # noqa: BLE001
                    text = ""
                text = normalize_text(text)
                if text:
                    pages.append((number, text))
            return pages
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        return [(1, normalize_text(text))] if normalize_text(text) else []
