"""core_tools_reasoning.py — reasoning packet + memory search tools.

This module is imported by core_tools_world_model and re-exported by core_tools.py.
Keep it free of imports from core_tools.py to avoid circular imports.
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
            kb_ids = [str(r["id"]) for r in grouped.get("knowledge_base", []) if r.get("id") and r["id"] != 1]
            if kb_ids:
                sb_patch("knowledge_base", f"id=in.({','.join(kb_ids)})", {"last_accessed": datetime.utcnow().isoformat()})
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


def t_reasoning_packet(query: str = "", domain: str = "", limit: str = "10", tables: str = "", per_table: str = "2") -> dict:
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
        return build_reasoning_packet(query=query, domain=domain, tables=table_list, limit=lim, per_table=pt)
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
            "error", "failed", "fail", "broken", "degraded", "stale",
            "blocked", "collision", "conflict", "missing", "invalid",
            "unauthorized", "crash", "traceback", "timeout",
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
        risk_markers = self._risk_markers(" ".join([
            self.query,
            self.state_hint,
            focus,
            context,
            " ".join(h.get("title", "") for h in hits[:5]),
            " ".join(h.get("body", "") for h in hits[:5]),
        ]))

        coherence_score = round(min(1.0, 0.35 + (table_support * 0.08) + (evidence_count * 0.03)), 3)
        evidence_score = round(min(1.0, 0.25 + (evidence_count * 0.08)), 3)
        risk_score = round(min(1.0, 0.12 * risk_markers), 3)
        readiness_score = round(max(0.0, min(1.0, (coherence_score * 0.45) + (evidence_score * 0.35) - (risk_score * 0.4))), 3)
        confidence = round(max(0.0, min(1.0, (evidence_score * 0.5) + (coherence_score * 0.3) + ((1.0 - risk_score) * 0.2))), 3)
        recommendation = "proceed" if readiness_score >= 0.72 and risk_score <= 0.24 else "defer" if risk_score >= 0.48 else "reassess"

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


def t_evaluate_state(query: str = "", domain: str = "general", tables: str = "", limit: str = "10", per_table: str = "2", state_hint: str = "") -> dict:
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


class DynamicRelationalGraph:
    """Build a lightweight relational graph from a unified reasoning packet."""

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
    def _node_id(table: str, raw_id, title: str) -> str:
        safe_title = _re.sub(r"[^a-z0-9]+", "-", (title or "item").lower()).strip("-")[:24]
        return f"{table}:{raw_id}:{safe_title or 'node'}"

    @staticmethod
    def _node_type(table: str) -> str:
        return {
            "knowledge_base": "memory",
            "behavioral_rules": "rule",
            "mistakes": "mistake",
            "hot_reflections": "reflection",
            "output_reflections": "reflection",
            "evolution_queue": "evolution",
            "conversation_episodes": "episode",
            "sessions": "session",
            "pattern_frequency": "pattern",
        }.get(table, "memory")

    def build(self) -> dict:
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

        nodes = [{
            "id": "query",
            "label": self.query[:120],
            "type": "query",
            "table": "query",
            "score": 1.0,
        }]
        edges = []
        seen = {"query"}
        table_nodes = {}

        for hit in hits:
            table = hit.get("table") or "unknown"
            raw = hit.get("raw") or {}
            raw_id = raw.get("id") or hit.get("id") or hit.get("topic") or hit.get("title") or "0"
            title = hit.get("title") or hit.get("body") or str(raw_id)
            node_id = self._node_id(table, raw_id, title)
            if node_id in seen:
                continue
            seen.add(node_id)
            node = {
                "id": node_id,
                "label": str(title)[:120],
                "type": self._node_type(table),
                "table": table,
                "score": round(float(hit.get("score") or hit.get("semantic_score") or 0.0), 3),
                "raw_id": raw_id,
            }
            nodes.append(node)
            table_nodes.setdefault(table, []).append(node_id)
            edges.append({
                "from": "query",
                "to": node_id,
                "type": "retrieved_from",
                "weight": node["score"],
            })

        for table, node_ids in table_nodes.items():
            if len(node_ids) > 1:
                head = node_ids[0]
                for other in node_ids[1:]:
                    edges.append({
                        "from": head,
                        "to": other,
                        "type": "same_table",
                        "weight": 0.35,
                    })

        graph = {
            "ok": True,
            "query": self.query,
            "domain": self.domain,
            "context": pkt.get("context", ""),
            "focus": pkt.get("focus", ""),
            "memory_by_table": by_table,
            "state_hint": self.state_hint,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "nodes": nodes,
            "edges": edges,
        }
        graph["density"] = round(len(edges) / max(1, len(nodes)), 3)
        graph["dominant_table"] = max(by_table.items(), key=lambda kv: int(kv[1] or 0))[0] if by_table else None
        return graph


def t_dynamic_relational_graph(query: str = "", domain: str = "general", tables: str = "", limit: str = "10", per_table: str = "2", state_hint: str = "") -> dict:
    """Build a deterministic relational graph from unified memory context."""
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
        return DynamicRelationalGraph(
            query=query,
            domain=domain,
            tables=table_list,
            limit=lim,
            per_table=pt,
            state_hint=state_hint,
        ).build()
    except Exception as e:
        return {"ok": False, "error": str(e)}
