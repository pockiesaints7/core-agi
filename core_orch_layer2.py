"""
core_orch_layer2.py — L2: Memory & Context
Hydrates OrchestratorMessage.context from real Supabase data.
Loads: session state, behavioral rules, domain mistakes, KB snippets.
No mocks.
"""
import asyncio
import json
from typing import Any, Dict, List

from orchestrator_message import OrchestratorMessage

from core_config import sb_get, GROQ_FAST, groq_chat


# ── Helpers ───────────────────────────────────────────────────────────────────
def _detect_domain(text: str) -> str:
    """Fast keyword-based domain detection for mistake/KB scoping."""
    t = text.lower()
    domain_map = [
        (["supabase", "sb_", "database", "table", "query", "insert", "upsert"], "db"),
        (["github", "commit", "patch_file", "gh_", "deploy", "build"], "code"),
        (["telegram", "notify", "bot", "webhook"], "bot"),
        (["mcp", "tool", "session_start", "session_end", "session_close"], "mcp"),
        (["training", "cold_processor", "hot_reflection", "evolution", "pattern"], "training"),
        (["knowledge", "kb", "search_kb", "add_knowledge"], "kb"),
        (["architecture", "refactor", "skill file", "system_map"], "core_agi.architecture"),
        (["patch", "old_str", "new_str", "multi_patch"], "core_agi.patching"),
    ]
    for keywords, domain in domain_map:
        if any(kw in t for kw in keywords):
            return domain
    return "general"


async def _load_session_context() -> Dict[str, Any]:
    """Load last session summary + in-progress tasks from Supabase."""
    try:
        sessions = sb_get(
            "sessions",
            "select=summary,actions,created_at&order=created_at.desc&limit=1",
        )
        last = sessions[0] if sessions else {}

        tasks = sb_get(
            "task_queue",
            "select=id,task,status,priority&status=in.(pending,in_progress)"
            "&source=in.(core_v6_registry,mcp_session)&order=priority.desc&limit=10",
        ) or []

        in_progress = [t for t in tasks if t.get("status") == "in_progress"]
        pending = [t for t in tasks if t.get("status") == "pending"]

        return {
            "last_session_summary": last.get("summary", ""),
            "last_session_ts": last.get("created_at", ""),
            "in_progress_tasks": in_progress,
            "pending_tasks": pending,
        }
    except Exception as exc:
        print(f"[L2] session_context error (non-fatal): {exc}")
        return {}


async def _load_behavioral_rules(domain: str) -> List[Dict[str, Any]]:
    """Load behavioral rules from knowledge_base scoped to domain."""
    try:
        rows = sb_get(
            "knowledge_base",
            f"select=instruction,content,confidence,topic,domain"
            f"&or=(domain.like.core_agi%25,domain.like.{domain}%25)"
            f"&order=confidence.desc&limit=15",
            svc=True,
        ) or []
        return rows
    except Exception as exc:
        print(f"[L2] behavioral_rules error (non-fatal): {exc}")
        return []


async def _load_domain_mistakes(domain: str) -> List[Dict[str, Any]]:
    """Load recent mistakes for domain (with global fallback)."""
    try:
        rows = sb_get(
            "mistakes",
            f"select=domain,context,what_failed,correct_approach,severity,root_cause,how_to_avoid"
            f"&domain=like.{domain}%25&order=severity.desc,created_at.desc&limit=5",
            svc=True,
        ) or []

        # Backfill with global recent if not enough
        if len(rows) < 3:
            global_rows = sb_get(
                "mistakes",
                "select=domain,context,what_failed,correct_approach,severity,root_cause,how_to_avoid"
                "&order=created_at.desc&limit=8",
                svc=True,
            ) or []
            seen = {r.get("context", "")[:80] for r in rows}
            for r in global_rows:
                if r.get("context", "")[:80] not in seen and len(rows) < 6:
                    rows.append(r)
                    seen.add(r.get("context", "")[:80])

        return rows
    except Exception as exc:
        print(f"[L2] domain_mistakes error (non-fatal): {exc}")
        return []


async def _load_relevant_kb(text: str, domain: str) -> List[Dict[str, Any]]:
    """Pull KB snippets relevant to the current message — semantic vector search."""
    try:
        if not text or len(text.strip()) < 3:
            return []
        from core_semantic import search as sem_search
        filters = f"&domain=eq.{domain}" if domain and domain not in ("all", "") else ""
        rows = sem_search("knowledge_base", text.strip()[:200], limit=5, filters=filters)
        return rows
    except Exception as exc:
        print(f"[L2] kb_search error (non-fatal): {exc}")
        return []


async def _load_conversation_history(chat_id: int, limit: int = 20) -> list:
    if not chat_id:
        return []
    try:
        rows = sb_get(
            "conversation_history",
            f"select=role,content,created_at&chat_id=eq.{chat_id}"
            f"&order=created_at.desc&limit={limit}",
            svc=True,
        ) or []
        return list(reversed(rows))
    except Exception as exc:
        print(f"[L2] conversation_history error (non-fatal): {exc}")
        return []


async def _load_system_health() -> dict:
    """Quick health probe: checks Supabase connectivity only (fast)."""
    try:
        rows = sb_get("sessions", "select=id&limit=1")
        return {"supabase": "ok" if rows is not None else "degraded"}
    except Exception:
        return {"supabase": "error"}


# ── Main layer ────────────────────────────────────────────────────────────────
async def layer_2_memory(msg: OrchestratorMessage):
    """
    Hydrate msg.context with everything downstream layers need.
    All Supabase calls are non-fatal — errors logged but pipeline continues.
    """
    msg.track_layer("L2-START")
    print(f"[L2] Building context for @{msg.user} …")

    # Detect domain from message text
    domain = _detect_domain(msg.text)
    msg.context["current_domain"] = domain

    if not hasattr(layer_2_memory, "_sem"):
        layer_2_memory._sem = asyncio.Semaphore(8)
    async with layer_2_memory._sem:
        pass
    try:
        results = await asyncio.wait_for(asyncio.gather(
            _load_session_context(),
            _load_behavioral_rules(domain),
            _load_domain_mistakes(domain),
            _load_relevant_kb(msg.text, domain),
            _load_system_health(),
            _load_conversation_history(msg.chat_id),
            return_exceptions=True,
        ), timeout=8.0)
    except asyncio.TimeoutError:
        print("[L2] Context load TIMEOUT")
        results = [{}, [], [], [], {"supabase": "timeout"}, []]

    def _safe(val, default):
        if isinstance(val, BaseException):
            print(f"[L2] gather sub-task failed (non-fatal): {val}")
            return default
        return val

    session_ctx      = _safe(results[0], {})
    behavioral_rules = _safe(results[1], [])
    domain_mistakes  = _safe(results[2], [])
    kb_snippets      = _safe(results[3], [])
    health           = _safe(results[4], {"supabase": "error"})
    conv_history     = _safe(results[5], [])

    msg.context["session"] = session_ctx
    msg.context["behavioral_rules"] = behavioral_rules
    msg.context["domain_mistakes"] = domain_mistakes
    msg.context["kb_snippets"] = kb_snippets
    msg.context["health"] = health
    msg.context["conversation_history"] = conv_history

    msg.track_layer("L2-COMPLETE")
    print(
        f"[L2] Context ready: domain={domain}  rules={len(behavioral_rules)}"
        f"  mistakes={len(domain_mistakes)}  kb={len(kb_snippets)}"
    )

    from core_orch_layer3 import layer_3_classify
    await layer_3_classify(msg)
