"""
core_orch_context.py — shared request/evidence/decision helpers for CORE ORC.
Keeps the orchestrator pipeline cohesive by building structured packets
instead of ad hoc dicts at each layer.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict
from datetime import datetime

import httpx

from core_config import SUPABASE_URL, _sbh_count_svc


# ── Basic helpers ────────────────────────────────────────────────────────────
def _safe_text(value: Any, limit: int = 500) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _count_table(table: str, where: str = "") -> int:
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?select=id&limit=1"
        if where:
            url += f"&{where}"
        r = httpx.get(url, headers=_sbh_count_svc(), timeout=10)
        cr = r.headers.get("content-range", "*/0")
        return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return -1


# ── Request profile ──────────────────────────────────────────────────────────
def classify_request_kind(
    text: str,
    command: str = "",
    message_type: str = "message",
    route: str = "conversation",
    intent: str | None = None,
) -> str:
    t = (text or "").lower()
    cmd = (command or "").lower()

    if cmd in {"/health", "/status", "/state"} or any(k in t for k in ("health", "status", "system state", "system health")):
        return "status"
    if any(k in t for k in ("how advanced", "capability", "capabilities", "what can you do", "strengths", "weaknesses", "limitations")):
        return "self_assessment"
    if cmd in {"/review"} or any(k in t for k in (
        "review queue", "owner review", "proposal queue", "owner only",
        "owner queue", "batch close", "cluster close", "close cluster",
        "review cluster", "manual queue", "proposal review",
    )):
        return "owner_review"
    if any(k in t for k in ("debug", "bug", "error", "broken", "crash", "stack trace")):
        return "debug"
    if intent in ("task_execution",):
        return "task"
    if intent in ("conversation", "greeting"):
        return "conversation"
    if route == "command":
        return "command"
    return "question"


def initial_request_profile(msg) -> Dict[str, Any]:
    cmd = msg.context.get("command", "") if hasattr(msg, "context") else ""
    request_kind = classify_request_kind(
        msg.text,
        command=cmd,
        message_type=msg.message_type,
        route=msg.route,
        intent=None,
    )
    response_mode = {
        "status": "status",
        "self_assessment": "capability",
        "owner_review": "review",
        "debug": "debug",
        "task": "task",
        "conversation": "conversation",
    }.get(request_kind, "tool")

    return {
        "request_kind": request_kind,
        "response_mode": response_mode,
        "route_reason": "initial_profile",
        "clarification_needed": False,
    }


def build_decision_packet(msg) -> Dict[str, Any]:
    classification = msg.context.get("intent_classification", {}) if hasattr(msg, "context") else {}
    intent = classification.get("intent") or msg.intent or "general_query"
    confidence = float(classification.get("confidence", 0.0) or 0.0)
    cmd = msg.context.get("command", "") if hasattr(msg, "context") else ""

    request_kind = classify_request_kind(
        msg.text,
        command=cmd,
        message_type=msg.message_type,
        route=msg.route,
        intent=intent,
    )

    response_mode = {
        "status": "status",
        "self_assessment": "capability",
        "owner_review": "review",
        "debug": "debug",
        "task": "task",
        "conversation": "conversation",
    }.get(request_kind, "tool")

    clarification_needed = False
    clarification_prompt = ""
    if intent in ("task_execution", "general_query") and confidence < 0.45:
        clarification_needed = True
        clarification_prompt = "I need a bit more detail. What exactly should I do, and what is the expected outcome?"

    agentic_hint = False
    if request_kind in ("task", "owner_review"):
        lower = (msg.text or "").lower()
        if any(k in lower for k in ("step by step", "keep going", "until", "comprehensive", "full analysis", "iterate", "repeat until")):
            agentic_hint = True
        if len(msg.text or "") > 200:
            agentic_hint = True

    return {
        "request_kind": request_kind,
        "response_mode": response_mode,
        "route_reason": "decision_packet",
        "clarification_needed": clarification_needed,
        "clarification_prompt": clarification_prompt,
        "agentic_hint": agentic_hint,
        "intent": intent,
        "confidence": confidence,
        "requires_tools": bool(classification.get("requires_tools", False)),
        "domain": classification.get("domain") or msg.context.get("current_domain", "general"),
        "command": cmd,
    }


# ── Evidence / capability packets ────────────────────────────────────────────
def build_evidence_packet(msg) -> Dict[str, Any]:
    ctx = msg.context or {}
    packet = {
        "request": {
            "text": _safe_text(msg.text, 800),
            "intent": msg.intent,
            "request_kind": getattr(msg, "request_kind", ""),
            "response_mode": getattr(msg, "response_mode", ""),
            "source": msg.source,
            "message_type": msg.message_type,
            "route": msg.route,
        },
        "domain": ctx.get("current_domain", "general"),
        "session": ctx.get("session", {}),
        "behavioral_rules": ctx.get("behavioral_rules", [])[:10],
        "domain_mistakes": ctx.get("domain_mistakes", [])[:10],
        "kb_snippets": ctx.get("kb_snippets", [])[:10],
        "conversation_history": ctx.get("conversation_history", [])[-10:],
        "health": ctx.get("health", {}),
        "timestamp": datetime.utcnow().isoformat(),
    }
    try:
        from core_reasoning_packet import build_reasoning_packet
        if msg.text and len(msg.text.strip()) > 2:
            sem = build_reasoning_packet(msg.text, domain=ctx.get("current_domain", "general"))
            if isinstance(sem, dict) and sem.get("ok"):
                packet["semantic"] = sem.get("packet", {})
    except Exception:
        pass
    return packet


def build_capability_packet(msg) -> Dict[str, Any]:
    # Counts
    counts = {
        "knowledge_base": _count_table("knowledge_base"),
        "mistakes": _count_table("mistakes"),
        "sessions": _count_table("sessions"),
        "task_pending": _count_table("task_queue", "status=eq.pending"),
        "task_in_progress": _count_table("task_queue", "status=eq.in_progress"),
        "task_done": _count_table("task_queue", "status=eq.done"),
        "task_failed": _count_table("task_queue", "status=eq.failed"),
        "evo_pending": _count_table("evolution_queue", "status=eq.pending"),
        "evo_applied": _count_table("evolution_queue", "status=eq.applied"),
        "evo_rejected": _count_table("evolution_queue", "status=eq.rejected"),
    }
    owner_only = _count_table("evolution_queue", "status=eq.pending&review_scope=eq.owner_only")
    if owner_only >= 0:
        counts["owner_review_pending"] = owner_only

    workers = {}
    try:
        from core_task_autonomy import autonomy_status
        workers["task_autonomy"] = autonomy_status()
    except Exception:
        pass
    try:
        from core_research_autonomy import research_autonomy_status
        workers["research_autonomy"] = research_autonomy_status()
    except Exception:
        pass
    try:
        from core_code_autonomy import code_autonomy_status
        workers["code_autonomy"] = code_autonomy_status()
    except Exception:
        pass
    try:
        from core_integration_autonomy import integration_autonomy_status
        workers["integration_autonomy"] = integration_autonomy_status()
    except Exception:
        pass
    try:
        from core_evolution_autonomy import evolution_autonomy_status
        workers["evolution_autonomy"] = evolution_autonomy_status()
    except Exception:
        pass
    try:
        from core_semantic_projection import semantic_projection_status
        workers["semantic_projection"] = semantic_projection_status()
    except Exception:
        pass

    strengths = []
    gaps = []
    if counts.get("task_done", 0) >= 0 and counts.get("task_failed", 0) >= 0:
        strengths.append("task worker lane is measurable and continuously reporting")
    if counts.get("evo_applied", 0) >= 0:
        strengths.append("evolution lane is continuously applying approved changes")
    if counts.get("knowledge_base", 0) >= 0:
        strengths.append("core memory stores KB, mistakes, sessions, and reflections")
    if counts.get("owner_review_pending", 0) and counts.get("owner_review_pending", 0) > 0:
        gaps.append(f"owner review has {counts['owner_review_pending']} pending cluster rows")
    if counts.get("task_pending", 0) and counts.get("task_pending", 0) > 0:
        gaps.append(f"task queue still has {counts['task_pending']} pending rows")
    if counts.get("evo_pending", 0) and counts.get("evo_pending", 0) > 0:
        gaps.append(f"evolution queue still has {counts['evo_pending']} pending rows")

    return {
        "counts": counts,
        "workers": workers,
        "headline": (
            "CORE has live orchestrator coverage across task, research, code, integration, evolution, "
            "and semantic projection lanes."
        ),
        "strengths": strengths,
        "gaps": gaps,
        "timestamp": datetime.utcnow().isoformat(),
    }
