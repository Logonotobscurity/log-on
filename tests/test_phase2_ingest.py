"""Document AI intake: chunking, layout/table extraction, provenance."""
from log_on.docai import Document, ingest_pdf

from conftest import SAMPLE_PDF


def test_chunks_carry_provenance(sample_doc):
    d = sample_doc
    assert d.pages == 3 and d.title.startswith("Lagoon Freight & Logistics Ltd")
    ids = [c.chunk_id for c in d.chunks]
    assert len(ids) == len(set(ids)) == 11
    for c in d.chunks:
        assert c.doc_id == d.doc_id and 1 <= c.page <= 3 and c.section and c.bbox and len(c.bbox) == 4
        assert c.provenance() == {"doc_id": d.doc_id, "page": c.page, "section": c.section, "chunk_id": c.chunk_id}
    sections = {(c.page, c.section) for c in d.chunks}
    assert {(1, "1. Executive Summary"), (2, "3. Contract Register"), (3, "6. Key Contacts")} <= sections


def test_tables_are_extracted_with_columns_and_rows(sample_doc):
    tables = {c.section: c for c in sample_doc.tables()}
    assert set(tables) == {"2. Customer Portfolio", "3. Contract Register", "4. Supplier Register",
                           "5. Supplier Allocations", "6. Key Contacts"}
    contracts = tables["3. Contract Register"]
    assert contracts.page == 2
    assert contracts.table["columns"] == ["Contract ID", "Customer", "Service", "Value (NGN m)", "Start", "Expiry"]
    assert len(contracts.table["rows"]) == 7 and contracts.quality == []
    assert contracts.table["rows"][4] == ["LF-C-2405", "Eko Pharma Distribution Ltd", "Temperature-controlled last mile",
                                          "240.0", "2025-01-01", "2027-03-31"]
    assert "Contract ID: LF-C-2401; Customer: Zuma Cement Plc" in contracts.text  # linearised for search


def test_text_outside_tables_is_not_duplicated(sample_doc):
    texts = [c.text for c in sample_doc.chunks if c.kind == "text"]
    assert not any("LF-C-2402" in t for t in texts)  # that id only appears inside the contract table
    assert any("subcontracts overflow capacity" in t for t in texts)


def test_json_roundtrip(sample_doc, tmp_path):
    p = sample_doc.save(tmp_path / "chunks.json")
    again = Document.load(p)
    assert again.to_dict() == sample_doc.to_dict()


def test_pypdf_fallback_keeps_page_provenance():
    d = ingest_pdf(SAMPLE_PDF, prefer="pypdf")
    assert [c.page for c in d.chunks] == [1, 2, 3]
    assert all(c.extractor == "pypdf" and "no_layout" in c.quality for c in d.chunks)
    assert d.chunks[1].section == "4. Supplier Register"  # last heading seen on page 2


def test_sample_generator_is_deterministic(tmp_path, sample_doc):
    import importlib.util
    spec = importlib.util.spec_from_file_location("mk", SAMPLE_PDF.parent.parent / "scripts" / "make_sample_pdf.py")
    mk = importlib.util.module_from_spec(spec); spec.loader.exec_module(mk)
    regenerated = ingest_pdf(mk.build(tmp_path / "again.pdf"))
    assert [c.text for c in regenerated.chunks] == [c.text for c in sample_doc.chunks]
