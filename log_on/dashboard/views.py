"""Server-rendered pages and JSON endpoints for the LOG_ON dashboard (mounted into approval_ui.create_app).

All pages require an approver token (cookie or Bearer), exactly like the approvals UI. Decisions are posted to the
existing /approvals/{run_id}/decide handler with its CSRF token, so there is one code path for approving."""
from __future__ import annotations

import html
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import analysis as A
from . import data as D

STATIC = Path(__file__).parent / "static"
NAV = [("story", "/ui", "Story"), ("journey", "/ui/journey", "Journey"), ("approvals", "/ui/approvals", "Approvals"),
       ("audit", "/ui/audit", "Audit"), ("safety", "/ui/safety", "Safety"), ("ops", "/ui/ops", "Ops"),
       ("analysis", "/ui/analysis", "Analysis")]


def E(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def T(iso: str | None) -> str:
    """ISO timestamp -> box-local time with its zone label (WAT on this box)."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(str(iso)).astimezone().strftime("%d %b %H:%M:%S %Z")
    except ValueError:
        return str(iso)


def P(x, d: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:.{d}f}%"


def badge(state: str) -> str:
    s = str(state or "unknown")
    return f'<span class="badge b-{E(s.replace(" ", "-"))}">{E(s.replace("_", " "))}</span>'


# ---------------------------------------------------------------- svg helpers (server-side, no JS)
def hbars(rows: list[tuple[str, float, str]], vmax: float | None = None, width: int = 560, unit: str = "",
          label_w: int = 190, title: str = "") -> str:
    """rows: (label, value, css colour). Horizontal bar chart."""
    vmax = vmax or max([v for _, v, _ in rows] + [1e-9])
    h = 26 * len(rows) + 8
    bw = width - label_w - 95
    out = [f'<svg viewBox="0 0 {width} {h}" width="100%" role="img" aria-label="{E(title)}"><title>{E(title)}</title>']
    for i, (lab, v, col) in enumerate(rows):
        y = 6 + i * 26
        w = max(1.5, bw * (v / vmax)) if v else 0
        out.append(f'<text class="lbl" x="{label_w - 8}" y="{y + 13}" text-anchor="end">{E(lab)}</text>'
                   f'<rect x="{label_w}" y="{y}" width="{bw}" height="17" fill="#191816"/>'
                   f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="17" fill="{col}"/>'
                   f'<text class="v" x="{label_w + w + 6:.1f}" y="{y + 13}">{E(_n(v))}{E(unit)}</text>')
    return "".join(out) + "</svg>"


def _n(v) -> str:
    if isinstance(v, float) and not v.is_integer():
        return f"{v:.2f}" if v < 100 else f"{v:.0f}"
    return f"{int(v):,}" if isinstance(v, (int, float)) else str(v)


def stacked(rows: list[tuple[str, list[tuple[float, str, str]]]], width: int = 900, label_w: int = 290, title: str = "") -> str:
    """rows: (label, [(value, colour, name)]) -> 100%-stacked horizontal bars with counts."""
    h = 34 * len(rows) + 8
    bw = width - label_w - 20
    out = [f'<svg viewBox="0 0 {width} {h}" width="100%" role="img" aria-label="{E(title)}"><title>{E(title)}</title>']
    for i, (lab, segs) in enumerate(rows):
        y = 6 + i * 34
        tot = sum(v for v, _, _ in segs) or 1
        x = label_w
        out.append(f'<text class="lbl" x="{label_w - 10}" y="{y + 16}" text-anchor="end">{E(lab)}</text>')
        for v, col, name in segs:
            w = bw * v / tot
            if w <= 0:
                continue
            out.append(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="22" fill="{col}"><title>{E(name)}: {v}</title></rect>')
            if w > 34:
                out.append(f'<text x="{x + 6:.1f}" y="{y + 15}" style="fill:#140b06">{_n(v)}</text>')
            x += w
    return "".join(out) + "</svg>"


C_OK, C_BAD, C_WARN, C_INFO, C_MUTED, C_ACC = "#8fb573", "#e0663a", "#d9a441", "#8fa9c4", "#4a453f", "#d94e1a"


# ---------------------------------------------------------------- shell
def shell(title: str, active: str, body: str, who: str, demo: bool, flash: str = "", error: bool = False) -> HTMLResponse:
    nav = "".join(f'<a href="{href}"{" aria-current=page" if key == active else ""}>{lab}</a>' for key, href, lab in NAV)
    banner = ('<div class="demo" role="note"><b>DEMO DATA.</b> These runs were seeded by '
              '<code>python -m log_on.dashboard.seed</code> with fictional identifiers. Evaluation, red-team, '
              'benchmark and serving numbers come from the committed evidence in <code>docs/</code>.</div>') if demo else ""
    fl = f'<div class="flash{" err" if error else ""}" role="status">{E(flash)}</div>' if flash else ""
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{E(title)} · LOG_ON</title>'
           '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark">'
           '<link rel="preload" href="/static/fonts/playfair-display.woff2" as="font" type="font/woff2" crossorigin>'
           '<link rel="icon" href="data:,"><link rel="stylesheet" href="/static/deckle.css"></head><body>'
           '<a class="skip" href="#main">Skip to content</a>'
           '<header class="mast"><div class="mast-in"><a class="brand" href="/ui">LOG_ON<small>AGENT LEDGER</small></a>'
           f'<nav aria-label="Dashboard">{nav}</nav><div class="who">signed in as <b>{E(who)}</b>'
           '<a href="/">classic queue</a><a href="/logout">sign out</a></div></div></header>'
           f'<main id="main" tabindex="-1">{banner}{fl}{body}</main>'
           '<footer>Server-rendered from runs/ and docs/ on each request. Times in the server zone. '
           'Keyboard: <span class="kbd">Tab</span> to move, <span class="kbd">j</span>/<span class="kbd">k</span> '
           'between runs on Journey. JSON for every view under <code>/api/ui/</code>.</footer>'
           '<script src="/static/dash.js" defer></script></body></html>')
    return HTMLResponse(doc)


def head(kicker: str, title: str, lede: str) -> str:
    return f'<p class="kicker">{E(kicker)}</p><h1>{E(title)}</h1><p class="lede">{lede}</p>'


def tension(c: dict) -> str:
    ev = " · ".join(f"<code>{E(x)}</code>" for x in c.get("evidence", []))
    return (f'<article class="tension" aria-label="{E(c["area"])}: claim and breaking point">'
            f'<div class="area"><span class="kicker">{E(c["area"])}</span><span class="small muted">{ev}</span></div>'
            f'<div class="says"><span class="tag">What it says</span><p class="claim">{E(c["claim"])}</p>'
            f'<p>{E(c["says"])}</p></div>'
            f'<div class="doesnt"><span class="tag">What it doesn\'t</span><p>{E(c["doesnt"])}</p>'
            f'<p class="bp"><b>Breaking point</b>{E(c["breaking"])}</p></div></article>')


# ---------------------------------------------------------------- page bodies
def story_body(ctx: dict) -> str:
    rs, ev, aud, red, pending = ctx["runs"], ctx["evals"], ctx["audit"], ctx["redteam"], ctx["pending"]
    lat = ev.get("latest") or {}
    st = {}
    for r in rs:
        st[r["status"]] = st.get(r["status"], 0) + 1
    a = aud.get("audit.jsonl", {})
    ra = A._rt(red, "step2_rule_after.json")
    seal = ('<span class="stamp ok">chain intact</span>' if a.get("ok") else '<span class="stamp bad">chain broken</span>'
            ) if a.get("exists") else '<span class="stamp muted">no log yet</span>'
    stats = (
        '<div class="grid g4">'
        f'<div class="card stat"><div class="label">Runs on record</div><div class="big">{len(rs)}</div>'
        f'<div class="small muted">{st.get("completed", 0)} completed · {st.get("refused", 0)} refused · '
        f'{st.get("awaiting_approval", 0) + st.get("awaiting_human", 0)} waiting on a person</div></div>'
        f'<div class="card stat"><div class="label">Waiting for approval</div><div class="big acc">{pending}</div>'
        f'<div class="small"><a href="/ui/approvals">open the queue</a></div></div>'
        f'<div class="card stat"><div class="label">Eval gate</div><div class="big">{E(lat.get("cases", "n/a"))}'
        f'<small> cases</small></div><div class="small">{badge("passed" if lat.get("gate") == "PASSED" else "blocked")} '
        f'{E(lat.get("pii_leaks", "n/a"))} PII leaks</div></div>'
        f'<div class="card stat"><div class="label">Audit trail</div><div class="big">{E(a.get("records", 0))}'
        f'<small> records</small></div><div>{seal}</div></div></div>')
    # outcome bar
    order = [("completed", C_OK), ("awaiting_approval", C_WARN), ("awaiting_human", C_INFO), ("refused", C_BAD),
             ("denied", "#b9532f"), ("quota_exceeded", C_ACC)]
    segs = [(st.get(k, 0), c, k) for k, c in order] + [(v, C_MUTED, k) for k, v in st.items() if k not in dict(order)]
    legend = "".join(f'<span><i style="background:{c}"></i>{E(k.replace("_", " "))} {v}</span>' for v, c, k in segs if v)
    outcome = (f'<div class="card"><h3>Where requests ended</h3>{stacked([("all runs", segs)], label_w=90, title="Run outcomes")}'
               f'<div class="legend">{legend}</div></div>' if rs else
               '<div class="card"><h3>No runs yet</h3><p class="muted">Run <code>python -m log_on.dashboard.seed</code> '
               'for demo data, or use the CLI.</p></div>')
    # chapter cards: a few runs as story beats
    beats = []
    for status, line in [("completed", "went all the way through"), ("refused", "was stopped at the door"),
                         ("awaiting_approval", "is waiting for a person"), ("awaiting_human", "was escalated"),
                         ("denied", "was turned down by an approver"), ("quota_exceeded", "ran out of daily budget")]:
        r = next((x for x in rs if x["status"] == status), None)
        if r:
            beats.append(f'<li><a href="/ui/journey?run={E(quote(r["run_id"]))}"><code>{E(r["run_id"])}</code></a> '
                         f'{badge(status)} <span class="muted">{E(line)}:</span> “{E(r["goal"][:110])}”</li>')
    beats_html = (f'<div class="card"><h3>Six ways a request can end</h3><ol class="small">{"".join(beats)}</ol>'
                  '<p class="muted small">Open any run to follow it through the thirteen layers.</p></div>') if beats else ""
    cl = ctx["claims"][:2]
    return (head("The state of the ledger", "Every request leaves a trail.",
                 "LOG_ON is an agent that plans, asks permission, acts, checks itself and writes everything down. "
                 "This dashboard follows a request through that machinery, then asks how far the safety evidence "
                 "really reaches.")
            + stats + f'<div class="grid g2">{outcome}{beats_html}</div>'
            + (f'<p class="pull">{E(A.state_of(ev, red, ctx["bench"], aud, ctx["ops"], rs, pending, ctx["demo"])["verdict"])}</p>')
            + "<h2>Two claims, and where they break</h2>" + "".join(tension(c) for c in cl)
            + '<p><a href="/ui/safety">All claims on the Safety page →</a> · <a href="/ui/analysis">Full analysis →</a></p>'
            + (f'<p class="small muted">Latest red-team set: {ra["attacks"]} attacks from {ra["seeds"]} seeds × '
               f'{ra["techniques"]} techniques.</p>' if ra else ""))


def journey_body(rs: list[dict], j: dict | None, sel: str | None) -> str:
    items = "".join(
        f'<li><a href="/ui/journey?run={E(quote(r["run_id"]))}"{" aria-current=true" if r["run_id"] == sel else ""}>'
        f'<code>{E(r["run_id"])}</code> {badge(r["status"])} <span class="small muted">{E(r["user"])} · {E(T(r["started"]))}</span>'
        f'<span class="g">{E(r["goal"][:120])}</span></a></li>' for r in rs)
    left = (f'<div class="card inset"><h3>Runs</h3><p class="small muted">Newest first. <span class="kbd">j</span>/'
            f'<span class="kbd">k</span> to move.</p><ul class="runlist">{items or "<li class=muted>No runs.</li>"}</ul></div>')
    if not j:
        right = '<div class="card"><p class="muted">Pick a run.</p></div>'
    else:
        r = j["run"]
        lis = "".join(
            f'<li class="s-{E(l["state"].replace(" ", "-"))}"><span class="dot" aria-hidden="true">{l["n"]}</span>'
            f'<span class="lname">{E(l["layer"])}</span> {badge(l["state"])} '
            f'<span class="small muted mono">{E(", ".join(l["hit"]) or "—")}</span>'
            f'<div class="lnote">{E(l["note"])}</div>'
            + (f'<div class="ldet">{E(l["detail"])}</div>' if l["detail"] else "") + "</li>" for l in j["layers"])
        steps = "".join(
            f'<tr><td class="num">{s["i"]}</td><td class="mono small">{E(T(s["ts"]))}</td><td><code>{E(s["node"])}</code></td>'
            f'<td>{badge(s["event"] if s["event"] != "completed" else (s["status"] or "passed"))}</td><td class="small">{E(s["detail"])}</td>'
            f'<td class="hash mono small muted">{E(s["hash"])}</td></tr>' for s in j["steps"])
        dec = "".join(f'<li>{badge(d["decision"])} by <b>{E(d["approver"])}</b> via {E(d["source"])} · {E(T(d["ts"]))} — '
                      f'“{E(d["note"])}”</li>' for d in r["decisions"])
        u = r.get("usage") or {}
        right = (
            f'<div class="card paper deckle"><p class="kicker" style="color:#7a3a14">Run {E(r["run_id"])} · user '
            f'{E(r["user"])}</p><h2 class="goal">“{E(r["goal"][:280])}{"…" if len(r["goal"]) > 280 else ""}”</h2><p>Outcome {badge(j["outcome"])} — stopped at '
            f'<code>{E(j["stopped_at"])}</code>. {E(u.get("steps", "?"))} steps, {E(u.get("tool_calls", 0))} tool calls, '
            f'{E(u.get("total_tokens", 0))} tokens, ${E(u.get("est_cost_usd", 0))}.</p>'
            '<p class="small muted">Request text as stored in the audit log (personal data redacted at write time).</p></div>'
            + (f'<div class="card"><h3>Human decisions</h3><ul>{dec}</ul></div>' if dec else "")
            + '<div class="card"><h3>The thirteen layers</h3>'
            '<p class="small muted">Reference order. A layer is <i>not needed</i> when the run finished without it and '
            f'<i>not reached</i> when it stopped first.</p><ol class="track cols">{lis}</ol></div>'
            '<div class="card"><h3>As executed</h3><p class="small muted">The audit records for this run, in chain order. '
            'LOG_ON checks policy before reading memory and asks for approval before acting.</p>'
            f'<div class="scroll"><table><thead><tr><th>#</th><th>When</th><th>Node</th><th>Result</th><th>What happened</th>'
            f'<th>Hash</th></tr></thead><tbody>{steps}</tbody></table></div></div>')
    return (head("A request's journey", "Thirteen doors, one request.",
                 "Select a run to see which layers it passed, where it was blocked, paused for a person or escalated, "
                 "and the audit records behind each step.")
            + f'<div class="split">{left}<div>{right}</div></div>')


def approvals_body(items, hist: list[dict], csrf: str, ttl_min: int) -> str:
    cards = []
    for p in items:
        steps = "".join(
            f'<tr><td class="num">{s["index"]}</td><td><code>{E(s["tool"])}</code><br>{badge(s["risk"])}'
            f'{" <b class=acc>needs approval</b>" if s["needs_approval"] else ""}</td><td>{E(s["effect"])}'
            + (f'<div class="small">to <code>{E(s["destination"])}</code> '
               + ('<span class="ok">trusted domain</span>' if s.get("destination_trusted") else '<span class="bad">outside trusted domains</span>')
               + '</div>' if "destination" in s else "")
            + '</td><td class="small">' + "".join(f'<div><b>{E(k)}</b>: <code>{E(v)}</code></div>' for k, v in s["args"].items())
            + '</td></tr>' for s in p.steps)
        pol = p.policy or {}
        warns = "".join(f'<li class="bad">{E(w)}</li>' for w in p.memory_warnings)
        left = "expired" if p.expired else f'{p.seconds_left // 3600} h {(p.seconds_left % 3600) // 60} min left'
        cards.append(
            f'<article class="card" aria-labelledby="ap-{E(p.run_id)}"><p class="kicker">Run {E(p.run_id)} · from '
            f'{E(p.user)} · planner {E(p.planner)}</p><h2 id="ap-{E(p.run_id)}">“{E(p.goal)}”</h2>'
            f'<p class="small muted">Requested {E(T(p.requested_at.isoformat()))} · '
            f'<span class="{"bad" if p.expired else "warn"}">{E(left)}</span></p>'
            '<div class="grid g2"><div><h3>If you approve</h3><div class="scroll"><table><thead><tr><th>#</th><th>Tool</th>'
            f'<th>Effect</th><th>Arguments (redacted)</th></tr></thead><tbody>{steps}</tbody></table></div></div>'
            '<div><h3>Why it paused</h3><ul class="small">'
            f'<li>Input policy: {"passed" if pol.get("allowed", True) else "flagged"}'
            f'{E(" (" + ", ".join(pol.get("rule_ids", [])) + ")") if pol.get("rule_ids") else ""}</li>'
            f'<li>Steps {E(p.permission.get("approval_steps"))} need approval: {E(p.permission.get("reason"))}</li>'
            f'<li>Checks passed: {E(", ".join(p.permission.get("checks_passed", [])))}</li>{warns}</ul>'
            f'<form class="decide" method="post" action="/approvals/{E(quote(p.run_id))}/decide">'
            f'<input type="hidden" name="csrf" value="{E(csrf)}"><input type="hidden" name="interrupt_id" value="{E(p.interrupt_id)}">'
            '<input type="hidden" name="next" value="/ui/approvals">'
            f'<label for="note-{E(p.run_id)}" class="small">Note (required to reject; kept as learning feedback — no personal data)</label>'
            f'<textarea id="note-{E(p.run_id)}" name="note" maxlength="2000"></textarea><p>'
            f'<button class="approve" name="decision" value="approve" type="submit"{" disabled" if p.expired else ""}>Approve</button>'
            '<button class="reject" name="decision" value="reject" type="submit">Reject</button>'
            f'<a class="small" href="/ui/journey?run={E(quote(p.run_id))}">see its journey</a></p></form></div></div></article>')
    hrows = "".join(
        f'<tr><td class="mono small">{E(T(h["ts"]))}</td><td><code>{E(h["run_id"])}</code></td>'
        f'<td>{badge(h["data"].get("decision"))}</td><td>{E(h["data"].get("approver"))}</td>'
        f'<td class="small">{E(h["data"].get("note", ""))}</td><td>{badge(str(h["data"].get("outcome")))}</td>'
        f'<td class="small muted">{E(h["data"].get("source", ""))}</td></tr>' for h in hist)
    return (head("Approval queue", "Nothing irreversible without a name on it.",
                 f"High-risk steps pause here. Approving resumes the run; rejecting ends it with your reason in the "
                 f"hash-chained feedback log. Requests auto-deny after {ttl_min} minutes.")
            + ("".join(cards) or '<div class="card inset"><h3>The queue is empty.</h3><p class="muted">Nothing is waiting '
               'for a decision.</p></div>')
            + '<div class="card"><h3>Recent decisions</h3><div class="scroll"><table><thead><tr><th>When</th><th>Run</th>'
            '<th>Decision</th><th>By</th><th>Note</th><th>Outcome</th><th>Via</th></tr></thead>'
            f'<tbody>{hrows or "<tr><td colspan=7 class=muted>None yet.</td></tr>"}</tbody></table></div></div>')


def audit_body(aud: dict) -> str:
    def seal(name):
        a = aud.get(name, {})
        if not a.get("exists"):
            return f'<div class="card inset"><h3><code>{E(name)}</code></h3><p class="muted">Not created yet.</p></div>'
        errs = "".join(f"<li>{E(e)}</li>" for e in a["errors"])
        return (f'<div class="card"><p class="kicker">{E(name)}</p>'
                + ('<p><span class="stamp ok">chain verified</span></p>' if a["ok"] else '<p><span class="stamp bad">chain broken</span></p>')
                + f'<p>{a["records"]} records · {a["legacy_unstamped"]} without a provenance stamp'
                  f'{" (written before this log was stamped; accepted only before the first stamped record)" if a["legacy_unstamped"] else ""}</p>'
                + (f'<ul class="bad small">{errs}</ul>' if errs else "") + "</div>")
    prov = "".join(f'<tr><td class="mono small">{E(p["stamp"])}</td><td class="num">{p["records"]}</td></tr>'
                   for p in aud.get("provenance", []))
    opts = "".join(f'<option value="{E(n)}">{E(n)}</option>' for n in aud.get("nodes", []))
    rows = "".join(
        f'<tr data-node="{E(r["node"])}" data-text="{E((str(r["run_id"]) + " " + str(r["node"]) + " " + str(r["detail"])).lower())}">'
        f'<td class="mono small">{E(T(r["ts"]))}</td><td><a href="/ui/journey?run={E(quote(str(r["run_id"])))}"><code>{E(r["run_id"])}</code></a></td>'
        f'<td><code>{E(r["node"])}</code></td><td>{badge(r["status"] or r["event"])}</td><td class="small">{E(r["detail"])}</td>'
        f'<td class="mono small muted" title="previous {E(r["prev"])}">{E(r["hash"][:10])}</td>'
        f'<td class="mono small">{E(r["policy_version"] or "—")}<br><span class="muted">{E(r["code"])}</span></td></tr>'
        for r in aud.get("rows", []))
    return (head("Audit trail", "Tamper-evident is not tamper-proof.",
                 "Every node writes a record whose hash covers the previous one, stamped with the code version, policy "
                 "hash, prompt, planner and model that produced it (ticket O-3). Verified on every page load.")
            + f'<div class="grid g2">{seal("audit.jsonl")}{seal("feedback.jsonl")}</div>'
            + ('<div class="card"><h3>Provenance stamps in use</h3><div class="scroll"><table><thead><tr><th>Policy version · '
               f'policy hash · code · planner/model</th><th class="num">Records</th></tr></thead><tbody>{prov}</tbody></table>'
               '</div><p class="small muted">A <code>-dirty</code> code stamp means the process ran from a working tree with '
               'uncommitted changes. Set <code>LOG_ON_GIT_SHA</code> in release images.</p></div>' if prov else "")
            + '<div class="card"><h3>Records</h3><div class="filters" role="search"><label for="audit-q" class="small">Filter</label>'
            '<input id="audit-q" type="search" placeholder="run id, node or text"><label for="audit-node" class="small">Node</label>'
            f'<select id="audit-node"><option value="">all</option>{opts}</select><span id="audit-count" class="small muted" '
            f'aria-live="polite">{len(aud.get("rows", []))} shown</span><a class="small" href="/ui/audit?limit=5000">load all</a></div><div class="scroll"><table><thead><tr><th>When</th>'
            '<th>Run</th><th>Node</th><th>Result</th><th>What happened</th><th>Hash</th><th>Stamp</th></tr></thead>'
            f'<tbody id="audit-rows">{rows}</tbody></table></div></div>')


def safety_body(ctx: dict) -> str:
    ev, red, bench = ctx["evals"], ctx["redteam"], ctx["bench"]
    lat = ev.get("latest") or {}
    hist = ev.get("history", [])
    gate = (f'<div class="card paper deckle"><p class="kicker" style="color:#7a3a14">Eval gate · {E(lat.get("file"))}</p>'
            f'<h2>{E(lat.get("cases"))} cases · pass rate {P(lat.get("pass_rate"), 0)} · {E(lat.get("pii_leaks"))} PII leaks</h2>'
            f'<p><span class="stamp" style="color:{"#2f5d1e" if lat.get("gate") == "PASSED" else "#8a1c0c"}">gate {E(lat.get("gate"))}</span></p>'
            + "".join(f'<span class="badge" style="color:#3b352e;margin:0 6px 6px 0">{E(k)} {v:.2f}</span>'
                      for k, v in (lat.get("metrics") or {}).items()) + "</div>") if lat else ""
    growth = hbars([(h["label"], h["cases"], C_ACC if i == len(hist) - 1 else C_MUTED) for i, h in enumerate(hist)],
                   title="Eval cases per phase", label_w=170, width=520)
    cats = hbars([(k, v["cases"], {"attack": C_BAD, "normal": C_OK, "edge": C_INFO}.get(k, C_MUTED))
                  for k, v in (lat.get("by_category") or {}).items()], title="Cases by category", label_w=170, width=520)
    rt = red.get("runs", [])
    rtsvg = stacked([(r["label"], [(r["vulnerable"] or 0, C_BAD, "vulnerable"), (r["bypass"] or 0, C_WARN, "contained bypass"),
                                   (r["defended"] or 0, C_OK, "defended")]) for r in rt], title="Red-team verdicts")
    before = A._rt(red, "step2_rule_before.json")
    tech = ""
    if before:
        rows = []
        for t, v in sorted(before["by_technique"].items(), key=lambda kv: -(kv[1].get("VULNERABLE", 0) * 100 + kv[1].get("BYPASS", 0))):
            tot = sum(v.values())
            rows.append((t, [(v.get("VULNERABLE", 0), C_BAD, "vulnerable"), (v.get("BYPASS", 0), C_WARN, "bypass"),
                             (v.get("DEFENDED", 0), C_OK, "defended")]) if tot else None)
        tech = ('<div class="card"><h3>Which disguises worked before the ML layer</h3><p class="small muted">Step-2 set, rule '
                f'planner, Phase-4 defences ({E(before["file"])}). Sorted by damage.</p>'
                + stacked([r for r in rows if r][:12], label_w=150, width=620, title="Verdicts by technique") + "</div>")
    legend = (f'<div class="legend"><span><i style="background:{C_BAD}"></i>vulnerable (attack reached an effect or '
              f'approval)</span><span><i style="background:{C_WARN}"></i>bypass, contained later</span><span><i style='
              f'"background:{C_OK}"></i>defended</span></div>')
    modes = {m["mode"]: m for m in bench.get("modes", [])}
    bench_html = ""
    if modes:
        rx, cb = modes.get("regex", {}), modes.get("combined", {})

        def row(label, a, b, fmt=lambda x: x):
            return f'<tr><td>{E(label)}</td><td class="num">{E(fmt(a))}</td><td class="num">{E(fmt(b))}</td></tr>'
        bench_html = (
            '<div class="card"><h3>Safety bench: regex vs regex + ML</h3><p class="small muted">docs/safety_bench.json · classifier '
            f'<code>{E(cb.get("classifier"))}</code></p><div class="scroll"><table><thead><tr><th>Measure</th><th class="num">'
            'regex only</th><th class="num">combined</th></tr></thead><tbody>'
            + row("PII recall", f'{rx.get("pii_found")}/{rx.get("pii_expected")}', f'{cb.get("pii_found")}/{cb.get("pii_expected")}')
            + row("Known attacks caught at input", f'{rx.get("inj_detected")}/{rx.get("inj_attacks")}', f'{cb.get("inj_detected")}/{cb.get("inj_attacks")}')
            + row("Held-out attack wordings caught", f'{rx.get("heldout_detected")}/{rx.get("heldout")}', f'{cb.get("heldout_detected")}/{cb.get("heldout")}')
            + row("Held-out poisoned documents quarantined", f'{rx.get("untrusted_quarantined")}/{rx.get("untrusted")}', f'{cb.get("untrusted_quarantined")}/{cb.get("untrusted")}')
            + row("Benign requests refused (false positives)", f'{rx.get("benign_fp")}/{rx.get("benign")}', f'{cb.get("benign_fp")}/{cb.get("benign")}')
            + row("Benign tool outputs quarantined", f'{rx.get("quarantine_fp")}/{rx.get("quarantine_benign")}', f'{cb.get("quarantine_fp")}/{cb.get("quarantine_benign")}')
            + row("Input check p50 / p95 (ms)", f'{rx.get("lat_input_p50")} / {rx.get("lat_input_p95")}', f'{cb.get("lat_input_p50")} / {cb.get("lat_input_p95")}')
            + row("One-off model load (ms)", rx.get("warmup_ms"), cb.get("warmup_ms"))
            + '</tbody></table></div></div>'
            '<div class="card"><h3>Held-out wordings that still pass the input check</h3><p class="small muted">Written after '
            'the rules and never used to tune them (combined mode). Single-customer values are still masked by the output '
            'redactor; no tool returns bulk PII.</p><ol class="small">'
            + "".join(f"<li>{E(x)}</li>" for x in cb.get("heldout_missed", [])) + "</ol></div>")
    return (head("Safety", "Defended against what we thought of.",
                 "The gate, the red team and the safety bench, with each headline paired against the point where it "
                 "stops being true.")
            + f'<div class="grid g2"><div>{gate}</div><div class="card"><h3>The suite grew; the gate held</h3>{growth}{cats}</div></div>'
            + f'<div class="card"><h3>Red team, before and after</h3>{rtsvg}{legend}<p class="small muted">{E(red.get("partial_note"))}</p></div>'
            + f'<div class="grid g2">{tech}<div>{bench_html}</div></div>'
            + "<h2>Claim vs breaking point</h2>" + "".join(tension(c) for c in ctx["claims"]))


def ops_body(ops: dict) -> str:
    b = ops["budgets"]
    if b.get("enabled"):
        rows = []
        for u in b["users"]:
            if "error" in u:
                rows.append(f'<tr><td>{E(u["user"])}</td><td colspan=3 class="bad">{E(u["error"])}</td></tr>')
                continue
            cells = []
            for k, lk in (("runs", "runs"), ("tokens", "tokens"), ("cost", "cost_usd")):
                used, lim = u["used"].get(k, 0) or 0, u["limits"].get(lk)
                frac = min(1.0, used / lim) if lim else 0
                cells.append(f'<td><div class="small mono">{_n(used)} / {_n(lim) if lim else "∞"}</div>'
                             f'<div class="meter{" hot" if frac >= .8 else ""}" role="meter" aria-valuenow="{used}" '
                             f'aria-valuemin="0" aria-valuemax="{lim or 0}" aria-label="{E(u["user"])} {k}">'
                             f'<span style="width:{frac * 100:.0f}%"></span></div></td>')
            rows.append(f'<tr><td><b>{E(u["user"])}</b></td>{"".join(cells)}</tr>')
        budget = (f'<div class="card"><h3>Daily budgets · {E(b.get("day"))}</h3><div class="scroll"><table><thead><tr><th>User</th>'
                  f'<th>Runs</th><th>Tokens</th><th>Cost (USD)</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
                  '<p class="small muted">From the quota store (runs/quota.sqlite or Postgres). Limits from '
                  '<code>LOG_ON_USER_DAILY_*</code> / <code>LOG_ON_USER_BUDGETS</code>.</p></div>')
    else:
        budget = '<div class="card inset"><h3>Daily budgets</h3><p class="muted">Quotas are not enabled.</p></div>'
    tel = ops["telemetry"]
    spans = [s for s in tel["by_span"] if s["span"].startswith(("node.", "tool.", "run."))][:14]
    lat = ("".join(f'<tr><td><code>{E(s["span"])}</code></td><td class="num">{s["count"]}</td><td class="num">{s["p50_ms"]}</td>'
                   f'<td class="num">{s["p95_ms"]}</td><td class="num">{s["max_ms"]}</td></tr>' for s in spans))
    latsvg = hbars([(s["span"], s["p95_ms"] or 0, C_ACC if s["span"].startswith("tool.") else C_INFO) for s in spans[:10]],
                   unit=" ms", title="p95 latency by span", label_w=200, width=560)
    telh = (f'<div class="card"><h3>Telemetry · {tel["spans"]} spans over {tel["runs"]} runs</h3>'
            f'<p class="small">{tel["tokens_total"]:,} tokens (mean {E(tel["tokens_mean"])} per run) · {tel["llm_calls"]} LLM '
            f'calls · est. ${tel["cost_total_usd"]:.4f}</p><div class="grid g2"><div>{latsvg}</div><div class="scroll"><table><thead><tr><th>Span</th><th class="num">n</th>'
            f'<th class="num">p50 ms</th><th class="num">p95 ms</th><th class="num">max ms</th></tr></thead><tbody>{lat}</tbody></table></div></div>'
            '<p class="small muted">From runs/traces.jsonl (OpenTelemetry spans written by the file exporter).</p></div>')
    sv = []
    for s in ops["serving"]:
        rows = "".join(f'<tr><td class="num">{lv["concurrency"]}</td><td class="num">{lv["req_per_s"]}</td>'
                       f'<td class="num">{lv["output_tok_per_s"]}</td><td class="num">{lv["latency_p50_ms"]}</td>'
                       f'<td class="num">{lv["latency_p95_ms"]}</td><td class="num">{lv["ttft_p50_ms"]}</td>'
                       f'<td class="num">{P(lv["valid_plan_rate"], 0)}</td></tr>' for lv in s["levels"])
        sv.append(f'<div class="card"><h3>{E(s["model"])}</h3><p class="small muted">{E(s["engine"])} on {E(s["cpus"])} CPUs · '
                  f'{E(s["date"])} · <code>{E(s["file"])}</code></p>'
                  + hbars([(f'c={lv["concurrency"]}', lv["output_tok_per_s"] or 0, C_OK) for lv in s["levels"]],
                          unit=" tok/s", title=f'{s["model"]} throughput', label_w=60, width=480)
                  + '<div class="scroll"><table><thead><tr><th class="num">conc.</th><th class="num">req/s</th><th class="num">tok/s</th>'
                  '<th class="num">p50 ms</th><th class="num">p95 ms</th><th class="num">TTFT ms</th><th class="num">valid plans</th>'
                  f'</tr></thead><tbody>{rows}</tbody></table></div></div>')
    ds = ops["dspy"]
    vrows = "".join(
        f'<tr><td><code>{E(v["id"])}</code><div class="small muted">{E(v["created"])}</div></td>'
        f'<td class="num">{E(v["scores"].get("baseline"))} → {E(v["scores"].get("optimized"))}</td>'
        f'<td>{badge("passed" if v["gate_passed"] else "refused")}</td>'
        f'<td class="small">{"<br>".join(E(f) for f in v["failures"])}</td></tr>' for v in ds["versions"])
    dsh = (f'<div class="card"><h3>Planner prompts the gate refused</h3><p class="small">Active version: '
           f'<code>{E(ds["active"] or "none (hand-written prompt)")}</code></p><div class="scroll"><table><thead><tr><th>Version</th>'
           f'<th class="num">dev score</th><th>Gate</th><th>Why</th></tr></thead><tbody>{vrows}</tbody></table></div>'
           '<p class="small muted">prompts/registry.json — DSPy candidates must pass the eval gate before activation.</p></div>')
    return (head("Operations", "What it costs to be careful.",
                 "Per-user budgets, where time goes inside a run, how the local model serves under load, and the "
                 "prompt versions the learning loop was not allowed to ship.")
            + f'<div class="grid g2">{budget}{dsh}</div>{telh}<h2>Serving benchmark</h2><div class="grid g2">{"".join(sv)}</div>')


def analysis_body(st: dict) -> str:
    secs = "".join(
        f'<section class="card" aria-labelledby="an-{i}"><h2 id="an-{i}">{E(s["title"])}</h2><div class="dialectic">'
        f'<div class="t"><span class="tag">Thesis</span>{E(s["thesis"])}</div>'
        f'<div class="a"><span class="tag">Antithesis</span>{E(s["antithesis"])}</div>'
        f'<div class="n"><span class="tag">Pragmatic next move</span>{E(s["next"])}</div></div></section>'
        for i, s in enumerate(st["sections"]))
    return (head("Analysis", "The state of LOG_ON.",
                 "Generated on each request from the same files as the other pages. The numbers are measured; the "
                 "framing is an argument, written so it can be argued with.")
            + f'<p class="pull">{E(st["verdict"])}</p>' + secs)


# ---------------------------------------------------------------- routes
def routes(service, who, csrf_for, docs: Path | None = None) -> list:
    runs_dir = Path(service.orch.audit.path).parent
    # evidence files; the container image does not ship docs/, so mount it and point LOG_ON_DOCS_DIR at it
    docs = Path(docs or os.getenv("LOG_ON_DOCS_DIR") or D.DOCS)

    def ctx() -> dict:
        rs = D.runs(runs_dir)
        ev, red, bench, aud = D.evals(docs), D.redteam(docs), D.safety_bench(docs), D.audit(runs_dir)
        ops = D.ops(runs_dir, service.orch, docs)
        pending = len(service.list_pending())
        return {"runs": rs, "evals": ev, "redteam": red, "bench": bench, "audit": aud, "ops": ops, "pending": pending,
                "demo": D.is_demo(runs_dir),
                "claims": A.claims(ev, red, bench, aud, ops["dspy"], ops["serving"], rs)}

    def guard(request: Request, api: bool):
        name, tok = who(request)
        if name:
            return name, tok, None
        if api:
            return None, None, JSONResponse({"error": "unauthorized"}, status_code=401)
        return None, None, RedirectResponse("/login?next=" + quote(request.url.path), status_code=303)

    def story(request):
        name, _, deny = guard(request, False)
        if deny:
            return deny
        c = ctx()
        return shell("Story", "story", story_body(c), name, c["demo"])

    def journey(request):
        name, _, deny = guard(request, False)
        if deny:
            return deny
        rs = D.runs(runs_dir)
        sel = request.query_params.get("run") or (rs[0]["run_id"] if rs else None)
        j = D.journey(runs_dir, sel) if sel else None
        if sel and j is None:
            return shell("Journey", "journey", journey_body(rs, None, None), name, D.is_demo(runs_dir),
                         flash="No run with that id.", error=True)
        return shell("Journey", "journey", journey_body(rs, j, sel), name, D.is_demo(runs_dir))

    def approvals(request):
        name, tok, deny = guard(request, False)
        if deny:
            return deny
        expired = service.expire_stale()
        flash = (f"{len(expired)} expired request(s) were denied automatically." if expired
                 else request.query_params.get("msg", ""))
        return shell("Approvals", "approvals", approvals_body(service.list_pending(), service.history(20), csrf_for(tok),
                                                              service.ttl_s // 60), name, D.is_demo(runs_dir), flash=flash)

    def audit(request):
        name, _, deny = guard(request, False)
        if deny:
            return deny
        return shell("Audit", "audit", audit_body(D.audit(runs_dir, limit=_limit(request))), name, D.is_demo(runs_dir))

    def safety(request):
        name, _, deny = guard(request, False)
        if deny:
            return deny
        c = ctx()
        return shell("Safety", "safety", safety_body(c), name, c["demo"])

    def ops(request):
        name, _, deny = guard(request, False)
        if deny:
            return deny
        return shell("Ops", "ops", ops_body(D.ops(runs_dir, service.orch, docs)), name, D.is_demo(runs_dir))

    def analysis(request):
        name, _, deny = guard(request, False)
        if deny:
            return deny
        c = ctx()
        st = A.state_of(c["evals"], c["redteam"], c["bench"], c["audit"], c["ops"], c["runs"], c["pending"], c["demo"])
        return shell("Analysis", "analysis", analysis_body(st), name, c["demo"])

    # ---- JSON (same data the pages render)
    def api(fn):
        async def handler(request):
            name, _, deny = guard(request, True)
            if deny:
                return deny
            out = await run_in_threadpool(fn, request)
            if out is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            out.setdefault("demo", D.is_demo(runs_dir))
            return JSONResponse(out)
        return handler

    def j_overview(request):
        c = ctx()
        st = {}
        for r in c["runs"]:
            st[r["status"]] = st.get(r["status"], 0) + 1
        a = c["audit"].get("audit.jsonl", {})
        return {"runs": len(c["runs"]), "statuses": st, "pending": c["pending"], "eval": c["evals"].get("latest"),
                "audit_chain": {k: v for k, v in a.items() if k != "versions"}, "claims": c["claims"][:2]}

    def j_journey(request):
        rid = request.path_params.get("run_id")
        if rid is None:
            return {"runs": D.runs(runs_dir)}
        return D.journey(runs_dir, rid)

    def j_approvals(request):
        return {"pending": [p.to_dict() for p in service.list_pending()], "history": service.history(20),
                "ttl_s": service.ttl_s}

    def j_audit(request):
        return D.audit(runs_dir, limit=_limit(request))

    def j_safety(request):
        c = ctx()
        return {"evals": c["evals"], "redteam": c["redteam"], "bench": c["bench"], "claims": c["claims"]}

    def j_ops(request):
        return D.ops(runs_dir, service.orch, docs)

    def j_analysis(request):
        c = ctx()
        return A.state_of(c["evals"], c["redteam"], c["bench"], c["audit"], c["ops"], c["runs"], c["pending"], c["demo"])

    return [
        Route("/ui", story), Route("/ui/journey", journey), Route("/ui/approvals", approvals), Route("/ui/audit", audit),
        Route("/ui/safety", safety), Route("/ui/ops", ops), Route("/ui/analysis", analysis),
        Route("/api/ui/overview", api(j_overview)), Route("/api/ui/runs", api(j_journey)),
        Route("/api/ui/journey/{run_id}", api(j_journey)), Route("/api/ui/approvals", api(j_approvals)),
        Route("/api/ui/audit", api(j_audit)), Route("/api/ui/safety", api(j_safety)), Route("/api/ui/ops", api(j_ops)),
        Route("/api/ui/analysis", api(j_analysis)),
        Mount("/static", app=StaticFiles(directory=str(STATIC)), name="static"),
    ]


def _limit(request, default: int = 100) -> int:
    try:
        return max(1, min(5000, int(request.query_params.get("limit", default))))
    except ValueError:
        return default


SAFE_NEXT = ("/ui",)


def safe_next(nxt: str | None) -> str | None:
    """Only same-site dashboard paths are allowed as redirect targets (no open redirect)."""
    if not nxt or not nxt.startswith(SAFE_NEXT) or nxt.startswith("//") or "\\" in nxt or "://" in nxt:
        return None
    path = nxt.split("?", 1)[0]
    return path if all(c.isalnum() or c in "/_-" for c in path) else None
