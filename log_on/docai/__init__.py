"""Document AI intake: PDF -> structured, provenance-tagged JSON chunks."""
from .pdf_ingest import Chunk, Document, ingest_pdf

__all__ = ["Chunk", "Document", "ingest_pdf"]
