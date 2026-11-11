"""Knowledge graph: extraction, entity resolution, PII hygiene, multi-hop QA on Kuzu and Neo4j."""
import json

import pytest

from log_on.kg import KuzuGraphStore, LLMExtractor, ReadOnlyViolation, RuleBasedExtractor, assert_read_only
from log_on.kg import qa
from log_on.kg.extract import Extraction, Resolver

from conftest import AS_OF, raw_bytes


def test_extraction_counts(extraction):
    assert extraction.warnings == []
    assert extraction.stats() == {
        "Customer": 6, "Supplier": 6, "Contract": 7, "City": 6, "Person": 6, "Organization": 1,
        "HAS_CONTRACT": 7, "FULFILS": 10, "SERVES": 9, "LOCATED_IN": 13, "MANAGES": 6, "CONTACT_FOR": 3,
        "SUBSIDIARY_OF": 1, "SUBCONTRACTS_TO": 1, "LEADS_RENEWAL": 1}


def test_tables_and_prose_resolve_to_the_same_entities(extraction):
    rels = extraction.relations
    # From the contract register (table) ...
    assert ("HAS_CONTRACT", "Customer:CUS-003", "Contract:LF-C-2403") in rels
    # ... and from narrative text on p.3, linked to the *same* supplier/contract nodes
    narrative = rels[("FULFILS", "Supplier:SUP-01", "Contract:LF-C-2403")]
    assert narrative.props["role"] == "per narrative" and narrative.sources[0]["page"] == 3
    assert ("SUBSIDIARY_OF", "Customer:CUS-006", "Customer:CUS-001") in rels
    assert ("SUBCONTRACTS_TO", "Supplier:SUP-02", "Supplier:SUP-05") in rels
    assert ("LEADS_RENEWAL", "Person:ifeoma-nwosu", "Contract:LF-C-2401") in rels
    # derived edge: Supplier FULFILS Contract <- HAS_CONTRACT Customer  =>  SERVES
    serves = rels[("SERVES", "Supplier:SUP-02", "Customer:CUS-002")]
    assert serves.props["derived"] is True
    org = extraction.entities["Organization:lagoon-freight-logistics-ltd"]
    assert org.name == "Lagoon Freight & Logistics Ltd"
    assert ("LOCATED_IN", "Organization:lagoon-freight-logistics-ltd", "City:lagos") in rels
    assert extraction.entities["Customer:CUS-005"].aliases >= {"eko pharma distribution", "eko pharma"}


def test_schema_validation_and_conflict_detection(sample_doc):
    ex = Extraction(); r = Resolver(ex)
    a, b = r.upsert("City", "Lagos"), r.upsert("Contract", "X-C-1", "X-C-1")
    r.relate("HAS_CONTRACT", a, b)  # City cannot hold a contract
    assert not ex.relations and "schema rejected" in ex.warnings[0]
    # Narrative that contradicts the register is flagged by the critic step
    import copy
    doc = copy.deepcopy(sample_doc)
    for c in doc.chunks:
        c.text = c.text.replace("expires on 31 December 2026", "expires on 30 June 2027")
    assert any("narrative expiry 2027-06-30 != register 2026-12-31" in w for w in RuleBasedExtractor().extract(doc).warnings)


def test_contact_pii_never_reaches_the_graph(kuzu_path):
    with KuzuGraphStore(kuzu_path, read_only=True) as g:
        contacts = g.query("MATCH (p:Person) WHERE p.role = 'Contact' RETURN p.name AS name, p.contact AS contact")
    assert len(contacts) == 3 and {c["contact"] for c in contacts} == {"<EMAIL> <PHONE>"}
    blob = raw_bytes(kuzu_path)
    for raw in [b"chidi.eze@zumacement.example", b"0803 555 0142", b"halima@harmattanhaulage.example", b"0705 555 0123"]:
        assert raw not in blob


def test_q1_suppliers_multi_hop(graph):
    a = qa.answer(graph, "Which suppliers serve customers whose contracts expire in Q1?", as_of=AS_OF)
    assert a.template == "suppliers_for_expiring_contracts" and a.hops == 3
    assert a.params == {"from_date": "2027-01-01", "to_date": "2027-03-31"}
    assert {(r["supplier"], r["contract"]) for r in a.rows} == {
        ("Harmattan Haulage Ltd", "LF-C-2401"), ("Harmattan Haulage Ltd", "LF-C-2403"),
        ("Onne Port Services Ltd", "LF-C-2403"), ("Atlantic Reefers Nigeria Ltd", "LF-C-2405"),
        ("Coldline Couriers Ltd", "LF-C-2405"), ("Atlantic Reefers Nigeria Ltd", "LF-C-2407")}
    # PetroMove (LF-C-2406, Sep 2027) and Arewa (LF-C-2404, Dec 2026) must be excluded
    assert "PetroMove" not in a.text and "Arewa" not in a.text
    assert {(s["page"], s["section"]) for s in a.sources} == {(2, "3. Contract Register"), (3, "5. Supplier Allocations")}


def test_three_hop_account_managers(graph):
    a = qa.answer(graph, "Which account managers have customers with contracts expiring in Q1, and which suppliers "
                         "are involved?", as_of=AS_OF)
    assert a.template == "managers_with_expiring_contracts"
    assert {r["manager"] for r in a.rows} == {"Ifeoma Nwosu", "Kunle Adebayo"}
    assert "Musa Bello" not in a.text


def test_contracts_expiring_and_quarter_resolution(graph):
    a = qa.answer(graph, "What contracts expire in Q1?", as_of=AS_OF)
    assert [r["contract"] for r in a.rows] == ["LF-C-2401", "LF-C-2403", "LF-C-2407", "LF-C-2405"]
    assert "NGN 903.0m" in a.text
    q4 = qa.answer(graph, "What contracts expire in Q4?", as_of=AS_OF)  # Q4 2026 has not ended yet
    assert [r["contract"] for r in q4.rows] == ["LF-C-2404"]
    assert qa.quarter_range(1, as_of=AS_OF) == ("2027-01-01", "2027-03-31", 2027)
    assert qa.quarter_range(3, 2026) == ("2026-07-01", "2026-09-30", 2026)


def test_entity_scoped_questions(graph):
    a = qa.answer(graph, "Who supplies Eko Pharma?")
    assert {r["supplier"] for r in a.rows} == {"Atlantic Reefers Nigeria Ltd", "Coldline Couriers Ltd"}
    g = qa.answer(graph, "What is the group exposure of Zuma Cement Plc and its subsidiaries?")
    assert g.template == "group_exposure" and "NGN 570.0m" in g.text
    loc = qa.answer(graph, "Where are the suppliers for Delta Agro Exports located?")
    assert {(r["supplier"], r["city"]) for r in loc.rows} == {("Harmattan Haulage Ltd", "Kano"),
                                                              ("Onne Port Services Ltd", "Port Harcourt")}
    # entity_ids pin an entity even when the question doesn't name it (used for memory-resolved references)
    pinned = qa.answer(graph, "Which suppliers serve my focus customer?", entity_ids=["CUS-005"])
    assert {r["supplier"] for r in pinned.rows} == {"Atlantic Reefers Nigeria Ltd", "Coldline Couriers Ltd"}


def test_find_path_and_lookup(graph):
    p = qa.path_between(graph, "Sahel Telecom Towers", "Onne Port Services")
    assert p["found"] and p["hops"] == 3
    assert p["path"]["nodes"][0]["id"] == "CUS-006" and p["path"]["nodes"][-1]["id"] == "SUP-03"
    assert not qa.path_between(graph, "Kano Textiles", "Coldline Couriers", max_hops=3)["found"]
    hit = qa.lookup(graph, "Atlantic Reefers")[0]
    assert hit["id"] == "SUP-02" and hit["score"] == 1.0
    assert qa.lookup(graph, "Atlantik Reefers Nigeria")[0]["id"] == "SUP-02"  # fuzzy


@pytest.mark.parametrize("q", ["MATCH (n) DETACH DELETE n", "CREATE (:City {id:'x'})", "MATCH (n) SET n.name='x'",
                               "CALL db.labels()", "match (n) merge (m:City {id:'y'})"])
def test_graph_query_is_read_only(q):
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(q)


def test_read_only_store_rejects_writes_even_without_the_screen(kuzu_path):
    with KuzuGraphStore(kuzu_path, read_only=True) as g:
        with pytest.raises(Exception):
            g.upsert_node("City", "x", {"name": "X"})
        assert qa.answer(g, cypher="MATCH (c:City) WHERE c.name = 'Kano' RETURN c.id AS id").rows == [{"id": "kano"}]


def test_llm_extractor_is_schema_checked_and_falls_back(sample_doc):
    calls = []

    def fake(system, user):
        calls.append(user)
        assert "chidi.eze@" not in user  # prose is redacted before it is sent to a model
        if "subcontracts" in user:
            return json.dumps({"entities": [{"label": "Supplier", "name": "Atlantic Reefers Nigeria Ltd"},
                                            {"label": "Supplier", "name": "Coldline Couriers Ltd"}],
                               "relations": [{"type": "SUBCONTRACTS_TO", "source": "Atlantic Reefers Nigeria Ltd",
                                              "target": "Coldline Couriers Ltd"},
                                             {"type": "SERVES", "source": "Coldline Couriers Ltd",
                                              "target": "Atlantic Reefers Nigeria Ltd"}]})  # invalid pair
        if "subsidiary" in user:
            return json.dumps({"entities": [{"label": "Spaceship", "name": "x"}]})  # invalid label -> fallback
        return "not json"  # -> fallback to rules
    ex = LLMExtractor("test-key", complete=fake).extract(sample_doc)
    assert calls and ("SUBCONTRACTS_TO", "Supplier:SUP-02", "Supplier:SUP-05") in ex.relations
    assert any("schema rejected (Supplier)-[:SERVES]->(Supplier)" in w for w in ex.warnings)
    assert ("SUBSIDIARY_OF", "Customer:CUS-006", "Customer:CUS-001") in ex.relations  # recovered by rule fallback
