"""Multi-hop question answering over the graph (KG course: 'GraphRAG' retrieval side).

`answer()` routes a natural-language question to a vetted, parameterised Cypher template (deterministic,
testable, no text-to-Cypher hallucination). Every answer carries the Cypher it ran, the hop count and
provenance (document page + section) collected from the nodes/edges on the matched paths.

Raw Cypher is also accepted (`cypher=`) for an LLM planner, but only after `assert_read_only()`.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .extract import norm
from .store import GraphStore, assert_read_only, sources_of

QUARTER_RE = re.compile(r"\bQ([1-4])(?:\s*(?:of\s+)?(20\d{2}))?\b", re.I)


@dataclass
class Answer:
    template: str
    text: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    cypher: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    hops: int = 0
    sources: list[dict] = field(default_factory=list)

    def render(self) -> str:
        out = self.text
        if self.sources:
            out += "\nSources: " + "; ".join(f"p.{s['page']} {s['section']}" for s in self.sources)
        out += f"\n[graph template={self.template}, hops={self.hops}]"
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"template": self.template, "text": self.text, "rows": self.rows, "cypher": self.cypher,
                "params": self.params, "hops": self.hops, "sources": self.sources}


def quarter_range(q: int, year: int | None = None, as_of: date | None = None) -> tuple[str, str, int]:
    """Q1 with no year means the *next* Q1 that hasn't ended yet relative to as_of."""
    as_of = as_of or date.today()
    ends = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    if year is None:
        year = as_of.year
        if date(year, *ends[q]) < as_of:
            year += 1
    start = date(year, 3 * q - 2, 1)
    return start.isoformat(), date(year, *ends[q]).isoformat(), year


def _collect_sources(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        for k, v in r.items():
            if k.endswith("_src"):
                for s in sources_of(v):
                    key = (s.get("page"), s.get("section"))
                    if key not in seen:
                        seen.add(key)
                        out.append({"page": s.get("page"), "section": s.get("section"), "doc_id": s.get("doc_id")})
    return sorted(out, key=lambda s: (s["page"] or 0, s["section"] or ""))


# --------------------------------------------------------------------------- entity linking
def link_entities(store: GraphStore, text: str, labels: tuple[str, ...] | None = None) -> list[dict]:
    """Find graph entities mentioned in free text (exact alias match, then fuzzy), in reading order."""
    low = " " + re.sub(r"[^a-z0-9& ]", " ", text.lower()) + " "
    nodes = [n for n in store.all_nodes() if labels is None or n["label"] in labels]
    hits: list[tuple[int, int, dict]] = []
    for n in nodes:
        aliases = {a for a in (n.get("aliases") or "").split("|") if a} | {norm(n["name"] or "")}
        for a in sorted(aliases, key=len, reverse=True):
            m = re.search(r"(?<= )" + re.escape(a) + r"(?= )", low)
            if m and len(a) >= 4:
                hits.append((m.start(), m.end(), n))
                break
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    out, last_end = [], -1
    for s, e, n in hits:  # drop mentions nested inside a longer one
        if s >= last_end:
            out.append({"id": n["id"], "label": n["label"], "name": n["name"], "match": "alias"})
            last_end = e
    return out


def lookup(store: GraphStore, name: str, limit: int = 3) -> list[dict]:
    """Best entity matches for a name or id: exact id, alias, then fuzzy ratio >= 0.6."""
    q = norm(name)
    scored = []
    for n in store.all_nodes():
        if n["id"].lower() == name.strip().lower():
            scored.append((1.01, n))
            continue
        aliases = {a for a in (n.get("aliases") or "").split("|") if a} | {norm(n["name"] or "")}
        best = max((1.0 if a == q else difflib.SequenceMatcher(None, a, q).ratio()) for a in aliases)
        if best >= 0.6:
            scored.append((best, n))
    scored.sort(key=lambda t: -t[0])
    return [{**n, "score": round(s, 3)} for s, n in scored[:limit]]


def describe_entity(store: GraphStore, label: str, id: str) -> dict[str, Any]:
    props = store.query(f"MATCH (n:{label}) WHERE n.id = $id RETURN n AS n", {"id": id})
    node = dict(props[0]["n"]) if props else {}
    node = {k: v for k, v in node.items() if not k.startswith("_") and v not in (None, "") and k != "aliases"}
    out_rels = store.query(f"MATCH (n:{label})-[r]->(m) WHERE n.id = $id RETURN label(r) AS type, m.name AS name, "
                           "m.id AS id", {"id": id}) if store.backend == "kuzu" else \
        store.query(f"MATCH (n:{label})-[r]->(m) WHERE n.id = $id RETURN type(r) AS type, m.name AS name, m.id AS id",
                    {"id": id})
    in_rels = store.query(f"MATCH (n:{label})<-[r]-(m) WHERE n.id = $id RETURN label(r) AS type, m.name AS name, "
                          "m.id AS id", {"id": id}) if store.backend == "kuzu" else \
        store.query(f"MATCH (n:{label})<-[r]-(m) WHERE n.id = $id RETURN type(r) AS type, m.name AS name, m.id AS id",
                    {"id": id})
    return {"label": label, "props": node, "out": out_rels, "in": in_rels,
            "sources": _collect_sources([{"n_src": node.get("sources")}])}


# --------------------------------------------------------------------------- templates
T_SUPPLIERS_EXPIRING = (
    "MATCH (s:Supplier)-[f:FULFILS]->(k:Contract)<-[h:HAS_CONTRACT]-(c:Customer), (s)-[:SERVES]->(c) "
    "WHERE k.expiry_date >= $from_date AND k.expiry_date <= $to_date "
    "RETURN s.name AS supplier, c.name AS customer, k.id AS contract, k.expiry_date AS expiry, "
    "k.value_ngn_m AS value_m, f.sources AS f_src, h.sources AS h_src ORDER BY supplier, contract")
T_MANAGERS_EXPIRING = (
    "MATCH (p:Person)-[m:MANAGES]->(c:Customer)-[h:HAS_CONTRACT]->(k:Contract)<-[f:FULFILS]-(s:Supplier) "
    "WHERE k.expiry_date >= $from_date AND k.expiry_date <= $to_date "
    "RETURN p.name AS manager, c.name AS customer, k.id AS contract, k.expiry_date AS expiry, s.name AS supplier, "
    "m.sources AS m_src, h.sources AS h_src, f.sources AS f_src ORDER BY manager, contract, supplier")
T_CONTRACTS_EXPIRING = (
    "MATCH (c:Customer)-[h:HAS_CONTRACT]->(k:Contract) WHERE k.expiry_date >= $from_date AND k.expiry_date <= $to_date "
    "RETURN k.id AS contract, c.name AS customer, k.service AS service, k.value_ngn_m AS value_m, "
    "k.expiry_date AS expiry, h.sources AS h_src ORDER BY expiry")
T_CUSTOMER_SUPPLIERS = (
    "MATCH (s:Supplier)-[f:FULFILS]->(k:Contract)<-[h:HAS_CONTRACT]-(c:Customer) WHERE c.id IN $ids "
    "RETURN c.name AS customer, s.name AS supplier, k.id AS contract, f.role AS role, f.sources AS f_src, "
    "h.sources AS h_src ORDER BY customer, supplier, contract")
T_SUPPLIER_CITIES = (
    "MATCH (c:Customer)<-[sv:SERVES]-(s:Supplier)-[l:LOCATED_IN]->(city:City) WHERE c.id IN $ids "
    "RETURN c.name AS customer, s.name AS supplier, city.name AS city, sv.sources AS sv_src, l.sources AS l_src "
    "ORDER BY supplier")
T_SUBCONTRACTS = (
    "MATCH (a:Supplier)-[r:SUBCONTRACTS_TO]->(b:Supplier) WHERE a.id IN $ids OR b.id IN $ids "
    "RETURN a.name AS contractor, b.name AS subcontractor, r.sources AS r_src")
T_SUBSIDIARIES = "MATCH (c:Customer)-[r:SUBSIDIARY_OF]->(p:Customer) WHERE p.id IN $ids RETURN c.id AS id, c.name AS name, r.sources AS r_src"
T_GROUP_CONTRACTS = (
    "MATCH (c:Customer)-[h:HAS_CONTRACT]->(k:Contract) WHERE c.id IN $ids "
    "RETURN c.name AS customer, k.id AS contract, k.value_ngn_m AS value_m, k.expiry_date AS expiry, h.sources AS h_src "
    "ORDER BY contract")


def _fmt_ngn(v: float | None) -> str:
    return f"NGN {v:,.1f}m" if v is not None else "n/a"


def answer(store: GraphStore, question: str = "", entity_ids: list[str] | None = None, cypher: str = "",
           as_of: date | None = None) -> Answer:
    if cypher:
        assert_read_only(cypher)
        if not re.search(r"\bLIMIT\b", cypher, re.I):
            cypher += " LIMIT 50"
        rows = store.query(cypher)
        rows = [{k: v for k, v in r.items()} for r in rows]
        return Answer("raw_cypher", f"{len(rows)} row(s):\n" + "\n".join(json.dumps(r, default=str) for r in rows),
                      rows, cypher, {}, 0, _collect_sources(rows))

    q = question.lower()
    linked = link_entities(store, question)
    ids = list(dict.fromkeys([*(entity_ids or []), *[e["id"] for e in linked]]))
    labels = {n["id"]: n["label"] for n in store.all_nodes() if n["id"] in ids}
    cust_ids = [i for i in ids if labels.get(i) == "Customer"]
    sup_ids = [i for i in ids if labels.get(i) == "Supplier"]
    qm = QUARTER_RE.search(question)
    expiring = qm and re.search(r"expir|renew|end", q)

    if expiring:
        start, end, year = quarter_range(int(qm.group(1)), int(qm.group(2)) if qm.group(2) else None, as_of)
        period = f"Q{qm.group(1)} {year} ({start} to {end})"
        params = {"from_date": start, "to_date": end}
        if re.search(r"account manager|manager|who manages|owner", q):
            rows = store.query(T_MANAGERS_EXPIRING, params)
            by: dict[str, dict[str, set]] = {}
            for r in rows:
                d = by.setdefault(r["manager"], {"contracts": set(), "suppliers": set()})
                d["contracts"].add(f"{r['contract']} ({r['customer']})")
                d["suppliers"].add(r["supplier"])
            lines = [f"{m}: contracts {', '.join(sorted(d['contracts']))}; suppliers involved: "
                     f"{', '.join(sorted(d['suppliers']))}" for m, d in sorted(by.items())]
            return Answer("managers_with_expiring_contracts",
                          f"{len(by)} account manager(s) have customers with contracts expiring in {period}:\n- "
                          + "\n- ".join(lines) if lines else f"No contracts expire in {period}.",
                          rows, T_MANAGERS_EXPIRING, params, 3, _collect_sources(rows))
        if "supplier" in q or "vendor" in q:
            rows = store.query(T_SUPPLIERS_EXPIRING, params)
            by2: dict[str, list[str]] = {}
            for r in rows:
                by2.setdefault(r["supplier"], []).append(f"{r['customer']} ({r['contract']}, expires {r['expiry']})")
            lines = [f"{s} -> {'; '.join(v)}" for s, v in sorted(by2.items())]
            return Answer("suppliers_for_expiring_contracts",
                          f"{len(by2)} supplier(s) serve customers whose contracts expire in {period}:\n- "
                          + "\n- ".join(lines) if lines else f"No contracts expire in {period}.",
                          rows, T_SUPPLIERS_EXPIRING, params, 3, _collect_sources(rows))
        rows = store.query(T_CONTRACTS_EXPIRING, params)
        total = sum(r["value_m"] or 0 for r in rows)
        lines = [f"{r['contract']} {r['customer']}: {r['service']}, {_fmt_ngn(r['value_m'])}, expires {r['expiry']}"
                 for r in rows]
        return Answer("contracts_expiring",
                      f"{len(rows)} contract(s) worth {_fmt_ngn(total)} expire in {period}:\n- " + "\n- ".join(lines)
                      if rows else f"No contracts expire in {period}.", rows, T_CONTRACTS_EXPIRING, params, 1,
                      _collect_sources(rows))

    if "subcontract" in q and sup_ids:
        rows = store.query(T_SUBCONTRACTS, {"ids": sup_ids})
        lines = [f"{r['contractor']} subcontracts to {r['subcontractor']}" for r in rows]
        return Answer("subcontracting", "\n".join(lines) or "No subcontracting relationships found.", rows,
                      T_SUBCONTRACTS, {"ids": sup_ids}, 1, _collect_sources(rows))

    if cust_ids and re.search(r"subsidiar|group|parent", q):
        subs = store.query(T_SUBSIDIARIES, {"ids": cust_ids})
        group = cust_ids + [s["id"] for s in subs]
        rows = store.query(T_GROUP_CONTRACTS, {"ids": group})
        total = sum(r["value_m"] or 0 for r in rows)
        lines = [f"{r['contract']} {r['customer']}: {_fmt_ngn(r['value_m'])}, expires {r['expiry']}" for r in rows]
        names = ", ".join(s["name"] for s in subs) or "none"
        return Answer("group_exposure", f"Group exposure {_fmt_ngn(total)} across {len(rows)} contract(s) "
                      f"(subsidiaries: {names}):\n- " + "\n- ".join(lines), subs + rows,
                      T_SUBSIDIARIES + " ; " + T_GROUP_CONTRACTS, {"ids": group}, 2, _collect_sources(subs + rows))

    if cust_ids and re.search(r"\b(where|cit(y|ies)|located|based)\b", q) and "supplier" in q:
        rows = store.query(T_SUPPLIER_CITIES, {"ids": cust_ids})
        lines = [f"{r['supplier']} ({r['city']}) serves {r['customer']}" for r in rows]
        return Answer("supplier_locations", "\n".join(lines) or "No suppliers found.", rows, T_SUPPLIER_CITIES,
                      {"ids": cust_ids}, 2, _collect_sources(rows))

    if cust_ids and re.search(r"supplier|vendor|who (serves|supplies)|serve", q):
        rows = store.query(T_CUSTOMER_SUPPLIERS, {"ids": cust_ids})
        by3: dict[str, list[str]] = {}
        for r in rows:
            by3.setdefault(r["customer"], []).append(f"{r['supplier']} ({r['contract']}, {r['role']})")
        lines = [f"{c}: {'; '.join(v)}" for c, v in by3.items()]
        return Answer("customer_suppliers", "Suppliers by customer:\n- " + "\n- ".join(lines) if lines
                      else "No suppliers found.", rows, T_CUSTOMER_SUPPLIERS, {"ids": cust_ids}, 2,
                      _collect_sources(rows))

    if ids:
        first = ids[0]
        d = describe_entity(store, labels.get(first, "Customer"), first)
        rel_lines = [f"-[{r['type']}]-> {r['name']}" for r in d["out"]] + [f"<-[{r['type']}]- {r['name']}" for r in d["in"]]
        props = {k: v for k, v in d["props"].items() if k not in ("sources", "id")}
        return Answer("entity_summary", f"{d['props'].get('name', first)} ({d['label']} {first}) {json.dumps(props)}\n"
                      + "\n".join(rel_lines), [d], "", {"id": first}, 1, d["sources"])

    return Answer("no_match", "I couldn't map that question to a graph query. Try naming a customer, supplier "
                  "or contract, or a quarter (e.g. 'Q1') with 'expire'.")


def path_between(store: GraphStore, source: str, target: str, max_hops: int = 5) -> dict[str, Any]:
    a, b = lookup(store, source, 1), lookup(store, target, 1)
    if not a or not b:
        missing = source if not a else target
        return {"found": False, "text": f"No entity matches {missing!r}."}
    p = store.shortest_path(a[0]["id"], b[0]["id"], max_hops)
    if p is None:
        return {"found": False, "text": f"No path within {max_hops} hops between {a[0]['name']} and {b[0]['name']}."}
    names = {n["id"]: n["name"] for n in p["nodes"]}
    parts = [p["nodes"][0]["name"]]
    for i, r in enumerate(p["rels"]):
        nxt = p["nodes"][i + 1]["id"]
        arrow = f"-[{r['type']}]->" if r["src"] == p["nodes"][i]["id"] else f"<-[{r['type']}]-"
        parts.append(f"{arrow} {names[nxt]}")
    srcs = _collect_sources([{"r_src": r.get("sources")} for r in p["rels"]])
    return {"found": True, "hops": len(p["rels"]), "path": p, "sources": srcs,
            "text": f"{len(p['rels'])}-hop path: " + " ".join(parts)}
