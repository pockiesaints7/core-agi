"""CORE v5.0 — Step 0 | MCP Server + Telegram Bot + Queue Poller
Owner: REINVAGNAR
Step 0 scope: MCP fully working, Telegram queues tasks, NO training, NO agent pipeline.
Training + agents added in Step 3.
"""
import base64, hashlib, json, os, threading, time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Env vars ────────────────────────────────────────────────────────────────
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

# ── Rate limiter ─────────────────────────────────────────────────────────────
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

    def tg(self):  return self._ok("tg",  3600, self.c.get("telegram_messages_per_hour", 30))
    def gh(self):  return self._ok("gh",  3600, self.c.get("github_pushes_per_hour", 20))
    def sbw(self): return self._ok("sbw", 3600, self.c.get("supabase_writes_per_hour", 500))
    def mcp(self, sid): return self._ok(f"mcp:{sid}", 60, self.c.get("mcp_tool_calls_per_minute", 30))

L = RateLimiter()

# ── Supabase ─────────────────────────────────────────────────────────────────
def _sbh(svc=False):
    k = SUPABASE_SVC if svc else SUPABASE_ANON
    return {"apikey": k, "Authorization": f"Bearer {k}",
            "Content-Type": "application/json", "Prefer": "return=minimal"}

def sb_get(t, qs=""):
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/{t}?{qs}", headers=_sbh(), timeout=15)
    r.raise_for_status()
    return r.json()

def sb_post(t, d):
    if not L.sbw(): return False
    return httpx.post(f"{SUPABASE_URL}/rest/v1/{t}", headers=_sbh(True), json=d, timeout=15).is_success

def sb_patch(t, m, d):
    if not L.sbw(): return False
    return httpx.patch(f"{SUPABASE_URL}/rest/v1/{t}?{m}", headers=_sbh(True), json=d, timeout=15).is_success

# ── Telegram ──────────────────────────────────────────────────────────────────
def notify(msg, cid=None):
    if not L.tg(): return False
    try:
        httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": cid or TELEGRAM_CHAT, "text": msg[:4000], "parse_mode": "Markdown"},
            timeout=10,
        )
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
def load_master_prompt():
    d = sb_get("master_prompt", "is_active=eq.true&order=version.desc&limit=1")
    return (d[0]["content"], d[0]["version"]) if d else ("", 0)

def get_agi_status():
    d = sb_get("agi_status", "limit=1")
    return d[0] if d else {}

# ── MCP session management ────────────────────────────────────────────────────
_sessions: dict = {}

def mcp_new(ip: str) -> str:
    tok = hashlib.sha256(f"{MCP_SECRET}{ip}{time.time()}".encode()).hexdigest()[:32]
    _sessions[tok] = {
        "ip": ip,
        "expires": (datetime.utcnow() + timedelta(hours=SESSION_TTL_H)).isoformat(),
        "calls": 0,
    }
    # Clean expired sessions
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
    mp = sb_get("master_prompt", "select=version,content&is_active=eq.true&limit=1")
    st = sb_get("agi_status", "limit=1")
    tq = sb_get("task_queue", "select=id,task,status&status=eq.pending&limit=5")
    return {
        "prompt_version": mp[0]["version"] if mp else "?",
        "prompt_preview": mp[0]["content"][:500] if mp else "",
        "system": st[0] if st else {},
        "pending_tasks": tq,
        "note": "Training starts Step 3.",
    }

def t_health():
    h = {"ts": datetime.utcnow().isoformat(), "components": {}}
    checks = [
        ("supabase",  lambda: sb_get("master_prompt", "select=id&limit=1")),
        ("groq",      lambda: httpx.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=5).raise_for_status()),
        ("telegram",  lambda: httpx.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=5).raise_for_status()),
        ("github",    lambda: gh_read("README.md")),
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

def t_get_mistakes(domain="general", limit=10):
    qs = f"select=context,what_failed,correct_approach&limit={limit}"
    if domain and domain != "all": qs += f"&domain=eq.{domain}"
    return sb_get("mistakes", qs)

def t_update_state(key, value, reason):
    return {"ok": sb_post("memory", {"category": "mcp_state", "key": key, "value": str(value), "note": reason}), "key": key}

def t_add_knowledge(domain, topic, content, tags, confidence="medium"):
    return {"ok": sb_post("knowledge_base", {"domain": domain, "topic": topic, "content": content,
                                              "confidence": confidence, "tags": tags, "source": "mcp_session"}), "topic": topic}

def t_log_mistake(context, what_failed, fix, domain="general"):
    return {"ok": sb_post("mistakes", {"domain": domain, "context": context,
                                        "what_failed": what_failed, "correct_approach": fix})}

def t_read_file(path, repo=""):
    try: return {"ok": True, "content": gh_read(path, repo or GITHUB_REPO)[:5000]}
    except Exception as e: return {"ok": False, "error": str(e)}

def t_write_file(path, content, message, repo=""):
    ok = gh_write(path, content, message, repo or GITHUB_REPO)
    if ok: notify(f"MCP write: `{path}`")
    return {"ok": ok, "path": path}

def t_notify(message, level="info"):
    icons = {"info": "ℹ️", "warn": "⚠️", "alert": "🚨", "ok": "✅"}
    return {"ok": notify(f"{icons.get(level, '»')} CORE\n{message}")}

def t_sb_query(table, query_string="", limit=20):
    return sb_get(table, f"{query_string}&limit={limit}" if query_string else f"limit={limit}")

def t_sb_insert(table, data):
    return {"ok": sb_post(table, data), "table": table}

def t_training_status():
    return {"status": "Step 3 — not started", "note": "Training loop added in Step 3."}

# ── Tool registry ─────────────────────────────────────────────────────────────
TOOLS = {
    "get_state":           {"fn": t_state,           "perm": "READ",    "args": []},
    "get_system_health":   {"fn": t_health,           "perm": "READ",    "args": []},
    "get_constitution":    {"fn": t_constitution,     "perm": "READ",    "args": []},
    "get_training_status": {"fn": t_training_status,  "perm": "READ",    "args": []},
    "search_kb":           {"fn": t_search_kb,        "perm": "READ",    "args": ["query", "domain", "limit"]},
    "get_mistakes":        {"fn": t_get_mistakes,     "perm": "READ",    "args": ["domain", "limit"]},
    "read_file":           {"fn": t_read_file,        "perm": "READ",    "args": ["path", "repo"]},
    "sb_query":            {"fn": t_sb_query,         "perm": "READ",    "args": ["table", "query_string", "limit"]},
    "update_state":        {"fn": t_update_state,     "perm": "WRITE",   "args": ["key", "value", "reason"]},
    "add_knowledge":       {"fn": t_add_knowledge,    "perm": "WRITE",   "args": ["domain", "topic", "content", "tags", "confidence"]},
    "log_mistake":         {"fn": t_log_mistake,      "perm": "WRITE",   "args": ["context", "what_failed", "fix", "domain"]},
    "notify_owner":        {"fn": t_notify,           "perm": "WRITE",   "args": ["message", "level"]},
    "sb_insert":           {"fn": t_sb_insert,        "perm": "WRITE",   "args": ["table", "data"]},
    "write_file":          {"fn": t_write_file,       "perm": "EXECUTE", "args": ["path", "content", "message", "repo"]},
}

# ── FastAPI ───────────────────────────────────────────────────────────────────
class Handshake(BaseModel):
    secret: str
    client_id: Optional[str] = "claude_desktop"

class ToolCall(BaseModel):
    session_token: str
    tool: str
    args: dict = {}

app = FastAPI(title="CORE v5.0 — Step 0", version="5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def root():
    s = get_agi_status()
    return {
        "service": "CORE v5.0",
        "step": "0 — MCP + Bot",
        "prompt_v": s.get("master_prompt_version", "?"),
        "knowledge": s.get("knowledge_entries", 0),
    }

@app.get("/health")
def health_ep():
    return t_health()

@app.post("/mcp/startup")
async def mcp_startup(body: Handshake, req: Request):
    if body.secret != MCP_SECRET:
        raise HTTPException(401, "Invalid secret")
    tok = mcp_new(req.client.host)
    notify(f"🔌 MCP Session\nClient: {body.client_id}\nToken: {tok[:8]}...")
    return {
        "session_token": tok,
        "expires_hours": SESSION_TTL_H,
        "state":        t_state(),
        "health":       t_health(),
        "constitution": t_constitution(),
        "tools":        list(TOOLS.keys()),
        "note":         "3 auto-calls complete. CORE Step 0 ready.",
    }

@app.post("/mcp/auth")
async def mcp_auth(body: Handshake, req: Request):
    if body.secret != MCP_SECRET:
        notify(f"⚠️ Invalid MCP auth from {req.client.host}")
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

@app.post("/webhook")
async def webhook(req: Request):
    try:
        u = await req.json()
        if "message" in u:
            threading.Thread(target=handle_msg, args=(u["message"],), daemon=True).start()
    except Exception as e:
        print(f"[WEBHOOK] {e}")
    return {"ok": True}

# ── Telegram handler (Step 0: queue only, no Groq) ────────────────────────────
def handle_msg(msg):
    cid  = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "").strip()
    if not text: return

    if text == "/start":
        s = get_agi_status()
        notify(
            f"*CORE v5.0 — Step 0*\n"
            f"Knowledge: {s.get('knowledge_entries', 0)}\n"
            f"Prompt: v{s.get('master_prompt_version', '?')}\n\n"
            f"Commands: /status /tasks /ask <query>\n"
            f"Tasks: send any message to queue it.",
            cid,
        )

    elif text == "/status":
        s = get_agi_status()
        h = t_health()
        notify(
            f"*Status*\n"
            f"Supabase: {h['components'].get('supabase')}\n"
            f"Groq: {h['components'].get('groq')}\n"
            f"Telegram: {h['components'].get('telegram')}\n"
            f"GitHub: {h['components'].get('github')}\n\n"
            f"Knowledge: {s.get('knowledge_entries', 0)}\n"
            f"Prompt: v{s.get('master_prompt_version', '?')}",
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
        # Queue the task — execution added in Step 3
        ok = sb_post("task_queue", {"task": text, "chat_id": cid, "status": "pending", "priority": 5})
        if ok:
            notify(f"✅ Queued: `{text[:80]}`\nExecution starts in Step 3.", cid)
        else:
            notify("❌ Failed to queue task. Try again.", cid)

# ── Queue poller (Step 0: marks tasks, no execution yet) ─────────────────────
def queue_poller():
    print("[QUEUE] Started — Step 0 mode (no execution)")
    while True:
        try:
            tasks = sb_get("task_queue", "status=eq.pending&order=priority.asc&limit=1")
            if tasks:
                t = tasks[0]
                # Step 0: just acknowledge — execution engine added Step 3
                sb_patch("task_queue", f"id=eq.{t['id']}", {
                    "status": "waiting",
                    "error": "Execution engine not yet active (Step 3)"
                })
                print(f"[QUEUE] Task {t['id']} acknowledged, waiting for Step 3 executor")
        except Exception as e:
            print(f"[QUEUE] {e}")
        time.sleep(30)

# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def on_start():
    set_webhook()
    threading.Thread(target=queue_poller, daemon=True).start()
    s = get_agi_status()
    notify(
        f"*CORE v5.0 Online — Step 0*\n"
        f"Knowledge: {s.get('knowledge_entries', 0)}\n"
        f"Prompt: v{s.get('master_prompt_version', '?')}\n"
        f"MCP: ready on /mcp/*\n"
        f"Training: Step 3 (not started)"
    )
    print(f"[CORE] v5.0 Step 0 online :{PORT}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("core:app", host="0.0.0.0", port=PORT, reload=False)
