"""GraphStore interface + two backends that both speak Cypher.

  KuzuGraphStore  (default) embedded, file-based, zero-ops, Cypher. Works in tests/CI and inside the MCP
                  subprocess without a server. Opened read-only by query tools.
  Neo4jGraphStore (optional) the course's database. Used when LOG_ON_GRAPH_BACKEND=neo4j; connection from
                  NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD (env only, never in code or git).

Query tools only ever see a read-only handle: Kuzu opens the database with read_only=True, Neo4j runs
queries in READ access mode, and `assert_read_only()` rejects write clauses before either is reached.
"""
from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .schema import NODE_TYPES, REL_PROPS, REL_TYPES, node_props

WRITE_CLAUSES = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|ALTER|COPY|LOAD|INSTALL|IMPORT|EXPORT|"
                           r"ATTACH|CALL|FOREACH)\b", re.I)


class ReadOnlyViolation(ValueError):
    pass


def assert_read_only(cypher: str) -> None:
    stripped = re.sub(r"'[^']*'|\"[^\"]*\"", "''", cypher)  # ignore keywords inside string literals
    m = WRITE_CLAUSES.search(stripped)
    if m:
        raise ReadOnlyViolation(f"write/admin clause '{m.group(1).upper()}' is not allowed in graph_query")


class GraphStore(ABC):
    backend = "abstract"
    read_only = False

    # --- writes (ingestion only) ------------------------------------------------------------------
    @abstractmethod
    def setup(self) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...

    def upsert_node(self, label: str, id: str, props: dict[str, Any]) -> None:
        allowed = node_props(label)
        props = {k: v for k, v in props.items() if k in allowed and k != "id"}
        sets = ", ".join(f"n.{k} = ${k}" for k in props)
        q = f"MERGE (n:{label} {{id: $id}})" + (f" SET {sets}" if sets else "")
        self._write(q, {"id": id, **props})

    def upsert_rel(self, rel: str, src_label: str, src_id: str, dst_label: str, dst_id: str,
                   props: dict[str, Any] | None = None) -> None:
        props = {k: v for k, v in (props or {}).items() if k in REL_PROPS}
        sets = ", ".join(f"r.{k} = ${k}" for k in props)
        q = (f"MATCH (a:{src_label} {{id: $src}}), (b:{dst_label} {{id: $dst}}) MERGE (a)-[r:{rel}]->(b)"
             + (f" SET {sets}" if sets else ""))
        self._write(q, {"src": src_id, "dst": dst_id, **props})

    @abstractmethod
    def _write(self, cypher: str, params: dict[str, Any] | None = None) -> None: ...

    # --- reads -------------------------------------------------------------------------------------
    @abstractmethod
    def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run a read-only Cypher query; rows as dicts keyed by RETURN aliases."""

    @abstractmethod
    def shortest_path(self, src_id: str, dst_id: str, max_hops: int = 5) -> dict[str, Any] | None:
        """{"nodes": [{id,name,label}], "rels": [{type, src, dst}]} or None."""

    @abstractmethod
    def all_nodes(self) -> list[dict[str, Any]]:
        """[{label, id, name, aliases}] (small graphs: entity linking is done in Python)."""

    def counts(self) -> dict[str, int]:
        out = {}
        for label in NODE_TYPES:
            out[label] = self.query(f"MATCH (n:{label}) RETURN count(n) AS c")[0]["c"]
        for rel in REL_TYPES:
            out[rel] = self.query(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c")[0]["c"]
        return out

    def close(self) -> None:  # pragma: no cover - backends override
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class KuzuGraphStore(GraphStore):
    backend = "kuzu"

    def __init__(self, path: str | os.PathLike, read_only: bool = False):
        import kuzu

        self.path = Path(path)
        if read_only and not self.path.exists():
            raise FileNotFoundError(f"graph database {self.path} does not exist; run `python -m log_on.ingest` first")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.read_only = read_only
        self._db = kuzu.Database(str(self.path), read_only=read_only)
        self._conn = kuzu.Connection(self._db)

    def setup(self) -> None:
        for label in NODE_TYPES:
            cols = ", ".join(f"{k} {t}" for k, t in node_props(label).items())
            self._conn.execute(f"CREATE NODE TABLE IF NOT EXISTS {label}({cols}, PRIMARY KEY(id))")
        for rel, pairs in REL_TYPES.items():
            ends = ", ".join(f"FROM {a} TO {b}" for a, b in pairs)
            props = ", ".join(f"{k} {t}" for k, t in REL_PROPS.items())
            self._conn.execute(f"CREATE REL TABLE IF NOT EXISTS {rel}({ends}, {props})")

    def clear(self) -> None:
        for rel in REL_TYPES:
            self._conn.execute(f"DROP TABLE IF EXISTS {rel}")
        for label in NODE_TYPES:
            self._conn.execute(f"DROP TABLE IF EXISTS {label}")
        self.setup()

    def _write(self, cypher, params=None):
        self._conn.execute(cypher, params or {})

    def query(self, cypher, params=None):
        assert_read_only(cypher)
        res = self._conn.execute(cypher, params or {})
        cols = res.get_column_names()
        rows = []
        while res.has_next():
            rows.append(dict(zip(cols, res.get_next())))
        return rows

    def shortest_path(self, src_id, dst_id, max_hops=5):
        max_hops = max(1, min(int(max_hops), 8))
        res = self._conn.execute(
            f"MATCH p = (a)-[* SHORTEST 1..{max_hops}]-(b) WHERE a.id = $a AND b.id = $b RETURN nodes(p), rels(p) LIMIT 1",
            {"a": src_id, "b": dst_id})
        if not res.has_next():
            return None
        nodes, rels = res.get_next()
        by_internal = {(n["_id"]["table"], n["_id"]["offset"]): n for n in nodes}
        return {"nodes": [{"id": n["id"], "name": n["name"], "label": n["_label"]} for n in nodes],
                "rels": [{"type": r["_label"],
                          "src": by_internal[(r["_src"]["table"], r["_src"]["offset"])]["id"],
                          "dst": by_internal[(r["_dst"]["table"], r["_dst"]["offset"])]["id"],
                          "sources": r.get("sources")} for r in rels]}

    def all_nodes(self):
        out = []
        for label in NODE_TYPES:
            for r in self.query(f"MATCH (n:{label}) RETURN n.id AS id, n.name AS name, n.aliases AS aliases"):
                out.append({"label": label, **r})
        return out

    def close(self):
        try:
            self._conn.close()
            self._db.close()
        except Exception:  # noqa: BLE001
            pass


class Neo4jGraphStore(GraphStore):
    backend = "neo4j"

    def __init__(self, uri: str | None = None, user: str | None = None, password: str | None = None,
                 database: str | None = None, read_only: bool = False):
        from neo4j import GraphDatabase

        uri = uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        user = user or os.environ.get("NEO4J_USER", "neo4j")
        password = password if password is not None else os.environ.get("NEO4J_PASSWORD", "")
        self.database = database or os.environ.get("NEO4J_DATABASE", "neo4j")
        self.read_only = read_only
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._driver.verify_connectivity()

    def setup(self):
        for label in NODE_TYPES:
            self._driver.execute_query(
                f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE",
                database_=self.database)

    def clear(self):
        labels = "|".join(NODE_TYPES)
        self._driver.execute_query(f"MATCH (n:{labels}) DETACH DELETE n", database_=self.database)
        self.setup()

    def _write(self, cypher, params=None):
        if self.read_only:
            raise ReadOnlyViolation("store opened read-only")
        self._driver.execute_query(cypher, params or {}, database_=self.database)

    def query(self, cypher, params=None):
        from neo4j import RoutingControl

        assert_read_only(cypher)
        recs, _, keys = self._driver.execute_query(cypher, params or {}, database_=self.database,
                                                   routing_=RoutingControl.READ)
        return [dict(zip(keys, r.values())) for r in recs]

    def shortest_path(self, src_id, dst_id, max_hops=5):
        max_hops = max(1, min(int(max_hops), 8))
        rows = self.query(
            f"MATCH (a {{id: $a}}), (b {{id: $b}}) MATCH p = shortestPath((a)-[*..{max_hops}]-(b)) "
            "RETURN [n IN nodes(p) | {id: n.id, name: n.name, label: labels(n)[0]}] AS ns, "
            "[r IN relationships(p) | {type: type(r), src: startNode(r).id, dst: endNode(r).id, sources: r.sources}] AS rs",
            {"a": src_id, "b": dst_id})
        if not rows:
            return None
        return {"nodes": rows[0]["ns"], "rels": rows[0]["rs"]}

    def all_nodes(self):
        labels = "|".join(NODE_TYPES)
        return self.query(f"MATCH (n:{labels}) RETURN labels(n)[0] AS label, n.id AS id, n.name AS name, "
                          "n.aliases AS aliases")

    def close(self):
        self._driver.close()


def default_graph_path() -> str:
    return os.environ.get("LOG_ON_GRAPH_PATH", "runs/graph.kuzu")


def make_graph_store(backend: str | None = None, path: str | os.PathLike | None = None,
                     read_only: bool = False) -> GraphStore:
    backend = (backend or os.environ.get("LOG_ON_GRAPH_BACKEND", "kuzu")).lower()
    if backend == "neo4j":
        return Neo4jGraphStore(read_only=read_only)
    if backend == "kuzu":
        return KuzuGraphStore(path or default_graph_path(), read_only=read_only)
    raise ValueError(f"unknown graph backend {backend!r} (kuzu|neo4j)")


def sources_of(value: Any) -> list[dict]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return []
