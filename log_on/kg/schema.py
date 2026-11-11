"""Graph schema: the contract between extraction, storage and query (KG course: 'schema first').

Kept deliberately small and typed so the same Cypher works on Kuzu (which needs declared tables)
and Neo4j (labels + uniqueness constraints).
"""
from __future__ import annotations

COMMON_PROPS = {"id": "STRING", "name": "STRING", "aliases": "STRING", "sources": "STRING"}

NODE_TYPES: dict[str, dict[str, str]] = {
    "Customer": {"sector": "STRING"},
    "Supplier": {"category": "STRING"},
    "Contract": {"service": "STRING", "value_ngn_m": "DOUBLE", "start_date": "STRING", "expiry_date": "STRING"},
    "City": {},
    "Person": {"role": "STRING", "contact": "STRING"},
    "Organization": {},
}

REL_PROPS = {"role": "STRING", "derived": "BOOLEAN", "sources": "STRING"}

REL_TYPES: dict[str, list[tuple[str, str]]] = {
    "HAS_CONTRACT": [("Customer", "Contract")],
    "FULFILS": [("Supplier", "Contract")],
    "SERVES": [("Supplier", "Customer")],
    "LOCATED_IN": [("Customer", "City"), ("Supplier", "City"), ("Organization", "City")],
    "MANAGES": [("Person", "Customer")],
    "LEADS_RENEWAL": [("Person", "Contract")],
    "SUBSIDIARY_OF": [("Customer", "Customer"), ("Customer", "Organization")],
    "SUBCONTRACTS_TO": [("Supplier", "Supplier")],
    "CONTACT_FOR": [("Person", "Customer"), ("Person", "Supplier"), ("Person", "Organization")],
}


def node_props(label: str) -> dict[str, str]:
    return {**COMMON_PROPS, **NODE_TYPES[label]}


def rel_allowed(rel: str, src_label: str, dst_label: str) -> bool:
    return (src_label, dst_label) in REL_TYPES.get(rel, [])


def describe() -> str:
    """Human/LLM-readable schema summary (used in LLM prompts and docs)."""
    lines = ["Nodes:"]
    for label, props in NODE_TYPES.items():
        lines.append(f"  ({label} {{{', '.join(['id', 'name', *props])}}})")
    lines.append("Relationships:")
    for rel, pairs in REL_TYPES.items():
        for a, b in pairs:
            lines.append(f"  ({a})-[:{rel}]->({b})")
    lines.append("Dates are ISO strings (YYYY-MM-DD); value_ngn_m is NGN millions; sources is JSON provenance.")
    return "\n".join(lines)
