"""CORE v5.0 — Step 0 | MCP Server + Telegram Bot + Queue Poller
Owner: REINVAGNAR
Step 0 scope: MCP fully working, Telegram queues tasks, NO training, NO agent pipeline.
Fix (2026-03-11): Added Streamable HTTP transport + proper MCP JSON-RPC protocol
so mcp-remote (latest) can connect via both http-first and sse-only strategies.
Fix (2026-03-11b): get_system_counts now uses service key so RLS doesn't truncate counts.
Fix (2026-03-11c): Tool audit fixes — get_mistakes/add_knowledge use svc key, log_mistake
  param names corrected (mistake+correction → context+what_failed+fix), sb_query exposed
  correctly, read_file default repo clarified in description.
"""
import asyncio
import base64
import hashlib
import json
import os
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

# ── Env vars ─────────────────────────────────────────────────────────────────
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
GROQ_MODEL     = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FAST      = os.environ.get("GROQ_MODEL_FAST", "llama-3.1-8b-instant")
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_SVC   = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_ANON  = os.environ["SUPABASE_ANON_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
GITHUB_PAT     = os.environ["GITHUB_PAT"]
GITHUB_REPO    = os.environ.get("GITHUB_USERNAME", "pockiesaints7") + "/core-agi"
MCP_SECRET     = os.environ["MCP_SECRET"]
PORT           = int(os.environ.get("PORT", 8080))
SESSION_TTL_H  = 8

MCP_PROTOCOL_VERSION = "2024-11-05"

# ── Rate limiter ──────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self):
        self.calls = defaultdict(list)
        try:
            self.c = json.load(open("resource_ceilings.json"))
        except:
            self.c = {
                "groq_calls_per_hour": 200,
                "supabase_writes_per_hour": 500,
                "github_pushes_per_hour": 20,
                "telegram_messages_per_hour": 30,
                "mcp_tool_calls_per_minute": 30,
            }

    def _ok(self, key, window, limit):
        now = time.time()
        self.calls[key] = [t for t in self.calls[key] if now - t < window]
        if len(self.calls[key]) >= limit:
            return False
        self.calls[key].append(now)
        return True

    def tg(self):       return self._ok("tg",  3600, self.c.get("telegram_messages_per_hour", 30))
    def gh(self):       return self._ok("gh",  3600, self.c.get("github_pushes_per_hour", 20))
    def sbw(self):      return self._ok("sbw", 3600, self.c.get("supabase_writes_per_hour", 500))
    def mcp(self, sid): return self._ok(f"mcp:{sid}", 60, self.c.get("mcp_tool_calls_per_minute", 30))

L = RateLimiter()

# ── Supabase ──────────────────────────────────────────────────────────────────
def _sbh(svc=False):
    k = SUPABASE_SVC if svc else SUPABASE_ANON
    return {"apikey": k, "Authorization": f"Bearer {k}",
            "Content-Type": "application/json", "Prefer": "return=minimal"}

def _sbh_count_svc():
    """Use service key for accurate counts — bypasses RLS."""
    return {
        "apikey": SUPABASE_SVC,
        "Authorization": f"Bearer {SUPABASE_SVC}",
        "Prefer": "count=exact",
    }

def sb_get(t, qs="", svc=False):
    """Query Supabase. Use svc=True to bypass RLS."""
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/{t}?{qs}", headers=_sbh(svc), timeout=15)
    r.raise_for_status()
    return r.json()

def sb_post(t, d):
    if not L.sbw(): return False
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/{t}", headers=_sbh(True), json=d, timeout=15)
    if not r.is_success:
        print(f"[SB POST] {t} failed: {r.status_code} {r.text[:200]}")
    return r.is_success

def sb_patch(t, m, d):
    if not L.sbw(): return False
    return httpx.patch(f"{SUPABASE_URL}/rest/v1/{t}?{m}", headers=_sbh(True), json=d, timeout=15).is_success

# ── Telegram ──────────────────────────────────────────────────────────────────
def notify(msg, cid=None):
    if not L.tg(): return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": cid or TELEGRAM_CHAT, "text": msg[:4000], "parse_mode": "Markdown"},
            timeout=10,
        )
        if not r.is_success:
            print(f"[TG] failed: {r.status_code} {r.text[:100]}")
            return False
        return True
    except Exception as e:
        print(f"[TG] {e}")
        return False

def set_webhook():
    d = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not d: return
    httpx.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
        data={"url": f"https://{d}/webhook"},
    )
    print("[CORE] Webhook set")

# ── GitHub ────────────────────────────────────────────────────────────────────
def _ghh(): return {"Authorization": f"Bearer {GITHUB_PAT}", "Accept": "application/vnd.github.v3+json"}

def gh_read(path, repo=None):
    r = httpx.get(f"https://api.github.com/repos/{repo or GITHUB_REPO}/contents/{path}", headers=_ghh(), timeout=15)
    r.raise_for_status()
    return base64.b64decode(r.json()["content"]).decode()

def gh_write(path, content, msg, repo=None):
    if not L.gh(): return False
    repo = repo or GITHUB_REPO
    h = _ghh()
    sha = None
    try:
        sha = httpx.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers=h, timeout=10).json().get("sha")
    except: pass
    p = {"message": msg, "content": base64.b64encode(content.encode()).decode()}
    if sha: p["sha"] = sha
    return httpx.put(f"https://api.github.com/repos/{repo}/contents/{path}", headers=h, json=p, timeout=20).is_success

# ── Supabase helpers ──────────────────────────────────────────────────────────
def get_latest_session():
    d = sb_get("sessions", "select=summary,actions,created_at&order=created_at.desc&limit=1")
    return d[0] if d else {}

def get_system_counts():
    """Use service key + Prefer: count=exact to get accurate row counts, bypassing RLS."""
    counts = {}
    for t in ["knowledge_base", "mistakes", "sessions", "task_queue"]:
        try:
            r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/{t}?select=id&limit=1",
                headers=_sbh_count_svc(),
                timeout=10,
            )
            cr = r.headers.get("content-range", "*/0")
            counts[t] = int(cr.split("/")[-1]) if "/" in cr else 0
        except:
            counts[t] = -1
    return counts

# ── MCP session management ────────────────────────────────────────────────────
_sessions: dict = {}

def mcp_new(ip: str) -> str:
    tok = hashlib.sha256(f"{MCP_SECRET}{ip}{time.time()}".encode()).hexdigest()[:32]
    _sessions[tok] = {
        "ip": ip,
        "expires": (datetime.utcnow() + timedelta(hours=SESSION_TTL_H)).isoformat(),
        "calls": 0,
    }
    now = datetime.utcnow()
    expired = [k for k, v in _sessions.items() if datetime.fromisoformat(v["expires"]) < now]
    for k in expired: del _sessions[k]
    return tok

def mcp_ok(tok: str) -> bool:
    if tok not in _sessions: return False
    if datetime.utcnow() > datetime.fromisoformat(_sessions[tok]["expires"]):
        del _sessions[tok]
        return False
    _sessions[tok]["calls"] += 1
    return True

# ── MCP tool implementations ──────────────────────────────────────────────────
def t_state():
    session = get_latest_session()
    counts  = get_system_counts()
    pending = sb_get("task_queue", "select=id,task,status&status=eq.pending&limit=5")
    return {
        "last_session":    session.get("summary", "No sessions yet."),
        "last_actions":    session.get("actions", []),
        "last_session_ts": session.get("created_at", ""),
        "counts":          counts,
        "pending_tasks":   pending,
        "note":            "Training starts Step 3.",
    }

def t_health():
    h = {"ts": datetime.utcnow().isoformat(), "components": {}}
    checks = [
        ("supabase", lambda: sb_get("sessions", "select=id&limit=1")),
        ("groq",     lambda: httpx.get("https://api.groq.com/openai/v1/models",
                             headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=5).raise_for_status()),
        ("telegram", lambda: httpx.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=5).raise_for_status()),
        ("github",   lambda: gh_read("README.md")),
    ]
    for name, fn in checks:
        try: fn(); h["components"][name] = "ok"
        except Exception as e: h["components"][name] = f"error:{e}"
    h["overall"] = "ok" if all(v == "ok" for v in h["components"].values()) else "degraded"
    return h

def t_constitution():
    try:
        with open("constitution.txt") as f: txt = f.read()
    except:
        txt = gh_read("constitution.txt")
    return {"constitution": txt, "immutable": True}

def t_search_kb(query="", domain="", limit=10):
    qs = f"select=domain,topic,content,confidence&limit={limit}"
    if domain: qs += f"&domain=eq.{domain}"
    if query:  qs += f"&content=ilike.*{query.split()[0]}*"
    return sb_get("knowledge_base", qs)

def t_get_mistakes(domain="", limit=10):
    """FIX: use svc=True to bypass RLS on mistakes table."""
    try:
        lim = int(limit) if limit else 10
    except:
        lim = 10
    qs = f"select=domain,context,what_failed,correct_approach&order=created_at.desc&limit={lim}"
    if domain and domain not in ("all", ""):
        qs += f"&domain=eq.{domain}"
    return sb_get("mistakes", qs, svc=True)

def t_update_state(key, value, reason):
    ok = sb_post("sessions", {
        "summary": f"[state_update] {key}: {str(value)[:200]}",
        "actions": [f"{key}={str(value)[:100]} — {reason}"],
        "interface": "mcp",
    })
    return {"ok": ok, "key": key}

def t_add_knowledge(domain, topic, content, tags="", confidence="medium"):
    """FIX: tags sent as list (array), added error logging."""
    tags_list = [t.strip() for t in tags.split(",")] if tags else []
    ok = sb_post("knowledge_base", {
        "domain": domain,
        "topic": topic,
        "content": content,
        "confidence": confidence,
        "tags": tags_list,
        "source": "mcp_session",
    })
    return {"ok": ok, "topic": topic}

def t_log_mistake(context, what_failed, fix, domain="general"):
    """FIX: param names are context/what_failed/fix — matches MCP schema."""
    ok = sb_post("mistakes", {
        "domain": domain,
        "context": context,
        "what_failed": what_failed,
        "correct_approach": fix,
    })
    return {"ok": ok}

def t_read_file(path, repo=""):
    """Read file from GitHub. repo defaults to pockiesaints7/core-agi if not provided."""
    try: return {"ok": True, "content": gh_read(path, repo or GITHUB_REPO)[:5000]}
    except Exception as e: return {"ok": False, "error": str(e)}

def t_write_file(path, content, message, repo=""):
    ok = gh_write(path, content, message, repo or GITHUB_REPO)
    if ok: notify(f"MCP write: `{path}`")
    return {"ok": ok, "path": path}

def t_notify(message, level="info"):
    icons = {"info": "\u2139\ufe0f", "warn": "\u26a0\ufe0f", "alert": "\U0001f6a8", "ok": "\u2705"}
    return {"ok": notify(f"{icons.get(level, '»')} CORE\n{message}")}

def t_sb_query(table, filters="", limit=20):
    """FIX: param renamed query_string→filters for clarity, svc=True to bypass RLS."""
    try:
        lim = int(limit) if limit else 20
    except:
        lim = 20
    qs = f"{filters}&limit={lim}" if filters else f"limit={lim}"
    return sb_get(table, qs, svc=True)

def t_sb_insert(table, data):
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception as e:
            return {"ok": False, "error": f"data must be valid JSON: {e}"}
    return {"ok": sb_post(table, data), "table": table}

def t_training_status():
    return {"status": "Step 3 — not started", "note": "Training loop added in Step 3."}

# ── Tool registry ─────────────────────────────────────────────────────────────
TOOLS = {
    "get_state":           {"fn": t_state,           "perm": "READ",    "args": [],
                            "desc": "Get current CORE state: last session, counts, pending tasks"},
    "get_system_health":   {"fn": t_health,          "perm": "READ",    "args": [],
                            "desc": "Check health of all components: Supabase, Groq, Telegram, GitHub"},
    "get_constitution":    {"fn": t_constitution,    "perm": "READ",    "args": [],
                            "desc": "Get CORE immutable constitution"},
    "get_training_status": {"fn": t_training_status, "perm": "READ",    "args": [],
                            "desc": "Get training loop status"},
    "search_kb":           {"fn": t_search_kb,       "perm": "READ",
                            "args": ["query", "domain", "limit"],
                            "desc": "Search knowledge base"},
    "get_mistakes":        {"fn": t_get_mistakes,    "perm": "READ",
                            "args": ["domain", "limit"],
                            "desc": "Get recorded mistakes. domain=optional filter, limit=number (default 10)"},
    "read_file":           {"fn": t_read_file,       "perm": "READ",
                            "args": ["path", "repo"],
                            "desc": "Read file from GitHub repo. repo defaults to pockiesaints7/core-agi"},
    "sb_query":            {"fn": t_sb_query,        "perm": "READ",
                            "args": ["table", "filters", "limit"],
                            "desc": "Query Supabase table. table=table name, filters=optional querystring, limit=number"},
    "update_state":        {"fn": t_update_state,    "perm": "WRITE",
                            "args": ["key", "value", "reason"],
                            "desc": "Write state update to sessions table"},
    "add_knowledge":       {"fn": t_add_knowledge,   "perm": "WRITE",
                            "args": ["domain", "topic", "content", "tags", "confidence"],
                            "desc": "Add entry to knowledge base. tags=comma-separated string"},
    "log_mistake":         {"fn": t_log_mistake,     "perm": "WRITE",
                            "args": ["context", "what_failed", "fix", "domain"],
                            "desc": "Log a mistake for learning. context=situation, what_failed=what went wrong, fix=correct approach"},
    "notify_owner":        {"fn": t_notify,          "perm": "WRITE",
                            "args": ["message", "level"],
                            "desc": "Send Telegram notification to REINVAGNAR. level=info/warn/alert/ok"},
    "sb_insert":           {"fn": t_sb_insert,       "perm": "WRITE",
                            "args": ["table", "data"],
                            "desc": "Insert row into Supabase table. data=JSON string"},
    "write_file":          {"fn": t_write_file,      "perm": "EXECUTE",
                            "args": ["path", "content", "message", "repo"],
                            "desc": "Write file to GitHub repo. repo defaults to pockiesaints7/core-agi"},
}

# ── MCP JSON-RPC handler ──────────────────────────────────────────────────────
def _mcp_tool_schema(name, tool):
    """Build JSON Schema for a tool's input."""
    props = {}
    for arg in tool["args"]:
        props[arg] = {"type": "string", "description": arg}
    return {
        "name": name,
        "description": tool.get("desc", name),
        "inputSchema": {
            "type": "object",
            "properties": props,
        }
    }

def handle_jsonrpc(body: dict, session_id: str = "") -> dict:
    """Handle a single MCP JSON-RPC request. Returns response dict."""
    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, msg):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}

    if method == "initialize":
        return ok({
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "CORE v5.0", "version": "5.0"},
        })

    elif method == "notifications/initialized":
        return None  # notification, no response needed

    elif method == "ping":
        return ok({})

    elif method == "tools/list":
        tools_list = [_mcp_tool_schema(n, t) for n, t in TOOLS.items()]
        return ok({"tools": tools_list})

    elif method == "tools/call":
        tool_name = params.get("name", "")
        args      = params.get("arguments", {})
        if tool_name not in TOOLS:
            return err(-32602, f"Unknown tool: {tool_name}")
        try:
            result = TOOLS[tool_name]["fn"](**args) if args else TOOLS[tool_name]["fn"]()
            return ok({
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "isError": False,
            })
        except Exception as e:
            return ok({
                "content": [{"type": "text", "text": str(e)}],
                "isError": True,
            })

    elif method == "resources/list":
        return ok({"resources": []})

    elif method == "prompts/list":
        return ok({"prompts": []})

    else:
        return err(-32601, f"Method not found: {method}")

# ── FastAPI app ───────────────────────────────────────────────────────────────
class Handshake(BaseModel):
    secret: str
    client_id: Optional[str] = "claude_desktop"

class ToolCall(BaseModel):
    session_token: str
    tool: str
    args: dict = {}

app = FastAPI(title="CORE v5.0", version="5.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    counts = get_system_counts()
    return {
        "service":   "CORE v5.0",
        "step":      "0 — MCP + Bot",
        "knowledge": counts.get("knowledge_base", 0),
        "sessions":  counts.get("sessions", 0),
        "mistakes":  counts.get("mistakes", 0),
    }

@app.get("/health")
def health_ep():
    return t_health()

# ── MCP Streamable HTTP transport (2024-11-05) ────────────────────────────────
@app.post("/mcp/sse")
async def mcp_post(req: Request):
    """Streamable HTTP transport — handles MCP JSON-RPC over POST."""
    secret = req.headers.get("X-MCP-Secret", "") or req.query_params.get("secret", "")
    if secret and secret != MCP_SECRET:
        return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32600, "message": "Unauthorized"}}, status_code=401)

    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}}, status_code=400)

    if isinstance(body, list):
        responses = []
        for item in body:
            r = handle_jsonrpc(item)
            if r is not None:
                responses.append(r)
        return JSONResponse(responses)

    response = handle_jsonrpc(body)
    if response is None:
        return JSONResponse({}, status_code=204)

    accept = req.headers.get("accept", "")
    if "text/event-stream" in accept:
        async def sse_single():
            data = json.dumps(response)
            yield f"data: {data}\n\n"
        return StreamingResponse(
            sse_single(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "mcp-session-id": str(uuid.uuid4())}
        )

    return JSONResponse(response)

@app.get("/mcp/sse")
async def mcp_sse_get(req: Request):
    """SSE GET endpoint — fallback for sse-only transport."""
    secret = req.headers.get("X-MCP-Secret", "") or req.query_params.get("secret", "")
    if secret and secret != MCP_SECRET:
        raise HTTPException(401, "Unauthorized")

    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sse_sessions[session_id] = queue

    async def event_stream():
        try:
            endpoint_url = f"/mcp/messages?session_id={session_id}"
            yield f"event: endpoint\ndata: {json.dumps(endpoint_url)}\n\n"

            while True:
                if await req.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield f": ping\n\n"
        finally:
            _sse_sessions.pop(session_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "X-Session-Id": session_id}
    )

_sse_sessions: dict = {}

@app.post("/mcp/messages")
async def mcp_messages(req: Request):
    """SSE message endpoint."""
    session_id = req.query_params.get("session_id", "")
    secret = req.headers.get("X-MCP-Secret", "") or req.query_params.get("secret", "")
    if secret and secret != MCP_SECRET:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "Parse error"}, status_code=400)

    response = handle_jsonrpc(body)

    if session_id and session_id in _sse_sessions:
        if response is not None:
            await _sse_sessions[session_id].put(response)
        return JSONResponse({"ok": True}, status_code=202)
    elif response is not None:
        return JSONResponse(response)
    else:
        return JSONResponse({}, status_code=204)

# ── Legacy custom MCP endpoints ───────────────────────────────────────────────
@app.post("/mcp/startup")
async def mcp_startup(body: Handshake, req: Request):
    if body.secret != MCP_SECRET:
        raise HTTPException(401, "Invalid secret")
    tok = mcp_new(req.client.host)
    notify(f"\U0001f50c MCP Session\nClient: {body.client_id}\nToken: {tok[:8]}...")
    return {
        "session_token": tok,
        "expires_hours": SESSION_TTL_H,
        "state":         t_state(),
        "health":        t_health(),
        "constitution":  t_constitution(),
        "tools":         list(TOOLS.keys()),
        "note":          "3 auto-calls complete. CORE Step 0 ready.",
    }

@app.post("/mcp/auth")
async def mcp_auth(body: Handshake, req: Request):
    if body.secret != MCP_SECRET:
        notify(f"\u26a0\ufe0f Invalid MCP auth from {req.client.host}")
        raise HTTPException(401, "Invalid secret")
    return {"session_token": mcp_new(req.client.host), "expires_hours": SESSION_TTL_H}

@app.post("/mcp/tool")
async def mcp_tool(body: ToolCall):
    if not mcp_ok(body.session_token):
        raise HTTPException(401, "Invalid/expired session")
    if not L.mcp(body.session_token):
        raise HTTPException(429, "Rate limit exceeded")
    if body.tool not in TOOLS:
        raise HTTPException(404, f"Tool not found: {body.tool}")
    try:
        res = TOOLS[body.tool]["fn"](**body.args) if body.args else TOOLS[body.tool]["fn"]()
        return {"ok": True, "tool": body.tool, "perm": TOOLS[body.tool]["perm"], "result": res}
    except HTTPException: raise
    except Exception as e:
        return {"ok": False, "tool": body.tool, "error": str(e)}

@app.get("/mcp/tools")
def list_tools():
    return {n: {"perm": t["perm"], "args": t["args"]} for n, t in TOOLS.items()}

# ── Telegram webhook ──────────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(req: Request):
    try:
        u = await req.json()
        if "message" in u:
            threading.Thread(target=handle_msg, args=(u["message"],), daemon=True).start()
    except Exception as e:
        print(f"[WEBHOOK] {e}")
    return {"ok": True}

# ── Telegram handler ──────────────────────────────────────────────────────────
def handle_msg(msg):
    cid  = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "").strip()
    if not text: return

    if text == "/start":
        counts = get_system_counts()
        notify(
            f"*CORE v5.0 — Step 0*\n"
            f"Knowledge: {counts.get('knowledge_base', 0)}\n"
            f"Sessions: {counts.get('sessions', 0)}\n\n"
            f"Commands: /status /tasks /ask <query>\n"
            f"Tasks: send any message to queue it.",
            cid,
        )
    elif text == "/status":
        h = t_health()
        counts = get_system_counts()
        notify(
            f"*Status*\n"
            f"Supabase: {h['components'].get('supabase')}\n"
            f"Groq: {h['components'].get('groq')}\n"
            f"Telegram: {h['components'].get('telegram')}\n"
            f"GitHub: {h['components'].get('github')}\n\n"
            f"Knowledge: {counts.get('knowledge_base', 0)}\n"
            f"Sessions: {counts.get('sessions', 0)}",
            cid,
        )
    elif text == "/tasks":
        tasks = sb_get("task_queue", "select=task,status&order=created_at.desc&limit=5")
        lines = "\n".join([f"- [{t.get('status')}] {t.get('task', '')[:60]}" for t in tasks])
        notify(f"*Recent Tasks*\n\n{lines}" if tasks else "No tasks yet.", cid)
    elif text.startswith("/ask "):
        q   = text[5:].strip()
        res = t_search_kb(q, limit=5)
        if res:
            lines = "\n\n".join([f"*{x.get('topic','')}*\n{str(x.get('content',''))[:200]}" for x in res])
        else:
            lines = "Nothing found in knowledge base."
        notify(lines, cid)
    else:
        ok = sb_post("task_queue", {"task": text, "chat_id": cid, "status": "pending", "priority": 5})
        if ok:
            notify(f"\u2705 Queued: `{text[:80]}`\nExecution starts in Step 3.", cid)
        else:
            notify("\u274c Failed to queue task. Try again.", cid)

# ── Queue poller ──────────────────────────────────────────────────────────────
def queue_poller():
    print("[QUEUE] Started — Step 0 mode (no execution)")
    while True:
        try:
            tasks = sb_get("task_queue", "status=eq.pending&order=priority.asc&limit=1")
            if tasks:
                t = tasks[0]
                sb_patch("task_queue", f"id=eq.{t['id']}", {
                    "status": "waiting",
                    "error": "Execution engine not yet active (Step 3)"
                })
                print(f"[QUEUE] Task {t['id']} acknowledged")
        except Exception as e:
            print(f"[QUEUE] {e}")
        time.sleep(30)

# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def on_start():
    set_webhook()
    threading.Thread(target=queue_poller, daemon=True).start()
    counts = get_system_counts()
    notify(
        f"*CORE v5.0 Online — Step 0*\n"
        f"Knowledge: {counts.get('knowledge_base', 0)}\n"
        f"Sessions: {counts.get('sessions', 0)}\n"
        f"MCP: Streamable HTTP + SSE ready\n"
        f"Training: Step 3 (not started)"
    )
    print(f"[CORE] v5.0 Step 0 online :{PORT} — MCP transport: Streamable HTTP + SSE")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("core:app", host="0.0.0.0", port=PORT, reload=False)
