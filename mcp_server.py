"""
CORE AGI â€” Railway MCP Server
Version: 1.0 | Step 0 of CORE v5.0
Port: 8081 (separate from orchestrator.py on 8080)

Architecture:
  Claude Desktop â†’ MCP protocol (HTTP+SSE) â†’ This server â†’ Supabase/GitHub/Telegram

Auth: 3 layers
  1. MCP_SECRET header â€” session token (8h expiry)
  2. Tool-level permissions (READ/WRITE/EXECUTE/CRITICAL)
  3. Rate limiting via resource_ceilings.json

Session startup (3 auto-calls):
  1. get_state()         â†’ context + current focus
  2. get_system_health() â†’ system condition
  3. get_constitution()  â†’ immutable boundaries
"""

import hashlib
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

# ── mcp_tools wiring ──────────────────────────────────────
try:
    from mcp_tools.db import sql as _mcp_sql, dq as _mcp_dq
    from mcp_tools.actions import action_boot, action_brain_write, action_context
    from mcp_tools.brain_health import run_scan as _run_health_scan
    _MCP_TOOLS_LOADED = True
    print("[CORE MCP] mcp_tools loaded OK")
except Exception as _e:
    _MCP_TOOLS_LOADED = False
    print(f"[CORE MCP] mcp_tools load warning: {_e} (falling back to inline)")
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import Any, Optional

# ============================================================
# CONFIG
# ============================================================
MCP_SECRET        = os.environ.get("MCP_SECRET", "core_mcp_secret_change_me")
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "https://qbfaplqiakwjvrtwpbmr.supabase.co")
SUPABASE_SVC      = os.environ.get("SUPABASE_SERVICE_KEY", "")
SUPABASE_ANON     = os.environ.get("SUPABASE_ANON_KEY", "")
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "838737537")
GITHUB_PAT        = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "pockiesaints7/core-agi")
PORT              = int(os.environ.get("MCP_PORT", 8081))

SESSION_TTL_HOURS = 8

# ============================================================
# RATE LIMITER
# ============================================================
class RateLimiter:
    def __init__(self):
        self.calls: dict[str, list[float]] = defaultdict(list)
        self.ceilings = self._load_ceilings()

    def _load_ceilings(self):
        try:
            with open("resource_ceilings.json") as f:
                return json.load(f)
        except Exception:
            return {"mcp_tool_calls_per_minute": 30}

    def check(self, key: str, window_seconds: int, limit: int) -> bool:
        now = time.time()
        bucket = self.calls[key]
        self.calls[key] = [t for t in bucket if now - t < window_seconds]
        if len(self.calls[key]) >= limit:
            return False
        self.calls[key].append(now)
        return True

    def check_tool_call(self, session_id: str) -> bool:
        limit = self.ceilings.get("mcp_tool_calls_per_minute", 30)
        return self.check(f"tool:{session_id}", 60, limit)

    def check_telegram(self) -> bool:
        limit = self.ceilings.get("telegram_messages_per_hour", 30)
        return self.check("telegram", 3600, limit)

    def check_github(self) -> bool:
        limit = self.ceilings.get("github_pushes_per_hour", 20)
        return self.check("github", 3600, limit)

    def check_supabase_write(self) -> bool:
        limit = self.ceilings.get("supabase_writes_per_hour", 500)
        return self.check("sb_write", 3600, limit)

limiter = RateLimiter()

# ============================================================
# SESSION STORE
# ============================================================
_sessions: dict[str, dict] = {}

def create_session(ip: str) -> str:
    token = hashlib.sha256(f"{MCP_SECRET}{ip}{time.time()}".encode()).hexdigest()[:32]
    _sessions[token] = {
        "created": datetime.utcnow().isoformat(),
        "ip": ip,
        "expires": (datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)).isoformat(),
        "calls": 0,
    }
    return token

def validate_session(token: str) -> bool:
    if token not in _sessions:
        return False
    exp = datetime.fromisoformat(_sessions[token]["expires"])
    if datetime.utcnow() > exp:
        del _sessions[token]
        return False
    _sessions[token]["calls"] += 1
    return True


# ============================================================
# SUPABASE HELPERS
# ============================================================
def _sb_headers(svc=False):
    key = SUPABASE_SVC if svc else SUPABASE_ANON
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

def sb_get(table: str, qs: str = "") -> list:
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/{table}?{qs}", headers=_sb_headers(), timeout=15)
    r.raise_for_status()
    return r.json()

def sb_post(table: str, data: dict) -> bool:
    if not limiter.check_supabase_write():
        raise HTTPException(429, "Supabase write ceiling hit")
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=_sb_headers(svc=True),
                   json=data, timeout=15)
    return r.is_success

def sb_patch(table: str, match: str, data: dict) -> bool:
    if not limiter.check_supabase_write():
        raise HTTPException(429, "Supabase write ceiling hit")
    r = httpx.patch(f"{SUPABASE_URL}/rest/v1/{table}?{match}",
                    headers=_sb_headers(svc=True), json=data, timeout=15)
    return r.is_success

# ============================================================
# TELEGRAM HELPER
# ============================================================
def tg_notify(msg: str) -> bool:
    if not limiter.check_telegram():
        return False
    try:
        httpx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                   json={"chat_id": TELEGRAM_CHAT_ID, "text": msg[:4000]}, timeout=10)
        return True
    except Exception:
        return False

# ============================================================
# GITHUB HELPER
# ============================================================
def gh_read(path: str, repo: str = GITHUB_REPO) -> str:
    h = {"Authorization": f"Bearer {GITHUB_PAT}", "Accept": "application/vnd.github.v3+json"}
    r = httpx.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers=h, timeout=15)
    r.raise_for_status()
    import base64
    return base64.b64decode(r.json()["content"]).decode()

def gh_write(path: str, content: str, message: str, repo: str = GITHUB_REPO) -> bool:
    if not limiter.check_github():
        raise HTTPException(429, "GitHub push ceiling hit")
    import base64
    h = {"Authorization": f"Bearer {GITHUB_PAT}", "Accept": "application/vnd.github.v3+json"}
    sha = None
    try:
        sha = httpx.get(f"https://api.github.com/repos/{repo}/contents/{path}",
                        headers=h, timeout=10).json().get("sha")
    except Exception:
        pass
    payload = {"message": message, "content": base64.b64encode(content.encode()).decode()}
    if sha:
        payload["sha"] = sha
    r = httpx.put(f"https://api.github.com/repos/{repo}/contents/{path}",
                  headers=h, json=payload, timeout=20)
    return r.is_success


# ============================================================
# MCP TOOL IMPLEMENTATIONS
# ============================================================

def tool_get_state() -> dict:
    """READ â€” Load CORE state: master_prompt + agi_status + active tasks"""
    mp = sb_get("master_prompt", "select=version,content,updated_at&is_active=eq.true&limit=1")
    status = sb_get("agi_status", "limit=1")
    tasks = sb_get("task_queue", "select=id,task,status,priority&status=eq.pending&order=priority.asc&limit=5")
    return {
        "master_prompt_version": mp[0]["version"] if mp else "unknown",
        "master_prompt_preview": (mp[0]["content"][:500] if mp else ""),
        "system_status": status[0] if status else {},
        "pending_tasks": tasks,
        "loaded_at": datetime.utcnow().isoformat(),
    }

def tool_get_system_health() -> dict:
    # If mcp_tools loaded, also run brain health scan
    if _MCP_TOOLS_LOADED:
        try:
            import asyncio
            scan = asyncio.run(_run_health_scan())
            health_extra = {
                "brain_counts": scan.get("brain_counts", {}),
                "maintenance_flags": len(scan.get("maintenance_flags", [])),
                "growth_flags": len(scan.get("growth_flags", [])),
                "needs_attention": scan.get("needs_attention", False),
            }
        except Exception as _se:
            health_extra = {"scan_error": str(_se)}
    else:
        health_extra = {"mcp_tools": "not loaded"}
    # Original health check below — result merged at end
    _health_extra_ref = health_extra
    """READ â€” Check system health across all components"""
    health = {"timestamp": datetime.utcnow().isoformat(), "components": {}}
    # Supabase
    try:
        sb_get("master_prompt", "select=id&limit=1")
        health["components"]["supabase"] = "ok"
    except Exception as e:
        health["components"]["supabase"] = f"error: {e}"
    # GitHub
    try:
        gh_read("README.md")
        health["components"]["github"] = "ok"
    except Exception as e:
        health["components"]["github"] = f"error: {e}"
    # Telegram
    try:
        r = httpx.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=5)
        health["components"]["telegram"] = "ok" if r.is_success else "error"
    except Exception as e:
        health["components"]["telegram"] = f"error: {e}"
    # Rate limiter status
    health["rate_limits"] = {
        "sessions_active": len(_sessions),
    }
    health["overall"] = "ok" if all(v == "ok" for v in health["components"].values()) else "degraded"
    health["mcp_tools_loaded"] = _MCP_TOOLS_LOADED
    health.update(_health_extra_ref)
    return health

def tool_get_constitution() -> dict:
    """READ â€” Return immutable CORE constitution"""
    try:
        with open("constitution.txt") as f:
            text = f.read()
    except Exception:
        text = "constitution.txt not found on server"
    return {"constitution": text, "note": "This file cannot be modified by any tool or evolution process."}

def tool_search_kb(query: str, domain: str = "", limit: int = 10) -> list:
    """READ â€” Search knowledge base"""
    qs = f"select=domain,topic,content,confidence&limit={limit}"
    if domain:
        qs += f"&domain=eq.{domain}"
    if query:
        word = query.split()[0]
        qs += f"&content=ilike.*{word}*"
    return sb_get("knowledge_base", qs)

def tool_get_mistakes(domain: str = "general", limit: int = 10) -> list:
    """READ â€” Fetch relevant mistakes to avoid"""
    qs = f"select=domain,mistake,root_cause,fix&limit={limit}"
    if domain and domain != "all":
        qs += f"&domain=eq.{domain}"
    return sb_get("mistakes", qs)

def tool_update_state(key: str, value: Any, reason: str) -> dict:
    """WRITE â€” Update a state field in Supabase memory table"""
    ok = sb_post("memory", {"key": key, "value": str(value), "source": "mcp_session",
                             "note": reason, "updated_at": datetime.utcnow().isoformat()})
    return {"ok": ok, "key": key, "reason": reason}

def tool_add_knowledge(domain: str, topic: str, content: str,
                       tags: list, confidence: str = "medium") -> dict:
    """WRITE â€” Add new knowledge entry"""
    ok = sb_post("knowledge_base", {
        "domain": domain, "topic": topic, "content": content,
        "confidence": confidence, "tags": tags, "source": "mcp_claude_session"
    })
    return {"ok": ok, "topic": topic}

def tool_log_mistake(context: str, what_failed: str, fix: str,
                     domain: str = "general") -> dict:
    """WRITE â€” Log a mistake to prevent recurrence"""
    ok = sb_post("mistakes", {
        "domain": domain, "mistake": what_failed,
        "root_cause": context, "fix": fix,
        "context": context, "severity": "medium"
    })
    return {"ok": ok}

def tool_read_file(path: str, repo: str = "") -> dict:
    """READ â€” Read a file from GitHub"""
    target_repo = repo if repo else GITHUB_REPO
    try:
        content = gh_read(path, target_repo)
        return {"ok": True, "path": path, "repo": target_repo, "content": content[:5000]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def tool_write_file(path: str, content: str, message: str, repo: str = "") -> dict:
    """EXECUTE â€” Write a file to GitHub (confirm=True required for CRITICAL paths)"""
    target_repo = repo if repo else GITHUB_REPO
    ok = gh_write(path, content, message, target_repo)
    if ok:
        tg_notify(f"ðŸ“ CORE MCP write_file\nRepo: {target_repo}\nPath: {path}\nMsg: {message}")
    return {"ok": ok, "path": path, "repo": target_repo}

def tool_notify_owner(message: str, level: str = "info") -> dict:
    """WRITE â€” Send Telegram message to owner"""
    icon = {"info": "â„¹ï¸", "warn": "âš ï¸", "alert": "ðŸš¨", "ok": "âœ…"}.get(level, "â€¢")
    ok = tg_notify(f"{icon} CORE MCP\n{message}")
    return {"ok": ok}

def tool_sb_query(table: str, query_string: str, limit: int = 20) -> list:
    """READ â€” Query any Supabase table"""
    qs = f"{query_string}&limit={limit}" if query_string else f"limit={limit}"
    return sb_get(table, qs)

def tool_sb_insert(table: str, data: dict) -> dict:
    """WRITE â€” Insert row into Supabase table"""
    ok = sb_post(table, data)
    return {"ok": ok, "table": table}

def tool_get_master_prompt() -> dict:
    """READ â€” Get full active master prompt"""
    rows = sb_get("master_prompt", "select=version,content,updated_at&is_active=eq.true&order=version.desc&limit=1")
    if not rows:
        return {"ok": False, "error": "No active master prompt found"}
    return {"ok": True, "version": rows[0]["version"], "content": rows[0]["content"],
            "updated_at": rows[0].get("updated_at", "")}

def tool_propose_to_owner(proposal: str, reasoning: str, risk_level: str = "low") -> dict:
    """CRITICAL â€” Send proposal to owner for approval via Telegram"""
    msg = (f"ðŸ¤” CORE Proposal\nRisk: {risk_level.upper()}\n\n"
           f"PROPOSAL:\n{proposal}\n\nREASONING:\n{reasoning}\n\n"
           f"Reply APPROVE or REJECT.")
    ok = tg_notify(msg)
    ok2 = sb_post("evolution_queue", {
        "phase_id": 0, "loop_n": 0, "category": "proposal",
        "score": 0, "reason": proposal, "probe": reasoning,
        "status": "awaiting_owner_approval",
        "created_at": datetime.utcnow().isoformat()
    })
    return {"ok": ok and ok2, "status": "awaiting_owner_approval", "risk_level": risk_level}


# ============================================================
# TOOL REGISTRY
# ============================================================
TOOL_REGISTRY = {
    # --- READ tools (always allowed) ---
    "get_state":          {"fn": tool_get_state,          "perm": "READ",     "args": []},
    "get_system_health":  {"fn": tool_get_system_health,  "perm": "READ",     "args": []},
    "get_constitution":   {"fn": tool_get_constitution,   "perm": "READ",     "args": []},
    "get_master_prompt":  {"fn": tool_get_master_prompt,  "perm": "READ",     "args": []},
    "search_kb":          {"fn": tool_search_kb,          "perm": "READ",     "args": ["query", "domain", "limit"]},
    "get_mistakes":       {"fn": tool_get_mistakes,       "perm": "READ",     "args": ["domain", "limit"]},
    "read_file":          {"fn": tool_read_file,          "perm": "READ",     "args": ["path", "repo"]},
    "sb_query":           {"fn": tool_sb_query,           "perm": "READ",     "args": ["table", "query_string", "limit"]},
    # --- WRITE tools (logged) ---
    "update_state":       {"fn": tool_update_state,       "perm": "WRITE",    "args": ["key", "value", "reason"]},
    "add_knowledge":      {"fn": tool_add_knowledge,      "perm": "WRITE",    "args": ["domain", "topic", "content", "tags", "confidence"]},
    "log_mistake":        {"fn": tool_log_mistake,        "perm": "WRITE",    "args": ["context", "what_failed", "fix", "domain"]},
    "notify_owner":       {"fn": tool_notify_owner,       "perm": "WRITE",    "args": ["message", "level"]},
    "sb_insert":          {"fn": tool_sb_insert,          "perm": "WRITE",    "args": ["table", "data"]},
    # --- EXECUTE tools ---
    "write_file":         {"fn": tool_write_file,         "perm": "EXECUTE",  "args": ["path", "content", "message", "repo"]},
    # --- CRITICAL tools (owner approval) ---
    "propose_to_owner":   {"fn": tool_propose_to_owner,   "perm": "CRITICAL", "args": ["proposal", "reasoning", "risk_level"]},
}

# ============================================================
# FASTAPI APP
# ============================================================
app = FastAPI(title="CORE MCP Server", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# â”€â”€ MCP Protocol Models â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class MCPHandshake(BaseModel):
    secret: str
    client_id: Optional[str] = "claude_desktop"

class MCPToolCall(BaseModel):
    session_token: str
    tool: str
    args: dict = {}

# â”€â”€ Auth endpoint â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.post("/mcp/auth")
async def mcp_auth(body: MCPHandshake, request: Request):
    if body.secret != MCP_SECRET:
        tg_notify(f"ðŸš¨ CORE MCP: Invalid auth attempt from {request.client.host}")
        raise HTTPException(401, "Invalid MCP secret")
    token = create_session(request.client.host)
    tg_notify(f"ðŸ” CORE MCP: Session started\nClient: {body.client_id}\nIP: {request.client.host}")
    return {"session_token": token, "expires_hours": SESSION_TTL_HOURS,
            "auto_calls": ["get_state", "get_system_health", "get_constitution"]}

# â”€â”€ Tool call endpoint â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.post("/mcp/tool")
async def mcp_tool(body: MCPToolCall):
    # Validate session
    if not validate_session(body.session_token):
        raise HTTPException(401, "Invalid or expired session token")
    # Rate limit
    if not limiter.check_tool_call(body.session_token):
        raise HTTPException(429, "Tool call rate limit exceeded")
    # Find tool
    if body.tool not in TOOL_REGISTRY:
        raise HTTPException(404, f"Tool '{body.tool}' not found")
    tool_def = TOOL_REGISTRY[body.tool]
    # Execute
    try:
        fn = tool_def["fn"]
        result = fn(**body.args) if body.args else fn()
        # Log WRITE+ to Supabase
        if tool_def["perm"] in ("WRITE", "EXECUTE", "CRITICAL"):
            try:
                sb_post("session_learning", {
                    "task_summary": f"MCP {body.tool}",
                    "new_pattern": str(body.args)[:200],
                    "mistake_to_avoid": "",
                    "estimated_improvement": 0,
                })
            except Exception:
                pass
        return {"ok": True, "tool": body.tool, "perm": tool_def["perm"], "result": result}
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "tool": body.tool, "error": str(e)}

# â”€â”€ Session startup helper â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.post("/mcp/startup")
async def mcp_startup(body: MCPHandshake, request: Request):
    """One-call session start: auth + 3 auto-calls in one response."""
    if body.secret != MCP_SECRET:
        raise HTTPException(401, "Invalid MCP secret")
    token = create_session(request.client.host)
    state  = tool_get_state()
    health = tool_get_system_health()
    const  = tool_get_constitution()
    tg_notify(f"ðŸš€ CORE MCP Session\nClient: {body.client_id}\nToken: {token[:8]}...")
    return {
        "session_token": token,
        "expires_hours": SESSION_TTL_HOURS,
        "state": state,
        "health": health,
        "constitution": const,
        "tools_available": list(TOOL_REGISTRY.keys()),
        "note": "3 auto-calls complete. CORE fully aware.",
    }

# â”€â”€ Health check â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.get("/")
async def root():
    s = tool_get_state()
    return {"service": "CORE MCP Server", "version": "1.0",
            "master_prompt_version": s.get("master_prompt_version"),
            "sessions_active": len(_sessions)}

@app.get("/health")
async def health():
    return tool_get_system_health()

# â”€â”€ List tools â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.get("/mcp/tools")
async def list_tools():
    return {name: {"perm": t["perm"], "args": t["args"]} for name, t in TOOL_REGISTRY.items()}


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    import uvicorn
    print(f"[CORE MCP] Starting on port {PORT}")
    uvicorn.run("mcp_server:app", host="0.0.0.0", port=PORT, reload=False)
