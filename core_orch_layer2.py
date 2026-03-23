"""
core_orch_layer2.py — L2: MEMORY / CONTEXT HYDRATION
======================================================
Hydrates the session context from Supabase before reasoning begins.
Maintains conversation history (short-term), working memory (scratchpad),
and loads long-term context (behavioral_rules, knowledge_base, mistakes,
task_queue) into a SessionContext object passed downstream.

SessionContext structure:
  {
    "intent":           dict,          # Intent object from L1
    "behavioral_rules": list,          # active rules for this domain
    "recent_mistakes":  list,          # last N mistakes
    "kb_snippets":      list,          # relevant KB entries for this message
    "in_progress_tasks":list,          # tasks currently in_progress
    "conversation":     list,          # recent turns [{role, content, ts}]
    "working_memory":   dict,          # scratchpad for this conversation
    "owner_profile":    list,          # owner dimension profile (P3-02)
    "active_goals":     list,          # cross-session goals
    "hydrated_at":      float,
    "fast_path":        bool           # True = trivial, skip heavy brain
  }

TOMBSTONED TABLES — never query:
  playbook, memory, master_prompt, patterns, training_sessions,
  training_sessions_v2, training_flags, session_learning, agent_registry,
  knowledge_blocks, agi_mistakes, stack_registry, vault_logs, vault
"""

import json
import time
import threading
from collections import deque
from datetime import datetime
from typing import Optional

from core_config import sb_get, SUPABASE_URL
from core_config import _sbh

# ── Short-term conversation buffer (in-memory, per chat_id) ──────────────────
_conv_memory: dict = {}          # cid → deque[{role, content, ts}]
_conv_lock          = threading.Lock()
MAX_HISTORY         = 20
COMPRESS_AT         = 12

# ── Working memory (scratchpad, per chat_id) ─────────────────────────────────
_working_mem: dict = {}          # cid → {key: value}
_wm_lock            = threading.Lock()

# ── Session context cache (avoid re-hydrating on every message) ──────────────
_ctx_cache: dict    = {}         # cid → {ctx, loaded_at}
_ctx_lock           = threading.Lock()
CTX_CACHE_TTL       = 600        # 10 min — re-hydrate if stale

# ── Tombstoned tables — guard against accidental queries ─────────────────────
_TOMBSTONED = {
    "playbook", "memory", "master_prompt", "patterns", "training_sessions",
    "training_sessions_v2", "training_flags", "session_learning",
    "agent_registry", "knowledge_blocks", "agi_mistakes", "stack_registry",
    "vault_logs", "vault",
}


def _safe_sb_get(table: str, qs: str, svc: bool = False) -> list:
    """Wrapper that hard-blocks tombstoned tables."""
    if table in _TOMBSTONED:
        print(f"[L2] CRITICAL: attempted query on tombstoned table '{table}' — blocked.")
        return []
    try:
        return sb_get(table, qs, svc=svc) or []
    except Exception as e:
        print(f"[L2] sb_get({table}) error (non-fatal): {e}")
        return []


# ── Conversation history ──────────────────────────────────────────────────────

def get_history(cid: str) -> list:
    with _conv_lock:
        if cid not in _conv_memory:
            # Load recent turns from Supabase
            rows = _safe_sb_get(
                "telegram_conversations",
                f"select=role,content,created_at"
                f"&chat_id=eq.{cid}&deleted=eq.false"
                f"&order=created_at.desc&limit={MAX_HISTORY}",
                svc=True,
            )
            turns = list(reversed([
                {"role": r["role"], "content": r["content"], "ts": r.get("created_at", "")}
                for r in rows
            ]))
            _conv_memory[cid] = deque(turns, maxlen=MAX_HISTORY)
        return list(_conv_memory[cid])


def append_history(cid: str, role: str, content: str):
    with _conv_lock:
        if cid not in _conv_memory:
            _conv_memory[cid] = deque(maxlen=MAX_HISTORY)
        _conv_memory[cid].append({
            "role": role,
            "content": content[:1500],
            "ts": datetime.utcnow().isoformat()
        })
    # Persist to Supabase (fire-and-forget)
    try:
        from core_config import sb_post
        sb_post("telegram_conversations", {
            "chat_id":    cid,
            "role":       role,
            "content":    content[:1500],
            "created_at": datetime.utcnow().isoformat(),
        })
    except Exception:
        pass


def clear_history(cid: str):
    with _conv_lock:
        _conv_memory.pop(cid, None)
    with _wm_lock:
        _working_mem.pop(cid, None)
    with _ctx_lock:
        _ctx_cache.pop(cid, None)
    try:
        from core_config import sb_patch
        sb_patch("telegram_conversations", f"chat_id=eq.{cid}", {"deleted": True})
    except Exception:
        pass


# ── Working memory ────────────────────────────────────────────────────────────

def wm_set(cid: str, key: str, value):
    with _wm_lock:
        if cid not in _working_mem:
            _working_mem[cid] = {}
        _working_mem[cid][key] = value


def wm_get(cid: str, key: str, default=None):
    with _wm_lock:
        return _working_mem.get(cid, {}).get(key, default)


def wm_clear(cid: str):
    with _wm_lock:
        _working_mem.pop(cid, None)


# ── Parallel brain fetch helpers ─────────────────────────────────────────────

def _fetch_behavioral_rules(domain: str, results: dict):
    try:
        rows = _safe_sb_get(
            "behavioral_rules",
            f"select=trigger,pointer,full_rule,domain,priority,confidence"
            f"&active=eq.true"
            f"&order=priority.asc,confidence.desc&limit=40",
            svc=True,
        )
        # Filter: universal rules + domain-specific
        filtered = [r for r in rows
                    if r.get("domain") in ("universal", domain, "general")]
        results["behavioral_rules"] = filtered
    except Exception as e:
        print(f"[L2] behavioral_rules fetch error: {e}")
        results["behavioral_rules"] = []


def _fetch_recent_mistakes(domain: str, results: dict):
    try:
        qs = (
            f"select=domain,context,what_failed,root_cause,correct_approach,how_to_avoid,severity"
            f"&order=created_at.desc&limit=8"
        )
        if domain and domain not in ("general", ""):
            qs += f"&domain=eq.{domain}"
        results["recent_mistakes"] = _safe_sb_get("mistakes", qs, svc=True)
    except Exception as e:
        print(f"[L2] mistakes fetch error: {e}")
        results["recent_mistakes"] = []


def _fetch_kb_snippets(text: str, results: dict):
    try:
        # Extract keywords
        import re
        stop = {"the","a","an","is","are","was","i","you","we","it","to","of",
                "and","or","in","on","at","for","with","do","can","please",
                "yang","ada","ini","itu","ke","dari","bisa","mau","tidak","saya"}
        words = [w for w in re.findall(r"[a-zA-Z]{4,}", text.lower()) if w not in stop]
        kws   = list(dict.fromkeys(words))[:3]

        if not kws:
            results["kb_snippets"] = []
            return

        kw_filter = ",".join(f"topic.ilike.*{k}*" for k in kws[:2])
        rows = _safe_sb_get(
            "knowledge_base",
            f"select=domain,topic,instruction,content,confidence"
            f"&or=({kw_filter})"
            f"&active=eq.true&order=confidence.desc&limit=6",
            svc=True,
        )
        results["kb_snippets"] = rows
    except Exception as e:
        print(f"[L2] kb_snippets fetch error: {e}")
        results["kb_snippets"] = []


def _fetch_in_progress_tasks(results: dict):
    try:
        rows = _safe_sb_get(
            "task_queue",
            "select=id,task,priority,status,source,next_step"
            "&source=in.(core_v6_registry,mcp_session)"
            "&status=in.(pending,in_progress)"
            "&order=priority.desc&limit=5",
        )
        results["in_progress_tasks"] = rows
    except Exception as e:
        print(f"[L2] task_queue fetch error: {e}")
        results["in_progress_tasks"] = []


def _fetch_owner_profile(results: dict):
    try:
        rows = _safe_sb_get(
            "owner_profile",
            "select=dimension,value,confidence&order=confidence.desc&limit=10",
            svc=True,
        )
        results["owner_profile"] = rows
    except Exception:
        results["owner_profile"] = []


def _fetch_active_goals(results: dict):
    try:
        rows = _safe_sb_get(
            "session_goals",
            "select=domain,goal,progress,status"
            "&status=eq.active&order=created_at.desc&limit=5",
            svc=True,
        )
        results["active_goals"] = rows
    except Exception:
        results["active_goals"] = []


# ── Main hydration ────────────────────────────────────────────────────────────

def _hydrate_sync(intent: dict) -> dict:
    """
    Synchronous parallel Supabase hydration. Returns full SessionContext.
    Runs 6 fetches in parallel threads — total wall-clock ~= slowest single fetch.
    """
    cid   = intent["sender_id"]
    text  = intent["text"]

    # Guess domain from text for behavioral_rules filter
    _dom_map = [
        (["supabase","sb_query","database","table"],        "db"),
        (["github","patch","deploy","railway","commit"],    "code"),
        (["telegram","notify","bot"],                       "bot"),
        (["mcp","tool","session"],                          "mcp"),
        (["training","cold","hot","evolution","pattern"],   "training"),
        (["knowledge","kb","learn"],                        "kb"),
    ]
    domain = "general"
    tl = text.lower()
    for kws, d in _dom_map:
        if any(k in tl for k in kws):
            domain = d
            break

    # Check cache
    with _ctx_lock:
        cached = _ctx_cache.get(cid)
        if cached and (time.time() - cached["loaded_at"]) < CTX_CACHE_TTL:
            # Return cached context with fresh intent + history
            ctx = dict(cached["ctx"])
            ctx["intent"]       = intent
            ctx["conversation"] = get_history(cid)
            ctx["working_memory"] = dict(_working_mem.get(cid, {}))
            ctx["fast_path"]    = intent.get("is_trivial", False)
            return ctx

    # Parallel fetch
    results = {}
    threads = [
        threading.Thread(target=_fetch_behavioral_rules,  args=(domain, results), daemon=True),
        threading.Thread(target=_fetch_recent_mistakes,   args=(domain, results), daemon=True),
        threading.Thread(target=_fetch_kb_snippets,       args=(text, results),   daemon=True),
        threading.Thread(target=_fetch_in_progress_tasks, args=(results,),         daemon=True),
        threading.Thread(target=_fetch_owner_profile,     args=(results,),         daemon=True),
        threading.Thread(target=_fetch_active_goals,      args=(results,),         daemon=True),
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=4)

    ctx = {
        "intent":            intent,
        "behavioral_rules":  results.get("behavioral_rules", []),
        "recent_mistakes":   results.get("recent_mistakes", []),
        "kb_snippets":       results.get("kb_snippets", []),
        "in_progress_tasks": results.get("in_progress_tasks", []),
        "owner_profile":     results.get("owner_profile", []),
        "active_goals":      results.get("active_goals", []),
        "conversation":      get_history(cid),
        "working_memory":    dict(_working_mem.get(cid, {})),
        "hydrated_at":       time.time(),
        "fast_path":         intent.get("is_trivial", False),
    }

    # Cache (without intent — per-message fields)
    cacheable = {k: v for k, v in ctx.items()
                 if k not in ("intent", "conversation", "working_memory", "fast_path")}
    with _ctx_lock:
        _ctx_cache[cid] = {"ctx": cacheable, "loaded_at": time.time()}

    print(f"[L2] Hydrated: rules={len(ctx['behavioral_rules'])} "
          f"kb={len(ctx['kb_snippets'])} mistakes={len(ctx['recent_mistakes'])} "
          f"tasks={len(ctx['in_progress_tasks'])}")
    return ctx


async def layer_2_hydrate(intent: dict):
    """
    Async entry point from L1. Hydrates context then passes to L3 reasoning.
    """
    try:
        # Run sync hydration in executor (non-blocking)
        import asyncio
        loop    = asyncio.get_event_loop()
        ctx     = await loop.run_in_executor(None, _hydrate_sync, intent)

        # Pass to L3: Reasoning
        from core_orch_layer3 import layer_3_reason
        await layer_3_reason(ctx)

    except Exception as e:
        print(f"[L2] Hydration error: {e}")
        cid = intent.get("sender_id", "")
        from core_config import notify
        notify(f"⚠️ Context hydration failed: {e}", cid)


def invalidate_cache(cid: str):
    """Call when behavioral rules or KB changes during a session."""
    with _ctx_lock:
        _ctx_cache.pop(cid, None)


if __name__ == "__main__":
    print("🛰️ Layer 2: Memory / Context Hydration — Online.")
