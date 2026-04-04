from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core_code_autonomy import code_autonomy_status
from core_config import _env_int, sb_get, sb_post
from core_evolution_autonomy import evolution_autonomy_status
from core_gap_audit import core_gap_audit_status
from core_github import notify
from core_integration_autonomy import integration_autonomy_status
from core_proposal_router import proposal_router_status
from core_repo_map import repo_map_status
from core_research_autonomy import research_autonomy_status
from core_semantic_projection import semantic_projection_status
from core_task_autonomy import autonomy_status
from core_tools import get_system_counts as tool_get_system_counts, t_ping_health, t_state_packet
from core_trading_specialization import trading_specialization_enabled

AUTONOMY_DIGEST_ENABLED = os.getenv("CORE_AUTONOMY_DIGEST_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off"
}
AUTONOMY_DIGEST_INTERVAL_S = max(3600, _env_int("CORE_AUTONOMY_DIGEST_INTERVAL_S", "43200"))
AUTONOMY_DIGEST_CHECK_S = max(300, _env_int("CORE_AUTONOMY_DIGEST_CHECK_S", "900"))
AUTONOMY_DIGEST_WINDOW_HOURS = max(1, _env_int("CORE_AUTONOMY_DIGEST_WINDOW_HOURS", "24"))
AUTONOMY_DIGEST_LIMIT = max(1, _env_int("CORE_AUTONOMY_DIGEST_LIMIT", "5000"))

_INTERFACES = (
    "task_autonomy",
    "research_autonomy",
    "code_autonomy",
    "integration_autonomy",
    "evolution_autonomy",
)

_lock = threading.Lock()
_state = {
    "running": False,
    "last_run_at": "",
    "last_error": "",
    "last_summary": {},
}
_STATE_FILE = Path(__file__).resolve().parent / ".runtime" / "autonomy_digest_state.json"

_NUMERIC_KEYS = {
    "processed",
    "completed",
    "deferred",
    "failed",
    "blocked",
    "errors",
    "skipped",
    "proposed",
    "duplicates",
    "queued",
    "pending",
    "pending_now",
    "in_progress_now",
    "pending_proposals_now",
    "pending_remaining",
    "pending_improvement_tasks",
    "follow_up_queued",
}


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _window_start(hours: int) -> str:
    return (datetime.utcnow() - timedelta(hours=hours)).isoformat()


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return 0


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _first_action(actions: Any) -> Any:
    if isinstance(actions, list):
        return actions[0] if actions else {}
    return actions


def _parse_session_payload(row: dict) -> dict:
    payload = _as_dict(_first_action(row.get("actions")))
    if payload:
        return payload
    summary = _as_dict(row.get("summary"))
    if summary:
        return summary
    text = str(_first_action(row.get("actions")) or "").strip()
    if not text:
        return {}
    payload = {}
    for key in sorted(_NUMERIC_KEYS):
        m = re.search(rf"\b{re.escape(key)}=(\d+)", text)
        if m:
            payload[key] = int(m.group(1))
    return payload


def _sum_field(rows: list[dict], field: str) -> int:
    total = 0
    for row in rows:
        total += _safe_int(row.get(field))
    return total


def _load_local_digest_state() -> dict[str, Any]:
    try:
        raw = _STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_local_digest_state(payload: dict[str, Any]) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception:
        pass


def _aggregate_sessions() -> dict[str, dict[str, int]]:
    window_start = _window_start(AUTONOMY_DIGEST_WINDOW_HOURS)
    rows = sb_get(
        "sessions",
        (
            "select=interface,summary,actions,created_at"
            f"&created_at=gte.{window_start}"
            f"&interface=in.({','.join(_INTERFACES)})"
            "&order=created_at.desc"
            f"&limit={AUTONOMY_DIGEST_LIMIT}"
        ),
        svc=True,
    ) or []

    aggregated: dict[str, Counter] = {name: Counter() for name in _INTERFACES}
    for row in rows:
        interface = str(row.get("interface") or "").strip()
        if interface not in aggregated:
            continue
        payload = _parse_session_payload(row)
        for key, value in payload.items():
            if key in _NUMERIC_KEYS:
                aggregated[interface][key] += _safe_int(value)
    return {k: dict(v) for k, v in aggregated.items()}


def _count_rows(table: str, qs: str) -> int:
    try:
        rows = sb_get(table, qs, svc=True) or []
        return len(rows)
    except Exception:
        return 0


def _count_recent_rows(table: str, ts_field: str = "created_at", extra_qs: str = "") -> int:
    extra = extra_qs if extra_qs.startswith("&") or not extra_qs else f"&{extra_qs}"
    return _count_rows(
        table,
        f"select=id&{ts_field}=gte.{_window_start(AUTONOMY_DIGEST_WINDOW_HOURS)}{extra}",
    )


def _escape(value: Any, limit: int = 220) -> str:
    if value is None:
        return ""
    return html.escape(str(value).strip()[:limit], quote=False)


def _worker_error_label(name: str, status: dict) -> str:
    err = str(status.get("last_error") or "").strip()
    if not err:
        return ""
    return f"{name}: {_escape(err, 160)}"


def _render_flag_list(flags: list[str]) -> str:
    items = [_escape(flag, 64) for flag in flags if str(flag).strip()]
    return ", ".join(items) if items else "none"


def _current_last_digest_at() -> str:
    local_state = _load_local_digest_state()
    local_last = str(local_state.get("last_digest_at") or "").strip()
    if local_last:
        return local_last
    try:
        rows = sb_get(
            "sessions",
            "select=created_at&interface=eq.autonomy_digest&order=created_at.desc&limit=1",
            svc=True,
        ) or []
    except Exception:
        rows = []
    if not rows:
        return ""
    return str(rows[0].get("created_at") or "")


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _build_message(agg: dict[str, dict[str, int]]) -> tuple[str, dict]:
    counts = tool_get_system_counts() or {}
    health = t_ping_health() or {}
    state_packet = t_state_packet(session_id="default") or {}
    state_verification = state_packet.get("verification") or {}
    task_status = autonomy_status() if callable(autonomy_status) else {}
    research_status = research_autonomy_status() if callable(research_autonomy_status) else {}
    code_status = code_autonomy_status() if callable(code_autonomy_status) else {}
    integration_status = integration_autonomy_status() if callable(integration_autonomy_status) else {}
    evolution_status = evolution_autonomy_status() if callable(evolution_autonomy_status) else {}
    proposal_status = proposal_router_status(limit=5) or {}
    repo_status = repo_map_status() or {}
    semantic_status = semantic_projection_status() or {}
    audit_status = core_gap_audit_status() or {}

    task = agg.get("task_autonomy", {})
    research = agg.get("research_autonomy", {})
    code = agg.get("code_autonomy", {})
    integration = agg.get("integration_autonomy", {})
    evolution = agg.get("evolution_autonomy", {})

    training = health.get("training_pipeline", {}) or {}
    readiness = health.get("trading_readiness", {}) or {}
    blocking_flags = list(training.get("blocking_flags", []) or [])
    informational_flags = list(training.get("informational_flags", []) or [])
    components = health.get("components", {}) or {}
    component_errors = [f"{name}={value}" for name, value in components.items() if str(value) != "ok"]

    review_ready_rows = _safe_int(proposal_status.get("cluster_review_ready_rows"))
    owner_in_queue = _safe_int(proposal_status.get("pending_owner_only"))
    approved = _count_recent_rows("evolution_queue", "updated_at", "&status=eq.applied")
    rejected = _count_recent_rows("evolution_queue", "updated_at", "&status=eq.rejected")

    kb_added = _count_recent_rows("knowledge_base")
    mistakes_added = _count_recent_rows("mistakes")
    sessions_added = _count_recent_rows("sessions")
    hot_added = _count_recent_rows("hot_reflections")
    output_reflections_added = _count_recent_rows("output_reflections")
    rules_added = _count_recent_rows("behavioral_rules")

    audit_report = audit_status.get("last_report", {}) or {}
    audit_summary = audit_report.get("summary", {}) or {}
    audit_gaps = _safe_int(audit_summary.get("gap_count"))
    audit_critical = _safe_int(audit_summary.get("critical_count"))
    audit_warning = _safe_int(audit_summary.get("warning_count"))

    worker_errors = [
        label for label in [
            _worker_error_label("task", task_status),
            _worker_error_label("research", research_status),
            _worker_error_label("code", code_status),
            _worker_error_label("integration", integration_status),
            _worker_error_label("evolution", evolution_status),
            _worker_error_label("semantic", semantic_status),
        ] if label
    ]

    working_ok = health.get("overall") == "ok" and bool(state_verification.get("verified")) and not component_errors and not blocking_flags
    learning_active = any([
        kb_added,
        mistakes_added,
        hot_added,
        output_reflections_added,
        rules_added,
        _safe_int(task.get("completed")),
        _safe_int(research.get("completed")),
    ])
    evolving_active = any([
        _safe_int(code.get("proposed")),
        _safe_int(integration.get("proposed")),
        _safe_int(evolution.get("queued")),
        approved,
        rejected,
        owner_in_queue,
        _safe_int(code_status.get("pending_review_proposals")),
        _safe_int(integration_status.get("pending_review_proposals")),
    ])

    blocking_items = []
    heads_up_items = []
    if component_errors:
        blocking_items.append(f"External checks: {_render_flag_list(component_errors)}")
    if blocking_flags:
        blocking_items.append(f"Pipeline blockers: {_render_flag_list(blocking_flags)}")
    if audit_critical:
        blocking_items.append(f"Manual work audit critical gaps: {audit_critical}")
    if worker_errors:
        blocking_items.extend(worker_errors[:4])
    if not state_verification.get("verified"):
        blocking_items.append(
            f"State continuity degraded: score {float(state_verification.get('verification_score') or 0):.2f}"
        )
    if owner_in_queue:
        heads_up_items.append(
            f"Owner review queue: {owner_in_queue} owner-only proposals ({review_ready_rows} review-ready rows)"
        )
    if informational_flags:
        heads_up_items.append(f"Informational flags: {_render_flag_list(informational_flags)}")
    if audit_gaps and not audit_critical:
        heads_up_items.append(f"Manual work audit gaps: total {audit_gaps} | warning {audit_warning}")
    state_warnings = list(state_verification.get("warnings") or [])
    if state_warnings:
        heads_up_items.append(f"State warnings: {_render_flag_list(state_warnings[:3])}")

    trading_line = ""
    readiness_counts = readiness.get("counts", {}) if isinstance(readiness, dict) else {}
    if trading_specialization_enabled() and isinstance(readiness, dict):
        trading_line = (
            f"Trading readiness: {'ready' if readiness.get('ready') else 'blocked'} | "
            f"rules {readiness_counts.get('rules', 0)} | KB {readiness_counts.get('knowledge_base', 0)} | "
            f"seed sources {readiness_counts.get('seed_sources', 0)} | seed concepts {readiness_counts.get('seed_concepts', 0)}"
        )

    activity_total = (
        kb_added
        + mistakes_added
        + sessions_added
        + hot_added
        + output_reflections_added
        + rules_added
        + _safe_int(task.get("completed"))
        + _safe_int(research.get("completed"))
        + _safe_int(code.get("proposed"))
        + _safe_int(integration.get("proposed"))
        + _safe_int(evolution.get("queued"))
        + approved
        + rejected
        + owner_in_queue
    )

    lines = [
        "<b>CORE Owner Summary</b>",
        f"Window: last {AUTONOMY_DIGEST_WINDOW_HOURS}h | generated {_escape(_utcnow(), 32)}",
        (
            f"Overall: {'WORKING' if working_ok else 'DEGRADED'} | "
            f"learning {'active' if learning_active else 'idle'} | "
            f"evolving {'active' if evolving_active else 'idle'}"
        ),
        "",
        "<b>Working Now</b>",
        (
            f"Health: {_escape(health.get('overall', 'unknown'), 24)} | "
            f"components supabase={_escape(components.get('supabase', 'unknown'), 24)} "
            f"groq={_escape(components.get('groq', 'unknown'), 24)} "
            f"telegram={_escape(components.get('telegram', 'unknown'), 24)} "
            f"github={_escape(components.get('github', 'unknown'), 24)}"
        ),
        f"Pipeline: {'ok' if not blocking_flags else 'degraded'} | blocking {_render_flag_list(blocking_flags)}",
        (
            f"State continuity: {'verified' if state_verification.get('verified') else 'degraded'} | "
            f"score {float(state_verification.get('verification_score') or 0):.2f} | "
            f"warnings {len(state_verification.get('warnings') or [])}"
        ),
    ]
    if trading_line:
        lines.append(trading_line)
    lines.extend([
        (
            f"Repo and semantic: repo {'enabled' if repo_status.get('enabled', True) else 'disabled'} | "
            f"components {counts.get('repo_components', 0)} | chunks {counts.get('repo_component_chunks', 0)} | "
            f"edges {counts.get('repo_component_edges', 0)} | semantic last_run {_escape(semantic_status.get('last_run_at') or 'n/a', 40)}"
        ),
        "",
        "<b>Learning</b>",
        (
            f"New memory: KB +{kb_added} | mistakes +{mistakes_added} | sessions +{sessions_added} | "
            f"hot reflections +{hot_added} | output reflections +{output_reflections_added} | rules +{rules_added}"
        ),
        (
            f"Current memory: KB {counts.get('knowledge_base', 0)} | mistakes {counts.get('mistakes', 0)} | "
            f"sessions {counts.get('sessions', 0)}"
        ),
        (
            f"Worker output: task completed {_safe_int(task.get('completed'))} | "
            f"research completed {_safe_int(research.get('completed'))} | "
            f"follow-up {_safe_int(research.get('follow_up_queued'))}"
        ),
        "",
        "<b>Evolving</b>",
        (
            f"Code: proposed {_safe_int(code.get('proposed'))} | pending code tasks {code_status.get('pending_code_tasks', 0)} | "
            f"review backlog {code_status.get('pending_review_proposals', 0)}"
        ),
        (
            f"Integration: proposed {_safe_int(integration.get('proposed'))} | pending integration tasks {integration_status.get('pending_integration_tasks', 0)} | "
            f"review backlog {integration_status.get('pending_review_proposals', 0)}"
        ),
        (
            f"Evolution: queued {_safe_int(evolution.get('queued'))} | pending {evolution_status.get('pending_evolutions', 0)} | "
            f"approved {approved} | rejected {rejected}"
        ),
        (
            f"Owner review: pending {owner_in_queue} | review-ready rows {review_ready_rows} | "
            f"pending improvement tasks {evolution_status.get('pending_improvement_tasks', 0)}"
        ),
        "",
        "<b>Attention</b>",
    ])
    if blocking_items:
        lines.extend(f"- {item}" for item in blocking_items[:5])
    else:
        lines.append("- No blocking issues detected.")
    if heads_up_items:
        lines.extend(f"- {item}" for item in heads_up_items[:5])
    else:
        lines.append("- No heads-up items right now.")

    summary = {
        "window_hours": AUTONOMY_DIGEST_WINDOW_HOURS,
        "activity_total": activity_total,
        "overall": "WORKING" if working_ok else "DEGRADED",
        "learning": "active" if learning_active else "idle",
        "evolving": "active" if evolving_active else "idle",
        "counts": {
            "knowledge_base": counts.get("knowledge_base", 0),
            "mistakes": counts.get("mistakes", 0),
            "sessions": counts.get("sessions", 0),
            "repo_components": counts.get("repo_components", 0),
            "repo_component_chunks": counts.get("repo_component_chunks", 0),
            "repo_component_edges": counts.get("repo_component_edges", 0),
        },
        "new_memory": {
            "knowledge_base": kb_added,
            "mistakes": mistakes_added,
            "sessions": sessions_added,
            "hot_reflections": hot_added,
            "output_reflections": output_reflections_added,
            "behavioral_rules": rules_added,
        },
        "task": {k: _safe_int(v) for k, v in task.items()},
        "research": {k: _safe_int(v) for k, v in research.items()},
        "code": {k: _safe_int(v) for k, v in code.items()},
        "integration": {k: _safe_int(v) for k, v in integration.items()},
        "evolution": {
            **{k: _safe_int(v) for k, v in evolution.items()},
            "approved_recent": approved,
            "rejected_recent": rejected,
        },
        "owner_review": {
            "in_queue": owner_in_queue,
            "review_ready_rows": review_ready_rows,
        },
        "health": {
            "overall": health.get("overall", "unknown"),
            "components": components,
            "blocking_flags": blocking_flags,
            "informational_flags": informational_flags,
        },
        "state_verification": {
            "verified": bool(state_verification.get("verified")),
            "verification_score": float(state_verification.get("verification_score") or 0),
            "warnings": list(state_verification.get("warnings") or []),
        },
        "audit": {
            "gap_count": audit_gaps,
            "critical_count": audit_critical,
            "warning_count": audit_warning,
        },
        "blocking_items": blocking_items,
        "heads_up_items": heads_up_items,
        "trading_readiness": readiness if isinstance(readiness, dict) else {},
    }
    return "\n".join(lines), summary


def build_owner_digest_message() -> tuple[str, dict]:
    return _build_message(_aggregate_sessions())


def run_autonomy_digest(force: bool = False) -> dict:
    if not AUTONOMY_DIGEST_ENABLED:
        return {"ok": False, "enabled": False, "message": "CORE_AUTONOMY_DIGEST_ENABLED=false"}

    with _lock:
        if _state["running"]:
            return {"ok": False, "busy": True, "message": "autonomy digest already running"}
        _state["running"] = True

    try:
        last_digest_at = _current_last_digest_at()
        last_dt = _parse_iso(last_digest_at)
        now = datetime.utcnow()
        due = force or not last_dt or (now - last_dt).total_seconds() >= AUTONOMY_DIGEST_INTERVAL_S
        if not due:
            remaining = max(0, AUTONOMY_DIGEST_INTERVAL_S - int((now - last_dt).total_seconds())) if last_dt else AUTONOMY_DIGEST_INTERVAL_S
            return {
                "ok": True,
                "enabled": True,
                "sent": False,
                "due": False,
                "next_check_in": remaining,
                "last_digest_at": last_digest_at,
            }

        message, summary = build_owner_digest_message()
        notify(message)
        try:
            sb_post(
                "sessions",
                {
                    "summary": f"[state_update] autonomy_digest_last_run: {_utcnow()}",
                    "actions": [json.dumps(summary, default=str)],
                    "interface": "autonomy_digest",
                },
            )
        except Exception:
            pass
        finished_at = _utcnow()
        _save_local_digest_state({
            "last_checked_at": finished_at,
            "last_digest_at": finished_at,
            "last_empty_at": "",
            "last_activity_total": _safe_int(summary.get("activity_total")),
            "summary": summary,
        })
        _state["last_run_at"] = finished_at
        _state["last_error"] = ""
        _state["last_summary"] = summary
        return {
            "ok": True,
            "enabled": True,
            "sent": True,
            "finished_at": finished_at,
            "last_digest_at": last_digest_at,
            "message": message,
            "summary": summary,
        }
    except Exception as e:
        _state["last_error"] = str(e)
        return {"ok": False, "enabled": True, "error": str(e)}
    finally:
        with _lock:
            _state["running"] = False


def autonomy_digest_loop() -> None:
    # Check frequently, but only emit a digest once per configured interval.
    while AUTONOMY_DIGEST_ENABLED:
        try:
            result = run_autonomy_digest(force=False)
            if result.get("busy"):
                time.sleep(min(60, AUTONOMY_DIGEST_CHECK_S))
            else:
                time.sleep(AUTONOMY_DIGEST_CHECK_S)
        except Exception as e:
            _state["last_error"] = str(e)
            time.sleep(min(300, AUTONOMY_DIGEST_CHECK_S))


def autonomy_digest_status() -> dict:
    return {
        "ok": True,
        "enabled": AUTONOMY_DIGEST_ENABLED,
        "running": _state["running"],
        "interval_seconds": AUTONOMY_DIGEST_INTERVAL_S,
        "check_seconds": AUTONOMY_DIGEST_CHECK_S,
        "window_hours": AUTONOMY_DIGEST_WINDOW_HOURS,
        "last_run_at": _state["last_run_at"],
        "last_error": _state["last_error"],
        "last_summary": _state["last_summary"],
        "last_digest_at": _current_last_digest_at(),
    }
