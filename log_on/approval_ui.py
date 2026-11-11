"""Local approval web UI (post-Phase-4 roadmap step 1): review and decide paused actions in a browser.

    export LOG_ON_APPROVER_TOKENS="ada:$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
    python -m log_on.approval_ui                     # http://127.0.0.1:8765  (same runs/ as the CLI)
    python -m log_on.approval_ui --gen-token ada     # prints a fresh export line for an approver

Server-rendered HTML on Starlette + uvicorn (no JavaScript, no frontend build). It shows every run paused at the
`approval` interrupt with a PII-redacted preview (each plan step, its risk tier, what it will do, where data goes,
the policy and permission decisions), and lets a *named* approver approve or reject with a note. Decisions go
through `log_on.approvals.ApprovalService` (expiry, stale-form check, audit log, feedback log), exactly like the CLI.

Auth: LOG_ON_APPROVER_TOKENS="name:token[,name:token...]" (tokens >= 16 chars). The approver's identity comes from
the token, not from a form field. Browser: sign in once, the token is kept in an HttpOnly, SameSite=Strict cookie;
every form also carries a CSRF value derived from the token. API: `Authorization: Bearer <token>`.
Binds to 127.0.0.1 by default. The server refuses to start without tokens. No token is ever written to disk or logs.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import secrets
from datetime import datetime, timezone

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .approvals import ApprovalError, ApprovalService

COOKIE = "log_on_approver"
MIN_TOKEN_LEN = 16
E = html.escape


def load_tokens(raw: str | None = None) -> dict[str, str]:
    """token -> approver name, from LOG_ON_APPROVER_TOKENS ("name:token,name:token")."""
    raw = os.getenv("LOG_ON_APPROVER_TOKENS", "") if raw is None else raw
    out: dict[str, str] = {}
    for part in raw.split(","):
        name, sep, tok = part.strip().partition(":")
        name, tok = name.strip(), tok.strip()
        if not sep or not name or not tok:
            continue
        if len(tok) < MIN_TOKEN_LEN:
            raise ValueError(f"approver token for {name!r} is shorter than {MIN_TOKEN_LEN} characters")
        out[tok] = name
    return out


def _fmt_ts(d: datetime | str) -> str:
    if isinstance(d, str):
        d = datetime.fromisoformat(d)
    return d.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _left(s: int) -> str:
    if s <= 0:
        return "expired"
    h, m = divmod(s // 60, 60)
    return f"{h} h {m} min left" if h else f"{m} min left"


CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f7f9;color:#1d2330}
header{background:#1d2330;color:#fff;padding:12px 24px;display:flex;justify-content:space-between;align-items:center}
header a{color:#cfd6e4;margin-left:16px;text-decoration:none} main{max-width:980px;margin:24px auto;padding:0 16px}
.card{background:#fff;border:1px solid #dde2ea;border-radius:8px;padding:16px 20px;margin-bottom:16px}
table{border-collapse:collapse;width:100%} th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eef0f4;
vertical-align:top;font-size:14px} code{background:#f0f2f6;padding:1px 4px;border-radius:4px;word-break:break-all}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.high{background:#fde2e1;color:#9b1c1c}.medium{background:#fff1d6;color:#8a5300}.low{background:#e3f4e6;color:#1e6b34}
.ok{color:#1e6b34}.bad{color:#9b1c1c;font-weight:600}.muted{color:#6b7385;font-size:13px}
textarea{width:100%;min-height:70px;font:inherit} button{font:inherit;padding:8px 18px;border-radius:6px;border:0;
cursor:pointer;margin-right:8px} .approve{background:#1e6b34;color:#fff}.reject{background:#9b1c1c;color:#fff}
.flash{padding:10px 14px;border-radius:6px;margin-bottom:16px;background:#e8eefc;border:1px solid #c9d6f5}
.err{background:#fde2e1;border-color:#f5c2c0}
"""


def page(title: str, body: str, who: str | None = None, flash: str = "", error: bool = False) -> HTMLResponse:
    nav = (f'<span>Signed in as <b>{E(who)}</b><a href="/">Pending</a><a href="/history">History</a>'
           f'<a href="/logout">Sign out</a></span>') if who else ""
    fl = f'<div class="flash{" err" if error else ""}">{E(flash)}</div>' if flash else ""
    doc = (f'<!doctype html><html><head><meta charset="utf-8"><title>{E(title)} · LOG_ON approvals</title>'
           f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style></head>'
           f'<body><header><b>LOG_ON · approvals</b>{nav}</header><main>{fl}{body}</main></body></html>')
    return HTMLResponse(doc)


def create_app(service: ApprovalService, tokens: dict[str, str] | None = None, sweep_expired: bool = True):
    tokens = load_tokens() if tokens is None else tokens
    if not tokens:
        raise RuntimeError("No approver tokens configured. Set LOG_ON_APPROVER_TOKENS=\"name:token\" "
                           "(python -m log_on.approval_ui --gen-token NAME prints one).")
    for t in tokens:
        if len(t) < MIN_TOKEN_LEN:
            raise ValueError(f"approver tokens must be at least {MIN_TOKEN_LEN} characters")

    def who(request: Request) -> tuple[str | None, str | None]:
        auth = request.headers.get("authorization", "")
        cand = auth[7:].strip() if auth.lower().startswith("bearer ") else request.cookies.get(COOKIE, "")
        for tok, name in tokens.items():
            if cand and hmac.compare_digest(cand.encode(), tok.encode()):
                return name, tok
        return None, None

    def csrf_for(tok: str) -> str:
        return hmac.new(tok.encode(), b"log_on-approval-csrf", hashlib.sha256).hexdigest()[:32]

    def check_csrf(request: Request, form, tok: str) -> bool:
        if request.headers.get("authorization", "").lower().startswith("bearer "):
            return True  # API clients do not use the cookie
        return hmac.compare_digest(str(form.get("csrf", "")), csrf_for(tok))

    # ---------------- HTML ----------------
    async def login(request: Request) -> Response:
        if request.method == "POST":
            form = await request.form()
            tok = str(form.get("token", "")).strip()
            name = next((n for t, n in tokens.items() if tok and hmac.compare_digest(tok.encode(), t.encode())), None)
            if not name:
                return page("Sign in", _login_form(), flash="Unknown approver token.", error=True)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, tok, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                            max_age=12 * 3600)
            return resp
        return page("Sign in", _login_form())

    async def logout(request: Request) -> Response:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    def index(request: Request) -> Response:
        name, _ = who(request)
        if not name:
            return RedirectResponse("/login", status_code=303)
        expired = service.expire_stale() if sweep_expired else []
        items = service.list_pending()
        rows = "".join(
            f'<tr><td><a href="/approvals/{E(p.run_id)}"><code>{E(p.run_id)}</code></a></td><td>{E(p.goal)}</td>'
            f'<td>{", ".join(E(s["tool"]) for s in p.steps if s["needs_approval"])}</td>'
            f'<td>{E(_fmt_ts(p.requested_at))}</td><td class="{"bad" if p.expired else ""}">{_left(p.seconds_left)}</td></tr>'
            for p in items)
        body = (f'<div class="card"><h2>Pending approvals ({len(items)})</h2>'
                + (f'<table><tr><th>Run</th><th>Request</th><th>Needs approval</th><th>Requested</th><th>Expiry</th></tr>'
                   f'{rows}</table>' if items else '<p class="muted">Nothing is waiting for approval.</p>')
                + f'<p class="muted">Requests expire after {service.ttl_s // 60} min and are then denied automatically.</p></div>')
        flash = f"{len(expired)} expired request(s) were denied automatically." if expired else request.query_params.get("msg", "")
        return page("Pending", body, who=name, flash=flash)

    def detail(request: Request) -> Response:
        name, tok = who(request)
        if not name:
            return RedirectResponse("/login", status_code=303)
        p = service.get(request.path_params["run_id"])
        if p is None:
            return page("Not found", '<div class="card">No pending approval for this run (already decided?). '
                        '<a href="/">Back</a></div>', who=name, error=True, flash="Not pending")
        return page(f"Run {p.run_id}", _detail_html(p, csrf_for(tok)), who=name,
                    flash=request.query_params.get("err", ""), error=bool(request.query_params.get("err")))

    async def decide(request: Request) -> Response:
        name, tok = who(request)
        if not name:
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        run_id = request.path_params["run_id"]
        if not check_csrf(request, form, tok):
            return page("Forbidden", '<div class="card">Invalid form token. Reload the page.</div>', who=name,
                        error=True, flash="CSRF check failed")
        approve = form.get("decision") == "approve"
        try:
            # the graph's MCP client uses asyncio.run(), so it must not run on the server's event loop
            res = await run_in_threadpool(service.decide, run_id, approve, name, str(form.get("note", "")),
                                          interrupt_id=str(form.get("interrupt_id", "")) or None, source="web")
        except ApprovalError as e:
            if e.code in (404, 410):
                return RedirectResponse(f"/?msg={_q(str(e))}", status_code=303)
            return RedirectResponse(f"/approvals/{run_id}?err={_q(str(e))}", status_code=303)
        msg = f"Run {run_id}: {'approved' if approve else 'rejected'} by {name}; status now {res.get('status')}."
        return RedirectResponse(f"/?msg={_q(msg)}", status_code=303)

    def history(request: Request) -> Response:
        name, _ = who(request)
        if not name:
            return RedirectResponse("/login", status_code=303)
        rows = "".join(
            f'<tr><td>{E(_fmt_ts(r["ts"]))}</td><td><code>{E(r["run_id"])}</code></td>'
            f'<td class="{"ok" if r["data"]["decision"] == "approve" else "bad"}">{E(r["data"]["decision"])}</td>'
            f'<td>{E(r["data"]["approver"])}</td><td>{E(r["data"].get("note", ""))}</td>'
            f'<td>{E(str(r["data"].get("outcome")))}</td><td>{E(r["data"].get("source", ""))}</td></tr>'
            for r in service.history())
        body = ('<div class="card"><h2>Decision history</h2><p class="muted">From the hash-chained feedback log '
                f'(<code>{E(str(service.feedback.path.name))}</code>, chain {"ok" if service.feedback.verify() else "BROKEN"}). '
                'Notes and reject reasons are kept as learning data.</p>'
                '<table><tr><th>When</th><th>Run</th><th>Decision</th><th>By</th><th>Note</th><th>Outcome</th><th>Via</th></tr>'
                f'{rows}</table></div>')
        return page("History", body, who=name)

    # ---------------- JSON API ----------------
    def api_list(request: Request) -> Response:
        name, _ = who(request)
        if not name:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        expired = service.expire_stale() if sweep_expired else []
        return JSONResponse({"approver": name, "expired_now": expired,
                             "pending": [p.to_dict() for p in service.list_pending()]})

    async def api_decide(request: Request) -> Response:
        name, _ = who(request)
        if not name:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "JSON body required"}, status_code=400)
        dec = str(body.get("decision", "")).lower()
        if dec not in ("approve", "reject"):
            return JSONResponse({"error": "decision must be 'approve' or 'reject'"}, status_code=400)
        try:
            res = await run_in_threadpool(service.decide, request.path_params["run_id"], dec == "approve", name,
                                          str(body.get("note", "")),
                                          interrupt_id=body.get("interrupt_id"), source="api")
        except ApprovalError as e:
            return JSONResponse({"error": str(e)}, status_code=e.code)
        return JSONResponse({"run_id": res["run_id"], "status": res.get("status"), "approver": name,
                             "final_output": res.get("final_output")})

    def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[
        Route("/", index), Route("/login", login, methods=["GET", "POST"]), Route("/logout", logout),
        Route("/approvals/{run_id}", detail), Route("/approvals/{run_id}/decide", decide, methods=["POST"]),
        Route("/history", history), Route("/api/approvals", api_list),
        Route("/api/approvals/{run_id}/decide", api_decide, methods=["POST"]), Route("/healthz", healthz)])

    return SecurityHeaders(app)


SECURITY_HEADERS = [
    (b"content-security-policy", b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"),
    (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer"), (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff")]


class SecurityHeaders:
    """Pure ASGI wrapper adding security headers to every response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + SECURITY_HEADERS
            await send(message)
        await self.app(scope, receive, send_wrapper)



def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(s[:300])


def _login_form() -> str:
    return ('<div class="card"><h2>Sign in</h2><form method="post" action="/login">'
            '<p>Approver token (from <code>LOG_ON_APPROVER_TOKENS</code>)</p>'
            '<input type="password" name="token" autocomplete="off" style="width:100%;padding:6px" required>'
            '<p><button class="approve" type="submit">Sign in</button></p></form></div>')


def _detail_html(p, csrf: str) -> str:
    steps = ""
    for s in p.steps:
        args = "".join(f"<div><b>{E(k)}</b>: <code>{E(str(v))}</code></div>" for k, v in s["args"].items())
        dest = ""
        if "destination" in s:
            dest = (f'<div>Destination: <code>{E(s["destination"])}</code> '
                    + ('<span class="ok">trusted domain</span>' if s["destination_trusted"]
                       else '<span class="bad">outside your trusted domains</span>') + "</div>")
        steps += (f'<tr><td>{s["index"]}</td><td><code>{E(s["tool"])}</code><br>'
                  f'<span class="tag {E(s["risk"])}">{E(s["risk"])}</span>'
                  f'{" <b>needs approval</b>" if s["needs_approval"] else ""}</td>'
                  f'<td>{E(s["effect"])}{dest}</td><td>{args}</td></tr>')
    pol = p.policy
    policy = ("passed" if pol.get("allowed") else "flagged") + (
        f' (rules: {E(", ".join(pol["rule_ids"]))})' if pol.get("rule_ids") else "") + (
        " — matched after normalising obfuscated text" if pol.get("normalized") else "")
    warns = "".join(f'<li class="bad">{E(w)}</li>' for w in p.memory_warnings)
    expired = p.expired
    return (
        f'<div class="card"><h2>Run <code>{E(p.run_id)}</code></h2>'
        f'<p><b>Request</b> (from user <code>{E(p.user)}</code>, PII redacted): {E(p.goal)}</p>'
        f'<p class="muted">Requested {E(_fmt_ts(p.requested_at))} · expires {E(_fmt_ts(p.expires_at))} '
        f'(<span class="{"bad" if expired else ""}">{_left(p.seconds_left)}</span>) · planner <code>{E(p.planner)}</code>'
        f'{f" · attempt {p.attempt}" if p.attempt else ""}</p></div>'
        f'<div class="card"><h3>What will happen if you approve</h3><table><tr><th>#</th><th>Tool</th><th>Effect</th>'
        f'<th>Arguments (redacted)</th></tr>{steps}</table>'
        f'<p class="muted">Steps run in order. Data passed between steps is scrubbed of personal data before it leaves.</p></div>'
        f'<div class="card"><h3>Why it is paused</h3><ul>'
        f'<li>Input policy: {policy}</li>'
        f'<li>Permission: steps {E(str(p.permission["approval_steps"]))} need approval — {E(p.permission["reason"])}</li>'
        f'<li>Checks already passed: {E(", ".join(p.permission["checks_passed"]))}</li>{warns}</ul></div>'
        + ('<div class="card bad">This request has expired. Submitting will deny it.</div>' if expired else "")
        + f'<div class="card"><h3>Decision</h3><form method="post" action="/approvals/{E(p.run_id)}/decide">'
        f'<input type="hidden" name="csrf" value="{E(csrf)}"><input type="hidden" name="interrupt_id" value="{E(p.interrupt_id)}">'
        f'<p>Note (required to reject; kept as feedback for learning — do not paste personal data):</p>'
        f'<textarea name="note" maxlength="2000"></textarea><p>'
        f'<button class="approve" name="decision" value="approve" type="submit"{" disabled" if expired else ""}>Approve</button>'
        f'<button class="reject" name="decision" value="reject" type="submit">Reject</button></p></form></div>')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m log_on.approval_ui")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--phase1", action="store_true", help="match a CLI started with --phase1")
    ap.add_argument("--no-loop", action="store_true", help="match a CLI started with --no-loop")
    ap.add_argument("--gen-token", metavar="NAME", help="print an export line with a fresh token for NAME and exit")
    a = ap.parse_args(argv)
    if a.gen_token:
        print(f'export LOG_ON_APPROVER_TOKENS="{a.gen_token}:{secrets.token_urlsafe(24)}"')
        return 0
    from .__main__ import build_orchestrator
    svc = ApprovalService(build_orchestrator(a.runs_dir, a.phase1, a.no_loop))
    app = create_app(svc)
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: binding beyond localhost. Put it behind HTTPS; tokens travel in a cookie/header.")
    import uvicorn
    print(f"LOG_ON approvals on http://{a.host}:{a.port}  (approvers: {', '.join(sorted(set(load_tokens().values())))})", flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
