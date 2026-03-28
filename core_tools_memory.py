"""core_tools_memory.py — reasoning packet + unified memory search + StateEvaluator.

This module is intentionally kept free of imports from core_tools.py (facade) and
other tool-family modules to avoid circular imports.
"""

from __future__ import annotations

import json
import re as _re
from datetime import datetime

from core_config import sb_patch


def _group_memory_hits(rows: list) -> dict:
    grouped: dict = {}
    for row in rows or []:
        table = row.get("semantic_table") or row.get("_table") or "knowledge_base"
        grouped.setdefault(table, []).append(row)
    return grouped


def t_search_memory(query: str = "", domain: str = "", limit: int = 10, tables: str = "") -> dict:
    """Unified semantic memory search across KB + native semantic tables."""
    try:
        if not query:
            return {"ok": False, "error": "query required", "results": []}
        lim = max(1, min(int(limit) if limit else 10, 50))
        table_list = [t.strip() for t in tables.split(",") if t.strip()] if tables else None
        from core_semantic import search_many

        rows = search_many(query, tables=table_list, limit=lim, domain=domain) or []
        grouped = _group_memory_hits(rows)
        try:
            kb_ids = [
                str(r["id"])
                for r in grouped.get("knowledge_base", [])
                if r.get("id") and r["id"] != 1
            ]
            if kb_ids:
                sb_patch(
                    "knowledge_base",
                    f"id=in.({','.join(kb_ids)})",
                    {"last_accessed": datetime.utcnow().isoformat()},
                )
        except Exception:
            pass
        return {
            "ok": True,
            "query": query,
            "domain": domain or "",
            "count": len(rows),
            "results": rows,
            "by_table": {k: len(v) for k, v in grouped.items()},
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "results": []}


def t_reasoning_packet(
    query: str = "",
    domain: str = "",
    limit: str = "10",
    tables: str = "",
    per_table: str = "2",
) -> dict:
    """Build the canonical reasoning packet for a query (single unified memory read)."""
    try:
        if not query:
            return {"ok": False, "error": "query required"}
        try:
            lim = max(1, min(int(limit), 50))
        except Exception:
            lim = 10
        try:
            pt = max(1, min(int(per_table), 5))
        except Exception:
            pt = 2
        table_list = [t.strip() for t in tables.split(",") if t.strip()] if tables else None
        from core_reasoning_packet import build_reasoning_packet

        return build_reasoning_packet(
            query=query,
            domain=domain,
            tables=table_list,
            limit=lim,
            per_table=pt,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


class StateEvaluator:
    """Evaluate an environment or system state using unified memory context."""

    def __init__(
        self,
        query: str,
        domain: str = "general",
        tables: list | None = None,
        limit: int = 10,
        per_table: int = 2,
        state_hint: str = "",
    ):
        self.query = (query or "").strip()
        self.domain = (domain or "general").strip()
        self.tables = tables
        self.limit = max(1, min(int(limit or 10), 50))
        self.per_table = max(1, min(int(per_table or 2), 5))
        self.state_hint = (state_hint or "").strip()

    @staticmethod
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
        ]
        return sum(1 for m in markers if m in text)

    def evaluate(self) -> dict:
        from core_reasoning_packet import build_reasoning_packet

        packet = build_reasoning_packet(
            self.query,
            domain=self.domain,
            tables=self.tables,
            limit=self.limit,
            per_table=self.per_table,
        )
        pkt = packet.get("packet") or {}
        hits = pkt.get("top_hits") or []
        by_table = pkt.get("memory_by_table") or {}
        focus = pkt.get("focus", "")
        context = pkt.get("context", "")

        table_support = len([k for k, v in by_table.items() if int(v or 0) > 0])
        evidence_count = len(hits)
        risk_markers = self._risk_markers(
            " ".join(
                [
                    self.query,
                    self.state_hint,
                    focus,
                    context,
                    " ".join(h.get("title", "") for h in hits[:5]),
                    " ".join(h.get("body", "") for h in hits[:5]),
                ]
            )
        )

        coherence_score = round(min(1.0, 0.35 + (table_support * 0.08) + (evidence_count * 0.03)), 3)
        evidence_score = round(min(1.0, 0.25 + (evidence_count * 0.08)), 3)
        risk_score = round(min(1.0, 0.12 * risk_markers), 3)
        readiness_score = round(
            max(
                0.0,
                min(
                    1.0,
                    (coherence_score * 0.45) + (evidence_score * 0.35) - (risk_score * 0.4),
                ),
            ),
            3,
        )
        confidence = round(
            max(
                0.0,
                min(
                    1.0,
                    (evidence_score * 0.5) + (coherence_score * 0.3) + ((1.0 - risk_score) * 0.2),
                ),
            ),
            3,
        )
        recommendation = (
            "proceed"
            if readiness_score >= 0.72 and risk_score <= 0.24
            else "defer"
            if risk_score >= 0.48
            else "reassess"
        )

        return {
            "ok": True,
            "query": self.query,
            "domain": self.domain,
            "packet_focus": focus,
            "context": context,
            "memory_by_table": by_table,
            "evidence_count": evidence_count,
            "table_support": table_support,
            "coherence_score": coherence_score,
            "evidence_score": evidence_score,
            "risk_score": risk_score,
            "readiness_score": readiness_score,
            "confidence": confidence,
            "recommendation": recommendation,
            "state_hint": self.state_hint,
            "top_hits": hits[:10],
        }


def t_evaluate_state(
    query: str = "",
    domain: str = "general",
    tables: str = "",
    limit: str = "10",
    per_table: str = "2",
    state_hint: str = "",
) -> dict:
    """Evaluate a state/query and return a compact scorecard."""
    try:
        if not query:
            return {"ok": False, "error": "query required"}
        try:
            lim = max(1, min(int(limit), 50))
        except Exception:
            lim = 10
        try:
            pt = max(1, min(int(per_table), 5))
        except Exception:
            pt = 2
        table_list = [t.strip() for t in tables.split(",") if t.strip()] if tables else None
        return StateEvaluator(
            query=query,
            domain=domain,
            tables=table_list,
            limit=lim,
            per_table=pt,
            state_hint=state_hint,
        ).evaluate()
    except Exception as e:
        return {"ok": False, "error": str(e)}

