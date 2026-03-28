"""core_proposal_router.py -- human-in-loop proposal routing for CORE.

This module is the operational surface for uncertain or architectural
proposals. It does not auto-apply code changes. Instead it:

- summarizes the pending review queue;
- classifies each proposal into a work track;
- produces review packets for the owner;
- reroutes approved proposals into the correct worker queue.
"""
from __future__ import annotations

import html
import json
import threading
from collections import Counter
from datetime import datetime
from typing import Any

import httpx

from core_config import SUPABASE_URL, _sbh_count_svc, sb_get, sb_patch, sb_post
from core_github import notify
from core_work_taxonomy import build_autonomy_contract

_lock = threading.Lock()
_state = {
    "last_run_at": "",
    "last_summary": {},
    "last_error": "",
    "running": False,
}

_AUTO_ROUTE_TRACKS = {
    "db_only": "db",
    "behavioral_rule": "behavior",
    "research": "research",
}

_OWNER_ONLY_TRACKS = {"code_patch", "new_module", "integration", "proposal_only"}
_OWNER_ONLY_TIERS = {"owner_only", "owner_review"}


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _safe_text(value: Any, limit: int = 240) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _escape(value: Any, limit: int = 240) -> str:
    return html.escape(_safe_text(value, limit), quote=False)


def _count_rows(table: str, qs: str) -> int:
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
            headers=_sbh_count_svc(),
            timeout=15,
        )
        cr = r.headers.get("content-range", "*/0")
        return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return 0


def _normalize_review_route(route: str) -> dict:
    token = (route or "").strip().lower().replace("-", "_")
    aliases = {
        "db": "db_only",
        "database": "db_only",
        "kb": "db_only",
        "knowledge": "db_only",
        "behavior": "behavioral_rule",
        "rule": "behavioral_rule",
        "research": "research",
        "code": "code_patch",
        "patch": "code_patch",
        "module": "new_module",
        "new_module": "new_module",
        "integration": "integration",
    }
    work_track = aliases.get(token, token)
    if work_track not in {"db_only", "behavioral_rule", "research", "code_patch", "new_module", "integration"}:
        return {}
    route_worker = {
        "db_only": "task_autonomy",
        "behavioral_rule": "task_autonomy",
        "research": "research_autonomy",
        "code_patch": "code_autonomy",
        "new_module": "code_autonomy",
        "integration": "integration_autonomy",
    }[work_track]
    route_source = "improvement" if work_track in {"db_only", "behavioral_rule"} else "mcp_session"
    return {
        "work_track": work_track,
        "route_worker": route_worker,
        "route_source": route_source,
        "task_group": {
            "db_only": "knowledge",
            "behavioral_rule": "behavior",
            "research": "research",
            "code_patch": "code",
            "new_module": "code",
            "integration": "integration",
        }[work_track],
    }


def _fetch_pending_reviews(limit: int = 5) -> list[dict]:
    try:
        # Use core_tools._sel_force when available to avoid schema drift 400s.
        try:
            from core_tools import _sel_force  # type: ignore
            sel = _sel_force(
                "evolution_queue",
                [
                    "id",
                    "status",
                    "change_type",
                    "change_summary",
                    "confidence",
                    "impact",
                    "recommendation",
                    "source",
                    "pattern_key",
                    "approval_tier",
                    "diff_content",
                    "created_at",
                ],
            )
        except Exception:
            sel = "id,status,change_type,change_summary,confidence,impact,recommendation,source,pattern_key,approval_tier,diff_content,created_at"
        rows = sb_get(
            "evolution_queue",
            f"select={sel}"
            f"&status=eq.pending&order=confidence.desc&limit={max(1, min(limit, 500))}",
            svc=True,
        ) or []
        return rows
    except Exception:
        return []


def _fetch_review_item(evolution_id: str | int) -> dict:
    try:
        eid = int(evolution_id)
    except Exception:
        return {}
    try:
        rows = sb_get(
            "evolution_queue",
            "select=id,status,change_type,change_summary,confidence,impact,recommendation,pattern_key,source,diff_content,created_at,updated_at"
            f"&id=eq.{eid}&limit=1",
            svc=True,
        ) or []
        return rows[0] if rows else {}
    except Exception:
        return {}


def _row_strategy(row: dict) -> dict:
    autonomy = row.get("autonomy") or {}
    if isinstance(autonomy, str):
        try:
            autonomy = json.loads(autonomy)
        except Exception:
            autonomy = {}
    if not autonomy:
        diff_content = row.get("diff_content") or {}
        if isinstance(diff_content, str):
            try:
                diff_content = json.loads(diff_content)
            except Exception:
                diff_content = {}
        if isinstance(diff_content, dict):
            autonomy = diff_content.get("autonomy") or {}
            if isinstance(autonomy, str):
                try:
                    autonomy = json.loads(autonomy)
                except Exception:
                    autonomy = {}
    summary = _safe_text(row.get("change_summary") or "", 500)
    change_type = _safe_text(row.get("change_type") or "unknown", 40)
    recommendation = _safe_text(row.get("recommendation") or "", 800)
    strategy = build_autonomy_contract(
        summary or f"Evolution #{row.get('id')}",
        description=f"{change_type}\n{summary}\n{recommendation}",
        source=_safe_text(row.get("source") or "evolution_queue", 80),
        autonomy=autonomy,
        context="proposal_router",
    )
    if not strategy.get("work_track"):
        strategy["work_track"] = "proposal_only"
    if not strategy.get("specialized_worker"):
        strategy["specialized_worker"] = "proposal_router"
    return strategy


def _explicit_owner_only(row: dict) -> bool | None:
    # Future-proof: allow explicit flags without breaking older rows.
    scope = (row.get("review_scope") or "").strip().lower()
    if scope in {"owner_only", "owner-review", "owner_review"}:
        return True
    if scope in {"worker", "auto", "automated"}:
        return False
    tier = (row.get("approval_tier") or "").strip().lower()
    if tier in _OWNER_ONLY_TIERS:
        return True
    if tier and tier not in _OWNER_ONLY_TIERS:
        return False
    return None


def _is_owner_only(row: dict, strategy: dict) -> bool:
    explicit = _explicit_owner_only(row)
    if explicit is not None:
        return explicit
    return (strategy.get("work_track") or "proposal_only") in _OWNER_ONLY_TRACKS


def _review_packet(row: dict) -> dict:
    strategy = _row_strategy(row)
    route = _normalize_review_route(strategy.get("work_track") or "")
    recommendation = _safe_text(row.get("recommendation") or "", 800)
    confidence = 0.0
    try:
        confidence = float(row.get("confidence") or 0)
    except Exception:
        confidence = 0.0
    return {
        "evolution_id": row.get("id"),
        "change_type": _safe_text(row.get("change_type") or "unknown", 40),
        "change_summary": _safe_text(row.get("change_summary") or "", 500),
        "confidence": confidence,
        "impact": _safe_text(row.get("impact") or "", 120),
        "recommendation": recommendation,
        "work_track": strategy.get("work_track") or "proposal_only",
        "review_scope": strategy.get("review_scope") or "owner_only",
        "owner_only": bool(strategy.get("owner_only", strategy.get("review_scope") == "owner_only")),
        "task_group": strategy.get("task_group") or route.get("task_group") or "architecture",
        "route_worker": route.get("route_worker") or "proposal_router",
        "route_source": route.get("route_source") or "mcp_session",
        "approval_tier": _safe_text(row.get("approval_tier") or "", 40),
        "source": _safe_text(row.get("source") or "evolution_queue", 80),
        "packet": {
            "kind": strategy.get("kind") or "architecture_proposal",
            "domain": strategy.get("domain") or "project",
            "verification": strategy.get("verification") or "evolution_queue artifact exists",
            "expected_artifact": strategy.get("expected_artifact") or "evolution_queue",
            "specialized_worker": strategy.get("specialized_worker") or "proposal_router",
        },
    }


def _auto_route_reviewable(rows: list[dict]) -> dict:
    auto_routed = []
    auto_errors = []
    route_counts = Counter()
    for row in rows:
        strategy = _row_strategy(row)
        work_track = strategy.get("work_track") or "proposal_only"
        if work_track not in _AUTO_ROUTE_TRACKS:
            continue
        try:
            res = queue_review_reroute(row, _AUTO_ROUTE_TRACKS[work_track], reason="Auto-routed to existing worker")
            if res.get("ok"):
                auto_routed.append({
                    "evolution_id": row.get("id"),
                    "work_track": work_track,
                    "route_worker": res.get("route_worker"),
                })
                route_counts[res.get("route_worker") or "unknown"] += 1
            else:
                auto_errors.append({
                    "evolution_id": row.get("id"),
                    "work_track": work_track,
                    "error": res.get("error") or "auto_route_failed",
                })
        except Exception as e:
            auto_errors.append({
                "evolution_id": row.get("id"),
                "work_track": work_track,
                "error": str(e),
            })
    return {
        "auto_routed": auto_routed,
        "auto_errors": auto_errors,
        "auto_route_counts": dict(sorted(route_counts.items())),
    }


def proposal_router_status(limit: int = 5) -> dict:
    with _lock:
        _state["running"] = True
    try:
        rows = _fetch_pending_reviews(limit=1000)
        auto = _auto_route_reviewable(rows)
        rows = [row for row in rows if (_row_strategy(row).get("work_track") or "proposal_only") not in _AUTO_ROUTE_TRACKS]
        packets = []
        for row in rows:
            strat = _row_strategy(row)
            if not _is_owner_only(row, strat):
                continue
            packets.append(_review_packet(row))
        track_counts = Counter(pkt["work_track"] for pkt in packets)
        route_counts = Counter(pkt["route_worker"] for pkt in packets)
        group_counts = Counter(pkt["task_group"] for pkt in packets)
        top_packets = packets[:max(1, min(limit, 25))]
        status = {
            "enabled": True,
            "running": False,
            "last_run_at": _state["last_run_at"],
            "pending": _count_rows("evolution_queue", "select=id&status=eq.pending"),
            "pending_owner_only": len(packets),
            "track_counts": dict(sorted(track_counts.items())),
            "route_counts": dict(sorted(route_counts.items())),
            "task_group_counts": dict(sorted(group_counts.items())),
            "review_packets": packets,
            "top_packets": top_packets,
            "auto_routed_counts": auto.get("auto_route_counts", {}),
            "auto_routed": auto.get("auto_routed", []),
            "auto_route_errors": auto.get("auto_errors", []),
        }
        with _lock:
            _state["last_run_at"] = _utcnow()
            _state["last_summary"] = status
            _state["last_error"] = ""
        return status
    except Exception as e:
        with _lock:
            _state["last_error"] = str(e)
        return {
            "enabled": True,
            "running": False,
            "last_run_at": _state["last_run_at"],
            "pending": 0,
            "track_counts": {},
            "route_counts": {},
            "task_group_counts": {},
            "review_packets": [],
            "top_packets": [],
            "auto_routed_counts": {},
            "auto_routed": [],
            "auto_route_errors": [],
            "error": str(e),
        }
    finally:
        with _lock:
            _state["running"] = False


def render_proposal_router_dashboard(status: dict | None = None, summary_note: str = "") -> str:
    status = status or proposal_router_status()
    lines = [
        f"Pending proposals (total): {status.get('pending', 0)}",
        f"Pending proposals (owner_only): {status.get('pending_owner_only', 0)}",
        f"Tracks: " + (", ".join(f"{_escape(k)}={v}" for k, v in sorted((status.get('track_counts') or {}).items())) or "none"),
        f"Routes: " + (", ".join(f"{_escape(k)}={v}" for k, v in sorted((status.get('route_counts') or {}).items())) or "none"),
        f"Auto-routed: " + (", ".join(f"{_escape(k)}={v}" for k, v in sorted((status.get('auto_routed_counts') or {}).items())) or "none"),
        "",
        "<b>Review (read-only)</b>",
        "This queue contains owner_only proposals only.",
        "Auto-routing note: db_only, behavioral_rule, and research proposals are routed automatically to existing workers.",
        "Apply actions happen in workers or desktop review flows, not from Telegram.",
    ]
    if summary_note:
        lines.append(summary_note)
    packets = status.get("top_packets") or []
    if packets:
        lines.append("<b>Top proposals</b>")
        for pkt in packets[:5]:
            lines.append(
                f"- #{pkt.get('evolution_id')} [{_escape(pkt.get('change_type') or 'unknown', 20)}] "
                f"conf={float(pkt.get('confidence') or 0):.2f} | {_escape(pkt.get('change_summary') or '', 120)}"
            )
            lines.append(
                f"  track={_escape(pkt.get('work_track') or 'proposal_only', 40)} | "
                f"route={_escape(pkt.get('route_worker') or 'proposal_router', 40)} | "
                f"group={_escape(pkt.get('task_group') or 'architecture', 40)}"
            )
    else:
        lines.append("No pending proposals right now.")
    return "\n".join(lines)


def queue_review_reroute(row: dict, route: str, reason: str = "") -> dict:
    proposal = _normalize_review_route(route)
    if not proposal:
        return {
            "ok": False,
            "error": "invalid route",
            "valid_routes": ["db", "behavior", "research", "code", "module", "integration"],
        }

    try:
        evolution_id = int(row.get("id") or 0)
    except Exception:
        return {"ok": False, "error": "invalid evolution id"}

    summary = _safe_text(row.get("change_summary") or "", 240)
    confidence = 0.0
    try:
        confidence = float(row.get("confidence") or 0)
    except Exception:
        confidence = 0.0
    route_worker = proposal["route_worker"]
    route_source = proposal["route_source"]
    work_track = proposal["work_track"]
    task_title = f"[ROUTED] {summary[:120]}" if summary else f"[ROUTED] Evolution #{evolution_id}"
    task_description = (
        f"Rerouted from evolution #{evolution_id}\n"
        f"Track: {work_track}\n"
        f"Worker: {route_worker}\n"
        f"Proposal confidence: {confidence:.2f}\n"
        f"Reason: {reason or 'owner reroute'}\n"
        f"Original change type: {_safe_text(row.get('change_type') or 'unknown', 40)}\n"
        f"Summary: {_safe_text(row.get('change_summary') or '', 800)}\n"
        f"Recommendation: {_safe_text(row.get('recommendation') or '', 800)}"
    )
    autonomy = build_autonomy_contract(
        task_title,
        description=task_description,
        source=route_source,
        autonomy={
            "kind": "proposal_router",
            "origin": "review_command",
            "source": route_source,
            "evolution_id": str(evolution_id),
            "work_track": work_track,
            "execution_mode": "proposal" if work_track in {"research", "code_patch", "new_module", "integration"} else "db_write",
            "verification": "task_queue row exists and is picked up by the matching worker",
            "expected_artifact": "task_queue",
            "task_group": proposal["task_group"],
            "specialized_worker": route_worker,
            "route": route_worker,
            "priority": 3 if confidence < 0.7 else 2 if confidence < 0.9 else 1,
        },
        context="proposal_router",
    )
    task_payload = {
        "title": task_title,
        "description": task_description,
        "source": route_source,
        "evolution_id": str(evolution_id),
        "review_action": "reroute",
        "review_reason": reason or "",
        "autonomy": autonomy,
    }
    task_ok = sb_post(
        "task_queue",
        {
            "task": json.dumps(task_payload, default=str),
            "status": "pending",
            "priority": autonomy.get("priority", 3),
            "source": route_source,
        },
    )
    if not task_ok:
        return {"ok": False, "error": "failed to queue routed task"}

    update_note = (
        f"Rerouted via /review to {route_worker} "
        f"(track={work_track}, source={route_source}). {reason or 'Owner reroute.'}"
    )
    sb_patch(
        "evolution_queue",
        f"id=eq.{evolution_id}",
        {
            "status": "synthesized",
            "recommendation": update_note[:800],
            "updated_at": datetime.utcnow().isoformat(),
        },
    )
    return {
        "ok": True,
        "evolution_id": evolution_id,
        "route_worker": route_worker,
        "work_track": work_track,
        "source": route_source,
        "task_title": task_title,
    }


def proposal_router_summary(limit: int = 5) -> dict:
    status = proposal_router_status(limit=limit)
    # Keep operator/UI payloads small: summary returns only top packets, not the full queue.
    top_packets = status.get("top_packets", []) or []
    return {
        "ok": True,
        "enabled": status.get("enabled", True),
        "pending": status.get("pending", 0),
        "pending_owner_only": status.get("pending_owner_only", 0),
        "track_counts": status.get("track_counts", {}),
        "route_counts": status.get("route_counts", {}),
        "task_group_counts": status.get("task_group_counts", {}),
        "auto_routed_counts": status.get("auto_routed_counts", {}),
        "review_packets": top_packets,
        "dashboard": render_proposal_router_dashboard(status),
        "last_run_at": status.get("last_run_at", ""),
        "error": status.get("error", ""),
    }


def register_tools() -> None:
    try:
        from core_tools import TOOLS
    except Exception:
        return

    if "proposal_router_status" not in TOOLS:
        TOOLS["proposal_router_status"] = {
            "fn": lambda limit="5": proposal_router_summary(limit=int(limit or 5)),
            "desc": "Return the proposal router dashboard, queue depth, and routing recommendations.",
            "args": [
                {"name": "limit", "type": "string", "description": "Maximum proposal packets to include (default 5)."},
            ],
        }


register_tools()
