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
import hashlib
import re as _re
import threading
from collections import Counter
from collections import defaultdict
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
_CLUSTER_THEMES = (
    ("changelog", ("changelog", "change log", "release note")),
    ("session", ("session", "state continuity", "state_update", "checkpoint", "resume")),
    ("knowledge", ("kb", "knowledge base", "knowledge_base", "knowledge")),
    ("verification", ("verification", "verify", "validation", "validator")),
    ("task", ("task", "queue", "tracking")),
    ("tool", ("tool", "mcp", "api", "helper", "packet")),
    ("research", ("research", "signal", "source", "evidence")),
)


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _safe_text(value: Any, limit: int = 240) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _escape(value: Any, limit: int = 240) -> str:
    return html.escape(_safe_text(value, limit), quote=False)


def _extract_text_blob(row: dict, strategy: dict | None = None) -> str:
    parts = [
        _safe_text(row.get("change_type") or "", 80),
        _safe_text(row.get("change_summary") or "", 500),
        _safe_text(row.get("recommendation") or "", 500),
        _safe_text(row.get("pattern_key") or "", 200),
    ]
    diff_content = row.get("diff_content")
    if diff_content:
        if isinstance(diff_content, str):
            parts.append(diff_content[:1000])
        else:
            parts.append(json.dumps(diff_content, default=str)[:1000])
    if strategy:
        parts.extend([
            _safe_text(strategy.get("work_track") or "", 80),
            _safe_text(strategy.get("route_worker") or "", 80),
            _safe_text(strategy.get("task_group") or "", 80),
        ])
    return " | ".join(p for p in parts if p)


def _extract_target_module(row: dict, strategy: dict | None = None) -> str:
    text = _extract_text_blob(row, strategy).lower()
    matches = _re.findall(r"(core_[a-z0-9_]+\.py)", text)
    if matches:
        return matches[0]
    if "changelog" in text:
        return "changelog"
    if "session" in text or "state_update" in text or "checkpoint" in text:
        return "session_state"
    if "knowledge" in text or "kb" in text:
        return "knowledge_base"
    if "task" in text or "queue" in text:
        return "task_queue"
    if "tool" in text or "mcp" in text:
        return "core_tools"
    if "train" in text or "learning" in text or "rarl" in text or "meta" in text:
        return "core_train"
    return "general"


def _extract_theme_bucket(row: dict, strategy: dict | None = None) -> str:
    text = _extract_text_blob(row, strategy).lower()
    for theme, needles in _CLUSTER_THEMES:
        if any(n in text for n in needles):
            return theme
    if "code" in text:
        return "code"
    if "integration" in text:
        return "integration"
    return "general"


def _owner_review_cluster_key(row: dict, strategy: dict | None = None) -> str:
    strategy = strategy or _row_strategy(row)
    work_track = strategy.get("work_track") or "proposal_only"
    module = _extract_target_module(row, strategy)
    theme = _extract_theme_bucket(row, strategy)
    route_worker = strategy.get("route_worker") or "proposal_router"
    return f"{work_track}|{route_worker}|{module}|{theme}"


def _owner_review_cluster_packet(rows: list[dict], persist: bool = True) -> dict:
    """Cluster owner-only rows into semantic review packets and optionally persist them."""
    clusters: dict[str, dict] = {}
    for row in rows or []:
        strat = _row_strategy(row)
        if not _is_owner_only(row, strat):
            continue
        key = _owner_review_cluster_key(row, strat)
        cluster = clusters.setdefault(key, {
            "cluster_key": key,
            "work_track": strat.get("work_track") or "proposal_only",
            "route_worker": strat.get("route_worker") or "proposal_router",
            "task_group": strat.get("task_group") or "architecture",
            "target_module": _extract_target_module(row, strat),
            "theme": _extract_theme_bucket(row, strat),
            "review_scope": "owner_only",
            "owner_only": True,
            "member_ids": [],
            "summaries": [],
            "confidence_values": [],
            "recommendations": [],
            "created_at_values": [],
            "source_values": [],
        })
        cluster["member_ids"].append(int(row.get("id") or 0))
        cluster["summaries"].append(_safe_text(row.get("change_summary") or "", 500))
        try:
            cluster["confidence_values"].append(float(row.get("confidence") or 0))
        except Exception:
            pass
        cluster["recommendations"].append(_safe_text(row.get("recommendation") or "", 500))
        cluster["created_at_values"].append(_safe_text(row.get("created_at") or "", 80))
        cluster["source_values"].append(_safe_text(row.get("source") or "", 40))

    packets = []
    for key, cluster in sorted(clusters.items(), key=lambda kv: (kv[1]["target_module"], kv[1]["theme"], kv[0])):
        members = sorted(set(m for m in cluster["member_ids"] if m))
        if not members:
            continue
        anchor_id = members[0]
        latest_id = members[-1]
        mean_conf = round(sum(cluster["confidence_values"]) / max(1, len(cluster["confidence_values"])), 2)
        sample_summaries = []
        for summary in cluster["summaries"]:
            summary = summary.strip()
            if summary and summary not in sample_summaries:
                sample_summaries.append(summary)
            if len(sample_summaries) >= 5:
                break
        packet = {
            "cluster_key": key,
            "cluster_id": hashlib.sha1(key.encode("utf-8")).hexdigest()[:12],
            "count": len(members),
            "anchor_id": anchor_id,
            "latest_id": latest_id,
            "member_ids": members,
            "work_track": cluster["work_track"],
            "route_worker": cluster["route_worker"],
            "task_group": cluster["task_group"],
            "target_module": cluster["target_module"],
            "theme": cluster["theme"],
            "review_scope": cluster["review_scope"],
            "owner_only": cluster["owner_only"],
            "mean_confidence": mean_conf,
            "sample_summaries": sample_summaries,
            "member_summary": sample_summaries[0] if sample_summaries else "",
            "shared_verification": (
                "code proposal queued for owner review"
                if cluster["work_track"] in {"code_patch", "new_module", "integration", "proposal_only"}
                else "owner review cluster"
            ),
            "shared_artifact": "evolution_queue",
            "shared_route": cluster["route_worker"],
            "close_ready": True,
            "close_eligible_members": len(members),
            "close_blocked_members": 0,
        }
        packets.append(packet)

    packets.sort(key=lambda p: (-int(p.get("count") or 0), int(p.get("anchor_id") or 0), p.get("cluster_key") or ""))
    persisted = []
    persist_errors = []
    if persist:
        try:
            from core_tools import t_kb_update
        except Exception as exc:
            t_kb_update = None  # type: ignore
            persist_errors.append(str(exc))
        if t_kb_update:
            for pkt in packets:
                topic = f"cluster:{pkt['cluster_id']}"
                content = json.dumps(pkt, default=str)
                instruction = (
                    "Use this owner-review cluster packet as the canonical batch summary. "
                    "Do not route worker lanes from this packet."
                )
                try:
                    res = t_kb_update(
                        domain="owner_review_cluster",
                        topic=topic,
                        instruction=instruction,
                        content=content,
                        confidence="high",
                        source_type="evolved",
                        source_ref=f"owner_review_cluster:{pkt['cluster_key']}",
                    )
                    persisted.append({
                        "cluster_key": pkt["cluster_key"],
                        "topic": topic,
                        "ok": bool(res.get("ok")),
                        "verified": bool(res.get("verified")),
                    })
                except Exception as exc:
                    persist_errors.append(f"{pkt['cluster_key']}: {exc}")
    return {
        "ok": True,
        "clusters": packets,
        "cluster_count": len(packets),
        "member_count": sum(int(pkt.get("count") or 0) for pkt in packets),
        "persisted": persisted,
        "persist_errors": persist_errors,
    }


def _index_cluster_packets(packet: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    clusters = packet.get("clusters") or []
    by_id = {}
    by_key = {}
    for cluster in clusters:
        cid = _safe_text(cluster.get("cluster_id") or "", 64)
        ckey = _safe_text(cluster.get("cluster_key") or "", 200)
        if cid:
            by_id[cid] = cluster
        if ckey:
            by_key[ckey] = cluster
    return by_id, by_key


def _resolve_cluster_for_close(
    packet: dict,
    cluster_id: str = "",
    cluster_key: str = "",
) -> dict:
    by_id, by_key = _index_cluster_packets(packet)
    cid = _safe_text(cluster_id or "", 64)
    ckey = _safe_text(cluster_key or "", 200)
    if cid and cid in by_id:
        return by_id[cid]
    if ckey and ckey in by_key:
        return by_key[ckey]
    if cid:
        return {}
    if ckey:
        return {}
    return {}


def _cluster_close_note(
    cluster: dict,
    outcome: str,
    reason: str = "",
    reviewer: str = "owner",
) -> str:
    core = (
        f"Cluster batch-close by {reviewer}: cluster={_safe_text(cluster.get('cluster_id') or '', 40)} "
        f"key={_safe_text(cluster.get('cluster_key') or '', 140)} "
        f"outcome={outcome} count={int(cluster.get('count') or 0)}."
    )
    if reason:
        core += f" Reason: {_safe_text(reason, 420)}"
    return core[:780]


def owner_review_cluster_close(
    cluster_id: str = "",
    cluster_key: str = "",
    outcome: str = "applied",
    reason: str = "",
    reviewed_by: str = "owner",
    dry_run: str = "false",
) -> dict:
    """Close a full owner-only cluster in one controlled loop.

    Outcome is `applied` or `rejected`. This function only targets rows that are
    still `status=pending` and owner-only by the current proposal-router policy.
    """
    target_outcome = _safe_text(outcome or "applied", 20).lower()
    if target_outcome not in {"applied", "rejected"}:
        return {"ok": False, "error": "invalid outcome", "valid_outcomes": ["applied", "rejected"]}

    dry = str(dry_run or "").strip().lower() in {"1", "true", "yes", "on"}
    rows = _fetch_pending_reviews(limit=5000)
    owner_rows = []
    for row in rows:
        strat = _row_strategy(row)
        if _is_owner_only(row, strat):
            owner_rows.append(row)

    packet = _owner_review_cluster_packet(owner_rows, persist=False)
    cluster = _resolve_cluster_for_close(packet, cluster_id=cluster_id, cluster_key=cluster_key)
    if not cluster:
        clusters = packet.get("clusters") or []
        return {
            "ok": False,
            "error": "cluster not found",
            "requested_cluster_id": _safe_text(cluster_id, 64),
            "requested_cluster_key": _safe_text(cluster_key, 200),
            "available_cluster_ids": [_safe_text(c.get("cluster_id") or "", 20) for c in clusters[:25]],
            "available_cluster_keys": [_safe_text(c.get("cluster_key") or "", 120) for c in clusters[:25]],
            "pending_owner_only": len(owner_rows),
        }

    members = [int(mid) for mid in (cluster.get("member_ids") or []) if int(mid or 0) > 0]
    if not members:
        return {"ok": False, "error": "cluster has no members", "cluster_id": cluster.get("cluster_id")}

    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    reviewer = _safe_text(reviewed_by or "owner", 40) or "owner"
    note = _cluster_close_note(cluster, target_outcome, reason=reason, reviewer=reviewer)
    patch_payload = {
        "status": target_outcome,
        "approval_tier": "owner_review",
        "tier_applied_at": now,
        "recommendation": note,
    }
    if target_outcome == "applied":
        patch_payload["applied_at"] = now
    if target_outcome == "rejected":
        patch_payload["rejected_by_owner"] = True

    attempted = 0
    updated = []
    failed = []
    for mid in members:
        attempted += 1
        if dry:
            updated.append(mid)
            continue
        ok = sb_patch("evolution_queue", f"id=eq.{mid}&status=eq.pending", patch_payload)
        if ok:
            updated.append(mid)
        else:
            failed.append(mid)

    result = {
        "ok": len(failed) == 0,
        "cluster_id": cluster.get("cluster_id"),
        "cluster_key": cluster.get("cluster_key"),
        "outcome": target_outcome,
        "attempted": attempted,
        "updated": len(updated),
        "failed": len(failed),
        "updated_ids": updated[:100],
        "failed_ids": failed[:100],
        "dry_run": dry,
        "note": note,
        "pending_owner_only_now": _count_rows(
            "evolution_queue",
            "select=id&status=eq.pending&approval_tier=in.(owner_only,owner_review)",
        ),
    }
    result["close_state"] = "complete" if len(failed) == 0 else "partial"
    result["closed_at"] = now
    result["closed_by"] = reviewer
    if dry:
        result["kb_persisted"] = False
        result["kb_persist_error"] = ""
        return result

    close_packet = dict(cluster)
    close_packet.update({
        "closed": True,
        "close_state": result["close_state"],
        "closed_at": now,
        "closed_by": reviewer,
        "decision": target_outcome,
        "reason": _safe_text(reason, 1200),
        "reviewed_by": reviewer,
        "updated_ids": updated[:100],
        "failed_ids": failed[:100],
        "close_note": note,
    })
    kb_persisted = False
    kb_persist_error = ""
    try:
        from core_tools import t_kb_update

        kb_res = t_kb_update(
            domain="owner_review_cluster",
            topic=f"cluster:{cluster.get('cluster_id')}",
            instruction="Use this owner-review cluster packet as the canonical closure record.",
            content=json.dumps(close_packet, default=str),
            confidence="high",
            source_type="evolved",
            source_ref=f"owner_review_cluster:{cluster.get('cluster_key')}",
        )
        kb_persisted = bool(kb_res.get("ok"))
    except Exception as exc:
        kb_persist_error = str(exc)
    result["kb_persisted"] = kb_persisted
    result["kb_persist_error"] = kb_persist_error
    if not dry and len(updated) > 0:
        try:
            notify(
                f"Owner-review cluster closed: {cluster.get('cluster_id')} "
                f"({target_outcome}) updated={len(updated)} failed={len(failed)}"
            )
        except Exception:
            pass
    return result


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
        cluster_packet = _owner_review_cluster_packet(rows, persist=True)
        track_counts = Counter(pkt["work_track"] for pkt in packets)
        route_counts = Counter(pkt["route_worker"] for pkt in packets)
        group_counts = Counter(pkt["task_group"] for pkt in packets)
        cluster_theme_counts = Counter(pkt["theme"] for pkt in (cluster_packet.get("clusters") or []))
        top_packets = packets[:max(1, min(limit, 25))]
        top_clusters = (cluster_packet.get("clusters") or [])[:max(1, min(limit, 10))]
        status = {
            "enabled": True,
            "running": False,
            "last_run_at": _state["last_run_at"],
            "pending": _count_rows("evolution_queue", "select=id&status=eq.pending"),
            "pending_owner_only": len(packets),
            "track_counts": dict(sorted(track_counts.items())),
            "route_counts": dict(sorted(route_counts.items())),
            "task_group_counts": dict(sorted(group_counts.items())),
            "cluster_count": cluster_packet.get("cluster_count", 0),
            "cluster_member_count": cluster_packet.get("member_count", 0),
            "cluster_theme_counts": dict(sorted(cluster_theme_counts.items())),
            "review_packets": packets,
            "top_packets": top_packets,
            "owner_review_clusters": cluster_packet.get("clusters", []),
            "top_clusters": top_clusters,
            "cluster_persisted": cluster_packet.get("persisted", []),
            "cluster_persist_errors": cluster_packet.get("persist_errors", []),
            "cluster_close_ready_count": sum(
                1 for c in (cluster_packet.get("clusters") or []) if bool(c.get("close_ready"))
            ),
            "cluster_close_ready_rows": sum(
                int(c.get("close_eligible_members") or 0) for c in (cluster_packet.get("clusters") or [])
            ),
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
            "cluster_count": 0,
            "cluster_member_count": 0,
            "cluster_theme_counts": {},
            "review_packets": [],
            "top_packets": [],
            "owner_review_clusters": [],
            "top_clusters": [],
            "cluster_persisted": [],
            "cluster_persist_errors": [],
            "cluster_close_ready_count": 0,
            "cluster_close_ready_rows": 0,
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
        f"Owner-review clusters: {status.get('cluster_count', 0)} groups / {status.get('cluster_member_count', 0)} rows",
        f"Cluster close-ready: {status.get('cluster_close_ready_count', 0)} groups / {status.get('cluster_close_ready_rows', 0)} rows",
        f"Cluster themes: " + (", ".join(f"{_escape(k)}={v}" for k, v in sorted((status.get('cluster_theme_counts') or {}).items())) or "none"),
        "Cluster packets are mirrored into knowledge_base as domain=owner_review_cluster.",
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
    clusters = status.get("top_clusters") or []
    if clusters:
        lines.append("<b>Owner-review clusters</b>")
        for cluster in clusters[:5]:
            lines.append(
                f"- cluster {cluster.get('cluster_id')} | count={int(cluster.get('count') or 0)} | "
                f"module={_escape(cluster.get('target_module') or 'general', 30)} | theme={_escape(cluster.get('theme') or 'general', 30)}"
            )
            members = ", ".join(f"#{mid}" for mid in (cluster.get("member_ids") or [])[:6])
            lines.append(
                f"  anchor=#{cluster.get('anchor_id')} | route={_escape(cluster.get('route_worker') or 'proposal_router', 30)} | "
                f"members={members}"
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
        "cluster_count": status.get("cluster_count", 0),
        "cluster_member_count": status.get("cluster_member_count", 0),
        "cluster_close_ready_count": status.get("cluster_close_ready_count", 0),
        "cluster_close_ready_rows": status.get("cluster_close_ready_rows", 0),
        "cluster_theme_counts": status.get("cluster_theme_counts", {}),
        "auto_routed_counts": status.get("auto_routed_counts", {}),
        "review_packets": top_packets,
        "owner_review_clusters": (status.get("top_clusters", []) or []),
        "dashboard": render_proposal_router_dashboard(status),
        "last_run_at": status.get("last_run_at", ""),
        "error": status.get("error", ""),
    }


def owner_review_cluster_packet(limit: int = 5, persist: str = "true") -> dict:
    """Return clustered owner-only review packets and optionally persist them to KB."""
    try:
        lim = max(1, min(int(limit or 5), 50))
    except Exception:
        lim = 5
    persist_bool = str(persist).strip().lower() not in {"false", "0", "no"}
    rows = _fetch_pending_reviews(limit=5000)
    owner_rows = []
    for row in rows:
        strat = _row_strategy(row)
        if _is_owner_only(row, strat):
            owner_rows.append(row)
    cluster_packet = _owner_review_cluster_packet(owner_rows, persist=persist_bool)
    clusters = cluster_packet.get("clusters") or []
    recommended_cluster = next((c for c in clusters if bool(c.get("close_ready"))), clusters[0] if clusters else {})
    theme_counts = Counter(pkt["theme"] for pkt in clusters)
    target_counts = Counter(pkt["target_module"] for pkt in clusters)
    return {
        "ok": True,
        "limit": lim,
        "owner_pending": len(owner_rows),
        "cluster_count": cluster_packet.get("cluster_count", 0),
        "cluster_member_count": cluster_packet.get("member_count", 0),
        "cluster_close_ready_count": sum(1 for c in clusters if bool(c.get("close_ready"))),
        "cluster_close_ready_rows": sum(int(c.get("close_eligible_members") or 0) for c in clusters),
        "recommended_cluster_id": recommended_cluster.get("cluster_id"),
        "recommended_cluster_key": recommended_cluster.get("cluster_key"),
        "recommended_cluster_count": recommended_cluster.get("count"),
        "recommended_cluster_close_ready": bool(recommended_cluster.get("close_ready")),
        "recommended_action": (
            f"batch_close cluster_id={recommended_cluster.get('cluster_id')} "
            f"cluster_key={recommended_cluster.get('cluster_key')}"
            if recommended_cluster.get("cluster_id") and recommended_cluster.get("close_ready")
            else "inspect_only"
        ),
        "close_instruction": (
            f"Use owner_review_cluster_close(cluster_id='{recommended_cluster.get('cluster_id')}', outcome='applied') "
            f"after verification"
            if recommended_cluster.get("cluster_id") and recommended_cluster.get("close_ready")
            else ""
        ),
        "theme_counts": dict(sorted(theme_counts.items())),
        "target_counts": dict(sorted(target_counts.items())),
        "clusters": clusters[:lim],
        "persisted": cluster_packet.get("persisted", []),
        "persist_errors": cluster_packet.get("persist_errors", []),
        "summary": (
            f"owner_review_clusters={cluster_packet.get('cluster_count', 0)} | "
            f"members={cluster_packet.get('member_count', 0)} | "
            f"close_ready_groups={sum(1 for c in clusters if bool(c.get('close_ready')))} | "
            f"recommended={recommended_cluster.get('cluster_id') or 'none'} | "
            f"action={('batch_close ' + str(recommended_cluster.get('cluster_id'))) if recommended_cluster.get('cluster_id') and recommended_cluster.get('close_ready') else 'inspect_only'} | "
            f"themes={', '.join(f'{k}={v}' for k, v in sorted(theme_counts.items())) or 'none'}"
        ),
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
    if "owner_review_cluster_packet" not in TOOLS:
        TOOLS["owner_review_cluster_packet"] = {
            "fn": lambda limit="5", persist="true": owner_review_cluster_packet(limit=int(limit or 5), persist=persist),
            "desc": "Return clustered owner-only proposal packets and optionally persist semantic cluster packets to KB.",
            "args": [
                {"name": "limit", "type": "string", "description": "Maximum clusters to return (default 5)."},
                {"name": "persist", "type": "string", "description": "Whether to persist cluster packets to KB (default true)."},
            ],
        }
    if "owner_review_cluster_close" not in TOOLS:
        TOOLS["owner_review_cluster_close"] = {
            "fn": lambda cluster_id="", cluster_key="", outcome="applied", reason="", reviewed_by="owner", dry_run="false": owner_review_cluster_close(
                cluster_id=cluster_id,
                cluster_key=cluster_key,
                outcome=outcome,
                reason=reason,
                reviewed_by=reviewed_by,
                dry_run=dry_run,
            ),
            "desc": "Batch-close one owner-only cluster by cluster_id or cluster_key. outcome=applied|rejected. Uses pending-owner rows only.",
            "args": [
                {"name": "cluster_id", "type": "string", "description": "Cluster id from owner_review_cluster_packet."},
                {"name": "cluster_key", "type": "string", "description": "Cluster key fallback if cluster_id is unknown."},
                {"name": "outcome", "type": "string", "description": "applied or rejected (default applied)."},
                {"name": "reason", "type": "string", "description": "Optional audit reason for the batch close."},
                {"name": "reviewed_by", "type": "string", "description": "Reviewer label (default owner)."},
                {"name": "dry_run", "type": "string", "description": "true to preview affected rows without patching."},
            ],
        }


register_tools()
