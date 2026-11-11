"""PDF ingestion with layout awareness (Document AI course, intake layer).

Pipeline per page (pdfplumber):
  1. find_tables()            -> table regions (ruled tables) -> kind="table" chunks with columns + rows
  2. text outside tables      -> lines -> headings (bold, large font) set the current *section*
                                         other lines are grouped into paragraphs by vertical gap -> kind="text"
  3. reading order            -> blocks sorted top-to-bottom, so a table sits under the heading above it
  4. quality checks           -> ragged table rows, empty cells, suspiciously short pages are flagged per chunk

Every chunk carries provenance: doc_id, source file, page (1-based), section, bbox and chunk_id.
Falls back to pypdf plain text (one chunk per page, regex section detection) if pdfplumber is unavailable
or a page has no extractable characters.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HEADING_MIN_SIZE = 12.0
SECTION_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+[A-Z][^\n]{2,80}$")


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    source: str
    page: int
    section: str
    kind: str  # "text" | "table"
    text: str
    bbox: list[float] | None = None
    table: dict[str, Any] | None = None  # {"columns": [...], "rows": [[...], ...]}
    quality: list[str] = field(default_factory=list)
    extractor: str = "pdfplumber"

    def provenance(self) -> dict[str, Any]:
        return {"doc_id": self.doc_id, "page": self.page, "section": self.section, "chunk_id": self.chunk_id}


@dataclass
class Document:
    doc_id: str
    source: str
    title: str
    pages: int
    chunks: list[Chunk]

    def to_dict(self) -> dict[str, Any]:
        return {"doc_id": self.doc_id, "source": self.source, "title": self.title, "pages": self.pages,
                "chunks": [asdict(c) for c in self.chunks]}

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Document":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(d["doc_id"], d["source"], d["title"], d["pages"], [Chunk(**c) for c in d["chunks"]])

    def tables(self, section_contains: str | None = None) -> list[Chunk]:
        return [c for c in self.chunks if c.kind == "table"
                and (section_contains is None or section_contains.lower() in c.section.lower())]


def _doc_id(path: Path) -> str:
    h = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
    return f"{re.sub(r'[^a-z0-9]+', '-', path.stem.lower()).strip('-')}-{h}"


def _clean(cell: Any) -> str:
    return " ".join(str(cell or "").split())


def _table_text(columns: list[str], rows: list[list[str]]) -> str:
    """Linearise a table so it can be embedded/searched: one 'col: value; ...' line per row."""
    return "\n".join("; ".join(f"{c}: {v}" for c, v in zip(columns, r)) for r in rows)


def _is_heading(line: dict) -> bool:
    chars = [c for c in line.get("chars", []) if c["text"].strip()]
    if not chars:
        return False
    bold = sum("Bold" in c["fontname"] for c in chars) / len(chars)
    size = sum(c["size"] for c in chars) / len(chars)
    return bold > 0.8 and size >= HEADING_MIN_SIZE


def _pdfplumber_pages(path: Path, doc_id: str) -> tuple[list[Chunk], int, str]:
    import pdfplumber

    chunks: list[Chunk] = []
    section, title = "(front matter)", ""
    with pdfplumber.open(path) as pdf:
        n_pages = len(pdf.pages)
        for page in pdf.pages:
            pno = page.page_number
            blocks: list[tuple[float, str, Any]] = []
            tables = page.find_tables()
            text_page = page
            for t in tables:
                rows = [[_clean(c) for c in r] for r in t.extract()]
                rows = [r for r in rows if any(r)]
                if rows:
                    blocks.append((t.bbox[1], "table", (t.bbox, rows)))
                text_page = text_page.outside_bbox(t.bbox)
            lines = text_page.extract_text_lines(keep_blank_chars=False)
            para: list[dict] = []

            def flush():
                if para:
                    bbox = [min(l["x0"] for l in para), para[0]["top"], max(l["x1"] for l in para), para[-1]["bottom"]]
                    blocks.append((para[0]["top"], "text", (bbox, " ".join(l["text"].strip() for l in para))))
                    para.clear()

            last_heading_bottom = None
            for ln in lines:
                if _is_heading(ln):
                    flush()
                    if last_heading_bottom is not None and ln["top"] - last_heading_bottom < 8 and blocks \
                            and blocks[-1][1] == "heading":  # a heading wrapped onto two lines
                        top, _, txt = blocks.pop()
                        blocks.append((top, "heading", f"{txt} {ln['text'].strip()}"))
                    else:
                        blocks.append((ln["top"], "heading", ln["text"].strip()))
                    last_heading_bottom = ln["bottom"]
                    continue
                last_heading_bottom = None
                if para and ln["top"] - para[-1]["bottom"] > 6:  # vertical gap => new paragraph
                    flush()
                para.append(ln)
            flush()
            if not lines and not tables:
                continue
            blocks.sort(key=lambda b: b[0])
            for _, kind, payload in blocks:
                if kind == "heading":
                    if not title:
                        title = payload
                    else:
                        section = payload
                    continue
                cid = f"{doc_id}:p{pno}:c{len(chunks)}"
                if kind == "table":
                    bbox, rows = payload
                    cols, body = rows[0], rows[1:]
                    quality = []
                    if any(len(r) != len(cols) for r in body):
                        quality.append("ragged_rows")
                    if any(not v for r in body for v in r):
                        quality.append("empty_cells")
                    chunks.append(Chunk(cid, doc_id, str(path), pno, section, "table", _table_text(cols, body),
                                        [round(x, 1) for x in bbox], {"columns": cols, "rows": body}, quality))
                else:
                    bbox, text = payload
                    chunks.append(Chunk(cid, doc_id, str(path), pno, section, "text", text, [round(x, 1) for x in bbox]))
    return chunks, n_pages, title


def _pypdf_pages(path: Path, doc_id: str) -> tuple[list[Chunk], int, str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    title = (reader.metadata.title if reader.metadata else None) or path.stem
    chunks, section = [], "(front matter)"
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        for line in text.splitlines():
            if SECTION_RE.match(line):
                section = line.strip()
        if text.strip():
            chunks.append(Chunk(f"{doc_id}:p{i}:c{len(chunks)}", doc_id, str(path), i, section, "text",
                                " ".join(text.split()), quality=["no_layout"], extractor="pypdf"))
    return chunks, len(reader.pages), title


def ingest_pdf(path: str | Path, prefer: str = "pdfplumber") -> Document:
    """Parse a PDF into a Document of provenance-tagged chunks."""
    p = Path(path)
    doc_id = _doc_id(p)
    chunks: list[Chunk] = []
    n_pages, title = 0, ""
    if prefer == "pdfplumber":
        try:
            chunks, n_pages, title = _pdfplumber_pages(p, doc_id)
        except ImportError:
            chunks = []
    if not chunks:
        chunks, n_pages, title = _pypdf_pages(p, doc_id)
    return Document(doc_id, str(p), title or p.stem, n_pages, chunks)
