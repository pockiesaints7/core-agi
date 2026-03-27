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
import secrets
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Header, Query, Depends, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from core_config import (
    MCP_SECRET, MCP_PROTOCOL_VERSION, PORT, SESSION_TTL_H,
    SUPABASE_URL, COLD_KB_GROWTH_THRESHOLD,
    TELEGRAM_CHAT, TELEGRAM_WEBHOOK_SECRET,
    L, sb_get, sb_post, sb_patch, sb_upsert, sb_post_critical,
    _sbh, _sbh_count_svc, groq_chat,
)
from core_github import gh_read, gh_write, notify, set_webhook
from core_train import cold_processor_loop, background_researcher
from core_tools import TOOLS, handle_jsonrpc

# ── Orchestrator v2 ───────────────────────────────────────────────────────────
from core_orch_main import handle_telegram_message_v2, startup_v2

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
        # No in_progress tasks -- check for pending
        pending = sb_get(
            "task_queue",
            "select=task,priority&source=in.(core_v6_registry,mcp_session)"
            "&status=eq.pending&order=priority.desc&limit=1"
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
    d = sb_get("sessions", "select=summary,actions,created_at&order=created_at.desc&limit=1")
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
    for evo_status in ("pending", "applied", "rejected"):
        try:
            r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/evolution_queue?select=id&limit=1&status=eq.{evo_status}",
                headers=_sbh_count_svc(), timeout=10
            )
            cr = r.headers.get("content-range", "*/0")
            counts[f"evolution_{evo_status}"] = int(cr.split("/")[-1]) if "/" in cr else 0
        except:
            counts[f"evolution_{evo_status}"] = -1
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


def _is_owner_chat(chat_id: str) -> bool:
    return bool(chat_id) and secrets.compare_digest(str(chat_id), str(TELEGRAM_CHAT))


def _telegram_webhook_ok(req: Request) -> bool:
    token = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    return bool(token) and secrets.compare_digest(str(token), str(TELEGRAM_WEBHOOK_SECRET))


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
app = FastAPI(title="CORE v6.0", version="6.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_sse_sessions: dict = {}


async def require_mcp_secret(
    x_mcp_secret: Optional[str] = Header(None, alias="X-MCP-Secret"),
    authorization: Optional[str] = Header(None),
    secret_query: Optional[str] = Query(None, alias="secret")
):
    token = x_mcp_secret or secret_query
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token or not secrets.compare_digest(str(token), str(MCP_SECRET)):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


def _git_commit() -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            text=True,
            timeout=5,
        ).strip()
    except Exception:
        return None


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _deployment_manifest() -> dict:
    base = Path(__file__).resolve().parent
    files = ["core_main.py", "core_orch_layer11.py", "core_meta_evaluator.py", "core_worker_critic.py", "core_worker_reflect.py"]
    manifest = {}
    for rel in files:
        path = base / rel
        manifest[rel] = {
            "sha256": _file_sha256(path),
            "bytes": path.stat().st_size,
        }
    return {
        "service": "core-agi",
        "pid": os.getpid(),
        "git_commit": _git_commit(),
        "cwd": str(base),
        "runtime_mode": {
            "port": PORT,
            "mcp_protocol_version": MCP_PROTOCOL_VERSION,
            "telegram_webhook_secret_configured": bool(TELEGRAM_WEBHOOK_SECRET),
            "mcp_secret_configured": bool(MCP_SECRET),
            "supabase_host": SUPABASE_URL.split("://", 1)[-1].split("/", 1)[0] if SUPABASE_URL else "",
        },
        "files": manifest,
        "generated_at": datetime.utcnow().isoformat(),
    }


def _trade_tokens(position_id: Optional[int], decision_id: Optional[int]) -> list[str]:
    tokens = []
    if position_id is not None:
        tokens.extend([f"Position ID: {position_id}", f"position_id={position_id}", f"_p{position_id}"])
    if decision_id is not None:
        tokens.extend([f"Decision ID: {decision_id}", f"decision_id={decision_id}", f"_d{decision_id}"])
    return tokens


def _matches_trade_trace(text: str, position_id: Optional[int], decision_id: Optional[int]) -> bool:
    haystack = str(text or "")
    if not haystack:
        return False
    matched = False
    for token in _trade_tokens(position_id, decision_id):
        if token in haystack:
            matched = True
            break
    if not matched:
        return False
    if position_id is not None and f"{position_id}" not in haystack:
        return False
    if decision_id is not None and f"{decision_id}" not in haystack:
        return False
    return True


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
async def ingest_knowledge_endpoint(req: IngestRequest, _auth=Depends(require_mcp_secret)):
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
                "select=created_at,task_summary&domain=eq.knowledge_ingestion&order=created_at.desc&limit=1",
                svc=True)
            if rows:
                last_ingest = {"ts": rows[0].get("created_at"), "summary": rows[0].get("task_summary", "")[:100]}
        except:
            pass

        return {"ok": True, "table_counts": counts, "last_ingest": last_ingest}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/")
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


@app.get("/ping")
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
  try{
    const r=await fetch('/api/translate-evolution',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:evo.id,change_type:evo.change_type,change_summary:evo.change_summary,confidence:evo.confidence})});
    const d=await r.json();
    if(!d.ok){throw new Error(d.error||'Backend error');}
    const raw=d.result||'{}';
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
def api_evolutions(_auth=Depends(require_mcp_secret)):
    rows = sb_get(
        "evolution_queue",
        "select=id,status,change_type,change_summary,confidence,pattern_key,diff_content,created_at"
        "&status=eq.pending&id=gt.1&order=created_at.desc&limit=50",
        svc=True,
    )
    return {"evolutions": rows, "count": len(rows)}


class TranslateRequest(BaseModel):
    id: int
    change_type: str = ""
    change_summary: str = ""
    confidence: float = 0.0


@app.post("/api/translate-evolution")
async def translate_evolution(body: TranslateRequest, _auth=Depends(require_mcp_secret)):
    """Server-side evolution translation using backend LLM (gemini_chat/groq_chat).
    Called by /review widget — replaces broken client-side Anthropic API call."""
    try:
        from core_config import gemini_chat
        system = (
            "You are CORE's evolution analyst. Translate a raw evolution entry into a structured prompt.\n"
            'Output MUST be valid JSON: {"what":"1-2 sentences","why":"1-2 sentences",'
            '"where":"which component","how":"2-4 concrete steps","expected_outcome":"1 sentence"}\n'
            "Output ONLY valid JSON, no preamble."
        )
        user = (
            f"Evolution ID: {body.id}\nType: {body.change_type}\n"
            f"Summary: {body.change_summary}\nConfidence: {body.confidence}\n"
            "Translate this evolution."
        )
        raw = gemini_chat(system=system, user=user, max_tokens=1000, json_mode=True)
        return {"ok": True, "result": raw}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/patch")
async def patch_file(body: PatchRequest):
    if not secrets.compare_digest(str(body.secret), str(MCP_SECRET)):
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


class TradingReflectionRequest(BaseModel):
    output_text: str
    context: dict = Field(default_factory=dict)


class EmbedRequest(BaseModel):
    text: str


@app.post("/internal/trading/reflect")
async def trading_reflect(body: TradingReflectionRequest, req: Request):
    """
    Called by core-trading-bot via core_bridge.fire_trading() after every trade close.
    Triggers the full CORE L11 pipeline: critic -> causal -> reflect -> meta evaluator.
    Auth: X-MCP-Secret header required.
    source='trading' so critic/causal/reflect know to treat this as trade outcome.
    """
    secret = req.headers.get("X-MCP-Secret", "")
    if not secrets.compare_digest(str(secret), str(MCP_SECRET)):
        raise HTTPException(status_code=401, detail="Unauthorized")

    from core_orch_layer11 import fire_trading
    context = body.context or {}
    trace_id = context.get("trace_id")
    position_id = context.get("position_id")
    decision_id = context.get("decision_id")
    if not body.output_text or len(body.output_text.strip()) < 20:
        raise HTTPException(status_code=400, detail="output_text is too short")
    if not trace_id or position_id in (None, "") or decision_id in (None, ""):
        raise HTTPException(
            status_code=400,
            detail="trace_id, position_id, and decision_id are required in context",
        )
    print(
        f"[TRADING_REFLECT] trace_id={trace_id} decision_id={decision_id} "
        f"position_id={position_id} queued"
    )
    fire_trading(body.output_text, context)

    return {
        "ok": True,
        "queued": True,
        "source": "trading",
        "trace_id": trace_id,
        "ts": datetime.utcnow().isoformat(),
    }


@app.get("/internal/trading/reflections/query")
async def trading_reflection_query(
    position_id: Optional[int] = Query(None),
    decision_id: Optional[int] = Query(None),
    _auth=Depends(require_mcp_secret),
):
    if position_id is None and decision_id is None:
        raise HTTPException(status_code=400, detail="position_id or decision_id is required")

    critiques = [
        row for row in sb_get(
            "output_critiques",
            "source=eq.trading&select=id,session_id,output_text,verdict,score,reason,failure_pattern,created_at"
            "&order=created_at.desc&limit=200",
            svc=True,
        )
        if _matches_trade_trace(row.get("output_text", ""), position_id, decision_id)
    ]
    session_ids = {row.get("session_id") for row in critiques if row.get("session_id")}

    causal_rows = [
        row for row in sb_get(
            "causal_chains",
            "source=eq.trading&select=session_id,root_knowledge,reasoning_type,confidence,created_at"
            "&order=created_at.desc&limit=200",
            svc=True,
        )
        if row.get("session_id") in session_ids
    ]
    reflection_rows = [
        row for row in sb_get(
            "output_reflections",
            "source=eq.trading&select=session_id,gap,gap_domain,new_behavior,evo_worthy,created_at"
            "&order=created_at.desc&limit=200",
            svc=True,
        )
        if row.get("session_id") in session_ids
    ]
    hot_rows = [
        row for row in sb_get(
            "hot_reflections",
            "domain=eq.trading&select=id,reflection_text,quality_score,created_at"
            "&order=created_at.desc&limit=200",
            svc=True,
        )
        if _matches_trade_trace(row.get("reflection_text", ""), position_id, decision_id)
    ]
    kb_rows = [
        row for row in sb_get(
            "knowledge_base",
            "domain=eq.trading&select=id,topic,content,confidence,created_at"
            "&order=created_at.desc&limit=200",
            svc=True,
        )
        if _matches_trade_trace(
            f"{row.get('topic', '')}\n{row.get('content', '')}",
            position_id,
            decision_id,
        )
    ]
    mistake_rows = [
        row for row in sb_get(
            "mistakes",
            "select=id,domain,what_failed,context,created_at&order=created_at.desc&limit=200",
            svc=True,
        )
        if _matches_trade_trace(
            f"{row.get('what_failed', '')}\n{row.get('context', '')}",
            position_id,
            decision_id,
        )
    ]

    return {
        "ok": True,
        "position_id": position_id,
        "decision_id": decision_id,
        "session_ids": sorted(session_ids),
        "counts": {
            "output_critiques": len(critiques),
            "causal_chains": len(causal_rows),
            "output_reflections": len(reflection_rows),
            "hot_reflections": len(hot_rows),
            "knowledge_base": len(kb_rows),
            "mistakes": len(mistake_rows),
        },
        "artifacts": {
            "output_critiques": critiques[:10],
            "causal_chains": causal_rows[:10],
            "output_reflections": reflection_rows[:10],
            "hot_reflections": hot_rows[:10],
            "knowledge_base": kb_rows[:10],
            "mistakes": mistake_rows[:10],
        },
    }


@app.get("/deployment-check")
async def deployment_check(_auth=Depends(require_mcp_secret)):
    return _deployment_manifest()


@app.post("/embed")
async def embed_text(body: EmbedRequest, _auth=Depends(require_mcp_secret)):
    from core_embeddings import _get_embedding

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    return {"ok": True, "embedding": _get_embedding(text)}

async def get_mcp_identity(
    x_mcp_secret: Optional[str] = Header(None, alias="X-MCP-Secret"),
    authorization: Optional[str] = Header(None),
    secret_query: Optional[str] = Query(None, alias="secret")
):
    """
    Centralized Auth. Securely extracts the secret from Headers or Query.
    """
    token = x_mcp_secret or secret_query
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    
    # Timing-attack resistant comparison against your core_config.MCP_SECRET
    if not token or not secrets.compare_digest(str(token), str(MCP_SECRET)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"jsonrpc": "2.0", "error": {"code": -32600, "message": "Unauthorized"}}
        )
    return True

# --- 2. Refactored Endpoints (Replace your existing ones) ---

@app.post("/mcp/sse")
async def mcp_post(req: Request, _auth=Depends(get_mcp_identity)):
    try:
        body = await req.json()
    except json.JSONDecodeError:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400
        )

    if isinstance(body, list):
        return JSONResponse([r for item in body if (r := handle_jsonrpc(item)) is not None])
    
    response = handle_jsonrpc(body, session_id=session_id)
    if response is None:
        return JSONResponse({}, status_code=204)

    # If client wants an SSE response for a single POST (standard MCP behavior)
    if "text/event-stream" in req.headers.get("accept", "").lower():
        async def sse_single():
            yield f"data: {json.dumps(response)}\n\n"
        return StreamingResponse(
            sse_single(), 
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache", 
                "X-Accel-Buffering": "no",
                "mcp-session-id": str(uuid.uuid4())
            }
        )
    return JSONResponse(response)

@app.get("/mcp/sse")
async def mcp_sse_get(req: Request, _auth=Depends(get_mcp_identity)):
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _sse_sessions[session_id] = queue

    async def event_stream():
        try:
            # Tell the client where to send POST messages
            yield f"event: endpoint\ndata: {json.dumps(f'/mcp/messages?session_id={session_id}')}\n\n"
            while True:
                if await req.is_disconnected():
                    break
                try:
                    # 25s heartbeat to keep Railway/Nginx connections alive
                    msg = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            _sse_sessions.pop(session_id, None)

    return StreamingResponse(
        event_stream(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache", 
            "X-Accel-Buffering": "no",
            "X-Session-Id": session_id
        }
    )

@app.post("/mcp/messages")
async def mcp_messages(req: Request, session_id: str = Query(...), _auth=Depends(get_mcp_identity)):
    try:
        body = await req.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Parse error"}, status_code=400)

    response = handle_jsonrpc(body, session_id=session_id)
    
    if session_id in _sse_sessions:
        if response is not None:
            await _sse_sessions[session_id].put(response)
        return JSONResponse({"ok": True}, status_code=202)
    
    return JSONResponse(response) if response else JSONResponse({}, status_code=204)

@app.post("/mcp/tool")
async def mcp_tool(body: ToolCall):
    # Keep using your custom mcp_ok check for session tokens
    if not mcp_ok(body.session_token):
        raise HTTPException(401, "Invalid/expired session")
    
    if not L.mcp(body.session_token):
        raise HTTPException(429, "Rate limit exceeded")
    
    # O(1) Tool lookup
    tool_data = TOOLS.get(body.tool)
    if not tool_data:
        raise HTTPException(404, f"Tool not found: {body.tool}")
        
    try:
        fn = tool_data["fn"]
        res = fn(**(body.args or {}))
        return {
            "ok": True, 
            "tool": body.tool, 
            "perm": tool_data.get("perm"), 
            "result": res
        }
    except Exception as e:
        # Structured error return instead of crashing
        return {"ok": False, "tool": body.tool, "error": str(e)}

@app.get("/mcp/tools")
def list_tools():
    # Modern dictionary comprehension
    return {n: {"perm": t.get("perm"), "args": t.get("args")} for n, t in TOOLS.items()}

@app.get("/debug/sim")
def debug_sim(_auth=Depends(require_mcp_secret)):
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
    _MAX_CHUNKS = 500  # cap to prevent OOM on long listen sessions
    try:
        for chunk in listen_stream():
            with _listen_lock:
                current = _listen_job.get("chunks", [])
                if len(current) < _MAX_CHUNKS:
                    current.append(chunk)
                _listen_job["chunks"] = current
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
    if not secrets.compare_digest(str(secret), str(MCP_SECRET)):
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
    if not secrets.compare_digest(str(secret), str(MCP_SECRET)):
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
    if not secrets.compare_digest(str(secret), str(MCP_SECRET)):
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
    if not secrets.compare_digest(str(secret), str(MCP_SECRET)):
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
        if not _telegram_webhook_ok(req):
            raise HTTPException(status_code=401, detail="Unauthorized")
        u = await req.json()
        keys = list(u.keys())
        print(f"[WEBHOOK] update keys={keys} update_id={u.get('update_id','?')}")
        if "message" in u:
            msg = u["message"]
            text = msg.get("text", "")
            cid = msg.get("chat", {}).get("id", "?")
            uname = msg.get("from", {}).get("username", "?")
            print(f"[WEBHOOK] message: chat_id={cid} user=@{uname} text={text!r:.80}")
            # Fire-and-forget in a thread — webhook must return 200 immediately
            threading.Thread(target=handle_msg, args=(msg,), daemon=True).start()
        else:
            # Non-message update (edited_message, callback_query, etc) — log and ignore
            print(f"[WEBHOOK] non-message update ignored: keys={keys}")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[WEBHOOK] error: {e}")
    return {"ok": True}  # Always return 200 immediately — never block here


# ---------------------------------------------------------------------------
# Telegram message handler
# ---------------------------------------------------------------------------
def handle_msg(msg):
    cid  = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "").strip()
    if not text:
        return
    if not _is_owner_chat(cid):
        print(f"[WEBHOOK] unauthorized chat rejected: chat_id={cid}")
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
            f"/status — health + system\n"
            f"/tstatus — training pipeline detail\n"
            f"/restart — restart CORE service (kills any stuck loop)\n"
            f"/kill — abort active agentic loop without restart\n"
            f"/project [list|id] — project context",
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

    elif text == "/restart":
        # Owner-only: restart the core-agi systemd service
        # Kills any running agentic loop immediately
        notify("🔄 Restarting CORE service...", cid)
        import subprocess as _sp
        import time as _time
        def _do_restart():
            _time.sleep(1)  # let the notify send first
            _sp.run(["sudo", "systemctl", "restart", "core-agi"], check=False)
        import threading as _thr
        _thr.Thread(target=_do_restart, daemon=True).start()
        # Note: no reply after this — service will be dead momentarily

    elif text == "/kill":
        # Owner-only: kill any running agentic loop without full restart
        # Finds the agentic loop task and marks it aborted, then notifies
        notify("🛑 Killing active agentic loop...", cid)
        try:
            import httpx as _hx
            from core_config import SUPABASE_URL, _sbh
            # Mark any active agentic sessions as aborted
            _hx.patch(
                f"{SUPABASE_URL}/rest/v1/agentic_sessions?status=eq.active",
                headers={**_sbh(True), "Prefer": "return=minimal"},
                json={"status": "aborted"},
                timeout=5
            )
            notify("✅ Loop killed (sessions marked aborted). Use /restart if CORE is stuck.", cid)
        except Exception as e:
            notify(f"⚠️ Kill failed: {e}", cid)

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

    else:
        # ── Orchestrator v2: all freeform messages routed through L0→L10 pipeline
        # Pass raw msg dict — core_orch_main wraps it into {"message": msg} internally.
        # Use sync wrapper (handle_telegram_message) — creates its own event loop safely.
        from core_orch_main import handle_telegram_message
        threading.Thread(
            target=handle_telegram_message,
            args=(msg,),
            daemon=True
        ).start()


# ---------------------------------------------------------------------------
# Background pollers
# ---------------------------------------------------------------------------
def queue_poller():
    """Notify-only mode — no auto-execution without owner approval.
    Polls task_queue for pending tasks and notifies owner via Telegram."""
    print("[QUEUE] Started - notify-only mode (no auto-execution)")
    _notified: set = set()
    while True:
        try:
            tasks = sb_get("task_queue", "status=eq.pending&order=priority.asc&limit=5")
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
# Deploy webhook — triggered by GitHub push to auto-pull + restart
# ---------------------------------------------------------------------------
@app.post("/deploy-webhook")
async def deploy_webhook(req: Request):
    """Auto-deploy: git pull latest from GitHub then restart core-agi service.
    Auth: X-MCP-Secret header (same secret as MCP).
    Call from GitHub Actions or manually after pushing code.
    Returns immediately — restart happens in background thread.
    """
    secret = req.headers.get("X-MCP-Secret", "")
    if not secrets.compare_digest(str(secret), str(MCP_SECRET)):
        raise HTTPException(status_code=401, detail="Unauthorized")

    def _do_deploy():
        import subprocess
        try:
            pull = subprocess.run(
                ["git", "pull", "origin", "main"],
                cwd="/home/ubuntu/core-agi",
                capture_output=True, text=True, timeout=60
            )
            print(f"[DEPLOY] git pull: {pull.stdout.strip()} {pull.stderr.strip()}")
            if pull.returncode != 0:
                notify(
                    f"⚠️ <b>CORE Auto-Deploy Failed</b>\n"
                    f"git pull returned {pull.returncode}\n{(pull.stderr or pull.stdout)[:300]}"
                )
                return
            notify(f"🚀 <b>CORE Auto-Deploy</b>\n{pull.stdout.strip() or 'already up to date'}")
            if "Already up to date" not in pull.stdout:
                restart = subprocess.run(
                    ["sudo", "-n", "systemctl", "restart", "core-agi.service"],
                    capture_output=True, text=True, timeout=15
                )
                if restart.returncode != 0:
                    notify(
                        f"⚠️ <b>CORE Auto-Deploy Restart Failed</b>\n"
                        f"{(restart.stderr or restart.stdout)[:300]}"
                    )
                    return
                current = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd="/home/ubuntu/core-agi",
                    capture_output=True, text=True, timeout=10
                )
                notify(
                    f"✅ <b>CORE Auto-Deploy Restarted</b>\n"
                    f"commit={current.stdout.strip()[:12] if current.returncode == 0 else 'unknown'}"
                )
        except Exception as e:
            print(f"[DEPLOY] error: {e}")
            notify(f"⚠️ CORE Deploy error: {e}")

    threading.Thread(target=_do_deploy, daemon=True).start()
    return {"ok": True, "status": "deploy_started"}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_start():
    set_webhook()
    startup_v2()  # ── Orchestrator v2 init
    # Auto-embed sync — patches sb_post to embed all semantic table inserts
    try:
        from core_embed_sync import install, install_critical
        install()
        install_critical()
    except Exception as _es_e:
        print(f"[EMBED_SYNC] install failed (non-fatal): {_es_e}")
    threading.Thread(target=queue_poller, daemon=True).start()
    threading.Thread(target=cold_processor_loop, daemon=True).start()
    # self_sync_check disabled -- CORE_SELF.md is tombstoned, superseded by system_map
    threading.Thread(target=background_researcher, daemon=True).start()
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
            pending = sb_get(
                "task_queue",
                "select=task,priority&source=in.(core_v6_registry,mcp_session)"
                "&status=eq.pending&order=priority.desc&limit=1"
            ) or []
            if pending:
                raw = pending[0].get("task", "")
                try:
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                    title = parsed.get("title") or str(parsed)[:60]
                except Exception:
                    title = str(raw)[:60]
                task_line = f"Next up: {title}"
            else:
                task_line = "No active tasks"
    except Exception as e:
        task_line = f"Tasks: unavailable ({e})"
    evo_line = f"Evolutions — pending: {evo_pending} | applied: {evo_applied} | rejected: {evo_rejected}"
    notify(
        f"<b>CORE Online</b>\n{resume}\n"
        f"KB: {counts.get('knowledge_base',0)} | Mistakes: {counts.get('mistakes',0)} | Sessions: {counts.get('sessions',0)}\n"
        f"MCP: {len(TOOLS)} tools\n"
        f"{evo_line}\n"
        f"{task_line}"
    )
    print(f"[CORE] v6.0 online :{PORT} - {resume}")


if __name__ == "__main__":
    import uvicorn
    # Tambahkan path sertifikat yang tadi sudah terdaftar di certbot
    uvicorn.run(
        "core_main:app", 
        host="0.0.0.0", 
        port=443,             # Pindah ke port 443
        reload=False,
        ssl_keyfile="/etc/letsencrypt/live/core-agi.duckdns.org/privkey.pem",
        ssl_certfile="/etc/letsencrypt/live/core-agi.duckdns.org/fullchain.pem"
    )
