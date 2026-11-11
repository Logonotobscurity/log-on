"""Versioned prompt registry with a human-approval gate (the learning layer *proposes*, a human *approves*).

Layout (committed to git, reviewed like code):
  prompts/registry.json                  index: per component, the `active` version and every version's metadata
  prompts/<component>/<version>.json     the compiled DSPy program state (instructions + few-shot demos)
  prompts/approvals.jsonl                hash-chained audit log of propose / approve / reject / rollback decisions

Rules enforced here (and tested):
  * `propose()` never activates anything. It refuses programs containing personal data (only RFC 2606
    `*.example` addresses and the documented fake identifiers are allowed) and records the file's sha256.
  * `approve()` needs a named human (not the learning job), a proposed version whose file still matches its
    sha256, an optimised score >= baseline (no regression), and a passed eval gate. Only then is it `active`.
  * `load_active()` re-checks the sha256, so a hand-edited or swapped prompt file is refused at runtime.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from ..audit import AuditLog

ROOT = Path(__file__).resolve().parents[2] / "prompts"
MACHINE_ACTORS = {"", "auto", "learning", "dspy", "optimizer", "bot", "ci", "system"}
FAKE_IDS = ("12345678901", "08000000001")


class PromotionRefused(RuntimeError):
    pass


def pii_findings(text: str) -> list[str]:
    from ..guards.validators import redact_text
    masked = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.example\b", "x", text)  # reserved demo domain
    for f in FAKE_IDS:
        masked = masked.replace(f, "x")
    red = redact_text(masked)
    return sorted(set(re.findall(r"<[A-Z_]+>", red)) - set(re.findall(r"<[A-Z_]+>", masked)))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PromptRegistry:
    def __init__(self, root: str | Path = ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "registry.json"
        self.log = AuditLog(self.root / "approvals.jsonl")

    # ---- index -------------------------------------------------------------------------------------
    def _load(self) -> dict:
        if self.index_path.exists():
            return json.loads(self.index_path.read_text())
        return {"_comment": "Managed by log_on.learning. Promote only with: python -m log_on.learning approve",
                "components": {}}

    def _save(self, data: dict) -> None:
        self.index_path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")

    def component(self, name: str) -> dict:
        return self._load()["components"].get(name, {"active": None, "versions": {}})

    def versions(self, name: str) -> dict:
        return self.component(name)["versions"]

    def active(self, name: str) -> str | None:
        return self.component(name).get("active")

    # ---- lifecycle ---------------------------------------------------------------------------------
    def propose(self, component: str, program_json: dict | str, meta: dict) -> str:
        text = program_json if isinstance(program_json, str) else json.dumps(program_json, indent=2, sort_keys=True)
        found = pii_findings(text)
        if found:
            raise PromotionRefused(f"compiled program contains personal data {found}; not saved")
        data = self._load()
        comp = data["components"].setdefault(component, {"active": None, "versions": {}})
        digest = hashlib.sha256(text.encode()).hexdigest()
        version = f"v{len(comp['versions']) + 1}-{time.strftime('%Y%m%d')}-{digest[:8]}"
        path = self.root / component / f"{version}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        comp["versions"][version] = {**meta, "status": "proposed", "file": str(path.relative_to(self.root)),
                                     "sha256": digest, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                     "proposed_by": meta.get("proposed_by", "learning")}
        self._save(data)
        self.log.record(component, "propose", "completed", {"version": version, "sha256": digest,
                                                             "scores": meta.get("scores", {})})
        return version

    def _check_promotable(self, component: str, version: str, by: str) -> dict:
        if (by or "").strip().lower() in MACHINE_ACTORS:
            raise PromotionRefused("an optimised prompt must be approved by a named human, not the learning layer")
        v = self.versions(component).get(version)
        if v is None:
            raise PromotionRefused(f"unknown version {version}")
        if v["status"] not in ("proposed", "retired"):
            raise PromotionRefused(f"version {version} is {v['status']}, not proposed")
        if sha256(self.root / v["file"]) != v["sha256"]:
            raise PromotionRefused("program file changed after it was proposed (sha256 mismatch)")
        s = v.get("scores", {})
        if s.get("optimized") is None or s.get("baseline") is None or s["optimized"] < s["baseline"]:
            raise PromotionRefused(f"optimised score {s.get('optimized')} is below baseline {s.get('baseline')}")
        gate = v.get("eval_gate") or {}
        if not gate.get("passed"):
            raise PromotionRefused(f"eval gate not passed for {version}: {gate.get('failures') or 'not run'}")
        return v

    def approve(self, component: str, version: str, by: str, note: str = "") -> dict:
        self._check_promotable(component, version, by)
        data = self._load()
        comp = data["components"][component]
        prev = comp.get("active")
        if prev and prev in comp["versions"]:
            comp["versions"][prev]["status"] = "retired"
        v = comp["versions"][version]
        v.update(status="approved", approved_by=by, approved_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), note=note)
        comp["active"] = version
        self._save(data)
        self.log.record(component, "approve", "completed", {"version": version, "by": by, "note": note,
                                                             "previous": prev, "sha256": v["sha256"]})
        return v

    def reject(self, component: str, version: str, by: str, note: str = "") -> dict:
        data = self._load()
        v = data["components"][component]["versions"][version]
        if data["components"][component].get("active") == version:
            raise PromotionRefused("cannot reject the active version; roll back first")
        v.update(status="rejected", rejected_by=by, note=note)
        self._save(data)
        self.log.record(component, "reject", "completed", {"version": version, "by": by, "note": note})
        return v

    def rollback(self, component: str, by: str, note: str = "") -> str | None:
        """Deactivate the current version (the component falls back to its unoptimised / rule path)."""
        data = self._load()
        comp = data["components"].get(component) or {}
        cur = comp.get("active")
        if cur:
            comp["versions"][cur]["status"] = "retired"
            comp["active"] = None
            self._save(data)
            self.log.record(component, "rollback", "completed", {"version": cur, "by": by, "note": note})
        return cur

    def load_active(self, component: str) -> tuple[str, Path] | None:
        version = self.active(component)
        if not version:
            return None
        v = self.versions(component)[version]
        path = self.root / v["file"]
        if v.get("status") != "approved" or sha256(path) != v["sha256"]:
            raise PromotionRefused(f"active {component} {version} failed its integrity check; refusing to load")
        return version, path

    def verify(self) -> list[str]:
        """Integrity problems (empty list = ok): every version file matches its recorded sha256 and the
        approvals log hash chain is intact."""
        problems = []
        for comp, c in self._load().get("components", {}).items():
            for ver, v in c.get("versions", {}).items():
                path = self.root / v["file"]
                if not path.exists():
                    problems.append(f"{comp} {ver}: file {v['file']} missing")
                elif sha256(path) != v["sha256"]:
                    problems.append(f"{comp} {ver}: sha256 mismatch")
            act = c.get("active")
            if act and c["versions"].get(act, {}).get("status") != "approved":
                problems.append(f"{comp}: active version {act} is not approved")
        if self.log.path.exists() and not self.log.verify():
            problems.append("approvals.jsonl hash chain broken")
        return problems

