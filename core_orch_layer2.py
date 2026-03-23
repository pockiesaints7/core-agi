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

HARD RULES (from CORE skill file):
  - id=gt.1 on every bigserial-PK table query (row 1 is probe/reserved)
  - ilike wildcards use % not * (PostgREST syntax)
  - asyncio.get_running_loop() not get_event_loop() inside async functions
"""

import time
import threading
import traceback
from collections import deque
from datetime import datetime, timezone

from core_config import sb_get

# ── Short-term conversation buffer (in-memory, per chat_id) ──────────────────
_conv_memory: dict = {}          # cid → deque[{role, content, ts}]
_conv_lock          = threading.Lock()
MAX_HISTORY         = 20
COMPRESS_AT         = 12         # reserved for future summarisation pass

# ── Working memory (scratchpad, per chat_id) ─────────────────────────────────
_working_mem: dict  = {}         # cid → {key: value}
_wm_lock            = threading.Lock()

# ── Session context cache (avoid re-hydrating on every message) ──────────────
_ctx_cache: dict    = {}         # cid → {ctx, loaded_at}
_ctx_lock           = threading.Lock()
CTX_CACHE_TTL       = 600        # 10 min — re-hydrate if stale

# ── Tombstoned tables — hard block against accidental queries ─────────────────
_TOMBSTONED = {
    "playbook", "memory", "master_prompt", "patterns", "training_sessions",
    "training_sessions_v2", "training_flags", "session_learning",
    "agent_registry", "knowledge_blocks", "agi_mistakes", "stack_registry",
    "vault_logs", "vault",
}

# Per-key write lock for results dict (protects against timed-out thread races)
_RESULTS_LOCK = threading.Lock()


def _safe_sb_get(table: str, qs: str, svc: bool = False) -> list:
    """Wrapper that hard-blocks tombstoned tables and normalises empty results."""
    if table in _TOMBSTONED:
        print(f"[L2] CRITICAL: attempted query on tombstoned table '{table}' — blocked.")
        return []
    try:
        return sb_get(table, qs, svc=svc) or []
    except Exception as e:
        print(f"[L2] sb_get({table}) error (non-fatal): {e}")
        return []


def _results_set(results: dict, key: str, value):
    """Thread-safe write to the shared results dict used by fetch threads."""
    with _RESULTS_LOCK:
        results[key] = value


# ── Conversation history ──────────────────────────────────────────────────────

def get_history(cid: str) -> list:
    with _conv_lock:
        if cid not in _conv_memory:
            # FIX BUG-L2-11: added id=gt.1 guard (bigserial table)
            rows = _safe_sb_get(
                "telegram_conversations",
                f"select=role,content,created_at"
                f"&chat_id=eq.{cid}&deleted=eq.false"
                f"&id=gt.1"
                f"&order=created_at.desc&limit={MAX_HISTORY}",
                svc=True,
            )
            turns = list(reversed([
                {"role": r["role"], "content": r["content"],
                 "ts": r.get("created_at", "")}
                for r in rows
            ]))
            _conv_memory[cid] = deque(turns, maxlen=MAX_HISTORY)
        return list(_conv_memory[cid])


def append_history(cid: str, role: str, content: str):
    """Append a turn to in-memory history and persist to Supabase async."""
    with _conv_lock:
        if cid not in _conv_memory:
            _conv_memory[cid] = deque(maxlen=MAX_HISTORY)
        _conv_memory[cid].append({
            "role":    role,
            "content": content[:1500],
            "ts":      datetime.now(timezone.utc).isoformat(),
        })

    # FIX GAP-L2-C: fire-and-forget in a daemon thread to avoid blocking L9
    def _persist():
        try:
            from core_config import sb_post
            sb_post("telegram_conversations", {
                "chat_id":    cid,
                "role":       role,
                "content":    content[:1500],
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            print(f"[L2] append_history persist error (non-fatal): {e}")

    threading.Thread(target=_persist, daemon=True).start()


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


def _wm_snapshot(cid: str) -> dict:
    """Thread-safe shallow copy of working memory for context injection."""
    with _wm_lock:
        return dict(_working_mem.get(cid, {}))


# ── Parallel brain fetch helpers ──────────────────────────────────────────────

def _fetch_behavioral_rules(domain: str, results: dict):
    try:
        # FIX BUG-L2-08: added id=gt.1 guard (bigserial table)
        rows = _safe_sb_get(
            "behavioral_rules",
            f"select=trigger,pointer,full_rule,domain,priority,confidence"
            f"&active=eq.true"
            f"&id=gt.1"
            f"&order=priority.asc,confidence.desc&limit=40",
            svc=True,
        )
        # Filter: universal + "general" rules always included; domain-specific added on top
        filtered = [r for r in rows
                    if r.get("domain") in ("universal", "general", domain)]
        _results_set(results, "behavioral_rules", filtered)
    except Exception as e:
        print(f"[L2] behavioral_rules fetch error: {e}")
        _results_set(results, "behavioral_rules", [])


def _fetch_recent_mistakes(domain: str, results: dict):
    try:
        # FIX BUG-L2-08: added id=gt.1 guard
        qs = (
            f"select=domain,context,what_failed,root_cause,correct_approach,how_to_avoid,severity"
            f"&id=gt.1"
            f"&order=created_at.desc&limit=8"
        )
        if domain and domain not in ("general", ""):
            qs += f"&domain=eq.{domain}"
        _results_set(results, "recent_mistakes",
                     _safe_sb_get("mistakes", qs, svc=True))
    except Exception as e:
        print(f"[L2] mistakes fetch error: {e}")
        _results_set(results, "recent_mistakes", [])


def _fetch_kb_snippets(text: str, results: dict):
    try:
        import re as _re
        stop = {
            "the", "a", "an", "is", "are", "was", "i", "you", "we", "it",
            "to", "of", "and", "or", "in", "on", "at", "for", "with", "do",
            "can", "please", "yang", "ada", "ini", "itu", "ke", "dari",
            "bisa", "mau", "tidak", "saya",
        }
        words = [w for w in _re.findall(r"[a-zA-Z]{4,}", text.lower())
                 if w not in stop]
        kws = list(dict.fromkeys(words))[:3]

        if not kws:
            _results_set(results, "kb_snippets", [])
            return

        # FIX BUG-L2-05: PostgREST ilike uses % wildcard NOT *
        # Previous code used `topic.ilike.*{k}*` — silently returned empty always
        kw_filter = ",".join(f"topic.ilike.%{k}%" for k in kws[:2])

        # FIX BUG-L2-08: added id=gt.1 guard
        rows = _safe_sb_get(
            "knowledge_base",
            f"select=domain,topic,instruction,content,confidence"
            f"&or=({kw_filter})"
            f"&active=eq.true"
            f"&id=gt.1"
            f"&order=confidence.desc&limit=6",
            svc=True,
        )
        _results_set(results, "kb_snippets", rows)
    except Exception as e:
        print(f"[L2] kb_snippets fetch error: {e}")
        _results_set(results, "kb_snippets", [])


def _fetch_in_progress_tasks(results: dict):
    try:
        # FIX BUG-L2-08: added id=gt.1 guard (bigserial table)
        rows = _safe_sb_get(
            "task_queue",
            "select=id,task,priority,status,source,next_step"
            "&source=in.(core_v6_registry,mcp_session)"
            "&status=in.(pending,in_progress)"
            "&id=gt.1"
            "&order=priority.desc&limit=5",
        )
        _results_set(results, "in_progress_tasks", rows)
    except Exception as e:
        print(f"[L2] task_queue fetch error: {e}")
        _results_set(results, "in_progress_tasks", [])


def _fetch_owner_profile(results: dict):
    try:
        # FIX BUG-L2-08: added id=gt.1 guard
        rows = _safe_sb_get(
            "owner_profile",
            "select=dimension,value,confidence"
            "&id=gt.1"
            "&order=confidence.desc&limit=10",
            svc=True,
        )
        _results_set(results, "owner_profile", rows)
    except Exception as e:
        print(f"[L2] owner_profile fetch error: {e}")
        _results_set(results, "owner_profile", [])


def _fetch_active_goals(results: dict):
    try:
        # FIX BUG-L2-08: added id=gt.1 guard
        rows = _safe_sb_get(
            "session_goals",
            "select=domain,goal,progress,status"
            "&status=eq.active"
            "&id=gt.1"
            "&order=created_at.desc&limit=5",
            svc=True,
        )
        _results_set(results, "active_goals", rows)
    except Exception as e:
        print(f"[L2] active_goals fetch error: {e}")
        _results_set(results, "active_goals", [])


# ── Main hydration ─────────────────────────────────────────────────────────────

def _hydrate_sync(intent: dict) -> dict:
    """
    Synchronous parallel Supabase hydration. Returns full SessionContext.
    Runs 6 fetches in parallel threads — total wall-clock ≈ slowest single fetch.

    Thread safety: each fetch thread writes to its own key via _results_set()
    which holds _RESULTS_LOCK. The main thread reads only after all threads
    have been joined (or timed out), so no concurrent access after join().
    """
    cid  = intent["sender_id"]
    text = intent["text"]

    # Detect domain for behavioral_rules scoping
    _dom_map = [
        (["supabase", "sb_query", "database", "table"],       "db"),
        (["github", "patch", "deploy", "railway", "commit"],  "code"),
        (["telegram", "notify", "bot"],                        "bot"),
        (["mcp", "tool", "session"],                           "mcp"),
        (["training", "cold", "hot", "evolution", "pattern"], "training"),
        (["knowledge", "kb", "learn"],                         "kb"),
    ]
    domain = "general"
    tl = text.lower()
    for kws, d in _dom_map:
        if any(k in tl for k in kws):
            domain = d
            break

    # ── Cache hit path ────────────────────────────────────────────────────────
    # Extract the cached payload under lock, then release lock BEFORE calling
    # get_history() and _wm_snapshot() which may themselves acquire _conv_lock
    # or _wm_lock and potentially do DB round-trips. Holding _ctx_lock during
    # a DB query would block all other _hydrate_sync calls for the same cid
    # for up to 4s (BUG-L2-P3-06 fix).
    cached_payload = None
    with _ctx_lock:
        cached = _ctx_cache.get(cid)
        if cached and (time.time() - cached["loaded_at"]) < CTX_CACHE_TTL:
            cached_payload = dict(cached["ctx"])   # shallow copy under lock

    if cached_payload is not None:
        # Lock released — safe to call get_history / _wm_snapshot now
        cached_payload["intent"]         = intent
        cached_payload["conversation"]   = get_history(cid)
        cached_payload["working_memory"] = _wm_snapshot(cid)
        cached_payload["fast_path"]      = intent.get("is_trivial", False)
        return cached_payload

    # ── Fresh hydration ───────────────────────────────────────────────────────
    results: dict = {}
    fetch_threads = [
        threading.Thread(target=_fetch_behavioral_rules,
                         args=(domain, results), daemon=True, name="fetch_rules"),
        threading.Thread(target=_fetch_recent_mistakes,
                         args=(domain, results), daemon=True, name="fetch_mistakes"),
        threading.Thread(target=_fetch_kb_snippets,
                         args=(text, results), daemon=True, name="fetch_kb"),
        threading.Thread(target=_fetch_in_progress_tasks,
                         args=(results,), daemon=True, name="fetch_tasks"),
        threading.Thread(target=_fetch_owner_profile,
                         args=(results,), daemon=True, name="fetch_profile"),
        threading.Thread(target=_fetch_active_goals,
                         args=(results,), daemon=True, name="fetch_goals"),
    ]
    for t in fetch_threads:
        t.start()

    # FIX BUG-L2-02: join with timeout, then check is_alive() to detect
    # timed-out threads. We use the results dict as written — a timed-out
    # thread that never wrote will simply be missing from results (defaulted
    # to [] below). If it wrote partially, _RESULTS_LOCK ensures atomicity.
    for t in fetch_threads:
        t.join(timeout=5)

    # Log any threads that exceeded the timeout
    for t in fetch_threads:
        if t.is_alive():
            print(f"[L2] Warning: fetch thread {t.name} still running after timeout "
                  f"— result may be partial, using default []")

    ctx = {
        "intent":            intent,
        "behavioral_rules":  results.get("behavioral_rules", []),
        "recent_mistakes":   results.get("recent_mistakes", []),
        "kb_snippets":       results.get("kb_snippets", []),
        "in_progress_tasks": results.get("in_progress_tasks", []),
        "owner_profile":     results.get("owner_profile", []),
        "active_goals":      results.get("active_goals", []),
        "conversation":      get_history(cid),
        # FIX BUG-L2-04: locked snapshot
        "working_memory":    _wm_snapshot(cid),
        "hydrated_at":       time.time(),
        "fast_path":         intent.get("is_trivial", False),
    }

    # Cache (strip per-message fields so they're always fresh)
    cacheable = {k: v for k, v in ctx.items()
                 if k not in ("intent", "conversation", "working_memory", "fast_path")}
    with _ctx_lock:
        _ctx_cache[cid] = {"ctx": cacheable, "loaded_at": time.time()}

    print(
        f"[L2] Hydrated: domain={domain} "
        f"rules={len(ctx['behavioral_rules'])} "
        f"kb={len(ctx['kb_snippets'])} "
        f"mistakes={len(ctx['recent_mistakes'])} "
        f"tasks={len(ctx['in_progress_tasks'])}"
    )
    return ctx


async def layer_2_hydrate(intent: dict):
    """
    Async entry point from L1. Hydrates context then passes to L3 reasoning.

    FIX BUG-L2-01: use asyncio.get_running_loop() not get_event_loop().
    get_event_loop() is deprecated in Python 3.10+ when called from a
    coroutine and raises DeprecationWarning (RuntimeError in future versions).
    """
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        ctx  = await loop.run_in_executor(None, _hydrate_sync, intent)

        from core_orch_layer3 import layer_3_reason
        await layer_3_reason(ctx)

    except Exception as e:
        # FIX BUG-L2-09: include full traceback for debuggability
        print(f"[L2] Hydration error: {e}\n{traceback.format_exc()}")
        cid = intent.get("sender_id", "")
        try:
            from core_config import notify
            notify(f"⚠️ Context hydration failed: {e}", cid)
        except Exception:
            pass


def invalidate_cache(cid: str):
    """
    Invalidate the context cache for a chat.
    Call this from L4/L9 after any KB, behavioral_rules, or task_queue write
    so the next message re-hydrates fresh context instead of serving stale data.
    """
    with _ctx_lock:
        _ctx_cache.pop(cid, None)
    print(f"[L2] Cache invalidated for cid={cid}")


if __name__ == "__main__":
    print("🛰️ Layer 2: Memory / Context Hydration — Online.")
