"""core_main.py — CORE AGI entry point
FastAPI app, all routes, Pydantic models, Telegram handler, queue_poller, startup.
Extracted from core.py as part of Task 2 architecture split.

Import chain:
  core_main imports: core_config, core_github, core_train, core_tools
  (no circular deps — core_config has no internal imports)

NOTE: This IS the live entry point (Procfile: web: python core_main.py). core.py deleted.
Activation: rename/swap after smoke test passes (Task 2.6).
"""
import asyncio
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from core_config import (
    MCP_SECRET, MCP_PROTOCOL_VERSION, PORT, SESSION_TTL_H,
    SUPABASE_URL, COLD_KB_GROWTH_THRESHOLD, GROQ_API_KEY,
    L, sb_get, sb_post, sb_patch, sb_upsert, sb_post_critical,
    _sbh, _sbh_count_svc, groq_chat,
)
from core_github import gh_read, gh_write, notify
from core_train import cold_processor_loop, background_researcher, evolution_tier_processor, proactive_surface_loop
from core_tools import TOOLS, handle_jsonrpc
# from core_orchestrator import handle_telegram_message, start_orchestrator
from core_orch_main import handle_telegram_message, startup_v2

# ---------------------------------------------------------------------------
# Shared helpers (used by routes + tools — defined here, imported by core_tools)
# ---------------------------------------------------------------------------
def get_resume_task() -> str:
    """Return title of highest-priority in_progress task from task_queue.
    Used in Telegram startup message and /state endpoint.
    SESSION.md no longer tracks current step -- task_queue is source of truth."""
    try:
        tasks = sb_get(
            "task_queue",
            "select=task,priority,status&source=in.(core_v6_registry,mcp_session)"
            "&status=eq.in_progress&order=priority.desc&limit=1"
        )
        if tasks and isinstance(tasks, list) and tasks[0]:
            raw = tasks[0].get("task", "")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                title = parsed.get("title") or parsed.get("task_id") or str(parsed)[:80]
            except Exception:
                title = str(raw)[:80]
            priority = tasks[0].get("priority", "?")
            return f"Resuming: {title} (P{priority})"
        # No in_progress tasks -- check for pending (exclude desktop_agent tasks)
        pending = sb_get(
            "task_queue",
            "select=task,priority&source=in.(core_v6_registry,mcp_session)"
            "&status=eq.pending&task=not.like.*desktop_agent*&order=priority.desc&limit=1"
        )
        if pending and isinstance(pending, list) and pending[0]:
            raw = pending[0].get("task", "")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                title = parsed.get("title") or str(parsed)[:80]
            except Exception:
                title = str(raw)[:80]
            return f"Next: {title}"
        return "No active tasks"
    except Exception as e:
        print(f"[STEP] get_resume_task error: {e}")
        return "task_queue unavailable"


def get_latest_session():
    d = sb_get("sessions", "select=summary,created_at&order=created_at.desc&limit=1")
    return d[0] if d else {}


def get_system_counts():
    counts = {}
    # Core brain tables — total counts
    table_filters = {
        "knowledge_base": "",
        "mistakes":       "",
        "sessions":       "",
    }
    for t, extra in table_filters.items():
        try:
            r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/{t}?select=id&limit=1{extra}",
                headers=_sbh_count_svc(), timeout=10
            )
            cr = r.headers.get("content-range", "*/0")
            counts[t] = int(cr.split("/")[-1]) if "/" in cr else 0
        except:
            counts[t] = -1
    # task_queue — pending only
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/task_queue?select=id&limit=1&status=eq.pending",
            headers=_sbh_count_svc(), timeout=10
        )
        cr = r.headers.get("content-range", "*/0")
        counts["task_queue_pending"] = int(cr.split("/")[-1]) if "/" in cr else 0
    except:
        counts["task_queue_pending"] = -1
    # evolution_queue — counts by status
    for status in ("pending", "applied", "rejected"):
        try:
            r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/evolution_queue?select=id&limit=1&status=eq.{status}",
                headers=_sbh_count_svc(), timeout=10
            )
            cr = r.headers.get("content-range", "*/0")
            counts[f"evolution_{status}"] = int(cr.split("/")[-1]) if "/" in cr else 0
        except:
            counts[f"evolution_{status}"] = -1
    return counts


def self_sync_check():
    from core_config import CORE_SELF_STALE_DAYS
    try:
        core_self = gh_read("CORE_SELF.md")
        last_updated = None
        for line in core_self.splitlines():
            if "Last updated:" in line:
                date_str = line.split("Last updated:")[-1].strip()
                try:
                    last_updated = datetime.strptime(date_str, "%Y-%m-%d")
                except:
                    pass
                break
        if not last_updated:
            notify("CORE Self-Sync Warning\nCORE_SELF.md has no Last updated date.")
            return {"ok": False, "reason": "no_date"}
        days_stale = (datetime.utcnow() - last_updated).days
        if days_stale > CORE_SELF_STALE_DAYS:
            recent = sb_get("sessions", "select=id&order=created_at.desc&limit=1", svc=True)
            if recent:
                notify(
                    f"CORE Self-Sync Warning\n"
                    f"CORE_SELF.md last updated {days_stale} days ago.\n"
                    f"Active sessions detected since then.\n"
                    f"Please review and update CORE_SELF.md.\n"
                    f"github.com/pockiesaints7/core-agi/blob/main/CORE_SELF.md"
                )
                print(f"[SELF_SYNC] WARNING: CORE_SELF.md is {days_stale} days stale")
                return {"ok": False, "days_stale": days_stale, "warned": True}
        print(f"[SELF_SYNC] OK - CORE_SELF.md updated {days_stale}d ago")
        return {"ok": True, "days_stale": days_stale}
    except Exception as e:
        print(f"[SELF_SYNC] error: {e}")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# MCP session management
# ---------------------------------------------------------------------------
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
    for k in expired:
        del _sessions[k]
    return tok


def mcp_ok(tok: str) -> bool:
    if tok not in _sessions:
        return False
    if datetime.utcnow() > datetime.fromisoformat(_sessions[tok]["expires"]):
        del _sessions[tok]
        return False
    _sessions[tok]["calls"] += 1
    return True


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class Handshake(BaseModel):
    secret: str
    client_id: Optional[str] = "claude_desktop"


class ToolCall(BaseModel):
    session_token: str
    tool: str
    args: dict = {}


class PatchRequest(BaseModel):
    secret: str
    path: str
    old_str: str
    new_str: str
    message: str
    repo: Optional[str] = ""


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan — replaces deprecated @app.on_event("startup")."""
    on_start()
    yield


app = FastAPI(title="CORE v6.0", version="6.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_sse_sessions: dict = {}


# ---------------------------------------------------------------------------
# Knowledge ingestion endpoints (TASK-22)
# ---------------------------------------------------------------------------
class IngestRequest(BaseModel):
    topic: str
    sources: list = ["arxiv", "docs", "medium", "reddit", "hackernews", "stackoverflow"]
    max_per_source: int = 50
    since_days: int = 7
    full_refresh: bool = False


@app.post("/ingest/knowledge")
async def ingest_knowledge_endpoint(req: IngestRequest):
    """Trigger knowledge ingestion pipeline for a topic."""
    try:
        from scraper.knowledge import ingest_knowledge
        from core_train import _ingest_to_hot_reflection

        print(f"[INGEST] Starting: topic={req.topic} sources={req.sources} max={req.max_per_source}")
        summary = await ingest_knowledge(
            topic=req.topic,
            sources=req.sources,
            max_per_source=req.max_per_source,
            since_days=req.since_days,
            full_refresh=req.full_refresh,
        )

        hot_ok = False
        if summary.get("concepts_found", 0) > 0:
            from scraper.knowledge.concept_extractor import AI_CONCEPTS
            concepts = list(AI_CONCEPTS.keys())[:summary["concepts_found"]]
            avg_eng = summary.get("avg_engagement", 50.0)
            source_str = ",".join(req.sources)
            hot_ok = _ingest_to_hot_reflection(req.topic, source_str, concepts, avg_eng)

        return {"ok": True, "hot_reflections_injected": hot_ok, **summary}
    except Exception as e:
        print(f"[INGEST] Error: {e}")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/ingest/status")
async def ingest_status():
    """Return ingestion pipeline status: table counts, last run time."""
    try:
        counts = {}
        for table in ("kb_sources", "kb_articles", "kb_concepts"):
            try:
                r = httpx.get(
                    f"{SUPABASE_URL}/rest/v1/{table}?select=id&limit=1",
                    headers=_sbh_count_svc(), timeout=8
                )
                cr = r.headers.get("content-range", "*/0")
                counts[table] = int(cr.split("/")[-1]) if "/" in cr else 0
            except:
                counts[table] = -1

        last_ingest = None
        try:
            rows = sb_get("hot_reflections",
                "select=created_at&domain=eq.knowledge_ingestion&order=created_at.desc&limit=1",
                svc=True)
            if rows:
                last_ingest = {"ts": rows[0].get("created_at"), "summary": rows[0].get("task_summary", "")[:100]}
        except:
            pass

        return {"ok": True, "table_counts": counts, "last_ingest": last_ingest}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.api_route("/", methods=["GET", "HEAD"])
def root():
    counts = get_system_counts()
    step = get_resume_task()
    return {
        "service": "CORE v6.0",
        "step": step,
        "knowledge": counts.get("knowledge_base", 0),
        "sessions": counts.get("sessions", 0),
        "mistakes": counts.get("mistakes", 0),
    }


@app.api_route("/health", methods=["GET", "HEAD"])
def health_ep():
    from core_tools import t_health
    return t_health()


@app.api_route("/ping", methods=["GET", "HEAD"])
def ping():
    """Fast health check - just confirms server is responding."""
    return {"ok": True, "service": "CORE v6.0", "ts": datetime.utcnow().isoformat()}


@app.get("/state")
def state_ep():
    from core_tools import t_state
    return t_state()


@app.get("/review")
async def review_widget():
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>CORE - Evolution Review</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0a;color:#e5e5e5;min-height:100vh;padding:2rem}
h1{font-size:1.4rem;font-weight:500;margin-bottom:.4rem;color:#fff}
.sub{font-size:.8rem;color:#666;margin-bottom:2rem}
.list{display:flex;flex-direction:column;gap:8px;margin-bottom:1.5rem}
.card{background:#141414;border:1px solid #222;border-radius:10px;padding:14px 16px;cursor:pointer;transition:border-color .15s}
.card:hover{border-color:#444}.card.sel{border-color:#4f6ef7}
.meta{display:flex;gap:6px;align-items:center;margin-bottom:6px;flex-wrap:wrap}
.badge{font-size:11px;font-weight:500;padding:2px 8px;border-radius:6px}
.p1{background:#3d1515;color:#f87171}.p2{background:#3d2b10;color:#fb923c}
.p3{background:#1a2d4a;color:#60a5fa}.p4,.p5{background:#1f1f1f;color:#888}
.btype{background:#1a1a1a;color:#666}.conf{font-size:11px;color:#555}
.etitle{font-size:13px;font-weight:500;color:#ccc}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 20px;font-size:13px;font-weight:500;border:1px solid #333;border-radius:8px;background:transparent;color:#e5e5e5;cursor:pointer}
.btn:hover{background:#1a1a1a}.btn:disabled{opacity:.4;cursor:not-allowed}
.result{margin-top:1.5rem}
.pb{background:#111;border-left:3px solid #4f6ef7;border-radius:0 8px 8px 0;padding:14px 16px;margin-bottom:10px}
.pk{font-size:10px;font-weight:600;color:#4f6ef7;letter-spacing:.1em;text-transform:uppercase;margin-bottom:5px}
.pv{font-size:13px;color:#ccc;line-height:1.7}
.spin{display:inline-block;width:13px;height:13px;border:2px solid #333;border-top-color:#4f6ef7;border-radius:50%;animation:s .7s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
.copy{font-size:11px;color:#4f6ef7;background:none;border:none;cursor:pointer;margin-top:8px}
.lbl{font-size:10px;font-weight:600;color:#555;letter-spacing:.08em;text-transform:uppercase;margin-bottom:10px}
</style>
</head>
<body>
<h1>CORE &mdash; Evolution Review</h1>
<p class="sub">Translate pending evolution entries into structured WHAT / WHY / WHERE / HOW prompts</p>
<p class="lbl">Pending evolutions</p>
<div class="list" id="list"><p style="color:#555;font-size:13px">Loading...</p></div>
<div id="ta" style="display:none">
  <button class="btn" id="btn" onclick="go()">Translate to structured prompt</button>
</div>
<div class="result" id="res" style="display:none"></div>
<script>
let evos=[],sel=null;
async function load(){
  try{
    const r=await fetch('/api/evolutions');
    const d=await r.json();
    evos=d.evolutions||[];
    render();
  }catch(e){
    document.getElementById('list').innerHTML='<p style="color:#f87171;font-size:13px">Error: '+e.message+'</p>';
  }
}
function render(){
  const el=document.getElementById('list');
  if(!evos.length){el.innerHTML='<p style="color:#555;font-size:13px">No pending evolutions.</p>';return;}
  el.innerHTML=evos.map(e=>{
    const p=(e.change_summary||'').match(/P(\\d)/)?.[1]||'3';
    const t=(e.change_summary||'').replace(/\\[.*?\\]/g,'').replace(/^\\s*:\\s*/,'').trim().slice(0,80);
    return '<div class="card" id="c'+e.id+'" onclick="pick('+e.id+')"><div class="meta"><span class="badge p'+p+'">P'+p+'</span><span class="badge btype">'+e.change_type+'</span><span class="conf">conf: '+(e.confidence||0).toFixed(2)+'</span></div><div class="etitle">#'+e.id+' &mdash; '+(t||e.change_summary?.slice(0,80)||'unnamed')+'</div></div>';
  }).join('');
}
function pick(id){
  document.querySelectorAll('.card').forEach(c=>c.classList.remove('sel'));
  const c=document.getElementById('c'+id);if(c)c.classList.add('sel');
  sel=id;
  document.getElementById('ta').style.display='block';
  document.getElementById('res').style.display='none';
}
async function go(){
  const evo=evos.find(e=>e.id===sel);if(!evo)return;
  const btn=document.getElementById('btn');
  btn.disabled=true;btn.innerHTML='<span class="spin"></span> Translating...';
  const res=document.getElementById('res');res.style.display='none';
  const sys=`You are CORE's evolution analyst. Translate a raw evolution entry into a structured prompt.\nOutput MUST be valid JSON:\n{"what":"1-2 sentences","why":"1-2 sentences","where":"which component","how":"2-4 concrete steps","expected_outcome":"1 sentence"}\nOutput ONLY valid JSON, no preamble.`;
  const usr="Evolution ID: "+evo.id+"\\nType: "+evo.change_type+"\\nSummary: "+evo.change_summary+"\\nConfidence: "+evo.confidence+"\\nTranslate this evolution.";
  try{
    const r=await fetch('https://api.anthropic.com/v1/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:'claude-sonnet-4-20250514',max_tokens:1000,system:sys,messages:[{role:'user',content:usr}]})});
    const d=await r.json();
    const raw=d.content?.find(b=>b.type==='text')?.text||'{}';
    const p=JSON.parse(raw.replace(/```json|```/g,'').trim());
    const fields=[{k:'WHAT',v:p.what},{k:'WHY',v:p.why},{k:'WHERE',v:p.where},{k:'HOW',v:p.how},{k:'EXPECTED OUTCOME',v:p.expected_outcome}];
    const full=fields.map(f=>f.k+':\\n'+f.v).join('\\n\\n');
    res.innerHTML='<p class="lbl" style="margin-bottom:12px">Structured prompt - Evolution #'+evo.id+'</p>'+fields.map(f=>'<div class="pb"><div class="pk">'+f.k+'</div><div class="pv">'+(f.v||'&mdash;')+'</div></div>').join('')+'<button class="copy" onclick="cp(this,`'+full.replace(/`/g,'\\u0060')+'`)">Copy as text</button>';
    res.style.display='block';
  }catch(e){
    res.innerHTML='<p style="color:#f87171;font-size:13px;margin-top:1rem">Error: '+e.message+'</p>';
    res.style.display='block';
  }
  btn.disabled=false;btn.innerHTML='Translate to structured prompt';
}
function cp(btn,t){navigator.clipboard.writeText(t).then(()=>{btn.textContent='Copied!';setTimeout(()=>btn.textContent='Copy as text',1500);})}
load();
</script>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=200)


@app.get("/api/evolutions")
def api_evolutions():
    rows = sb_get(
        "evolution_queue",
        "select=id,status,change_type,change_summary,confidence,pattern_key,created_at"
        "&status=eq.pending&id=gt.1&order=created_at.desc&limit=50",
        svc=True,
    )
    return {"evolutions": rows, "count": len(rows)}


@app.post("/patch")
async def patch_file(body: PatchRequest):
    if body.secret != MCP_SECRET:
        raise HTTPException(401, "Invalid secret")
    from core_tools import t_gh_search_replace
    from core_config import GITHUB_REPO
    result = t_gh_search_replace(
        path=body.path, old_str=body.old_str, new_str=body.new_str,
        message=body.message, repo=body.repo or GITHUB_REPO
    )
    if result.get("ok"):
        notify(f"Patch applied: `{body.path}`\n{body.message[:100]}")
    return result


@app.post("/mcp/sse")
async def mcp_post(req: Request):
    secret = req.headers.get("X-MCP-Secret", "") or req.query_params.get("secret", "")
    if not secret:
        auth = req.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            secret = auth[7:]
    if secret and secret != MCP_SECRET:
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Unauthorized"}},
            status_code=401
        )
    try:
        body = await req.json()
    except:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400
        )
    if isinstance(body, list):
        return JSONResponse([r for item in body if (r := handle_jsonrpc(item)) is not None])
    response = handle_jsonrpc(body)
    if response is None:
        return JSONResponse({}, status_code=204)
    if "text/event-stream" in req.headers.get("accept", ""):
        async def sse_single():
            yield f"data: {json.dumps(response)}\n\n"
        return StreamingResponse(
            sse_single(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "mcp-session-id": str(uuid.uuid4())}
        )
    return JSONResponse(response)


@app.get("/mcp/sse")
async def mcp_sse_get(req: Request):
    secret = req.headers.get("X-MCP-Secret", "") or req.query_params.get("secret", "")
    if not secret:
        auth = req.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            secret = auth[7:]
    if secret and secret != MCP_SECRET:
        raise HTTPException(401, "Unauthorized")
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sse_sessions[session_id] = queue

    async def event_stream():
        try:
            yield f"event: endpoint\ndata: {json.dumps(f'/mcp/messages?session_id={session_id}')}\n\n"
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
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "X-Session-Id": session_id}
    )


@app.post("/mcp/messages")
async def mcp_messages(req: Request):
    session_id = req.query_params.get("session_id", "")
    secret = req.headers.get("X-MCP-Secret", "") or req.query_params.get("secret", "")
    if not secret:
        auth = req.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            secret = auth[7:]
    if secret and secret != MCP_SECRET:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await req.json()
    except:
        return JSONResponse({"error": "Parse error"}, status_code=400)
    response = handle_jsonrpc(body)
    if session_id and session_id in _sse_sessions:
        if response is not None:
            await _sse_sessions[session_id].put(response)
        return JSONResponse({"ok": True}, status_code=202)
    return JSONResponse(response) if response else JSONResponse({}, status_code=204)


@app.post("/mcp/startup")
async def mcp_startup(body: Handshake, req: Request):
    if body.secret != MCP_SECRET:
        raise HTTPException(401, "Invalid secret")
    from core_tools import t_state, t_health, t_constitution
    tok = mcp_new(req.client.host)
    resume = get_resume_task()
    notify(f"MCP Session\nClient: {body.client_id}\nToken: {tok[:8]}...")
    return {
        "session_token": tok,
        "expires_hours": SESSION_TTL_H,
        "state": t_state(),
        "health": t_health(),
        "constitution": t_constitution(),
        "tools": list(TOOLS.keys()),
        "note": f"CORE v6.0 ready. {resume}",
    }


@app.post("/mcp/auth")
async def mcp_auth(body: Handshake, req: Request):
    if body.secret != MCP_SECRET:
        notify(f"Invalid MCP auth from {req.client.host}")
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
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "tool": body.tool, "error": str(e)}


@app.get("/mcp/tools")
def list_tools():
    return {n: {"perm": t["perm"], "args": t["args"]} for n, t in TOOLS.items()}


@app.get("/debug/sim")
def debug_sim():
    """Patch _run_simulation_batch to expose the raw Groq response + actual failure point."""
    import traceback, json as _json
    from core_config import groq_chat, GROQ_MODEL, sb_get, SUPABASE_URL, _sbh_count_svc
    import httpx as _hx
    diag = {}
    try:
        from core_tools import TOOLS
        tool_list = list(TOOLS.keys())
    except Exception:
        tool_list = []
    try:
        mistakes = sb_get("mistakes", "select=domain,what_failed&order=id.desc&limit=10", svc=True)
        kb_sample = sb_get("knowledge_base", "select=domain,topic&order=id.desc&limit=20", svc=True)
        failure_modes = "\n".join([f"- [{r.get('domain','?')}] {r.get('what_failed','')[:120]}" for r in mistakes]) or "None recorded yet."
        kb_domains = list({r.get("domain", "general") for r in kb_sample})
        kb_topics_sample = [r.get("topic", "") for r in kb_sample[:10]]
        try:
            kc = _hx.get(f"{SUPABASE_URL}/rest/v1/knowledge_base?select=id&limit=1", headers=_sbh_count_svc(), timeout=8)
            kb_total = int(kc.headers.get("content-range", "*/0").split("/")[-1])
        except Exception as ke:
            kb_total = len(kb_sample)
            diag["kb_count_err"] = str(ke)
        runtime_context = (f"CORE MCP tools ({len(tool_list)}): {', '.join(tool_list[:20])}\n"
            f"KB total entries: {kb_total}\nKB domains: {', '.join(kb_domains)}\n"
            f"Known failure modes:\n{failure_modes}\nSample KB topics: {', '.join(kb_topics_sample)}")
        system = """You are simulating 1,000,000 users of CORE - a personal AGI orchestration system.
Output MUST be valid JSON:\n{\n  \"domain\": \"code|db|bot|mcp|training|kb|general\",\n  \"patterns\": [\"pattern1\", \"pattern2\"],\n  \"gaps\": \"1-2 sentences\",\n  \"summary\": \"1 sentence\"\n}\nOutput ONLY valid JSON, no preamble."""
        user = f"{runtime_context}\n\nSimulate 1,000,000 users. What patterns emerge?"
        diag["prompt_len"] = len(user)
        raw = groq_chat(system, user, model=GROQ_MODEL, max_tokens=900)
        diag["raw"] = raw[:1000]
        raw2 = raw.strip()
        if raw2.startswith("```"): raw2 = raw2.split("```")[1]
        if raw2.startswith("json"): raw2 = raw2[4:]
        diag["raw_after_strip"] = raw2[:500]
        result = _json.loads(raw2.strip())
        diag["parsed_ok"] = True
        diag["patterns"] = result.get("patterns", [])
        from core_config import sb_post
        post_ok = sb_post("hot_reflections", {
            "task_summary": "debug sim test",
            "domain": result.get("domain", "general"),
            "new_patterns": result.get("patterns", []),
            "gaps_identified": [result.get("gaps")] if result.get("gaps") else None,
            "reflection_text": result.get("summary", ""),
            "processed_by_cold": 0, "source": "simulation",
            "quality_score": 0.6,
        })
        diag["sb_post_ok"] = post_ok
    except Exception as e:
        diag["error"] = str(e)
        diag["trace"] = traceback.format_exc()
    return diag


@app.get("/debug/real")
def debug_real():
    """Run real signal extraction synchronously and return full result for diagnosis."""
    import traceback
    try:
        from core_train import _extract_real_signal
        ok = _extract_real_signal()
        return {"ok": ok, "error": None}
    except Exception as e:
        return {"ok": False, "error": str(e), "trace": traceback.format_exc()}


# -- Listen mode background state -------------------------------------------
_listen_job: dict = {}   # {id, status, chunks, started_at, stopped_at, stop_reason}
_listen_lock = threading.Lock()

def _run_listen_job(job_id: str):
    """Background thread: drain listen_stream(), accumulate chunks, update _listen_job."""
    from core_train import listen_stream
    chunks = []
    try:
        for chunk in listen_stream():
            with _listen_lock:
                _listen_job["chunks"] = _listen_job.get("chunks", []) + [chunk]
                parsed = {}
                try: parsed = json.loads(chunk) if isinstance(chunk, str) else chunk
                except Exception: pass
                if parsed.get("type") == "stop":
                    _listen_job["status"] = "done"
                    _listen_job["stop_reason"] = parsed.get("reason", "unknown")
                    _listen_job["stopped_at"] = datetime.utcnow().isoformat()
                    print(f"[LISTEN] job={job_id} done reason={parsed.get('reason')}")
                    return
    except Exception as e:
        print(f"[LISTEN] job={job_id} error: {e}")
        with _listen_lock:
            _listen_job["status"] = "error"
            _listen_job["error"] = str(e)
            _listen_job["stopped_at"] = datetime.utcnow().isoformat()


@app.get("/listen")
async def listen_mode(req: Request):
    """LISTEN MODE: start background listen job, return job_id immediately.
    Poll GET /listen/status for results. Replaces blocking StreamingResponse.
    Auth: X-MCP-Secret header required.
    """
    global _listen_job
    secret = req.headers.get("X-MCP-Secret", "")
    if secret != MCP_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    with _listen_lock:
        if _listen_job.get("status") == "running":
            return JSONResponse({"ok": True, "job_id": _listen_job["id"], "status": "running", "note": "already running"})
        job_id = str(uuid.uuid4())[:8]
        _listen_job = {"id": job_id, "status": "running", "chunks": [], "started_at": datetime.utcnow().isoformat(), "stop_reason": None}

    t = threading.Thread(target=_run_listen_job, args=(job_id,), daemon=True)
    t.start()
    print(f"[LISTEN] Started job={job_id}")
    return JSONResponse({"ok": True, "job_id": job_id, "status": "running"})


@app.get("/listen/status")
async def listen_status(req: Request):
    """Poll listen job status. Returns current chunks + done/running/error status."""
    secret = req.headers.get("X-MCP-Secret", "")
    if secret != MCP_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    with _listen_lock:
        job = dict(_listen_job)
    chunks = job.get("chunks", [])
    cold_runs = [c for c in chunks if isinstance(c, dict) and c.get("type") == "cold_run"]
    return JSONResponse({
        "ok": True,
        "job_id": job.get("id"),
        "status": job.get("status", "idle"),
        "stop_reason": job.get("stop_reason"),
        "started_at": job.get("started_at"),
        "stopped_at": job.get("stopped_at"),
        "chunk_count": len(chunks),
        "cycles": len(cold_runs),
        "total_patterns_found": sum(c.get("patterns_found", 0) for c in cold_runs),
        "total_evolutions_queued": sum(c.get("evolutions_queued", 0) for c in cold_runs),
        "chunks": chunks,
    })


# -- Backfill job background state -------------------------------------------
_backfill_job: dict = {}   # {id, status, inserted, checked, started_at, stopped_at, error}
_backfill_lock = threading.Lock()

def _run_backfill_job(job_id: str, batch_size: int):
    """Background thread: run _backfill_patterns(), update _backfill_job."""
    from core_train import _backfill_patterns
    try:
        inserted = _backfill_patterns(batch_size=batch_size)
        with _backfill_lock:
            _backfill_job["status"] = "done"
            _backfill_job["inserted"] = inserted
            _backfill_job["stopped_at"] = datetime.utcnow().isoformat()
        print(f"[BACKFILL-JOB] job={job_id} done inserted={inserted}")
    except Exception as e:
        print(f"[BACKFILL-JOB] job={job_id} error: {e}")
        with _backfill_lock:
            _backfill_job["status"] = "error"
            _backfill_job["error"] = str(e)
            _backfill_job["stopped_at"] = datetime.utcnow().isoformat()


@app.get("/backfill")
async def backfill_start(req: Request, batch_size: int = 20):
    """Start backfill job in background. Returns job_id immediately. Poll /backfill/status."""
    global _backfill_job
    secret = req.headers.get("X-MCP-Secret", "")
    if secret != MCP_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    with _backfill_lock:
        if _backfill_job.get("status") == "running":
            return JSONResponse({"ok": True, "job_id": _backfill_job["id"], "status": "running", "note": "already running"})
        job_id = str(uuid.uuid4())[:8]
        _backfill_job = {"id": job_id, "status": "running", "inserted": 0, "started_at": datetime.utcnow().isoformat(), "stopped_at": None, "error": None}
    t = threading.Thread(target=_run_backfill_job, args=(job_id, batch_size), daemon=True)
    t.start()
    print(f"[BACKFILL-JOB] Started job={job_id} batch_size={batch_size}")
    return JSONResponse({"ok": True, "job_id": job_id, "status": "running", "batch_size": batch_size})


@app.get("/backfill/status")
async def backfill_status(req: Request):
    """Poll backfill job status. Returns inserted count + done/running/error."""
    secret = req.headers.get("X-MCP-Secret", "")
    if secret != MCP_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    with _backfill_lock:
        job = dict(_backfill_job)
    return JSONResponse({
        "ok": True,
        "job_id": job.get("id"),
        "status": job.get("status", "idle"),
        "inserted": job.get("inserted", 0),
        "started_at": job.get("started_at"),
        "stopped_at": job.get("stopped_at"),
        "error": job.get("error"),
    })


@app.post("/webhook")
async def webhook(req: Request):
    try:
        u = await req.json()
        if "message" in u:
            threading.Thread(target=handle_msg, args=(u["message"],), daemon=True).start()
    except Exception as e:
        print(f"[WEBHOOK] {e}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Telegram message handler
# ---------------------------------------------------------------------------
def handle_msg(msg):
    cid  = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "").strip()
    if not text and not msg.get("photo") and not msg.get("caption") and not msg.get("document") and not msg.get("audio") and not msg.get("voice") and not msg.get("video") and not msg.get("video_note") and not msg.get("sticker"):
        return
    # Strip bot username suffix from commands (e.g. /status@reinvagnarbot -> /status)
    if text.startswith("/") and "@" in text:
        text = text.split("@")[0]

    if text == "/start":
        counts = get_system_counts()
        resume = get_resume_task()
        notify(
            f"<b>CORE v6.0</b>\n{resume}\n"
            f"Knowledge: {counts.get('knowledge_base',0)} | Sessions: {counts.get('sessions',0)}\n\n"
            f"<b>Commands:</b>\n"
            f"/status \u2014 health check of all components\n"
            f"/tstatus \u2014 training pipeline detailed status\n"
            f"/project \u2014 list or prepare project context\n"
            f"/metrics \u2014 orchestrator performance stats\n"
            f"/p3status \u2014 phase 3 features status\n"
            f"/model \u2014 current AI model info\n"
            f"/refresh \u2014 clear session cache\n"
            f"/cancel \u2014 stop active loop\n\n"
            f"<b>Trading:</b>\n"
            f"/tradestatus \u2014 performance + graduation progress\n"
            f"/winrate \u2014 30d win rate summary\n"
            f"/decision \u2014 last LLM trading decision\n"
            f"/why \u2014 explain last decision\n"
            f"/market \u2014 latest market snapshot\n"
            f"/balance \u2014 paper account balance\n"
            f"/fullreport \u2014 detailed report with mistakes + patterns\n"
            f"/criteria \u2014 graduation criteria",
            cid
        )

    elif text == "/status":
        from core_tools import t_health
        h = t_health()
        counts = get_system_counts()
        resume = get_resume_task()
        notify(
            f"<b>Status</b>\n{resume}\n"
            f"Supabase: {h['components'].get('supabase')} | Groq: {h['components'].get('groq')}\n"
            f"Telegram: {h['components'].get('telegram')} | GitHub: {h['components'].get('github')}\n\n"
            f"KB: {counts.get('knowledge_base',0)} | Sessions: {counts.get('sessions',0)} | Mistakes: {counts.get('mistakes',0)}\n"
            f"MCP tools: {len(TOOLS)}\n\n"
            f"Use /tstatus for training pipeline details.",
            cid
        )

    elif text == "/tstatus":
        from core_tools import t_get_training_pipeline
        tp = t_get_training_pipeline()
        hot = tp.get("hot", {})
        cold = tp.get("cold", {})
        pat = tp.get("patterns", {})
        evo = tp.get("evolutions", {})
        qual = tp.get("quality", {})
        flags = tp.get("health_flags", [])
        pipeline_ok = tp.get("pipeline_ok", False)

        # Hot section
        last_real = hot.get("last_real")
        last_sim  = hot.get("last_simulation")
        hot_line  = f"Hot: {hot.get('total','?')} total | {hot.get('unprocessed','?')} unprocessed"
        real_line = f"  Last real: {last_real['ts']} | domain={last_real['domain']} | q={last_real['quality']}" if last_real else "  Last real: none"
        sim_line  = f"  Simulation: {hot.get('total_simulation',0)} entries" if hot.get("simulation_ok") else "  Simulation: \u26a0\ufe0f DEAD (0 entries)"

        # Cold section
        cold_ago  = f"{cold.get('last_run_mins_ago','?')}min ago" if cold.get("last_run_mins_ago") is not None else "never"
        cold_line = f"Cold: last={cold_ago} | hots={cold.get('last_hot_count',0)} | patterns={cold.get('last_patterns_found',0)} | evos={cold.get('last_evolutions_queued',0)}"
        thresh_line = f"  Threshold: {cold.get('threshold','?')} hots to trigger"

        # Patterns
        top = pat.get("top")
        pat_line  = f"Patterns: {pat.get('active_count',0)} active | {pat.get('stale_count',0)} stale"
        top_line  = f"  Top: \"{top['key'][:60]}\" (freq={top['freq']}, {top['domain']})" if top else "  Top: none"

        # Quality
        q_avg   = qual.get("7d_avg", "?")
        q_trend = qual.get("trend", "?")
        trend_icon = "\u2191" if q_trend == "improving" else ("\u2193" if q_trend == "declining" else "\u2192")
        qual_line = f"Quality 7d: avg={q_avg} {trend_icon} {q_trend} ({qual.get('sample_count',0)} samples)"

        # Evolution
        evo_line = f"Evolutions: {evo.get('pending',0)} pending | {evo.get('applied',0)} applied"

        # Health
        if flags:
            health_line = "\u26a0\ufe0f Issues: " + " | ".join(flags)
        else:
            health_line = "\u2705 Pipeline healthy"

        notify(
            f"<b>Training Pipeline Status</b>\n\n"
            f"{hot_line}\n{real_line}\n{sim_line}\n\n"
            f"{cold_line}\n{thresh_line}\n\n"
            f"{pat_line}\n{top_line}\n\n"
            f"{qual_line}\n"
            f"{evo_line}\n\n"
            f"{health_line}",
            cid
        )

    elif text.startswith("/project"):
        from core_tools import t_project_list, t_project_prepare
        parts = text.split()[1:]
        if not parts or parts[0] == "list":
            result = t_project_list()
            projects = result.get("projects", [])
            if projects:
                lines = [f"*{p['name']}* ({p['project_id']}) — {p['status']}" for p in projects]
                notify("*Projects:*\n" + "\n".join(lines), cid)
            else:
                notify("No projects registered. Use Claude Desktop to register first.", cid)
        else:
            ids = ",".join(parts)
            result = t_project_prepare(ids)
            prepared = result.get("prepared", [])
            if prepared:
                notify(f"Context prepared for: {', '.join(prepared)}. Open Claude Desktop to activate.", cid)
            else:
                notify(f"Could not prepare: {ids}. Check project IDs with /project list.", cid)

    # ── Trading Bot — read-only reporting commands ───────────────────────────
    elif text in ("/tradestatus", "/performance", "/stats"):
        rows = sb_get(
            "trading_positions",
            "status=eq.closed&order=closed_at.asc"
            "&select=id,symbol,strategy,capital_usd,realized_pnl_usd,"
            "total_funding_usd,opened_at,closed_at,close_reason"
        )
        if not rows:
            notify("📊 No completed trades yet.", cid)
        else:
            total_trades = len(rows)
            wins   = [p for p in rows if (p.get("realized_pnl_usd") or 0) > 0]
            losses = [p for p in rows if (p.get("realized_pnl_usd") or 0) <= 0]
            win_rate = len(wins) / total_trades if total_trades else 0
            total_pnl    = sum((p.get("realized_pnl_usd") or 0) for p in rows)
            gross_profit = sum((p.get("realized_pnl_usd") or 0) for p in wins)
            gross_loss   = abs(sum((p.get("realized_pnl_usd") or 0) for p in losses))
            profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else 0
            avg_win  = round(gross_profit / len(wins), 4)   if wins   else 0
            avg_loss = round(gross_loss   / len(losses), 4) if losses else 0
            rr_ratio = round(avg_win / avg_loss, 3) if avg_loss > 0 else 0
            # Drawdown
            cfg = sb_get("trading_config", "key=eq.max_capital_usdt&select=value")
            start_cap = float(cfg[0]["value"]) if cfg else 50.0
            equity = start_cap; peak = start_cap; max_dd = 0.0
            for p in rows:
                equity += (p.get("realized_pnl_usd") or 0)
                if equity > peak: peak = equity
                dd = (peak - equity) / peak if peak > 0 else 0
                if dd > max_dd: max_dd = dd
            # Days trading
            try:
                days = (
                    datetime.fromisoformat(rows[-1].get("closed_at","")[:10]) -
                    datetime.fromisoformat(rows[0].get("opened_at","")[:10])
                ).days + 1
            except Exception:
                days = 0
            # Confidence from decisions
            dec_rows = sb_get(
                "trading_decisions",
                "action_taken=in.(executed,owner_confirmed)&select=confidence,market_regime"
            )
            confidences = [float(d.get("confidence") or 0) for d in dec_rows]
            avg_conf = round(sum(confidences) / len(confidences), 3) if confidences else 0
            regimes = set(d.get("market_regime") for d in dec_rows if d.get("market_regime") and d.get("market_regime") != "uncertain")
            # Recent form
            recent = rows[-10:]
            recent_wins = sum(1 for p in recent if (p.get("realized_pnl_usd") or 0) > 0)
            recent_wr   = round(recent_wins / len(recent), 3) if recent else 0
            recent_pnl  = round(sum((p.get("realized_pnl_usd") or 0) for p in recent), 4)
            # Graduation criteria
            CRITERIA = {
                "win_rate": 0.80, "avg_confidence": 0.80, "min_trades": 30,
                "min_days": 20, "max_single_loss_pct": 0.10,
                "max_drawdown_pct": 0.15, "min_sharpe": 1.5,
                "min_regimes": 3, "min_profit_factor": 1.5,
            }
            max_single_loss_pct = max((abs(p.get("realized_pnl_usd") or 0) for p in losses), default=0) / start_cap if start_cap else 0
            checks = {
                "win_rate":       (win_rate,              CRITERIA["win_rate"],           ">="),
                "avg_confidence": (avg_conf,              CRITERIA["avg_confidence"],     ">="),
                "total_trades":   (total_trades,          CRITERIA["min_trades"],         ">="),
                "days_trading":   (days,                  CRITERIA["min_days"],           ">="),
                "max_single_loss":(max_single_loss_pct,   CRITERIA["max_single_loss_pct"],"<="),
                "max_drawdown":   (max_dd,                CRITERIA["max_drawdown_pct"],   "<="),
                "regimes_seen":   (len(regimes),          CRITERIA["min_regimes"],        ">="),
                "profit_factor":  (profit_factor,         CRITERIA["min_profit_factor"],  ">="),
            }
            def tick(k): v, t2, op = checks[k]; return "✅" if (v >= t2 if op == ">=" else v <= t2) else "❌"
            passed_count = sum(1 for k in checks if (lambda v,t2,op: v>=t2 if op==">=" else v<=t2)(*checks[k])) 
            ret_pct = round((equity - start_cap) / start_cap * 100, 2) if start_cap else 0
            notify(
                f"🤖 <b>CORE Trading — Performance Report</b>\n"
                f"<i>Paper Trading Mode</i>\n\n"
                f"📊 Graduation: {passed_count}/{len(checks)} criteria passed\n\n"
                f"<b>📈 Performance</b>\n"
                f"Capital: ${start_cap:.2f} → ${equity:.2f} ({ret_pct:+.2f}%)\n"
                f"Total P&L: ${total_pnl:+.4f}\n"
                f"Win rate: {win_rate:.1%} ({len(wins)}/{total_trades})\n"
                f"Avg confidence: {avg_conf:.2f}\n"
                f"Reward/Risk: {rr_ratio:.2f}x | PF: {profit_factor:.2f}x\n"
                f"Days active: {days}\n\n"
                f"<b>📉 Risk</b>\n"
                f"Max drawdown: {max_dd:.1%}\n"
                f"Max single loss: {max_single_loss_pct:.1%}\n\n"
                f"<b>🎯 Recent (last 10)</b>\n"
                f"Win rate: {recent_wr:.1%} | P&L: ${recent_pnl:+.4f}\n"
                f"Regimes seen: {', '.join(regimes) or 'none'}\n\n"
                f"<b>✅ Graduation Checklist</b>\n"
                f"{tick('win_rate')} Win rate: {win_rate:.1%} (need ≥80%)\n"
                f"{tick('avg_confidence')} Avg confidence: {avg_conf:.2f} (need ≥0.80)\n"
                f"{tick('total_trades')} Trades: {total_trades}/30 min\n"
                f"{tick('days_trading')} Days: {days}/20 min\n"
                f"{tick('profit_factor')} Profit factor: {profit_factor:.2f}x (need ≥1.5x)\n"
                f"{tick('max_drawdown')} Max drawdown: {max_dd:.1%} (need ≤15%)\n"
                f"{tick('max_single_loss')} Max single loss: {max_single_loss_pct:.1%} (need ≤10%)\n"
                f"{tick('regimes_seen')} Regimes seen: {len(regimes)}/3 min",
                cid
            )

    elif text == "/winrate":
        cutoff = (datetime.utcnow() - timedelta(days=30)).date().isoformat()
        rows = sb_get(
            "trading_pnl_daily",
            f"date=gte.{cutoff}&order=date.desc"
            "&select=date,total_pnl_usd,win_count,loss_count"
        )
        if not rows:
            notify("📈 No completed trades yet.", cid)
        else:
            total_pnl = sum((r.get("total_pnl_usd") or 0) for r in rows)
            wins      = sum((r.get("win_count") or 0) for r in rows)
            losses    = sum((r.get("loss_count") or 0) for r in rows)
            trades    = wins + losses
            wr_str    = f"{wins/trades:.0%}" if trades else "N/A"
            notify(
                f"📈 <b>Win Rate (last 30d)</b>\n\n"
                f"Total P&L: ${total_pnl:+.4f}\n"
                f"Trades: {trades} (W:{wins} L:{losses})\n"
                f"Win rate: {wr_str}",
                cid
            )

    elif text == "/decision":
        rows = sb_get(
            "trading_decisions",
            "order=created_at.desc&limit=1"
            "&select=strategy,symbol,confidence,action_taken,"
            "market_regime,reasoning,created_at"
        )
        if not rows:
            notify("🧠 No decisions recorded yet.", cid)
        else:
            d = rows[0]
            notify(
                f"🧠 <b>Last Decision</b>\n\n"
                f"Time: {(d.get('created_at') or '')[:16]}\n"
                f"Regime: {d.get('market_regime','?')}\n"
                f"Strategy: {d.get('strategy','?')}\n"
                f"Symbol: {d.get('symbol') or 'none'}\n"
                f"Confidence: {float(d.get('confidence') or 0):.0%}\n"
                f"Action: {d.get('action_taken','?')}\n\n"
                f"<b>Reasoning:</b>\n{d.get('reasoning','?')}",
                cid
            )

    elif text == "/why":
        rows = sb_get(
            "trading_decisions",
            "order=created_at.desc&limit=1"
            "&select=strategy,reasoning,created_at"
        )
        if not rows:
            notify("💭 No decisions recorded yet.", cid)
        else:
            d = rows[0]
            notify(
                f"💭 <b>Why {d.get('strategy','?')}?</b>\n\n"
                f"{d.get('reasoning','No reasoning available.')}",
                cid
            )

    elif text == "/market":
        rows = sb_get(
            "market_snapshots",
            "order=recorded_at.desc&limit=4"
            "&select=symbol,price,price_change_24h,funding_rate,"
            "fear_greed,btc_dominance,recorded_at"
        )
        if not rows:
            notify("📊 No market data yet.", cid)
        else:
            ts  = (rows[0].get("recorded_at") or "")[:16]
            fg  = rows[0].get("fear_greed")
            btd = rows[0].get("btc_dominance")
            lines = [f"📊 <b>Market Snapshot</b> ({ts})\n"]
            if fg:  lines.append(f"Fear &amp; Greed: {fg}/100")
            if btd: lines.append(f"BTC Dominance: {btd}%")
            lines.append("")
            for r in rows:
                fr  = r.get("funding_rate") or 0
                prc = r.get("price") or 0
                chg = r.get("price_change_24h") or 0
                lines.append(
                    f"<b>{r['symbol']}</b>: ${prc:,.2f} ({chg:+.2f}%)\n"
                    f"  Funding: {fr*100:.4f}%/8h"
                )
            notify("\n".join(lines), cid)

    elif text == "/balance":
        cfg = sb_get("trading_config", "key=eq.max_capital_usdt&select=value")
        starting = float(cfg[0]["value"]) if cfg else 50.0
        open_rows   = sb_get("trading_positions", "status=eq.open&select=capital_usd")
        closed_rows = sb_get("trading_positions", "status=eq.closed&select=realized_pnl_usd")
        locked   = sum((r.get("capital_usd") or 0) for r in open_rows)
        realized = sum((r.get("realized_pnl_usd") or 0) for r in closed_rows)
        available = starting - locked + realized
        notify(
            f"💰 <b>Account Balance (Paper)</b>\n\n"
            f"Starting capital: ${starting:.2f}\n"
            f"Locked in positions: ${locked:.2f}\n"
            f"Realized P&amp;L: ${realized:+.4f}\n"
            f"Available: ${available:.2f}",
            cid
        )

    elif text == "/fullreport":
        # Performance summary (reuse tradestatus logic abbreviated)
        rows = sb_get(
            "trading_positions",
            "status=eq.closed&select=realized_pnl_usd,total_funding_usd,symbol,strategy"
        )
        total_pnl = sum((p.get("realized_pnl_usd") or 0) for p in rows)
        wins  = sum(1 for p in rows if (p.get("realized_pnl_usd") or 0) > 0)
        wr    = round(wins / len(rows), 3) if rows else 0
        # Mistakes
        mistakes = sb_get(
            "trading_mistakes",
            "order=created_at.desc&limit=5"
            "&select=what_failed,how_to_avoid,severity,created_at"
        )
        mistake_lines = "\n".join(
            f"  [{m.get('severity','?')}] {(m.get('what_failed') or '')[:80]}"
            for m in mistakes
        ) or "  None logged yet"
        # Patterns
        patterns = sb_get(
            "trading_patterns",
            "order=win_rate.desc&limit=5"
            "&select=pattern_key,win_rate,win_count,total_count"
        )
        pattern_lines = "\n".join(
            f"  {p.get('pattern_key','?')}: {float(p.get('win_rate') or 0):.0%} ({p.get('win_count',0)}/{p.get('total_count',0)})"
            for p in patterns
        ) or "  None learned yet"
        notify(
            f"📋 <b>Full Trading Report</b>\n\n"
            f"<b>Summary</b>\n"
            f"Closed trades: {len(rows)} | Win rate: {wr:.1%}\n"
            f"Total P&amp;L: ${total_pnl:+.4f}\n\n"
            f"<b>Recent Mistakes Logged</b>\n{mistake_lines}\n\n"
            f"<b>Patterns Learned</b>\n{pattern_lines}",
            cid
        )

    elif text == "/criteria":
        notify(
            "🎓 <b>Graduation Criteria</b>\n\n"
            "Win rate ≥ 80%\n"
            "Avg confidence ≥ 0.80\n"
            "Min trades: 30\n"
            "Min days: 20\n"
            "Max single loss: 10% of capital\n"
            "Max drawdown: 15%\n"
            "Sharpe ratio ≥ 1.5\n"
            "Market regimes seen: 3\n"
            "Profit factor ≥ 1.5x\n\n"
            "Use /tradestatus to see your current progress.",
            cid
        )

    else:
        threading.Thread(target=handle_telegram_message, args=(msg,), daemon=True).start()
        
# ---------------------------------------------------------------------------
# Background pollers
# ---------------------------------------------------------------------------
def queue_poller():
    """Notify-only mode — no auto-execution without owner approval.
    Only notifies NEW tasks (created in last 5 minutes) to avoid spam on redeploy."""
    print("[QUEUE] Started - notify-only mode (no auto-execution)")
    _notified: set = set()
    while True:
        try:
            # Only fetch tasks created in last 5 minutes — avoids spam on redeploy
            cutoff = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
            tasks = sb_get("task_queue", f"status=eq.pending&created_at=gte.{cutoff}&order=priority.asc&limit=5")
            if tasks:
                for t in tasks:
                    tid = t["id"]
                    if tid in _notified:
                        continue
                    task_text = t.get("task", "")[:200]
                    priority = t.get("priority", 0)
                    source = t.get("source", "unknown")
                    notify(
                        f"Pending task (P{priority}) from {source}:\n"
                        f"`{task_text}`\n"
                        f"ID: `{tid}`\n"
                        f"Review via Claude Desktop → task_queue"
                    )
                    _notified.add(tid)
                    if len(_notified) > 200:
                        _notified.clear()
        except Exception as e:
            print(f"[QUEUE] {e}")
        time.sleep(60)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def polling_loop():
    """Poll Telegram getUpdates every 2s. No webhook/HTTPS needed for VM."""
    from core_config import TELEGRAM_TOKEN
    print("[POLLING] Telegram polling loop started")
    try:
        httpx.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook", timeout=10)
        print("[POLLING] Webhook deleted — polling mode active")
    except Exception as e:
        print(f"[POLLING] deleteWebhook warning: {e}")
    offset = 0
    while True:
        try:
            r = httpx.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"timeout": 10, "offset": offset},
                timeout=15,
            )
            if r.is_success:
                updates = r.json().get("result", [])
                for update in updates:
                    offset = update["update_id"] + 1
                    msg = update.get("message") or update.get("edited_message")
                    if msg:
                        try:
                            threading.Thread(target=handle_msg, args=(msg,), daemon=True).start()
                        except Exception as e:
                            print(f"[POLLING] handle_msg error: {e}")
        except Exception as e:
            print(f"[POLLING] getUpdates error: {e}")
        time.sleep(2)


def on_start():
    threading.Thread(target=polling_loop, daemon=True).start()
    threading.Thread(target=queue_poller, daemon=True).start()
    threading.Thread(target=cold_processor_loop, daemon=True).start()
    # self_sync_check disabled -- CORE_SELF.md is tombstoned, superseded by system_map
    threading.Thread(target=background_researcher, daemon=True).start()
    threading.Thread(target=evolution_tier_processor, daemon=True).start()
    threading.Thread(target=proactive_surface_loop,   daemon=True).start()  # P2-04
    startup_v2()
    # start_orchestrator()
    counts = get_system_counts()
    resume = get_resume_task()
    evo_pending  = counts.get('evolution_pending', 0)
    evo_applied  = counts.get('evolution_applied', 0)
    evo_rejected = counts.get('evolution_rejected', 0)
    # Show in_progress tasks brief
    try:
        in_progress = sb_get(
            "task_queue",
            "select=task,priority,status&source=in.(core_v6_registry,mcp_session)"
            "&status=eq.in_progress&order=priority.desc&limit=3"
        ) or []
        if in_progress:
            lines = []
            for t in in_progress:
                raw = t.get("task", "")
                try:
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                    title = parsed.get("title") or str(parsed)[:60]
                except Exception:
                    title = str(raw)[:60]
                lines.append(f"  ▶ {title} (P{t.get('priority','?')})")
            task_line = "In progress:\n" + "\n".join(lines)
        else:
            task_line = ""  # already shown in resume line above
    except Exception as e:
        task_line = f"Tasks: unavailable ({e})"
    evo_line = f"Evolutions — pending: {evo_pending} | applied: {evo_applied} | rejected: {evo_rejected}"
    notify(
        f"<b>CORE Online</b>\n{resume}\n"
        f"KB: {counts.get('knowledge_base',0)} | Mistakes: {counts.get('mistakes',0)} | Sessions: {counts.get('sessions',0)}\n"
        f"MCP: {len(TOOLS)} tools\n"
        f"{evo_line}"
    )
    print(f"[CORE] v8.0 online :{PORT} - {resume}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("core_main:app", host="0.0.0.0", port=PORT, reload=False)
