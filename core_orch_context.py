"""
core_orch_context.py — shared request/evidence/decision helpers for CORE ORC.
Keeps the orchestrator pipeline cohesive by building structured packets
instead of ad hoc dicts at each layer.
"""
from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any, Dict, Iterable, List
from datetime import datetime

import httpx

from core_config import SUPABASE_URL, _sbh_count_svc


# ── Basic helpers ────────────────────────────────────────────────────────────
def _safe_text(value: Any, limit: int = 500) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _keyword_hits(text: str, keywords: Iterable[str]) -> int:
    lower = (text or "").lower()
    return sum(1 for kw in keywords if kw in lower)


def _extract_code_targets(text: str) -> List[str]:
    """Pull likely repo/file targets from a request string."""
    if not text:
        return []
    candidates = set()
    for match in re.findall(r"[\w./:-]+\.(?:py|ts|js|jsx|tsx|json|md|yml|yaml|toml)", text):
        candidates.add(match.strip(" ,;:()[]{}<>"))
    for match in re.findall(r"(?:/[\w.-]+)+", text):
        if "." not in match and match.count("/") <= 1:
            continue
        if "." in match or "/" in match:
            candidates.add(match.strip(" ,;:()[]{}<>"))
    return sorted(candidates)[:5]


def _pick_public_sources(text: str) -> List[str]:
    """Choose public ingestion sources that fit the request."""
    lower = (text or "").lower()
    sources: List[str] = []
    if any(k in lower for k in ("paper", "arxiv", "research", "study", "scientific", "academic", "benchmark")):
        sources.extend(["arxiv"])
    if any(k in lower for k in ("docs", "documentation", "api", "reference", "manual", "guide", "how to", "official")):
        sources.extend(["docs", "stackoverflow"])
    if any(k in lower for k in ("news", "latest", "current", "today", "release", "update", "announce", "trending")):
        sources.extend(["hackernews", "reddit", "medium"])
    if any(k in lower for k in ("blog", "article", "tutorial", "explain", "learn", "overview")):
        sources.extend(["medium", "stackoverflow"])
    if any(k in lower for k in ("community", "discussion", "forum")):
        sources.extend(["reddit", "stackoverflow"])
    if not sources:
        sources = ["arxiv", "docs", "medium", "reddit", "hackernews", "stackoverflow"]
    # Preserve order while deduping.
    out: List[str] = []
    for src in sources:
        if src not in out:
            out.append(src)
    return out[:6]


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


def build_evidence_gate(msg) -> Dict[str, Any]:
    """Codex-style gate: prefer evidence, then external search, then clarification."""
    ctx = msg.context or {}
    evidence = ctx.get("evidence_packet", {}) or {}
    decision = ctx.get("decision_packet", {}) or {}
    request_kind = getattr(msg, "request_kind", "") or decision.get("request_kind", "")
    intent = getattr(msg, "intent", "") or decision.get("intent", "")
    text = msg.text or ""
    if not request_kind:
        request_kind = classify_request_kind(
            text,
            command=ctx.get("command", "") if isinstance(ctx, dict) else "",
            message_type=getattr(msg, "message_type", "message"),
            route=getattr(msg, "route", "conversation"),
            intent=intent or None,
        )

    kb = list(evidence.get("kb_snippets", []) or [])
    rules = list(evidence.get("behavioral_rules", []) or [])
    mistakes = list(evidence.get("domain_mistakes", []) or [])
    session = evidence.get("session", {}) or {}
    semantic = evidence.get("semantic", {}) or {}
    sem_hits = 0
    if isinstance(semantic, dict):
        mem = semantic.get("memory_by_table", {}) or {}
        if isinstance(mem, dict):
            sem_hits = sum(int(v or 0) for v in mem.values() if isinstance(v, (int, float)))
        if semantic.get("results"):
            sem_hits += len(semantic.get("results") or [])

    kb_hits = len(kb)
    rule_hits = len(rules)
    mistake_hits = len(mistakes)
    session_hits = 1 if session else 0
    evidence_score = min(1.0, (kb_hits * 0.18) + (rule_hits * 0.06) + (mistake_hits * 0.05) + (sem_hits * 0.01) + (session_hits * 0.15))

    code_markers = (
        "code", "repo", "repository", "file", "commit", "branch", "diff", "patch", "function",
        "variable", "line", "traceback", "stack trace", "git", "pull", "push", "status", "module",
        "python", ".py", "fix", "refactor", "review", "implement"
    )
    web_markers = (
        "latest", "current", "today", "news", "public", "internet", "web", "docs", "documentation",
        "api", "how to", "what is", "who is", "price", "weather", "search", "look up", "find"
    )
    public_markers = (
        "research", "paper", "arxiv", "study", "benchmark", "official", "docs", "documentation",
        "api", "current", "latest", "news", "public", "internet", "web", "blog", "tutorial",
        "guide", "community", "forum", "reddit", "hackernews", "stackoverflow"
    )

    code_hits = _keyword_hits(text, code_markers)
    web_hits = _keyword_hits(text, web_markers)
    public_hits = _keyword_hits(text, public_markers)
    code_targets = _extract_code_targets(text)
    public_sources = _pick_public_sources(text)

    if request_kind in {"status", "self_assessment"}:
        if code_hits >= 1 or code_targets or web_hits >= 1:
            retrieval_mode = "code_then_web" if web_hits else "code"
            preferred_tools = ["git", "search_in_file", "read_file"]
            if web_hits or evidence_score < 0.25:
                preferred_tools.append("web_search")
        else:
            retrieval_mode = "state_only"
            preferred_tools = []
    elif request_kind in {"owner_review", "debug"} and not (code_hits >= 1 or code_targets or web_hits >= 1):
        retrieval_mode = "supabase_then_web"
        preferred_tools = ["search_kb", "web_search"]
    elif code_hits >= 2 or code_targets:
        retrieval_mode = "code_then_web" if web_hits else "code"
        preferred_tools = ["git", "search_in_file", "read_file"]
        if web_hits or evidence_score < 0.25:
            preferred_tools.append("web_search")
    elif public_hits >= 1 or web_hits >= 1:
        retrieval_mode = "public_research_then_web" if web_hits else "public_research"
        preferred_tools = ["search_kb", "ingest_knowledge", "web_search"]
    else:
        retrieval_mode = "supabase_then_web"
        preferred_tools = ["search_kb", "web_search"]

    needs_retrieval = retrieval_mode != "state_only" and (evidence_score < 0.45 or retrieval_mode in {"code", "code_then_web", "public_research", "public_research_then_web"})

    # Keep the gate strict: if no local evidence and no web intent, clarification is the last resort.
    if request_kind not in {"status", "self_assessment"} and evidence_score < 0.12 and not code_targets and web_hits == 0:
        needs_retrieval = True

    clarification_prompt = (
        "I checked CORE memory and external evidence, but I still do not have enough context. "
        "Upload the missing file, repo path, URL, commit hash, or the exact details you want me to verify."
    )

    return {
        "request_kind": request_kind,
        "intent": intent,
        "score": round(evidence_score, 3),
        "state": "rich" if evidence_score >= 0.8 else "moderate" if evidence_score >= 0.45 else "sparse" if evidence_score >= 0.15 else "empty",
        "needs_retrieval": needs_retrieval,
        "retrieval_mode": retrieval_mode,
        "preferred_tools": preferred_tools,
        "search_query": _safe_text(text, 220),
        "code_targets": code_targets,
        "clarification_prompt": clarification_prompt,
        "needs_clarification_after_retrieval": retrieval_mode != "state_only" and evidence_score < 0.25,
        "public_research_needed": bool(public_hits or (web_hits and request_kind not in {"status", "self_assessment"} and not code_targets)),
        "public_sources": public_sources if (public_hits or web_hits) else [],
        "source_counts": {
            "kb_hits": kb_hits,
            "rule_hits": rule_hits,
            "mistake_hits": mistake_hits,
            "session_hits": session_hits,
            "semantic_hits": sem_hits,
        },
    }


def tool_result_has_evidence(tool_name: str, result: Any) -> bool:
    """Best-effort check whether a tool result meaningfully answered the request."""
    if not isinstance(result, dict):
        return bool(str(result).strip())
    if result.get("ok") is False:
        return False
    if tool_name == "search_kb":
        return bool(result.get("results") or result.get("matches") or result.get("rows") or result.get("items"))
    if tool_name in {"web_search"}:
        return bool(result.get("results") or result.get("items"))
    if tool_name in {"ingest_knowledge"}:
        return any(
            result.get(k)
            for k in ("records_inserted", "records_updated", "raw_count", "deduped_count", "concepts_found", "hot_reflections_injected")
        )
    if tool_name in {"web_fetch", "summarize_url", "read_file", "gh_read_lines", "search_in_file"}:
        for key in ("content", "text", "snippet", "summary", "lines", "result"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                return True
            if isinstance(val, list) and val:
                return True
        return False
    if tool_name in {"git"}:
        return any(result.get(k) for k in ("stdout", "status", "diff", "log", "commit", "branch"))
    if tool_name in {"get_state", "state_packet", "session_snapshot", "system_verification_packet"}:
        return True
    return any(
        result.get(k)
        for k in ("content", "text", "summary", "result", "data", "rows", "state", "status", "details")
    )
