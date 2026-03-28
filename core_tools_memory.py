"""core_tools_memory.py — reasoning packet + unified memory search + StateEvaluator.

This module is intentionally kept free of imports from core_tools.py (facade) and
other tool-family modules to avoid circular imports.
"""

from __future__ import annotations

import json
import re as _re
from dataclasses import dataclass
from datetime import datetime

import httpx

from core_config import SUPABASE_URL, _sbh_count_svc, sb_get, sb_patch


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


def _safe_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _safe_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _count_rows(table: str, filters: str = "") -> int:
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?select=id&limit=1"
        if filters:
            url += f"&{filters}"
        r = httpx.get(url, headers=_sbh_count_svc(), timeout=10)
        cr = r.headers.get("content-range", "*/0")
        return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return -1


def _latest_agentic_session(session_id: str = "default") -> dict:
    try:
        filters = f"session_id=eq.{session_id}" if session_id else ""
        q = "select=id,session_id,state,step_index,current_step,completed_steps,action_log,last_updated,goal,status,chat_id,created_at&order=created_at.desc&limit=1"
        rows = sb_get("agentic_sessions", f"{filters}&{q}" if filters else q, svc=True) or []
        if not rows and session_id in ("", "default"):
            rows = sb_get(
                "agentic_sessions",
                "select=id,session_id,state,step_index,current_step,completed_steps,action_log,last_updated,goal,status,chat_id,created_at&order=created_at.desc&limit=1",
                svc=True,
            ) or []
        if not rows:
            return {"ok": False, "found": False, "row": None}
        row = rows[0]
        row["state"] = _safe_dict(row.get("state"))
        row["completed_steps"] = _safe_list(row.get("completed_steps"))
        row["action_log"] = _safe_list(row.get("action_log"))
        return {"ok": True, "found": True, "row": row}
    except Exception as e:
        return {"ok": False, "found": False, "error": str(e), "row": None}


def _latest_checkpoint() -> dict:
    try:
        rows = sb_get(
            "sessions",
            "select=id,summary,checkpoint_data,checkpoint_ts,created_at&order=created_at.desc&limit=5",
            svc=True,
        ) or []
        for row in rows:
            checkpoint = _safe_dict(row.get("checkpoint_data"))
            if checkpoint:
                return {
                    "ok": True,
                    "found": True,
                    "session_id": row.get("id"),
                    "checkpoint": checkpoint,
                    "checkpoint_ts": row.get("checkpoint_ts"),
                    "created_at": row.get("created_at"),
                }
        return {"ok": True, "found": False, "checkpoint": None}
    except Exception as e:
        return {"ok": False, "found": False, "error": str(e), "checkpoint": None}


def _collect_state_updates(limit: int = 20) -> dict:
    try:
        lim = max(1, min(int(limit or 20), 50))
    except Exception:
        lim = 20
    try:
        rows = sb_get(
            "sessions",
            f"select=summary,created_at&summary=like.*%5Bstate_update%5D*&order=created_at.desc&limit={lim}",
            svc=True,
        ) or []
    except Exception:
        rows = []

    updates: dict[str, str] = {}
    ordered: list[dict] = []
    for row in rows:
        raw = row.get("summary") or ""
        if not isinstance(raw, str) or "[state_update]" not in raw:
            continue
        payload = raw.split("[state_update]", 1)[-1].strip()
        if ": " in payload:
            key, value = payload.split(": ", 1)
        else:
            key, value = payload, ""
        key = key.strip()
        value = value.strip()
        if key and key not in updates:
            updates[key] = value
        ordered.append({
            "key": key,
            "value": value,
            "created_at": row.get("created_at"),
        })
    return {"latest": updates, "rows": ordered, "count": len(ordered)}


def _latest_session_snapshot_raw() -> dict:
    try:
        rows = sb_get(
            "sessions",
            "select=summary,created_at&summary=like.*%5Bstate_update%5D+session_snapshot:*&order=created_at.desc&limit=1",
            svc=True,
        ) or []
        if not rows:
            return {"ok": False, "found": False, "snapshot": None, "created_at": None}
        raw = rows[0].get("summary", "") or ""
        prefix = "[state_update] session_snapshot: "
        payload = raw[len(prefix):].strip() if raw.startswith(prefix) else raw
        try:
            snapshot = json.loads(payload)
        except Exception:
            snapshot = {"raw": payload}
        return {
            "ok": True,
            "found": True,
            "snapshot": snapshot,
            "created_at": rows[0].get("created_at"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "found": False, "snapshot": None}


@dataclass
class StatePacket:
    session_id: str
    latest_session: dict
    agentic_session: dict
    checkpoint: dict
    session_snapshot: dict
    state_updates: dict
    state_update_rows: list
    counts: dict
    verification: dict

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "latest_session": self.latest_session,
            "agentic_session": self.agentic_session,
            "checkpoint": self.checkpoint,
            "session_snapshot": self.session_snapshot,
            "state_updates": self.state_updates,
            "state_update_rows": self.state_update_rows,
            "counts": self.counts,
            "verification": self.verification,
        }


def _build_state_packet(session_id: str = "default", strict: bool = False) -> StatePacket:
    latest_session_rows = sb_get(
        "sessions",
        "select=id,summary,actions,created_at,interface,checkpoint_data,checkpoint_ts,resume_task,quality_score,domain&order=created_at.desc&limit=1",
        svc=True,
    ) or []
    latest_session = latest_session_rows[0] if latest_session_rows else {}
    latest_session["actions"] = _safe_list(latest_session.get("actions"))
    latest_session["checkpoint_data"] = _safe_dict(latest_session.get("checkpoint_data"))

    agentic = _latest_agentic_session(session_id=session_id)
    agentic_row = agentic.get("row") or {}
    checkpoint = _latest_checkpoint()
    checkpoint_row = checkpoint.get("checkpoint") or {}
    state_updates = _collect_state_updates(limit=20)

    counts = {
        "sessions": _count_rows("sessions"),
        "agentic_sessions": _count_rows("agentic_sessions"),
        "task_pending": _count_rows("task_queue", "status=eq.pending"),
        "task_in_progress": _count_rows("task_queue", "status=eq.in_progress"),
        "task_done": _count_rows("task_queue", "status=eq.done"),
        "task_failed": _count_rows("task_queue", "status=eq.failed"),
        "evolution_pending": _count_rows("evolution_queue", "status=eq.pending"),
        "evolution_applied": _count_rows("evolution_queue", "status=eq.applied"),
        "evolution_rejected": _count_rows("evolution_queue", "status=eq.rejected"),
    }

    passed_checks = []
    failed_checks = []
    warnings = []

    if latest_session:
        passed_checks.append("latest_session_found")
    else:
        failed_checks.append("latest_session_missing")

    if agentic.get("found") and isinstance(agentic_row.get("state"), dict):
        passed_checks.append("agentic_state_dict")
    else:
        failed_checks.append("agentic_state_missing")

    if checkpoint.get("found") and isinstance(checkpoint_row, dict):
        passed_checks.append("checkpoint_available")
    else:
        warnings.append("checkpoint_missing_or_empty")

    if state_updates.get("count", 0) > 0:
        passed_checks.append("state_updates_present")
    else:
        warnings.append("no_state_updates_found")

    if counts.get("sessions", -1) >= 0 and counts.get("agentic_sessions", -1) >= 0:
        passed_checks.append("counts_available")
    else:
        warnings.append("count_lookup_issue")

    coverage = len(passed_checks) + len(failed_checks)
    verification_score = round(len(passed_checks) / max(1, coverage), 3)
    blocked = bool(strict and failed_checks)
    verified = verification_score >= 0.6 and not blocked
    summary = (
        f"session={'ok' if latest_session else 'missing'} | "
        f"agentic={'ok' if agentic.get('found') else 'missing'} | "
        f"checkpoint={'ok' if checkpoint.get('found') else 'missing'} | "
        f"state_updates={state_updates.get('count', 0)}"
    )

    verification = {
        "verified": verified,
        "blocked": blocked,
        "verification_score": verification_score,
        "passed_checks": passed_checks,
        "failed_checks": failed_checks,
        "warnings": warnings,
        "summary": summary,
    }

    return StatePacket(
        session_id=session_id,
        latest_session=latest_session,
        agentic_session=agentic_row,
        checkpoint=checkpoint_row,
        session_snapshot=_safe_dict(_latest_session_snapshot_raw().get("snapshot")),
        state_updates=state_updates.get("latest", {}),
        state_update_rows=state_updates.get("rows", []),
        counts=counts,
        verification=verification,
    )


def t_state_packet(
    session_id: str = "default",
    strict: str = "false",
) -> dict:
    """Canonical state packet for continuity, checkpoints, and verification."""
    try:
        packet = _build_state_packet(
            session_id=session_id or "default",
            strict=str(strict).strip().lower() in ("true", "1", "yes"),
        ).to_dict()
        return {"ok": True, **packet}
    except Exception as e:
        return {"ok": False, "error": str(e), "session_id": session_id or "default"}


def t_state_consistency_check(
    session_id: str = "default",
    strict: str = "false",
) -> dict:
    """Lightweight verification wrapper over the canonical state packet."""
    try:
        packet = t_state_packet(session_id=session_id or "default", strict=strict)
        if not packet.get("ok"):
            return packet
        verification = packet.get("verification") or {}
        return {
            "ok": True,
            "session_id": packet.get("session_id") or (session_id or "default"),
            "verified": verification.get("verified", False),
            "blocked": verification.get("blocked", False),
            "verification_score": verification.get("verification_score", 0.0),
            "passed_checks": verification.get("passed_checks", []),
            "failed_checks": verification.get("failed_checks", []),
            "warnings": verification.get("warnings", []),
            "summary": verification.get("summary", ""),
            "state_packet": packet,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "session_id": session_id or "default"}


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
