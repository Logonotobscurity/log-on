"""Entity + relationship extraction (Agentic Knowledge Graph Construction course).

Course pipeline -> this module
  1. user intent / schema proposal  -> `schema.py` (fixed, reviewed schema; extraction must fit it)
  2. structured-data agent          -> `_from_table()`: header signatures map tables to node/rel types
  3. unstructured-data agent        -> `_from_text()`: entity mentions + relation cue phrases in sentences
                                       (or `LLMExtractor` for prose when an OpenAI-compatible key is set)
  4. entity resolution              -> `Resolver`: canonical keys, legal-suffix-insensitive aliases
  5. critic / validation            -> schema check of every relation, table-vs-prose consistency checks
  6. graph construction             -> `load_into()` writes idempotently (MERGE), with provenance on everything

PII never enters the graph: person contact details are passed through the Phase 1 redactor first.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Protocol

from ..docai import Chunk, Document
from ..guards import redact_text
from .schema import NODE_TYPES, REL_TYPES, describe, rel_allowed

LEGAL_SUFFIX = re.compile(r"\b(ltd|limited|plc|inc|llc|nig\.?)\.?$", re.I)
ORG_RE = re.compile(r"\b([A-Z][\w'-]*\s+(?:(?:[A-Z][\w'-]*|&)\s+){0,5}(?:Ltd|Plc|Limited|Inc))\b")
CONTRACT_RE = re.compile(r"\b[A-Z]{1,4}-C-\d{3,5}\b")
DATE_TEXT_RE = re.compile(r"\b(\d{1,2}) (January|February|March|April|May|June|July|August|September|October|"
                          r"November|December) (\d{4})\b")


def norm(name: str) -> str:
    s = re.sub(r"[^a-z0-9& ]+", " ", name.lower().replace("&amp;", "&"))
    s = " ".join(s.split())
    return LEGAL_SUFFIX.sub("", s).strip()


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@dataclass
class Entity:
    label: str
    id: str
    name: str
    props: dict[str, Any] = field(default_factory=dict)
    sources: list[dict] = field(default_factory=list)
    aliases: set[str] = field(default_factory=set)


@dataclass
class Relation:
    type: str
    src: str  # entity key "Label:id"
    dst: str
    props: dict[str, Any] = field(default_factory=dict)
    sources: list[dict] = field(default_factory=list)


@dataclass
class Extraction:
    entities: dict[str, Entity] = field(default_factory=dict)
    relations: dict[tuple[str, str, str], Relation] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entities.values():
            out[e.label] = out.get(e.label, 0) + 1
        for r in self.relations.values():
            out[r.type] = out.get(r.type, 0) + 1
        return out


class Resolver:
    """Entity resolution: one canonical entity per real-world thing, found by id, name or alias."""

    def __init__(self, ex: Extraction):
        self.ex = ex
        self.by_alias: dict[str, set[str]] = {}

    @staticmethod
    def key(label: str, id: str) -> str:
        return f"{label}:{id}"

    def aliases_for(self, name: str) -> set[str]:
        base = norm(name)
        out = {base}
        words = base.split()
        if len(words) >= 3:
            out.add(" ".join(words[:2]))  # "eko pharma distribution" -> "eko pharma"
        return {a for a in out if len(a) >= 3}

    def find(self, name: str, label: str | None = None) -> Entity | None:
        keys = self.by_alias.get(norm(name), set())
        cands = [self.ex.entities[k] for k in keys if label is None or self.ex.entities[k].label == label]
        return cands[0] if len(cands) == 1 else None

    def upsert(self, label: str, name: str, id: str | None = None, props: dict | None = None,
               source: dict | None = None) -> Entity:
        name = " ".join(name.replace("&amp;", "&").split())
        existing = self.find(name, label) if id is None else self.ex.entities.get(self.key(label, id)) or \
            self.find(name, label)
        if existing is None:
            ent = Entity(label, id or slug(name), name)
            self.ex.entities[self.key(label, ent.id)] = ent
        else:
            ent = existing
        for k, v in (props or {}).items():
            if v not in (None, ""):
                if k in ent.props and ent.props[k] != v:
                    self.ex.warnings.append(f"conflict on {ent.label}:{ent.id}.{k}: {ent.props[k]!r} vs {v!r} "
                                            f"(kept first, from p.{ent.sources[0]['page'] if ent.sources else '?'})")
                    continue
                ent.props[k] = v
        if source and source not in ent.sources:
            ent.sources.append(source)
        for a in self.aliases_for(name):
            ent.aliases.add(a)
            self.by_alias.setdefault(a, set()).add(self.key(ent.label, ent.id))
        return ent

    def relate(self, rel: str, a: Entity, b: Entity, props: dict | None = None, source: dict | None = None) -> None:
        if not rel_allowed(rel, a.label, b.label):
            self.ex.warnings.append(f"schema rejected ({a.label})-[:{rel}]->({b.label}) for {a.name} -> {b.name}")
            return
        k = (rel, self.key(a.label, a.id), self.key(b.label, b.id))
        r = self.ex.relations.get(k) or Relation(rel, k[1], k[2])
        r.props.update({kk: vv for kk, vv in (props or {}).items() if vv not in (None, "")})
        if source and source not in r.sources:
            r.sources.append(source)
        self.ex.relations[k] = r

    def mentions(self, text: str) -> list[tuple[int, int, Entity]]:
        """Known-entity mentions in text (longest alias first, non-overlapping), in reading order."""
        low = " " + re.sub(r"[^a-z0-9& ]", " ", text.lower().replace("&amp;", "&")) + " "
        found: list[tuple[int, int, Entity]] = []
        taken: list[tuple[int, int]] = []
        for alias in sorted(self.by_alias, key=len, reverse=True):
            keys = self.by_alias[alias]
            if len(keys) != 1:
                continue
            for m in re.finditer(r"(?<=[ ])" + re.escape(alias) + r"(?=[ ])", low):
                s, e = m.start(), m.end()
                if any(s < te and e > ts for ts, te in taken):
                    continue
                taken.append((s, e))
                found.append((s, e, self.ex.entities[next(iter(keys))]))
        return sorted(found, key=lambda t: t[0])


def _col(columns: list[str], *needles: str) -> int | None:
    for i, c in enumerate(columns):
        lc = c.lower()
        if all(n in lc for n in needles):
            return i
    return None


def _num(v: str) -> float | None:
    try:
        return float(re.sub(r"[^\d.]", "", v))
    except ValueError:
        return None


def _iso(v: str) -> str | None:
    v = v.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v
    m = DATE_TEXT_RE.search(v)
    if m:
        return datetime.strptime(" ".join(m.groups()), "%d %B %Y").date().isoformat()
    return None


class Extractor(Protocol):
    name: str

    def extract(self, doc: Document) -> Extraction: ...


class RuleBasedExtractor:
    """Deterministic default: table header signatures + cue-phrase patterns over known mentions."""

    name = "rule-based"

    def __init__(self, prose_extractor: Callable[[Chunk, Resolver], None] | None = None):
        self.prose_extractor = prose_extractor

    def extract(self, doc: Document) -> Extraction:
        ex = Extraction()
        res = Resolver(ex)
        tables = [c for c in doc.chunks if c.kind == "table"]
        # Order matters for resolution: registers with ids first, then link tables.
        def rank(c: Chunk) -> int:
            cols = [x.lower() for x in c.table["columns"]]
            return 0 if any(x.endswith(" id") for x in cols) else 1
        for chunk in sorted(tables, key=rank):
            self._from_table(chunk, res)
        self._derive(res)
        for chunk in doc.chunks:
            if chunk.kind == "text":
                (self.prose_extractor or self._from_text)(chunk, res)
        self._derive(res)
        return ex

    # --- structured: tables -----------------------------------------------------------------------
    def _from_table(self, chunk: Chunk, res: Resolver) -> None:
        cols, rows = chunk.table["columns"], chunk.table["rows"]
        src = chunk.provenance()
        c_cust_id, c_contract_id, c_sup_id = _col(cols, "customer", "id"), _col(cols, "contract", "id"), _col(cols, "supplier", "id")
        if c_cust_id is not None:  # customer register
            c_name, c_sector, c_city, c_mgr = _col(cols, "customer"), _col(cols, "sector"), _col(cols, "city"), _col(cols, "manager")
            c_name = next(i for i, c in enumerate(cols) if c.lower() == "customer") if "Customer" in cols else c_name
            for r in rows:
                cust = res.upsert("Customer", r[c_name], r[c_cust_id], {"sector": r[c_sector] if c_sector is not None else None}, src)
                if c_city is not None and r[c_city]:
                    res.relate("LOCATED_IN", cust, res.upsert("City", r[c_city], source=src), source=src)
                if c_mgr is not None and r[c_mgr]:
                    res.relate("MANAGES", res.upsert("Person", r[c_mgr], props={"role": "Account Manager"}, source=src),
                               cust, source=src)
        elif c_contract_id is not None:  # contract register
            c_cust, c_srv, c_val = _col(cols, "customer"), _col(cols, "service"), _col(cols, "value")
            c_start, c_exp = _col(cols, "start"), _col(cols, "expir")
            for r in rows:
                k = res.upsert("Contract", r[c_contract_id], r[c_contract_id], {
                    "service": r[c_srv] if c_srv is not None else None,
                    "value_ngn_m": _num(r[c_val]) if c_val is not None else None,
                    "start_date": _iso(r[c_start]) if c_start is not None else None,
                    "expiry_date": _iso(r[c_exp]) if c_exp is not None else None}, src)
                cust = res.find(r[c_cust], "Customer") if c_cust is not None else None
                if cust is None and c_cust is not None:
                    res.ex.warnings.append(f"contract {k.id}: unknown customer {r[c_cust]!r}, created from contract row")
                    cust = res.upsert("Customer", r[c_cust], source=src)
                if cust:
                    res.relate("HAS_CONTRACT", cust, k, source=src)
        elif c_sup_id is not None:  # supplier register
            c_name = next(i for i, c in enumerate(cols) if c.lower() == "supplier")
            c_cat, c_city = _col(cols, "category"), _col(cols, "city")
            for r in rows:
                sup = res.upsert("Supplier", r[c_name], r[c_sup_id], {"category": r[c_cat] if c_cat is not None else None}, src)
                if c_city is not None and r[c_city]:
                    res.relate("LOCATED_IN", sup, res.upsert("City", r[c_city], source=src), source=src)
        elif _col(cols, "supplier") is not None and _col(cols, "contract") is not None:  # allocations (link table)
            c_sup, c_k, c_role = _col(cols, "supplier"), _col(cols, "contract"), _col(cols, "role")
            for r in rows:
                sup = res.find(r[c_sup], "Supplier") or res.upsert("Supplier", r[c_sup], source=src)
                k = res.ex.entities.get(res.key("Contract", r[c_k]))
                if k is None:
                    res.ex.warnings.append(f"allocation references unknown contract {r[c_k]!r}")
                    continue
                res.relate("FULFILS", sup, k, {"role": r[c_role] if c_role is not None else None}, src)
        elif _col(cols, "name") is not None and (_col(cols, "email") is not None or _col(cols, "phone") is not None):
            c_name, c_org = _col(cols, "name"), _col(cols, "organi")
            c_mail, c_phone = _col(cols, "email"), _col(cols, "phone")
            for r in rows:
                raw = " ".join(r[i] for i in (c_mail, c_phone) if i is not None and r[i])
                person = res.upsert("Person", r[c_name], props={"role": "Contact", "contact": redact_text(raw)}, source=src)
                org = res.find(r[c_org]) if c_org is not None else None
                if org is not None:
                    res.relate("CONTACT_FOR", person, org, source=src)
        else:
            res.ex.warnings.append(f"no schema mapping for table on p.{chunk.page} ({chunk.section}): {cols}")

    # --- unstructured: sentences --------------------------------------------------------------------
    def _from_text(self, chunk: Chunk, res: Resolver) -> None:
        src = chunk.provenance()
        for sent in re.split(r"(?<=[.!?])\s+", chunk.text):
            for m in ORG_RE.finditer(sent):  # unknown organisations become Organization nodes
                if res.find(m.group(1)) is None and not res.mentions(m.group(1)):
                    res.upsert("Organization", m.group(1), source=src)
            ments = res.mentions(sent)
            contracts = [res.ex.entities.get(res.key("Contract", c)) for c in CONTRACT_RE.findall(sent)]
            contracts = [c for c in contracts if c is not None]
            low = sent.lower()
            for i, (s1, e1, a) in enumerate(ments):
                for s2, e2, b in ments[i + 1:]:
                    between = low[e1:s2]
                    if "subsidiary of" in between:
                        res.relate("SUBSIDIARY_OF", a, b, source=src)
                    elif "headquartered in" in between and b.label == "City":
                        res.relate("LOCATED_IN", a, b, source=src)
                    elif "subcontract" in between and a.label == b.label == "Supplier":
                        res.relate("SUBCONTRACTS_TO", a, b, source=src)
                    elif a.label == "Supplier" and b.label == "Customer" and "under contract" in low:
                        for k in contracts:
                            res.relate("FULFILS", a, k, {"role": "per narrative"}, src)
                    elif a.label == "Contract" and b.label == "Person" and "led by" in between:
                        res.relate("LEADS_RENEWAL", b, a, source=src)
            m = re.search(r"\bheadquartered in ([A-Z][a-z]+(?: [A-Z][a-z]+)?)", sent)
            if m and ments and ments[0][2].label in ("Organization", "Customer", "Supplier"):
                city = res.find(m.group(1), "City") or res.upsert("City", m.group(1), source=src)
                res.relate("LOCATED_IN", ments[0][2], city, source=src)
            # critic: cross-check narrative expiry dates against the contract register
            for k in contracts:
                d = DATE_TEXT_RE.search(sent[sent.find(k.id):]) if "expire" in low else None
                if d:
                    iso = _iso(d.group(0))
                    if k.props.get("expiry_date") and iso != k.props["expiry_date"]:
                        res.ex.warnings.append(f"{k.id}: narrative expiry {iso} != register {k.props['expiry_date']}")
                    elif iso:
                        k.sources.append(src) if src not in k.sources else None

    @staticmethod
    def _derive(res: Resolver) -> None:
        """Inference: Supplier FULFILS Contract <- HAS_CONTRACT Customer  =>  Supplier SERVES Customer."""
        owner = {r.dst: r for r in res.ex.relations.values() if r.type == "HAS_CONTRACT"}
        for r in list(res.ex.relations.values()):
            if r.type == "FULFILS" and r.dst in owner:
                sup, cust = res.ex.entities[r.src], res.ex.entities[owner[r.dst].src]
                res.relate("SERVES", sup, cust, {"derived": True}, r.sources[0] if r.sources else None)


LLM_EXTRACT_PROMPT = """Extract entities and relationships from the text for a knowledge graph.
Use ONLY this schema:
{schema}
Return JSON: {{"entities":[{{"label":"...","name":"...","id":"optional id"}}],
"relations":[{{"type":"...","source":"entity name or id","target":"entity name or id"}}]}}
Do not include emails, phone numbers or ID numbers."""


class LLMExtractor(RuleBasedExtractor):
    """Tables stay deterministic; prose chunks go to an OpenAI-compatible model, output is schema-validated.

    Any model/network/parse failure falls back to the rule-based prose extractor for that chunk.
    """

    name = "llm"

    def __init__(self, api_key: str, base_url: str | None = None, model: str | None = None,
                 complete: Callable[[str, str], str] | None = None, timeout: float = 60):
        super().__init__()
        self.prose_extractor = self._llm_prose
        self.api_key = api_key
        self.base_url = (base_url or os.getenv("LOG_ON_LLM_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model = model or os.getenv("LOG_ON_LLM_MODEL", "gpt-4o-mini")
        self._complete = complete or self._http_complete
        self.timeout = timeout
        self.errors: list[str] = []

    def _http_complete(self, system: str, user: str) -> str:
        body = json.dumps({"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())["choices"][0]["message"]["content"]

    def _llm_prose(self, chunk: Chunk, res: Resolver) -> None:
        src = chunk.provenance()
        try:
            data = json.loads(self._complete(LLM_EXTRACT_PROMPT.format(schema=describe()), redact_text(chunk.text)))
            ents: dict[str, Entity] = {}
            for e in data.get("entities", []):
                if e.get("label") not in NODE_TYPES or not e.get("name"):
                    raise ValueError(f"bad entity {e!r}")
                found = res.find(e["name"], e["label"]) or res.ex.entities.get(res.key(e["label"], e.get("id") or ""))
                ent = found or res.upsert(e["label"], e["name"], e.get("id"), source=src)
                ents[e["name"]] = ent
                if e.get("id"):
                    ents[e["id"]] = ent
            for r in data.get("relations", []):
                if r.get("type") not in REL_TYPES:
                    raise ValueError(f"bad relation type {r!r}")
                a = ents.get(r.get("source")) or res.find(str(r.get("source")))
                b = ents.get(r.get("target")) or res.find(str(r.get("target")))
                if a and b:
                    res.relate(r["type"], a, b, source=src)  # schema-checked inside relate()
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"{chunk.chunk_id}: {type(exc).__name__}: {exc}")
            self._from_text(chunk, res)


def get_extractor() -> Extractor:
    key = os.getenv("LOG_ON_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    return LLMExtractor(key) if key else RuleBasedExtractor()


def load_into(store, ex: Extraction, clear: bool = True) -> dict[str, int]:
    """Write an Extraction into a GraphStore (idempotent MERGE). Returns node/rel counts written."""
    store.setup()
    if clear:
        store.clear()
    for e in ex.entities.values():
        props = {k: v for k, v in e.props.items() if v is not None}
        props.update(name=e.name, aliases="|".join(sorted(e.aliases)), sources=json.dumps(e.sources))
        store.upsert_node(e.label, e.id, {k: redact_text(v) if isinstance(v, str) and k != "sources" else v
                                          for k, v in props.items()})
    for r in ex.relations.values():
        a, b = ex.entities[r.src], ex.entities[r.dst]
        props = {**r.props, "sources": json.dumps(r.sources)}
        store.upsert_rel(r.type, a.label, a.id, b.label, b.id, props)
    return ex.stats()


def iter_entities(ex: Extraction, label: str) -> Iterable[Entity]:
    return (e for e in ex.entities.values() if e.label == label)
