"""core_tools_governance.py — tool-reliance governance for CORE.

This module keeps the policy for "use memory first, tools second" out of the
facade while still being easy to import from core_tools.py and related helpers.
It is read-only and deterministic.
"""

from __future__ import annotations

from typing import Any

from core_reasoning_packet import build_reasoning_packet


def _safe_text(value: Any, limit: int = 500) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _risk_markers(text: str) -> int:
    text = (text or "").lower()
    markers = [
        "error",
        "failed",
        "fail",
        "broken",
        "degraded",
        "stale",
        "blocked",
        "collision",
        "conflict",
        "missing",
        "invalid",
        "unauthorized",
        "crash",
        "traceback",
        "timeout",
        "deploy",
        "schema",
        "patch",
        "write",
        "delete",
    ]
    return sum(1 for m in markers if m in text)


class ToolRelianceAdvisor:
    """Assess whether CORE should stay memory-first or involve more tools."""

    @staticmethod
    def assess_packet(packet: dict, planned_action: str = "", state_hint: str = "") -> dict:
        pkt = packet or {}
        query = _safe_text(pkt.get("query") or planned_action, 500)
        domain = _safe_text(pkt.get("domain"), 120)
        focus = _safe_text(pkt.get("focus"), 240)
        context = _safe_text(pkt.get("context"), 800)
        top_hits = pkt.get("top_hits") or []
        by_table = pkt.get("memory_by_table") or {}

        evidence_count = len(top_hits)
        table_support = len([k for k, v in by_table.items() if int(v or 0) > 0])
        joined = " ".join(
            [
                query,
                planned_action or "",
                state_hint or "",
                focus,
                context,
                " ".join(_safe_text(hit.get("title"), 120) for hit in top_hits[:8]),
                " ".join(_safe_text(hit.get("body"), 180) for hit in top_hits[:8]),
            ]
        )
        risk_markers = _risk_markers(joined)
        memory_strength = round(min(1.0, 0.18 + (evidence_count * 0.09) + (table_support * 0.08)), 3)
        risk_score = round(min(1.0, (0.08 * risk_markers)), 3)

        # Directly risky action families should stay conservative even if memory is rich.
        if any(term in joined.lower() for term in ("deploy", "schema", "delete", "irreversible", "patch core", "write file")):
            risk_score = round(min(1.0, risk_score + 0.15), 3)

        readiness_score = round(
            max(0.0, min(1.0, (memory_strength * 0.7) + ((1.0 - risk_score) * 0.3))),
            3,
        )

        if risk_score >= 0.55:
            strategy = "owner_confirm"
            tool_budget = 3
        elif readiness_score >= 0.72 and risk_score <= 0.24:
            strategy = "memory_first"
            tool_budget = 0
        elif readiness_score >= 0.55:
            strategy = "tool_light"
            tool_budget = 1
        else:
            strategy = "tool_required"
            tool_budget = 2

        recommended_tools = []
        if strategy == "memory_first":
            recommended_tools = ["reasoning_packet", "evaluate_state"]
        elif strategy == "tool_light":
            recommended_tools = ["reasoning_packet", "evaluate_state", "verify_before_deploy"]
        elif strategy == "tool_required":
            recommended_tools = ["reasoning_packet", "evaluate_state", "reason_chain"]
        else:
            recommended_tools = ["reasoning_packet", "evaluate_state", "reason_chain", "verify_before_deploy"]

        rationale = (
            f"memory_strength={memory_strength:.2f}, evidence={evidence_count}, tables={table_support}, "
            f"risk_score={risk_score:.2f}, readiness={readiness_score:.2f}"
        )
        return {
            "ok": True,
            "query": query,
            "domain": domain,
            "state_hint": state_hint,
            "evidence_count": evidence_count,
            "table_support": table_support,
            "memory_strength": memory_strength,
            "risk_score": risk_score,
            "readiness_score": readiness_score,
            "tool_strategy": strategy,
            "tool_budget": tool_budget,
            "should_use_tools": strategy != "memory_first",
            "recommended_tools": recommended_tools,
            "packet_focus": focus,
            "context": context,
            "top_hits": top_hits[:10],
            "rationale": rationale,
        }


def t_tool_reliance_assessor(
    query: str = "",
    domain: str = "general",
    tables: str = "",
    limit: str = "10",
    per_table: str = "2",
    state_hint: str = "",
    planned_action: str = "",
) -> dict:
    """Assess whether the current query/action should stay memory-first or use more tools."""
    try:
        if not query and not planned_action:
            return {"ok": False, "error": "query or planned_action is required"}
        try:
            lim = max(1, min(int(limit), 50))
        except Exception:
            lim = 10
        try:
            pt = max(1, min(int(per_table), 5))
        except Exception:
            pt = 2
        table_list = [t.strip() for t in tables.split(",") if t.strip()] if tables else None
        packet_result = build_reasoning_packet(
            query=query or planned_action,
            domain=domain,
            tables=table_list,
            limit=lim,
            per_table=pt,
        )
        if not packet_result.get("ok"):
            return {"ok": False, "error": packet_result.get("error", "packet build failed")}
        packet = packet_result.get("packet") or {}
        return ToolRelianceAdvisor.assess_packet(
            packet,
            planned_action=planned_action or query,
            state_hint=state_hint,
        )
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

