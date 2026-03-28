"""core_reasoning_packet.py — canonical memory context packet for CORE reasoning.

This is the single, deterministic read model used by agentic tools so they all:
- run one semantic query (fan-out across tables),
- group + normalize results,
- produce a concise focus summary and context string.

No Telegram notifications. No writes. No embedding work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_DEFAULT_TABLES = [
    "knowledge_base",
    "mistakes",
    "behavioral_rules",
    "pattern_frequency",
    "hot_reflections",
    "output_reflections",
    "evolution_queue",
    "conversation_episodes",
]


def _safe_text(value: Any, limit: int = 500) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _normalize_hit(table: str, row: dict) -> dict:
    """Normalize heterogeneous semantic rows into a consistent hit shape."""
    score = _as_float(
        row.get("semantic_score")
        or row.get("score")
        or row.get("similarity")
        or row.get("match_score")
        or 0.0,
        0.0,
    )
    rid = row.get("id") or row.get("event_id") or row.get("topic") or row.get("pattern_key")

    title = ""
    body = ""
    if table == "knowledge_base":
        title = _safe_text(row.get("topic"), 140) or f"kb:{rid}"
        body = _safe_text(row.get("instruction") or row.get("content"), 800)
    elif table == "mistakes":
        title = _safe_text(row.get("what_failed"), 160) or f"mistake:{rid}"
        body = " | ".join(
            p for p in [
                _safe_text(row.get("root_cause"), 240),
                _safe_text(row.get("correct_approach"), 240),
                _safe_text(row.get("how_to_avoid"), 240),
            ]
            if p
        )[:800]
    elif table == "behavioral_rules":
        title = _safe_text(row.get("trigger"), 160) or f"rule:{rid}"
        body = _safe_text(row.get("full_rule") or row.get("pointer"), 800)
    elif table == "pattern_frequency":
        title = _safe_text(row.get("pattern_key"), 160) or f"pattern:{rid}"
        body = _safe_text(row.get("description"), 800)
    elif table == "hot_reflections":
        title = _safe_text(row.get("task_summary"), 160) or f"hot:{rid}"
        body = _safe_text(row.get("reflection_text"), 800)
    elif table == "output_reflections":
        title = _safe_text(row.get("gap_domain"), 160) or f"reflect:{rid}"
        body = " | ".join(
            p for p in [
                _safe_text(row.get("gap"), 380),
                _safe_text(row.get("new_behavior"), 380),
                _safe_text(row.get("verdict"), 120),
            ]
            if p
        )[:800]
    elif table == "evolution_queue":
        title = _safe_text(row.get("pattern_key"), 160) or _safe_text(row.get("change_type"), 80) or f"evo:{rid}"
        body = _safe_text(row.get("change_summary"), 800)
    elif table == "conversation_episodes":
        title = _safe_text(row.get("chat_id"), 80) or f"episode:{rid}"
        body = _safe_text(row.get("summary"), 800)
    else:
        title = _safe_text(rid, 160) or table
        body = _safe_text(row, 800)

    return {
        "table": table,
        "id": str(rid) if rid is not None else "",
        "score": score,
        "title": title,
        "body": body,
        "raw": row,
    }


def _focus_from_grouped(grouped: dict[str, list[dict]]) -> str:
    parts = []
    kb = grouped.get("knowledge_base") or []
    mistakes = grouped.get("mistakes") or []
    rules = grouped.get("behavioral_rules") or []
    if mistakes:
        sev = _safe_text(mistakes[0]["raw"].get("severity"), 24) or "unknown"
        parts.append(f"avoid_top_mistake(sev={sev}): {mistakes[0]['title'][:80]}")
    if rules:
        parts.append(f"top_rule: {rules[0]['title'][:80]}")
    if kb:
        parts.append(f"top_kb: {kb[0]['title'][:80]}")
    if not parts:
        return "no_memory_hits"
    return " | ".join(parts)[:220]


def _context_from_grouped(grouped: dict[str, list[dict]], per_table: int = 2) -> str:
    """Render a short, stable context string for LLM consumption."""
    lines: list[str] = []
    order = [
        "behavioral_rules",
        "mistakes",
        "knowledge_base",
        "output_reflections",
        "hot_reflections",
        "pattern_frequency",
        "evolution_queue",
        "conversation_episodes",
    ]
    for table in order:
        hits = grouped.get(table) or []
        if not hits:
            continue
        lines.append(f"[{table}]")
        for h in hits[:per_table]:
            lines.append(f"- {h['title']}: {h['body'][:320]}")
        lines.append("")
    return "\n".join(lines).strip()


def build_reasoning_packet(
    query: str,
    domain: str = "",
    tables: list[str] | None = None,
    limit: int = 10,
    per_table: int = 2,
) -> dict:
    """Build the canonical packet used by agentic reasoning tools."""
    if not query or len(str(query).strip()) < 2:
        return {"ok": False, "error": "query required", "packet": None}

    tables = tables or list(_DEFAULT_TABLES)
    try:
        from core_semantic import search_many
        rows = search_many(query=query, tables=tables, limit=int(limit or 10), domain=domain) or []
    except Exception as e:
        return {"ok": False, "error": str(e), "packet": None}

    # Normalize + group
    normalized = []
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        table = (r.get("semantic_table") or "").strip() or "knowledge_base"
        h = _normalize_hit(table, r)
        normalized.append(h)
        grouped.setdefault(table, []).append(h)

    # Sort within each table by score desc
    for t in list(grouped.keys()):
        grouped[t].sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)

    memory_by_table = {t: len(v) for t, v in grouped.items()}
    focus = _focus_from_grouped(grouped)
    context = _context_from_grouped(grouped, per_table=max(1, int(per_table or 2)))

    packet = {
        "query": str(query),
        "domain": domain or "",
        "tables": tables,
        "memory_by_table": memory_by_table,
        "hit_count": len(normalized),
        "top_hits": normalized[: min(len(normalized), max(1, int(limit or 10)))],
        "focus": focus,
        "context": context,
    }
    return {"ok": True, "packet": packet}
