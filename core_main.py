"""core_main.py â€” CORE AGI entry point
FastAPI app, all routes, Pydantic models, Telegram handler, queue_poller, startup.
Extracted from core.py as part of Task 2 architecture split.

Import chain:
  core_main imports: core_config, core_github, core_train, core_tools
  (no circular deps â€” core_config has no internal imports)

NOTE: This IS the live entry point (Procfile: web: python core_main.py). core.py deleted.
Activation: rename/swap after smoke test passes (Task 2.6).
"""
import asyncio
import hashlib
import html
import json
import os
import threading
import time
import uuid
import secrets
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Header, Query, Depends, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from core_config import (
    MCP_SECRET, MCP_PROTOCOL_VERSION, PORT, SESSION_TTL_H,
    SUPABASE_URL, COLD_KB_GROWTH_THRESHOLD,
    TELEGRAM_CHAT, TELEGRAM_WEBHOOK_SECRET,
    L, sb_get, sb_post, sb_patch, sb_upsert, sb_post_critical,
    _sbh, _sbh_count_svc, groq_chat,
)
from core_github import gh_read, gh_write, notify, set_telegram_commands, set_webhook
from core_train import cold_processor_loop, background_researcher
from core_tools import TOOLS, handle_jsonrpc
from core_reflection_audit import (
    build_reflection_context,
    fetch_reflection_events,
    register_reflection_event,
)
from core_task_autonomy import AUTONOMY_ENABLED, autonomy_loop, autonomy_status, run_autonomy_cycle
from core_code_autonomy import (
    AUTONOMY_ENABLED as CODE_AUTONOMY_ENABLED,
    code_autonomy_loop,
    code_autonomy_status,
    render_code_status_report,
    run_code_autonomy_cycle,
)
from core_integration_autonomy import (
    AUTONOMY_ENABLED as INTEGRATION_AUTONOMY_ENABLED,
    integration_autonomy_loop,
    integration_autonomy_status,
    render_integration_status_report,
    run_integration_autonomy_cycle,
)
from core_evolution_autonomy import (
    AUTONOMY_ENABLED as EVOLUTION_AUTONOMY_ENABLED,
    evolution_autonomy_loop,
    evolution_autonomy_status,
    run_evolution_autonomy_cycle,
)
from core_research_autonomy import (
    AUTONOMY_ENABLED as RESEARCH_AUTONOMY_ENABLED,
    render_research_status_report,
    research_autonomy_loop,
    research_autonomy_status,
    run_research_autonomy_cycle,
)
from core_proposal_router import (
    proposal_router_summary,
    proposal_router_status,
    queue_review_reroute,
    render_proposal_router_dashboard,
)
from core_semantic_projection import (
    PROJECTION_ENABLED as SEMANTIC_PROJECTION_ENABLED,
    semantic_projection_loop,
    semantic_projection_status,
    run_semantic_projection_cycle,
)
from core_repo_map import (
    repo_map_loop,
    repo_map_status,
    render_repo_map_status_report,
    run_repo_map_cycle,
)
from core_gap_audit import (
    build_core_gap_audit,
    core_gap_audit_loop,
    core_gap_audit_status,
    format_core_gap_audit,
    format_core_gap_audit_status,
    notify_core_gap_audit,
)
from core_work_taxonomy import build_autonomy_contract
from core_supabase_bootstrap import bootstrap_supabase as bootstrap_core_supabase

# â”€â”€ Orchestrator v2 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from core_orch_main import handle_telegram_message_v2, startup_v2

CORE_TELEGRAM_COMMANDS = [
    {"command": "start", "description": "Startup brief and quick actions"},
    {"command": "help", "description": "Command catalog"},
    {"command": "status", "description": "Live system and queue summary"},
    {"command": "health", "description": "Component health check"},
    {"command": "queues", "description": "Queue depths and backlog"},
    {"command": "task", "description": "Task autonomy status"},
    {"command": "tasks", "description": "Task autonomy status"},
    {"command": "research", "description": "Research autonomy status"},
    {"command": "code", "description": "Code autonomy status and code plan"},
    {"command": "integration", "description": "Integration autonomy status and cross-repo contract plans"},
    {"command": "evolutions", "description": "Evolution autonomy status"},
    {"command": "review", "description": "Read-only owner-only proposal queue"},
    {"command": "memory", "description": "Knowledge and semantic memory"},
    {"command": "autonomy", "description": "Overall autonomy overview"},
    {"command": "evolution", "description": "Evolution worker details or run"},
    {"command": "semantic", "description": "Semantic projection status or run"},
    {"command": "repo", "description": "Repository semantic map status or sync"},
    {"command": "audit", "description": "CORE manual work audit and gap notifier"},
    {"command": "deploycheck", "description": "Running commit and file hashes"},
    {"command": "project", "description": "Project context tools"},
    {"command": "restart", "description": "Restart CORE service"},
    {"command": "kill", "description": "Abort active loop"},
]

# ---------------------------------------------------------------------------
# Shared helpers (used by routes + tools â€” defined here, imported by core_tools)
# ---------------------------------------------------------------------------
def get_resume_task() -> str:
    """Return title of highest-priority in_progress task from task_queue.
    Used in Telegram startup message and /state endpoint.
    SESSION.md no longer tracks current step -- task_queue is source of truth."""
    try:
        tasks = sb_get(
            "task_queue",
            "select=task,priority,status&source=in.(core_v6_registry,mcp_session)"
            "&status=eq.in_progress&order=priority.desc&limit=1"
        )
        if tasks and isinstance(tasks, list) and tasks[0]:
            raw = tasks[0].get("task", "")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                title = parsed.get("title") or parsed.get("task_id") or str(parsed)[:80]
            except Exception:
                title = str(raw)[:80]
            priority = tasks[0].get("priority", "?")
            return f"Resuming: {title} (P{priority})"
        # No in_progress tasks -- check for pending
        pending = sb_get(
            "task_queue",
            "select=task,priority&source=in.(core_v6_registry,mcp_session)"
            "&status=eq.pending&order=priority.desc&limit=1"
        )
        if pending and isinstance(pending, list) and pending[0]:
            raw = pending[0].get("task", "")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                title = parsed.get("title") or str(parsed)[:80]
            except Exception:
                title = str(raw)[:80]
            return f"Next: {title}"
        return "No active tasks"
    except Exception as e:
        print(f"[STEP] get_resume_task error: {e}")
        return "task_queue unavailable"


def get_latest_session():
    d = sb_get("sessions", "select=summary,actions,created_at&order=created_at.desc&limit=1")
    return d[0] if d else {}


def get_system_counts():
    counts = {}
    # Core brain tables â€” total counts
    table_filters = {
        "knowledge_base": "",
        "mistakes":       "",
        "sessions":       "",
    }
    for t, extra in table_filters.items():
        try:
            r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/{t}?select=id&limit=1{extra}",
                headers=_sbh_count_svc(), timeout=10
            )
            cr = r.headers.get("content-range", "*/0")
            counts[t] = int(cr.split("/")[-1]) if "/" in cr else 0
        except:
            counts[t] = -1
    # task_queue â€” counts by status
    for task_status in ("pending", "in_progress", "done", "failed"):
        try:
            r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/task_queue?select=id&limit=1&status=eq.{task_status}",
                headers=_sbh_count_svc(), timeout=10
            )
            cr = r.headers.get("content-range", "*/0")
            counts[f"task_queue_{task_status}"] = int(cr.split("/")[-1]) if "/" in cr else 0
        except:
            counts[f"task_queue_{task_status}"] = -1
    # evolution_queue â€” counts by status
    for evo_status in ("pending", "applied", "rejected"):
        try:
            r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/evolution_queue?select=id&limit=1&status=eq.{evo_status}",
                headers=_sbh_count_svc(), timeout=10
            )
            cr = r.headers.get("content-range", "*/0")
            counts[f"evolution_{evo_status}"] = int(cr.split("/")[-1]) if "/" in cr else 0
        except:
            counts[f"evolution_{evo_status}"] = -1
    for table in ("repo_components", "repo_component_chunks", "repo_component_edges", "repo_scan_runs"):
        try:
            extra = "&active=eq.true" if table != "repo_scan_runs" else ""
            r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/{table}?select=id&limit=1{extra}",
                headers=_sbh_count_svc(), timeout=10
            )
            cr = r.headers.get("content-range", "*/0")
            counts[table] = int(cr.split("/")[-1]) if "/" in cr else 0
        except:
            counts[table] = -1
    return counts


def _tg_escape(value: object, limit: int = 300) -> str:
    if value in (None, ""):
        return ""
    return html.escape(str(value).strip()[:limit], quote=False)


def _render_section(title: str, lines: list[str], footer: str = "") -> str:
    parts = [f"<b>{_tg_escape(title, 120)}</b>"]
    parts.extend(lines)
    if footer:
        parts.append(footer)
    return "\n".join(parts)


def _render_command_catalog() -> str:
    sections: dict[str, list[str]] = {}
    for item in CORE_TELEGRAM_COMMANDS:
        sections.setdefault(item["command"], [])
    grouped: dict[str, list[dict]] = {
        "Overview": [],
        "Monitoring": [],
        "Workers": [],
        "Review": [],
        "Memory": [],
        "Ops": [],
    }
    for item in CORE_TELEGRAM_COMMANDS:
        if item["command"] in {"start", "help"}:
            grouped["Overview"].append(item)
        elif item["command"] in {"status", "health", "queues"}:
            grouped["Monitoring"].append(item)
        elif item["command"] in {"tasks", "research", "code", "integration", "evolutions", "autonomy", "evolution", "semantic", "repo"}:
            grouped["Workers"].append(item)
        elif item["command"] in {"review"}:
            grouped["Review"].append(item)
        elif item["command"] in {"memory"}:
            grouped["Memory"].append(item)
        else:
            grouped["Ops"].append(item)

    lines = ["<b>CORE Telegram Commands</b>"]
    for group, items in grouped.items():
        if not items:
            continue
        lines.append(f"\n<b>{group}</b>")
        for item in items:
            lines.append(f"/{item['command']} â€” {_tg_escape(item['description'], 120)}")
    lines.append("\nUse /start for the current system snapshot.")
    return "\n".join(lines)


def _count_rows(table: str, qs: str) -> int:
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
            headers=_sbh_count_svc(), timeout=10
        )
        cr = r.headers.get("content-range", "*/0")
        return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return -1


def _memory_counts() -> dict:
    tables = [
        "knowledge_base",
        "mistakes",
        "behavioral_rules",
        "hot_reflections",
        "output_reflections",
        "conversation_episodes",
        "pattern_frequency",
    ]
    out = {}
    for table in tables:
        out[table] = _count_rows(table, "select=id&limit=1")
    return out


def _fetch_pending_reviews(limit: int = 5) -> list[dict]:
    return (proposal_router_status(limit=max(1, min(limit, 25))).get("review_packets") or [])


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


def _normalize_review_route(route: str) -> dict:
    # Backward-compatible wrapper for the dedicated proposal-router module.
    from core_proposal_router import _normalize_review_route as _route
    return _route(route)


def _render_review_dashboard(rows: list[dict], summary_note: str = "") -> str:
    status = proposal_router_status(limit=max(1, min(len(rows) or 5, 25)))
    return _render_section("Proposal Router", render_proposal_router_dashboard(status, summary_note=summary_note).splitlines())


def _proposal_router_cluster_close(
    cluster_id: str = "",
    cluster_key: str = "",
    decision: str = "applied",
    reason: str = "",
    verification_note: str = "",
    reviewed_by: str = "owner",
    dry_run: str = "false",
) -> dict:
    """Call cluster close handler from core_proposal_router with safe fallback."""
    try:
        import core_proposal_router as _pr  # local import to avoid hard startup coupling
    except Exception as e:
        return {"ok": False, "error": f"proposal router unavailable: {e}"}

    candidates = (
        "owner_review_cluster_close",
        "close_owner_review_cluster",
        "owner_review_cluster_resolve",
        "proposal_router_cluster_close",
    )
    fn = None
    for name in candidates:
        maybe = getattr(_pr, name, None)
        if callable(maybe):
            fn = maybe
            break
    if not fn:
        return {
            "ok": False,
            "error": "cluster close function not available in core_proposal_router",
            "expected_any": list(candidates),
        }
    try:
        return fn(
            cluster_id=cluster_id,
            cluster_key=cluster_key,
            outcome=decision,
            reason=reason,
            verification_note=verification_note,
            reviewed_by=reviewed_by,
            dry_run=dry_run,
        )
    except TypeError:
        # Backward-compatible call signature if newer kwargs are not yet supported.
        return fn(
            cluster_id=cluster_id,
            cluster_key=cluster_key,
            outcome=decision,
            reason=reason,
            reviewed_by=reviewed_by,
            dry_run=dry_run,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _queue_review_reroute(row: dict, route: str, reason: str = "") -> dict:
    return queue_review_reroute(row, route, reason=reason)


def _parse_task_title(row: dict) -> str:
    raw = row.get("task", "")
    if isinstance(raw, dict):
        task = raw
    else:
        try:
            task = json.loads(raw) if raw else {}
        except Exception:
            task = {}
    if isinstance(task, dict):
        return str(task.get("title") or task.get("description") or row.get("id") or "task")[:120]
    return str(row.get("id") or "task")[:120]


def _parse_task_track(row: dict) -> str:
    raw = row.get("task", "")
    try:
        task = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        task = {}
    if isinstance(task, dict):
        autonomy = task.get("autonomy") or {}
        if isinstance(autonomy, str):
            try:
                autonomy = json.loads(autonomy)
            except Exception:
                autonomy = {}
        if isinstance(autonomy, dict):
            return str(autonomy.get("work_track") or autonomy.get("kind") or row.get("source") or "unknown")
    return str(row.get("source") or "unknown")


def _render_task_status_report(task_auto: dict) -> str:
    last = task_auto.get("last_summary") or {}
    if not last and task_auto.get("processed_tasks"):
        last = task_auto
    lines = [
        f"Status: <b>{'enabled' if task_auto.get('enabled') else 'disabled'}</b> | running={task_auto.get('running')} | interval={task_auto.get('interval_seconds')}s | batch={task_auto.get('batch_limit')}",
        f"Sources: {_tg_escape(', '.join(task_auto.get('sources') or []), 120)}",
        f"Queue: pending {task_auto.get('pending', 0)} | in_progress {task_auto.get('in_progress', 0)}",
    ]
    if task_auto.get("track_counts"):
        lines.append("Tracks: " + ", ".join(f"{_tg_escape(k)}={v}" for k, v in sorted(task_auto["track_counts"].items())))
    if task_auto.get("deferred"):
        lines.append(f"Deferred tasks: {task_auto.get('deferred', 0)}")
    if last.get("last_run_at"):
        lines.append(f"Last run: {_tg_escape(last.get('finished_at') or task_auto.get('last_run_at'), 40)}")
    processed = last.get("processed_tasks") or []
    if processed:
        lines.append("")
        lines.append("<b>Recent cycle</b>")
        for item in processed[:3]:
            strategy = item.get("strategy") or {}
            execution = item.get("execution") or {}
            status = "done" if item.get("ok") else "failed"
            lines.append(
                f"- #{item.get('task_id')} [{status}] {_tg_escape(item.get('title') or '', 80)} "
                f"({_tg_escape(strategy.get('work_track') or 'unknown', 40)} â†’ {_tg_escape(execution.get('artifact_type') or item.get('artifact_type') or 'unknown', 40)})"
            )
    return _render_section("Task Autonomy", lines)


def _render_evolution_status_report(evo_auto: dict) -> str:
    last = evo_auto.get("last_summary") or {}
    if not last and evo_auto.get("details"):
        last = evo_auto
    monitor = evo_auto.get("backlog_monitor") or last.get("backlog_monitor") or {}
    lines = [
        f"Status: <b>{'enabled' if evo_auto.get('enabled') else 'disabled'}</b> | running={evo_auto.get('running')} | interval={evo_auto.get('interval_seconds')}s | batch={evo_auto.get('batch_limit')}",
        f"Queue: pending {evo_auto.get('pending_evolutions', 0)} | synthesized {evo_auto.get('synthesized_evolutions', 0)} | follow-up tasks {evo_auto.get('pending_improvement_tasks', 0)}",
    ]
    if evo_auto.get("track_counts"):
        lines.append("Tracks: " + ", ".join(f"{_tg_escape(k)}={v}" for k, v in sorted(evo_auto["track_counts"].items())))
    if monitor:
        trend = monitor.get("trend") or "unknown"
        growth = monitor.get("growth")
        task_growth = monitor.get("task_growth")
        lines.append(
            f"Backlog monitor: {_tg_escape(trend, 20)} | window={monitor.get('window', '?')} | growth={growth if growth is not None else '?'} | task_growth={task_growth if task_growth is not None else '?'}"
        )
        alert = monitor.get("alert") or ""
        if alert:
            lines.append(f"Monitor alert: {_tg_escape(alert, 180)}")
    if last.get("last_run_at"):
        lines.append(f"Last run: {_tg_escape(last.get('finished_at') or evo_auto.get('last_run_at'), 40)}")
    details = last.get("details") or []
    if details:
        lines.append("")
        lines.append("<b>Recent synthesis</b>")
        for item in details[:3]:
            outcome = item.get("outcome") or ("created" if item.get("task_created") else "duplicate")
            lines.append(
                f"- #{item.get('evolution_id')} [{_tg_escape(item.get('change_type') or 'unknown', 20)}] {outcome} "
                f"({_tg_escape(item.get('task_group') or 'unknown', 40)} / {_tg_escape(item.get('work_track') or 'unknown', 40)})"
            )
            reason = item.get("reason") or item.get("error")
            if reason:
                lines.append(f"  reason: {_tg_escape(reason, 180)}")
    return _render_section("Evolution Autonomy", lines)


def _render_memory_report() -> str:
    counts = _memory_counts()
    sem = semantic_projection_status() if SEMANTIC_PROJECTION_ENABLED else {}
    lines = [
        f"Knowledge base: {counts.get('knowledge_base', 0)}",
        f"Mistakes: {counts.get('mistakes', 0)} | Behavioral rules: {counts.get('behavioral_rules', 0)}",
        f"Hot reflections: {counts.get('hot_reflections', 0)} | Output reflections: {counts.get('output_reflections', 0)}",
        f"Conversation episodes: {counts.get('conversation_episodes', 0)} | Pattern frequency: {counts.get('pattern_frequency', 0)}",
        f"Semantic projection: {'enabled' if SEMANTIC_PROJECTION_ENABLED else 'disabled'} | last_run={sem.get('last_run_at', 'n/a')}",
    ]
    return _render_section("Memory", lines)


def _render_queue_report(counts: dict, task_auto: dict, evo_auto: dict) -> str:
    review_rows = _fetch_pending_reviews(limit=3)
    code_auto = code_autonomy_status() if CODE_AUTONOMY_ENABLED else {}
    integration_auto = integration_autonomy_status() if INTEGRATION_AUTONOMY_ENABLED else {}
    lines = [
        f"Task queue: pending {counts.get('task_queue_pending', 0)} | in_progress {counts.get('task_queue_in_progress', 0)} | done {counts.get('task_queue_done', 0)} | failed {counts.get('task_queue_failed', 0)}",
        f"Evolution queue: pending {counts.get('evolution_pending', 0)} | applied {counts.get('evolution_applied', 0)} | rejected {counts.get('evolution_rejected', 0)}",
        f"Repo map: components {counts.get('repo_components', 0)} | chunks {counts.get('repo_component_chunks', 0)} | edges {counts.get('repo_component_edges', 0)} | scans {counts.get('repo_scan_runs', 0)}",
        f"Repo map: components {counts.get('repo_components', 0)} | chunks {counts.get('repo_component_chunks', 0)} | edges {counts.get('repo_component_edges', 0)} | scans {counts.get('repo_scan_runs', 0)}",
        f"Task autonomy backlog: {task_auto.get('pending', 0)} pending | {task_auto.get('in_progress', 0)} in progress",
        f"Code autonomy backlog: {code_auto.get('pending_code_tasks', 0)} pending code tasks | {code_auto.get('pending_review_proposals', 0)} review proposals",
        f"Integration autonomy backlog: {integration_auto.get('pending_integration_tasks', 0)} pending integration tasks | {integration_auto.get('pending_review_proposals', 0)} review proposals",
        f"Evolution autonomy backlog: {evo_auto.get('pending_evolutions', counts.get('evolution_pending', 0))} pending",
    ]
    if review_rows:
        lines.append("")
        lines.append("<b>Top proposals</b>")
        for row in review_rows:
            lines.append(
                f"- #{row.get('id')} [{_tg_escape(row.get('change_type') or 'unknown', 20)}] conf={float(row.get('confidence') or 0):.2f} "
                f"{_tg_escape(row.get('change_summary') or '', 110)}"
            )
    return _render_section("Queues", lines)


def _render_autonomy_overview_report(counts: dict, task_auto: dict, evo_auto: dict) -> str:
    from core_tools import t_get_training_pipeline
    tp = t_get_training_pipeline()
    sem = semantic_projection_status() if SEMANTIC_PROJECTION_ENABLED else {}
    review_rows = _fetch_pending_reviews(limit=5)
    proposal = proposal_router_status(limit=5)
    research = research_autonomy_status() if RESEARCH_AUTONOMY_ENABLED else {}
    code_auto = code_autonomy_status() if CODE_AUTONOMY_ENABLED else {}
    integration_auto = integration_autonomy_status() if INTEGRATION_AUTONOMY_ENABLED else {}
    task_last = task_auto.get("last_summary") or {}
    evo_last = evo_auto.get("last_summary") or {}
    code_last = code_auto.get("last_summary") or {}
    integration_last = integration_auto.get("last_summary") or {}
    lines = [
        f"Health: pipeline {'ok' if tp.get('pipeline_ok') else 'degraded'} | flags {'none' if not tp.get('health_flags') else ' | '.join(tp.get('health_flags') or [])}",
        "",
        "<b>Current workers</b>",
        f"Task autonomy: {'enabled' if task_auto.get('enabled') else 'disabled'} | scope=db_only + behavioral_rule | pending {task_auto.get('pending', 0)} | in_progress {task_auto.get('in_progress', 0)}",
        f"  Last run: {_tg_escape(task_last.get('finished_at') or task_auto.get('last_run_at') or 'n/a', 40)} | tracks: " + (
            ", ".join(f"{_tg_escape(k)}={v}" for k, v in sorted(task_auto.get("track_counts", {}).items())) or "none"
        ),
        f"Research autonomy: {'enabled' if research.get('enabled') else 'disabled'} | scope=research proposals -> knowledge capture | pending {research.get('pending', 0)}",
        f"  Last run: {_tg_escape((research.get('last_summary') or {}).get('finished_at') or research.get('last_run_at') or 'n/a', 40)} | completed {research.get('completed_tasks', 0)} | follow-up queued {research.get('follow_up_queued', 0)}",
        f"Code autonomy: {'enabled' if code_auto.get('enabled') else 'disabled'} | scope=code_patch + new_module -> review packet | pending {code_auto.get('pending_code_tasks', 0)} | proposals {code_auto.get('pending_review_proposals', 0)}",
        f"  Last run: {_tg_escape(code_last.get('finished_at') or code_auto.get('last_run_at') or 'n/a', 40)} | tracks: " + (
            ", ".join(f"{_tg_escape(k)}={v}" for k, v in sorted(code_auto.get("track_counts", {}).items())) or "none"
        ),
        f"Integration autonomy: {'enabled' if integration_auto.get('enabled') else 'disabled'} | scope=endpoint wiring + module plumbing + cross-repo contracts -> review packet | pending {integration_auto.get('pending_integration_tasks', 0)} | proposals {integration_auto.get('pending_review_proposals', 0)}",
        f"  Last run: {_tg_escape(integration_last.get('finished_at') or integration_auto.get('last_run_at') or 'n/a', 40)} | tracks: " + (
            ", ".join(f"{_tg_escape(k)}={v}" for k, v in sorted(integration_auto.get("track_counts", {}).items())) or "none"
        ),
        f"Evolution autonomy: {'enabled' if evo_auto.get('enabled') else 'disabled'} | scope=improvement synthesis | pending {evo_auto.get('pending_evolutions', 0)} | synthesized {evo_auto.get('synthesized_evolutions', 0)} | follow-up tasks {evo_auto.get('pending_improvement_tasks', 0)}",
        f"  Queue: pending {counts.get('evolution_pending', 0)} | applied {counts.get('evolution_applied', 0)} | rejected {counts.get('evolution_rejected', 0)}",
        f"  Last run: {_tg_escape(evo_last.get('finished_at') or evo_auto.get('last_run_at') or 'n/a', 40)} | tracks: " + (
            ", ".join(f"{_tg_escape(k)}={v}" for k, v in sorted(evo_auto.get("track_counts", {}).items())) or "none"
        ),
        f"Repo map: {'enabled' if repo_map_status().get('enabled') else 'disabled'} | components {counts.get('repo_components', 0)} | chunks {counts.get('repo_component_chunks', 0)} | edges {counts.get('repo_component_edges', 0)} | scans {counts.get('repo_scan_runs', 0)}",
        f"Proposal router: {'enabled' if proposal.get('enabled') else 'disabled'} | pending {proposal.get('pending', 0)} | routes " + (
            ", ".join(f"{_tg_escape(k)}={v}" for k, v in sorted(proposal.get("route_counts", {}).items())) or "none"
        ),
        f"Semantic projection: {'enabled' if SEMANTIC_PROJECTION_ENABLED else 'disabled'} | last_run {_tg_escape(sem.get('last_run_at') or 'n/a', 40)}",
        "",
        "<b>Worker map</b>",
        "research_autonomy â€” active: validates research proposals, writes knowledge, queues follow-up work",
        "code_autonomy â€” active: generates code-change packets and queues owner review",
        "integration_autonomy â€” active: generates integration packets for endpoint wiring, module plumbing, and cross-repo contracts",
        "proposal_router â€” active: read-only owner-only proposal queue for manual review",
    ]
    if review_rows:
        lines.append("")
        lines.append("<b>Pending proposals</b>")
        for row in review_rows[:5]:
            lines.append(
                f"- #{row.get('id')} [{_tg_escape(row.get('change_type') or 'unknown', 20)}] conf={float(row.get('confidence') or 0):.2f} "
                f"{_tg_escape(row.get('change_summary') or '', 110)}"
            )
    lines.append("")
    lines.append(f"Queue backlog: task {counts.get('task_queue_pending', 0)} | evolution {counts.get('evolution_pending', 0)}")
    return _render_section("Autonomy Overview", lines)


def _render_health_report() -> str:
    from core_tools import t_health
    from core_tools import t_get_training_pipeline
    h = t_health()
    tp = t_get_training_pipeline()
    comps = h.get("components", {})
    lines = [
        f"Supabase: {_tg_escape(comps.get('supabase', 'unknown'))}",
        f"Groq: {_tg_escape(comps.get('groq', 'unknown'))}",
        f"Telegram: {_tg_escape(comps.get('telegram', 'unknown'))}",
        f"GitHub: {_tg_escape(comps.get('github', 'unknown'))}",
        f"Pipeline: {_tg_escape((tp.get('pipeline_ok') and 'ok') or (tp.get('health_flags') and 'degraded') or 'unknown')}",
        f"Pipeline flags: {_tg_escape(' | '.join(tp.get('health_flags') or []) or 'none')}",
    ]
    return _render_section("Health", lines)


def _render_code_status_report(code_auto: dict) -> str:
    return render_code_status_report(code_auto)


def _render_integration_status_report(integration_auto: dict) -> str:
    return render_integration_status_report(integration_auto)


def _render_deployment_report() -> str:
    manifest = _deployment_manifest()
    lines = [
        f"Service: {_tg_escape(manifest.get('service', 'core-agi'))}",
        f"Commit: {_tg_escape(manifest.get('git_commit') or 'unknown', 40)}",
        f"Runtime: port {manifest.get('runtime_mode', {}).get('port', '?')} | MCP {manifest.get('runtime_mode', {}).get('mcp_protocol_version', '?')}",
        f"Files tracked: {len(manifest.get('files', {}))}",
    ]
    return _render_section("Deployment", lines)


def _render_repo_report() -> str:
    status = repo_map_status()
    return render_repo_map_status_report(status)


def _build_startup_brief(resume: str, counts: dict, orch: dict, task_auto: dict | None = None, evo_auto: dict | None = None) -> str:
    from core_tools import t_state_packet
    task_pending = counts.get("task_queue_pending", 0)
    task_in_progress = counts.get("task_queue_in_progress", 0)
    task_done = counts.get("task_queue_done", 0)
    task_failed = counts.get("task_queue_failed", 0)
    if resume and resume != "No active tasks":
        task_summary = resume
    elif task_pending > 0:
        task_summary = f"Pending backlog: {task_pending} task(s)"
    else:
        task_summary = "Task queue idle"
    evo_pending = counts.get("evolution_pending", 0)
    evo_applied = counts.get("evolution_applied", 0)
    evo_rejected = counts.get("evolution_rejected", 0)
    task_auto = task_auto or {}
    evo_auto = evo_auto or {}
    proposal = {"enabled": False, "pending": 0, "route_counts": {}}
    try:
        proposal = proposal_router_status(limit=3) or proposal
    except Exception as e:
        print(f"[CORE] startup brief proposal router unavailable: {e}")
    task_sources = ", ".join(task_auto.get("sources") or ["self_assigned", "improvement"])
    evo_pending_count = evo_auto.get("pending_evolutions", evo_pending)
    evo_synthesized = evo_auto.get("synthesized_evolutions", 0)
    evo_task_pending = evo_auto.get("pending_improvement_tasks", 0)
    code_auto = {}
    integration_auto = {}
    sem_proj = {}
    audit_status = {"enabled": False, "last_run_at": "n/a", "last_report": {}}
    state_packet = {}
    try:
        if CODE_AUTONOMY_ENABLED:
            code_auto = code_autonomy_status() or {}
    except Exception as e:
        print(f"[CORE] startup brief code autonomy unavailable: {e}")
    try:
        if INTEGRATION_AUTONOMY_ENABLED:
            integration_auto = integration_autonomy_status() or {}
    except Exception as e:
        print(f"[CORE] startup brief integration autonomy unavailable: {e}")
    try:
        if SEMANTIC_PROJECTION_ENABLED:
            sem_proj = semantic_projection_status() or {}
    except Exception as e:
        print(f"[CORE] startup brief semantic projection unavailable: {e}")
    try:
        audit_status = core_gap_audit_status() or audit_status
    except Exception as e:
        print(f"[CORE] startup brief manual audit unavailable: {e}")
    try:
        state_packet = t_state_packet(session_id="default") or {}
    except Exception as e:
        print(f"[CORE] startup brief state packet unavailable: {e}")
    state_verification = state_packet.get("verification") or {}
    try:
        research_pending = research_autonomy_status().get("pending", 0) if RESEARCH_AUTONOMY_ENABLED else 0
    except Exception as e:
        print(f"[CORE] startup brief research autonomy unavailable: {e}")
        research_pending = 0
    try:
        audit_label = format_core_gap_audit_status(audit_status)
    except Exception as e:
        print(f"[CORE] startup brief audit formatting failed: {e}")
        audit_label = "unavailable"
    return (
        f"🧠 <b>CORE Online</b>\n"
        f"Orchestrator: <b>{orch.get('model', 'unknown')}</b> | {orch.get('layers', 'L0-L9 active')} | {orch.get('blueprint', '')}\n\n"
        f"<b>State</b>\n"
        f"KB: {counts.get('knowledge_base', 0)} | Mistakes: {counts.get('mistakes', 0)} | Sessions: {counts.get('sessions', 0)}\n"
        f"Repo map: components {counts.get('repo_components', 0)} | chunks {counts.get('repo_component_chunks', 0)} | edges {counts.get('repo_component_edges', 0)} | scans {counts.get('repo_scan_runs', 0)}\n"
        f"Manual work audit: {audit_label}\n"
        f"State continuity: {'verified' if state_verification.get('verified') else 'degraded'} | score {state_verification.get('verification_score', 0):.2f} | warnings {len(state_verification.get('warnings') or [])}\n"
        f"Task queue: pending {task_pending} | in_progress {task_in_progress} | done {task_done} | failed {task_failed} | {task_summary}\n"
        f"Evolutions: pending {evo_pending} | applied {evo_applied} | rejected {evo_rejected}\n"
        f"Task autonomy: {'enabled' if AUTONOMY_ENABLED else 'disabled'} | pending {task_auto.get('pending', 0)} | in_progress {task_auto.get('in_progress', 0)} | sources {task_sources}\n"
        f"Research autonomy: {'enabled' if RESEARCH_AUTONOMY_ENABLED else 'disabled'} | pending {research_pending}\n"
        f"Code autonomy: {'enabled' if CODE_AUTONOMY_ENABLED else 'disabled'} | pending {code_auto.get('pending_code_tasks', 0)} | proposals {code_auto.get('pending_review_proposals', 0)}\n"
        f"Integration autonomy: {'enabled' if INTEGRATION_AUTONOMY_ENABLED else 'disabled'} | pending {integration_auto.get('pending_integration_tasks', 0)} | proposals {integration_auto.get('pending_review_proposals', 0)}\n"
        f"Evolution autonomy: {'enabled' if EVOLUTION_AUTONOMY_ENABLED else 'disabled'} | pending {evo_pending_count} | synthesized {evo_synthesized} | follow-up tasks {evo_task_pending}\n"
        f"Proposal router: {'enabled' if proposal.get('enabled') else 'disabled'} | pending {proposal.get('pending', 0)} | routes {', '.join(f'{k}={v}' for k, v in sorted((proposal.get('route_counts') or {}).items())) or 'none'}\n"
        f"Semantic projection: {'enabled' if SEMANTIC_PROJECTION_ENABLED else 'disabled'} | last_run {sem_proj.get('last_run_at', 'n/a')}\n"
        f"MCP: {len(TOOLS)} tools | Webhook: set | Loops: queue, cold, research, synthesis, diagnosis, autonomy, code-autonomy, integration-autonomy, research-autonomy, evolution-autonomy, semantic-projection, repo-map"
    )

def self_sync_check():
    from core_config import CORE_SELF_STALE_DAYS
    try:
        core_self = gh_read("CORE_SELF.md")
        last_updated = None
        for line in core_self.splitlines():
            if "Last updated:" in line:
                date_str = line.split("Last updated:")[-1].strip()
                try:
                    last_updated = datetime.strptime(date_str, "%Y-%m-%d")
                except:
                    pass
                break
        if not last_updated:
            notify("CORE Self-Sync Warning\nCORE_SELF.md has no Last updated date.")
            return {"ok": False, "reason": "no_date"}
        days_stale = (datetime.utcnow() - last_updated).days
        if days_stale > CORE_SELF_STALE_DAYS:
            recent = sb_get("sessions", "select=id&order=created_at.desc&limit=1", svc=True)
            if recent:
                notify(
                    f"CORE Self-Sync Warning\n"
                    f"CORE_SELF.md last updated {days_stale} days ago.\n"
                    f"Active sessions detected since then.\n"
                    f"Please review and update CORE_SELF.md.\n"
                    f"github.com/pockiesaints7/core-agi/blob/main/CORE_SELF.md"
                )
                print(f"[SELF_SYNC] WARNING: CORE_SELF.md is {days_stale} days stale")
                return {"ok": False, "days_stale": days_stale, "warned": True}
        print(f"[SELF_SYNC] OK - CORE_SELF.md updated {days_stale}d ago")
        return {"ok": True, "days_stale": days_stale}
    except Exception as e:
        print(f"[SELF_SYNC] error: {e}")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# MCP session management
# ---------------------------------------------------------------------------
_sessions: dict = {}


def _is_owner_chat(chat_id: str) -> bool:
    return bool(chat_id) and secrets.compare_digest(str(chat_id), str(TELEGRAM_CHAT))


def _telegram_webhook_ok(req: Request) -> bool:
    token = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    return bool(token) and secrets.compare_digest(str(token), str(TELEGRAM_WEBHOOK_SECRET))


def mcp_new(ip: str) -> str:
    tok = hashlib.sha256(f"{MCP_SECRET}{ip}{time.time()}".encode()).hexdigest()[:32]
    _sessions[tok] = {
        "ip": ip,
        "expires": (datetime.utcnow() + timedelta(hours=SESSION_TTL_H)).isoformat(),
        "calls": 0,
    }
    now = datetime.utcnow()
    expired = [k for k, v in _sessions.items() if datetime.fromisoformat(v["expires"]) < now]
    for k in expired:
        del _sessions[k]
    return tok


def mcp_ok(tok: str) -> bool:
    if tok not in _sessions:
        return False
    if datetime.utcnow() > datetime.fromisoformat(_sessions[tok]["expires"]):
        del _sessions[tok]
        return False
    _sessions[tok]["calls"] += 1
    return True


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class Handshake(BaseModel):
    secret: str
    client_id: Optional[str] = "claude_desktop"


class ToolCall(BaseModel):
    session_token: str
    tool: str
    args: dict = {}


class PatchRequest(BaseModel):
    secret: str
    path: str
    old_str: str
    new_str: str
    message: str
    repo: Optional[str] = ""


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="CORE v6.0", version="6.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_sse_sessions: dict = {}
_sse_shutdown = asyncio.Event()

def _shutdown_sse_sessions() -> None:
    """Wake all open SSE streams so shutdown can complete promptly."""
    _sse_shutdown.set()
    for queue in list(_sse_sessions.values()):
        while queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


async def require_mcp_secret(
    x_mcp_secret: Optional[str] = Header(None, alias="X-MCP-Secret"),
    authorization: Optional[str] = Header(None),
    secret_query: Optional[str] = Query(None, alias="secret")
):
    token = x_mcp_secret or secret_query
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token or not secrets.compare_digest(str(token), str(MCP_SECRET)):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


def _git_commit() -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            text=True,
            timeout=5,
        ).strip()
    except Exception:
        return None


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _deployment_manifest() -> dict:
    base = Path(__file__).resolve().parent
    files = [
        "core_main.py",
        "core_orch_layer11.py",
        "core_meta_evaluator.py",
        "core_tools.py",
        "core_tools_code_reader.py",
        "core_tools_memory.py",
        "core_tools_governance.py",
        "core_tools_task.py",
        "core_reasoning_packet.py",
        "core_work_taxonomy.py",
        "core_code_autonomy.py",
        "core_integration_autonomy.py",
        "core_proposal_router.py",
        "core_research_autonomy.py",
        "core_reflection_audit.py",
        "core_task_autonomy.py",
        "core_evolution_autonomy.py",
        "core_train.py",
        "core_semantic_projection.py",
        "core_worker_critic.py",
        "core_worker_reflect.py",
        "core_orch_agent.py",
        "run_reflection_audit_ddl.py",
    ]
    manifest = {}
    for rel in files:
        path = base / rel
        manifest[rel] = {
            "sha256": _file_sha256(path),
            "bytes": path.stat().st_size,
        }
    return {
        "service": "core-agi",
        "pid": os.getpid(),
        "git_commit": _git_commit(),
        "cwd": str(base),
        "runtime_mode": {
            "port": PORT,
            "mcp_protocol_version": MCP_PROTOCOL_VERSION,
            "telegram_webhook_secret_configured": bool(TELEGRAM_WEBHOOK_SECRET),
            "mcp_secret_configured": bool(MCP_SECRET),
            "supabase_host": SUPABASE_URL.split("://", 1)[-1].split("/", 1)[0] if SUPABASE_URL else "",
        },
        "files": manifest,
        "generated_at": datetime.utcnow().isoformat(),
    }


def _trade_tokens(position_id: Optional[int], decision_id: Optional[int]) -> list[str]:
    tokens = []
    if position_id is not None:
        tokens.extend([f"Position ID: {position_id}", f"position_id={position_id}", f"_p{position_id}"])
    if decision_id is not None:
        tokens.extend([f"Decision ID: {decision_id}", f"decision_id={decision_id}", f"_d{decision_id}"])
    return tokens


def _matches_trade_trace(text: str, position_id: Optional[int], decision_id: Optional[int]) -> bool:
    haystack = str(text or "")
    if not haystack:
        return False
    matched = False
    for token in _trade_tokens(position_id, decision_id):
        if token in haystack:
            matched = True
            break
    if not matched:
        return False
    if position_id is not None and f"{position_id}" not in haystack:
        return False
    if decision_id is not None and f"{decision_id}" not in haystack:
        return False
    return True


# ---------------------------------------------------------------------------
# Knowledge ingestion endpoints (TASK-22)
# ---------------------------------------------------------------------------
class IngestRequest(BaseModel):
    topic: str
    sources: list = ["arxiv", "docs", "medium", "reddit", "hackernews", "stackoverflow"]
    max_per_source: int = 50
    since_days: int = 7
    full_refresh: bool = False


@app.post("/ingest/knowledge")
async def ingest_knowledge_endpoint(req: IngestRequest, _auth=Depends(require_mcp_secret)):
    """Trigger knowledge ingestion pipeline for a topic."""
    try:
        from scraper.knowledge import ingest_knowledge
        from core_train import _ingest_to_hot_reflection

        print(f"[INGEST] Starting: topic={req.topic} sources={req.sources} max={req.max_per_source}")
        summary = await ingest_knowledge(
            topic=req.topic,
            sources=req.sources,
            max_per_source=req.max_per_source,
            since_days=req.since_days,
            full_refresh=req.full_refresh,
        )

        hot_ok = False
        if summary.get("concepts_found", 0) > 0:
            from scraper.knowledge.concept_extractor import AI_CONCEPTS
            concepts = list(AI_CONCEPTS.keys())[:summary["concepts_found"]]
            avg_eng = summary.get("avg_engagement", 50.0)
            source_str = ",".join(req.sources)
            hot_ok = _ingest_to_hot_reflection(req.topic, source_str, concepts, avg_eng)

        return {"ok": True, "hot_reflections_injected": hot_ok, **summary}
    except Exception as e:
        print(f"[INGEST] Error: {e}")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/ingest/status")
async def ingest_status():
    """Return ingestion pipeline status: table counts, last run time."""
    try:
        counts = {}
        for table in ("kb_sources", "kb_articles", "kb_concepts"):
            try:
                r = httpx.get(
                    f"{SUPABASE_URL}/rest/v1/{table}?select=id&limit=1",
                    headers=_sbh_count_svc(), timeout=8
                )
                cr = r.headers.get("content-range", "*/0")
                counts[table] = int(cr.split("/")[-1]) if "/" in cr else 0
            except:
                counts[table] = -1

        last_ingest = None
        try:
            rows = sb_get("hot_reflections",
                "select=created_at,task_summary&domain=eq.knowledge_ingestion&order=created_at.desc&limit=1",
                svc=True)
            if rows:
                last_ingest = {"ts": rows[0].get("created_at"), "summary": rows[0].get("task_summary", "")[:100]}
        except:
            pass

        return {"ok": True, "table_counts": counts, "last_ingest": last_ingest}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/")
def root():
    counts = get_system_counts()
    step = get_resume_task()
    return {
        "service": "CORE v6.0",
        "step": step,
        "knowledge": counts.get("knowledge_base", 0),
        "sessions": counts.get("sessions", 0),
        "mistakes": counts.get("mistakes", 0),
    }


@app.api_route("/health", methods=["GET", "HEAD"])
def health_ep():
    from core_tools import t_health
    return t_health()


@app.get("/ping")
def ping():
    """Fast health check - just confirms server is responding."""
    return {"ok": True, "service": "CORE v6.0", "ts": datetime.utcnow().isoformat()}


@app.get("/state")
def state_ep():
    from core_tools import t_state, t_state_packet
    return {"state": t_state(), "state_packet": t_state_packet()}


@app.get("/review")
async def review_widget():
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>CORE - Evolution Review</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0a;color:#e5e5e5;min-height:100vh;padding:2rem}
h1{font-size:1.4rem;font-weight:500;margin-bottom:.4rem;color:#fff}
.sub{font-size:.8rem;color:#666;margin-bottom:2rem}
.list{display:flex;flex-direction:column;gap:8px;margin-bottom:1.5rem}
.card{background:#141414;border:1px solid #222;border-radius:10px;padding:14px 16px;cursor:pointer;transition:border-color .15s}
.card:hover{border-color:#444}.card.sel{border-color:#4f6ef7}
.meta{display:flex;gap:6px;align-items:center;margin-bottom:6px;flex-wrap:wrap}
.badge{font-size:11px;font-weight:500;padding:2px 8px;border-radius:6px}
.p1{background:#3d1515;color:#f87171}.p2{background:#3d2b10;color:#fb923c}
.p3{background:#1a2d4a;color:#60a5fa}.p4,.p5{background:#1f1f1f;color:#888}
.btype{background:#1a1a1a;color:#666}.conf{font-size:11px;color:#555}
.etitle{font-size:13px;font-weight:500;color:#ccc}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 20px;font-size:13px;font-weight:500;border:1px solid #333;border-radius:8px;background:transparent;color:#e5e5e5;cursor:pointer}
.btn:hover{background:#1a1a1a}.btn:disabled{opacity:.4;cursor:not-allowed}
.result{margin-top:1.5rem}
.pb{background:#111;border-left:3px solid #4f6ef7;border-radius:0 8px 8px 0;padding:14px 16px;margin-bottom:10px}
.pk{font-size:10px;font-weight:600;color:#4f6ef7;letter-spacing:.1em;text-transform:uppercase;margin-bottom:5px}
.pv{font-size:13px;color:#ccc;line-height:1.7}
.spin{display:inline-block;width:13px;height:13px;border:2px solid #333;border-top-color:#4f6ef7;border-radius:50%;animation:s .7s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
.copy{font-size:11px;color:#4f6ef7;background:none;border:none;cursor:pointer;margin-top:8px}
.lbl{font-size:10px;font-weight:600;color:#555;letter-spacing:.08em;text-transform:uppercase;margin-bottom:10px}
</style>
</head>
<body>
<h1>CORE &mdash; Evolution Review</h1>
<p class="sub">Translate pending evolution entries into structured WHAT / WHY / WHERE / HOW prompts</p>
<p class="lbl">Pending evolutions</p>
<div class="list" id="list"><p style="color:#555;font-size:13px">Loading...</p></div>
<div id="ta" style="display:none">
  <button class="btn" id="btn" onclick="go()">Translate to structured prompt</button>
</div>
<div class="result" id="res" style="display:none"></div>
<script>
let evos=[],sel=null;
async function load(){
  try{
    const r=await fetch('/api/evolutions');
    const d=await r.json();
    evos=d.evolutions||[];
    render();
  }catch(e){
    document.getElementById('list').innerHTML='<p style="color:#f87171;font-size:13px">Error: '+e.message+'</p>';
  }
}
function render(){
  const el=document.getElementById('list');
  if(!evos.length){el.innerHTML='<p style="color:#555;font-size:13px">No pending evolutions.</p>';return;}
  el.innerHTML=evos.map(e=>{
    const p=(e.change_summary||'').match(/P(\\d)/)?.[1]||'3';
    const t=(e.change_summary||'').replace(/\\[.*?\\]/g,'').replace(/^\\s*:\\s*/,'').trim().slice(0,80);
    return '<div class="card" id="c'+e.id+'" onclick="pick('+e.id+')"><div class="meta"><span class="badge p'+p+'">P'+p+'</span><span class="badge btype">'+e.change_type+'</span><span class="conf">conf: '+(e.confidence||0).toFixed(2)+'</span></div><div class="etitle">#'+e.id+' &mdash; '+(t||e.change_summary?.slice(0,80)||'unnamed')+'</div></div>';
  }).join('');
}
function pick(id){
  document.querySelectorAll('.card').forEach(c=>c.classList.remove('sel'));
  const c=document.getElementById('c'+id);if(c)c.classList.add('sel');
  sel=id;
  document.getElementById('ta').style.display='block';
  document.getElementById('res').style.display='none';
}
async function go(){
  const evo=evos.find(e=>e.id===sel);if(!evo)return;
  const btn=document.getElementById('btn');
  btn.disabled=true;btn.innerHTML='<span class="spin"></span> Translating...';
  const res=document.getElementById('res');res.style.display='none';
  try{
    const r=await fetch('/api/translate-evolution',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:evo.id,change_type:evo.change_type,change_summary:evo.change_summary,confidence:evo.confidence})});
    const d=await r.json();
    if(!d.ok){throw new Error(d.error||'Backend error');}
    const raw=d.result||'{}';
    const p=JSON.parse(raw.replace(/```json|```/g,'').trim());
    const fields=[{k:'WHAT',v:p.what},{k:'WHY',v:p.why},{k:'WHERE',v:p.where},{k:'HOW',v:p.how},{k:'EXPECTED OUTCOME',v:p.expected_outcome}];
    const full=fields.map(f=>f.k+':\\n'+f.v).join('\\n\\n');
    res.innerHTML='<p class="lbl" style="margin-bottom:12px">Structured prompt - Evolution #'+evo.id+'</p>'+fields.map(f=>'<div class="pb"><div class="pk">'+f.k+'</div><div class="pv">'+(f.v||'&mdash;')+'</div></div>').join('')+'<button class="copy" onclick="cp(this,`'+full.replace(/`/g,'\\u0060')+'`)">Copy as text</button>';
    res.style.display='block';
  }catch(e){
    res.innerHTML='<p style="color:#f87171;font-size:13px;margin-top:1rem">Error: '+e.message+'</p>';
    res.style.display='block';
  }
  btn.disabled=false;btn.innerHTML='Translate to structured prompt';
}
function cp(btn,t){navigator.clipboard.writeText(t).then(()=>{btn.textContent='Copied!';setTimeout(()=>btn.textContent='Copy as text',1500);})}
load();
</script>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=200)


@app.get("/api/evolutions")
def api_evolutions(_auth=Depends(require_mcp_secret)):
    rows = sb_get(
        "evolution_queue",
        "select=id,status,change_type,change_summary,confidence,pattern_key,diff_content,created_at"
        "&status=eq.pending&id=gt.1&order=created_at.desc&limit=50",
        svc=True,
    )
    return {"evolutions": rows, "count": len(rows)}


class TranslateRequest(BaseModel):
    id: int
    change_type: str = ""
    change_summary: str = ""
    confidence: float = 0.0


@app.post("/api/translate-evolution")
async def translate_evolution(body: TranslateRequest, _auth=Depends(require_mcp_secret)):
    """Server-side evolution translation using backend LLM (gemini_chat/groq_chat).
    Called by /review widget â€” replaces broken client-side Anthropic API call."""
    try:
        from core_config import gemini_chat
        system = (
            "You are CORE's evolution analyst. Translate a raw evolution entry into a structured prompt.\n"
            'Output MUST be valid JSON: {"what":"1-2 sentences","why":"1-2 sentences",'
            '"where":"which component","how":"2-4 concrete steps","expected_outcome":"1 sentence"}\n'
            "Output ONLY valid JSON, no preamble."
        )
        user = (
            f"Evolution ID: {body.id}\nType: {body.change_type}\n"
            f"Summary: {body.change_summary}\nConfidence: {body.confidence}\n"
            "Translate this evolution."
        )
        raw = gemini_chat(system=system, user=user, max_tokens=1000, json_mode=True)
        return {"ok": True, "result": raw}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/patch")
async def patch_file(body: PatchRequest):
    if not secrets.compare_digest(str(body.secret), str(MCP_SECRET)):
        raise HTTPException(401, "Invalid secret")
    from core_tools import t_gh_search_replace
    from core_config import GITHUB_REPO
    result = t_gh_search_replace(
        path=body.path, old_str=body.old_str, new_str=body.new_str,
        message=body.message, repo=body.repo or GITHUB_REPO
    )
    if result.get("ok"):
        notify(f"Patch applied: `{body.path}`\n{body.message[:100]}")
    return result


class TradingReflectionRequest(BaseModel):
    output_text: str
    context: dict = Field(default_factory=dict)


class EmbedRequest(BaseModel):
    text: str


@app.post("/internal/trading/reflect")
async def trading_reflect(body: TradingReflectionRequest, req: Request):
    """
    Called by core-trading-bot via core_bridge.fire_trading() after every trade close.
    Triggers the full CORE L11 pipeline: critic -> causal -> reflect -> meta evaluator.
    Auth: X-MCP-Secret header required.
    source='trading' so critic/causal/reflect know to treat this as trade outcome.
    """
    secret = req.headers.get("X-MCP-Secret", "")
    if not secrets.compare_digest(str(secret), str(MCP_SECRET)):
        raise HTTPException(status_code=401, detail="Unauthorized")

    from core_orch_layer11 import fire_trading
    context = body.context or {}
    trace_id = context.get("trace_id")
    position_id = context.get("position_id")
    decision_id = context.get("decision_id")
    if not body.output_text or len(body.output_text.strip()) < 20:
        raise HTTPException(status_code=400, detail="output_text is too short")
    if not trace_id or position_id in (None, "") or decision_id in (None, ""):
        raise HTTPException(
            status_code=400,
            detail="trace_id, position_id, and decision_id are required in context",
        )
    context = build_reflection_context(
        body.context or {},
        source_domain="trading",
        source_branch="unknown",
        source_service="core-trading-bot",
        output_text=body.output_text,
    )
    ingress = register_reflection_event(context, body.output_text)
    if ingress is None:
        raise HTTPException(status_code=500, detail="failed to persist reflection event")
    print(
        f"[TRADING_REFLECT] event_id={context['event_id']} trace_id={trace_id} "
        f"decision_id={decision_id} position_id={position_id} queued"
    )
    context["event_id"] = ingress["event_id"]
    context["l11_session_id"] = ingress.get("l11_session_id")
    fire_trading(body.output_text, context)

    return {
        "ok": True,
        "queued": True,
        "source": "trading",
        "event_id": context["event_id"],
        "trace_id": trace_id,
        "ts": datetime.utcnow().isoformat(),
    }


@app.get("/internal/reflection-events/query")
async def reflection_events_query(
    event_id: Optional[str] = Query(None),
    trace_id: Optional[str] = Query(None),
    decision_id: Optional[int] = Query(None),
    position_id: Optional[int] = Query(None),
    source_domain: Optional[str] = Query(None),
    source_branch: Optional[str] = Query(None),
    source_service: Optional[str] = Query(None),
    _auth=Depends(require_mcp_secret),
):
    rows = fetch_reflection_events(
        event_id=event_id,
        trace_id=trace_id,
        decision_id=decision_id,
        position_id=position_id,
        source_domain=source_domain,
        source_branch=source_branch,
        source_service=source_service,
        limit=50,
    )
    return {
        "ok": True,
        "count": len(rows),
        "events": rows,
    }


@app.get("/internal/trading/reflections/query")
async def trading_reflection_query(
    position_id: Optional[int] = Query(None),
    decision_id: Optional[int] = Query(None),
    _auth=Depends(require_mcp_secret),
):
    if position_id is None and decision_id is None:
        raise HTTPException(status_code=400, detail="position_id or decision_id is required")

    ledger_rows = fetch_reflection_events(
        decision_id=decision_id,
        position_id=position_id,
        source_domain="trading",
        limit=25,
    )

    critiques = [
        row for row in sb_get(
            "output_critiques",
            "source=eq.trading&select=id,session_id,output_text,verdict,score,reason,failure_pattern,created_at"
            "&order=created_at.desc&limit=200",
            svc=True,
        )
        if _matches_trade_trace(row.get("output_text", ""), position_id, decision_id)
    ]
    session_ids = {row.get("session_id") for row in critiques if row.get("session_id")}

    causal_rows = [
        row for row in sb_get(
            "causal_chains",
            "source=eq.trading&select=session_id,root_knowledge,reasoning_type,confidence,created_at"
            "&order=created_at.desc&limit=200",
            svc=True,
        )
        if row.get("session_id") in session_ids
    ]
    reflection_rows = [
        row for row in sb_get(
            "output_reflections",
            "source=eq.trading&select=session_id,gap,gap_domain,new_behavior,evo_worthy,created_at"
            "&order=created_at.desc&limit=200",
            svc=True,
        )
        if row.get("session_id") in session_ids
    ]
    hot_rows = [
        row for row in sb_get(
            "hot_reflections",
            "domain=eq.trading&select=id,reflection_text,quality_score,created_at"
            "&order=created_at.desc&limit=200",
            svc=True,
        )
        if _matches_trade_trace(row.get("reflection_text", ""), position_id, decision_id)
    ]
    kb_rows = [
        row for row in sb_get(
            "knowledge_base",
            "domain=eq.trading&select=id,topic,content,confidence,created_at"
            "&order=created_at.desc&limit=200",
            svc=True,
        )
        if _matches_trade_trace(
            f"{row.get('topic', '')}\n{row.get('content', '')}",
            position_id,
            decision_id,
        )
    ]
    mistake_rows = [
        row for row in sb_get(
            "mistakes",
            "select=id,domain,what_failed,context,created_at&order=created_at.desc&limit=200",
            svc=True,
        )
        if _matches_trade_trace(
            f"{row.get('what_failed', '')}\n{row.get('context', '')}",
            position_id,
            decision_id,
        )
    ]

    return {
        "ok": True,
        "position_id": position_id,
        "decision_id": decision_id,
        "session_ids": sorted(session_ids),
        "counts": {
            "output_critiques": len(critiques),
            "causal_chains": len(causal_rows),
            "output_reflections": len(reflection_rows),
            "hot_reflections": len(hot_rows),
            "knowledge_base": len(kb_rows),
            "mistakes": len(mistake_rows),
        },
        "artifacts": {
            "output_critiques": critiques[:10],
            "causal_chains": causal_rows[:10],
            "output_reflections": reflection_rows[:10],
            "hot_reflections": hot_rows[:10],
            "knowledge_base": kb_rows[:10],
            "mistakes": mistake_rows[:10],
            "reflection_events": ledger_rows,
        },
    }


@app.get("/deployment-check")
async def deployment_check(_auth=Depends(require_mcp_secret)):
    return _deployment_manifest()


@app.post("/embed")
async def embed_text(body: EmbedRequest, _auth=Depends(require_mcp_secret)):
    from core_embeddings import _get_embedding

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    return {"ok": True, "embedding": _get_embedding(text)}


@app.get("/operator/autonomy/status")
async def operator_autonomy_status(_auth=Depends(require_mcp_secret)):
    return autonomy_status()


@app.post("/operator/autonomy/run")
async def operator_autonomy_run(
    max_tasks: int = Query(1, ge=1, le=10),
    source: str = Query("self_assigned,improvement"),
    _auth=Depends(require_mcp_secret),
):
    return run_autonomy_cycle(max_tasks=max_tasks, source=source)


@app.get("/operator/research-autonomy/status")
async def operator_research_autonomy_status(_auth=Depends(require_mcp_secret)):
    return research_autonomy_status()


@app.post("/operator/research-autonomy/run")
async def operator_research_autonomy_run(
    max_tasks: int = Query(2, ge=1, le=10),
    _auth=Depends(require_mcp_secret),
):
    return run_research_autonomy_cycle(max_tasks=max_tasks)


@app.get("/operator/code-autonomy/status")
async def operator_code_autonomy_status(_auth=Depends(require_mcp_secret)):
    return code_autonomy_status()


@app.post("/operator/code-autonomy/run")
async def operator_code_autonomy_run(
    max_tasks: int = Query(1, ge=1, le=10),
    _auth=Depends(require_mcp_secret),
):
    return run_code_autonomy_cycle(max_tasks=max_tasks)


@app.get("/operator/integration-autonomy/status")
async def operator_integration_autonomy_status(_auth=Depends(require_mcp_secret)):
    return integration_autonomy_status()


@app.post("/operator/integration-autonomy/run")
async def operator_integration_autonomy_run(
    max_tasks: int = Query(1, ge=1, le=10),
    _auth=Depends(require_mcp_secret),
):
    return run_integration_autonomy_cycle(max_tasks=max_tasks)


@app.get("/operator/evolution-autonomy/status")
async def operator_evolution_autonomy_status(_auth=Depends(require_mcp_secret)):
    return evolution_autonomy_status()


@app.post("/operator/evolution-autonomy/run")
async def operator_evolution_autonomy_run(
    max_evolutions: int = Query(3, ge=1, le=10),
    _auth=Depends(require_mcp_secret),
):
    return run_evolution_autonomy_cycle(max_evolutions=max_evolutions)


@app.get("/operator/proposal-router/status")
async def operator_proposal_router_status(_auth=Depends(require_mcp_secret)):
    # Return a slim summary (top packets only) to avoid huge responses on large backlogs.
    return proposal_router_summary(limit=10)


@app.post("/operator/proposal-router/run")
async def operator_proposal_router_run(
    limit: int = Query(5, ge=1, le=25),
    _auth=Depends(require_mcp_secret),
):
    return proposal_router_summary(limit=limit)


@app.post("/operator/proposal-router/cluster-close")
async def operator_proposal_router_cluster_close(
    cluster_id: str = Query("", description="Owner-review cluster_id to close."),
    cluster_key: str = Query("", description="Owner-review cluster_key to close."),
    decision: str = Query("applied", pattern="^(applied|rejected)$"),
    reason: str = Query("", description="Reason or note for this cluster decision."),
    verification_note: str = Query("", description="Verification evidence note for applied closes."),
    reviewed_by: str = Query("owner", description="Reviewer identity label."),
    dry_run: bool = Query(False, description="Preview the cluster close without changing any rows."),
    _auth=Depends(require_mcp_secret),
):
    if not str(cluster_id or "").strip() and not str(cluster_key or "").strip():
        raise HTTPException(status_code=400, detail="cluster_id or cluster_key is required")
    result = _proposal_router_cluster_close(
        cluster_id=str(cluster_id or "").strip(),
        cluster_key=str(cluster_key or "").strip(),
        decision=str(decision or "applied").strip().lower(),
        reason=str(reason or "").strip(),
        verification_note=str(verification_note or "").strip(),
        reviewed_by=str(reviewed_by or "owner").strip(),
        dry_run="true" if dry_run else "false",
    )
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


@app.get("/operator/semantic-projection/status")
async def operator_semantic_projection_status(_auth=Depends(require_mcp_secret)):
    return semantic_projection_status()


@app.post("/operator/semantic-projection/run")
async def operator_semantic_projection_run(
    max_rows: int = Query(20, ge=1, le=50),
    _auth=Depends(require_mcp_secret),
):
    return run_semantic_projection_cycle(max_rows=max_rows)

async def get_mcp_identity(
    x_mcp_secret: Optional[str] = Header(None, alias="X-MCP-Secret"),
    authorization: Optional[str] = Header(None),
    secret_query: Optional[str] = Query(None, alias="secret")
):
    """
    Centralized Auth. Securely extracts the secret from Headers or Query.
    """
    token = x_mcp_secret or secret_query
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    
    # Timing-attack resistant comparison against your core_config.MCP_SECRET
    if not token or not secrets.compare_digest(str(token), str(MCP_SECRET)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"jsonrpc": "2.0", "error": {"code": -32600, "message": "Unauthorized"}}
        )
    return True

# --- 2. Refactored Endpoints (Replace your existing ones) ---

@app.post("/mcp/sse")
async def mcp_post(req: Request, _auth=Depends(get_mcp_identity)):
    try:
        body = await req.json()
    except json.JSONDecodeError:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400
        )

    if isinstance(body, list):
        return JSONResponse([r for item in body if (r := handle_jsonrpc(item)) is not None])
    
    session_id = (
        req.query_params.get("session_id")
        or req.headers.get("X-Session-Id")
        or req.headers.get("mcp-session-id")
        or ""
    )
    response = handle_jsonrpc(body, session_id=session_id)
    if response is None:
        return Response(status_code=204)

    # If client wants an SSE response for a single POST (standard MCP behavior)
    if "text/event-stream" in req.headers.get("accept", "").lower():
        async def sse_single():
            yield f"data: {json.dumps(response)}\n\n"
        return StreamingResponse(
            sse_single(), 
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache", 
                "X-Accel-Buffering": "no",
                "mcp-session-id": str(uuid.uuid4())
            }
        )
    return JSONResponse(response)
@app.get("/mcp/sse")
async def mcp_sse_get(req: Request, _auth=Depends(get_mcp_identity)):
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _sse_sessions[session_id] = queue

    async def event_stream():
        try:
            # Tell the client where to send POST messages
            yield f"event: endpoint\ndata: {json.dumps(f'/mcp/messages?session_id={session_id}')}\n\n"
            while True:
                if _sse_shutdown.is_set() or await req.is_disconnected():
                    break
                try:
                    # 25s heartbeat to keep Railway/Nginx connections alive
                    msg = await asyncio.wait_for(queue.get(), timeout=25.0)
                    if msg is None or _sse_shutdown.is_set():
                        break
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    if _sse_shutdown.is_set():
                        break
                    yield ": ping\n\n"
        finally:
            _sse_sessions.pop(session_id, None)

    return StreamingResponse(
        event_stream(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache", 
            "X-Accel-Buffering": "no",
            "X-Session-Id": session_id
        }
    )

@app.post("/mcp/messages")
async def mcp_messages(req: Request, session_id: str = Query(...), _auth=Depends(get_mcp_identity)):
    try:
        body = await req.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Parse error"}, status_code=400)

    response = handle_jsonrpc(body, session_id=session_id)
    
    if session_id in _sse_sessions:
        if response is not None:
            await _sse_sessions[session_id].put(response)
        return JSONResponse({"ok": True}, status_code=202)
    
    if response is None:
        return Response(status_code=204)
    return JSONResponse(response)
@app.on_event("shutdown")
async def shutdown_sse_sessions() -> None:
    _shutdown_sse_sessions()


@app.post("/mcp/tool")
async def mcp_tool(body: ToolCall):
    # Keep using your custom mcp_ok check for session tokens
    if not mcp_ok(body.session_token):
        raise HTTPException(401, "Invalid/expired session")
    
    if not L.mcp(body.session_token):
        raise HTTPException(429, "Rate limit exceeded")
    
    # O(1) Tool lookup
    tool_data = TOOLS.get(body.tool)
    if not tool_data:
        raise HTTPException(404, f"Tool not found: {body.tool}")
        
    try:
        fn = tool_data["fn"]
        res = fn(**(body.args or {}))
        return {
            "ok": True, 
            "tool": body.tool, 
            "perm": tool_data.get("perm"), 
            "result": res
        }
    except Exception as e:
        # Structured error return instead of crashing
        return {"ok": False, "tool": body.tool, "error": str(e)}

@app.get("/mcp/tools")
def list_tools():
    # Modern dictionary comprehension
    return {n: {"perm": t.get("perm"), "args": t.get("args")} for n, t in TOOLS.items()}

@app.get("/debug/sim")
def debug_sim(_auth=Depends(require_mcp_secret)):
    """Patch _run_simulation_batch to expose the raw Groq response + actual failure point."""
    import traceback, json as _json
    from core_config import groq_chat, GROQ_MODEL, sb_get, SUPABASE_URL, _sbh_count_svc
    import httpx as _hx
    diag = {}
    try:
        from core_tools import TOOLS
        tool_list = list(TOOLS.keys())
    except Exception:
        tool_list = []
    try:
        mistakes = sb_get("mistakes", "select=domain,what_failed&order=id.desc&limit=10", svc=True)
        kb_sample = sb_get("knowledge_base", "select=domain,topic&order=id.desc&limit=20", svc=True)
        failure_modes = "\n".join([f"- [{r.get('domain','?')}] {r.get('what_failed','')[:120]}" for r in mistakes]) or "None recorded yet."
        kb_domains = list({r.get("domain", "general") for r in kb_sample})
        kb_topics_sample = [r.get("topic", "") for r in kb_sample[:10]]
        try:
            kc = _hx.get(f"{SUPABASE_URL}/rest/v1/knowledge_base?select=id&limit=1", headers=_sbh_count_svc(), timeout=8)
            kb_total = int(kc.headers.get("content-range", "*/0").split("/")[-1])
        except Exception as ke:
            kb_total = len(kb_sample)
            diag["kb_count_err"] = str(ke)
        runtime_context = (f"CORE MCP tools ({len(tool_list)}): {', '.join(tool_list[:20])}\n"
            f"KB total entries: {kb_total}\nKB domains: {', '.join(kb_domains)}\n"
            f"Known failure modes:\n{failure_modes}\nSample KB topics: {', '.join(kb_topics_sample)}")
        system = """You are simulating 1,000,000 users of CORE - a personal AGI orchestration system.
Output MUST be valid JSON:\n{\n  \"domain\": \"code|db|bot|mcp|training|kb|general\",\n  \"patterns\": [\"pattern1\", \"pattern2\"],\n  \"gaps\": \"1-2 sentences\",\n  \"summary\": \"1 sentence\"\n}\nOutput ONLY valid JSON, no preamble."""
        user = f"{runtime_context}\n\nSimulate 1,000,000 users. What patterns emerge?"
        diag["prompt_len"] = len(user)
        raw = groq_chat(system, user, model=GROQ_MODEL, max_tokens=900)
        diag["raw"] = raw[:1000]
        raw2 = raw.strip()
        if raw2.startswith("```"): raw2 = raw2.split("```")[1]
        if raw2.startswith("json"): raw2 = raw2[4:]
        diag["raw_after_strip"] = raw2[:500]
        result = _json.loads(raw2.strip())
        diag["parsed_ok"] = True
        diag["patterns"] = result.get("patterns", [])
        from core_config import sb_post
        post_ok = sb_post("hot_reflections", {
            "task_summary": "debug sim test",
            "domain": result.get("domain", "general"),
            "new_patterns": result.get("patterns", []),
            "gaps_identified": [result.get("gaps")] if result.get("gaps") else None,
            "reflection_text": result.get("summary", ""),
            "processed_by_cold": 0, "source": "simulation",
            "quality_score": 0.6,
        })
        diag["sb_post_ok"] = post_ok
    except Exception as e:
        diag["error"] = str(e)
        diag["trace"] = traceback.format_exc()
    return diag


@app.get("/debug/real")
def debug_real():
    """Run real signal extraction synchronously and return full result for diagnosis."""
    import traceback
    try:
        from core_train import _extract_real_signal
        ok = _extract_real_signal()
        return {"ok": ok, "error": None}
    except Exception as e:
        return {"ok": False, "error": str(e), "trace": traceback.format_exc()}


# -- Listen mode background state -------------------------------------------
_listen_job: dict = {}   # {id, status, chunks, started_at, stopped_at, stop_reason}
_listen_lock = threading.Lock()

def _run_listen_job(job_id: str):
    """Background thread: drain listen_stream(), accumulate chunks, update _listen_job."""
    from core_train import listen_stream
    _MAX_CHUNKS = 500  # cap to prevent OOM on long listen sessions
    try:
        for chunk in listen_stream():
            with _listen_lock:
                current = _listen_job.get("chunks", [])
                if len(current) < _MAX_CHUNKS:
                    current.append(chunk)
                _listen_job["chunks"] = current
                parsed = {}
                try: parsed = json.loads(chunk) if isinstance(chunk, str) else chunk
                except Exception: pass
                if parsed.get("type") == "stop":
                    _listen_job["status"] = "done"
                    _listen_job["stop_reason"] = parsed.get("reason", "unknown")
                    _listen_job["stopped_at"] = datetime.utcnow().isoformat()
                    print(f"[LISTEN] job={job_id} done reason={parsed.get('reason')}")
                    return
    except Exception as e:
        print(f"[LISTEN] job={job_id} error: {e}")
        with _listen_lock:
            _listen_job["status"] = "error"
            _listen_job["error"] = str(e)
            _listen_job["stopped_at"] = datetime.utcnow().isoformat()


@app.get("/listen")
async def listen_mode(req: Request):
    """LISTEN MODE: start background listen job, return job_id immediately.
    Poll GET /listen/status for results. Replaces blocking StreamingResponse.
    Auth: X-MCP-Secret header required.
    """
    global _listen_job
    secret = req.headers.get("X-MCP-Secret", "")
    if not secrets.compare_digest(str(secret), str(MCP_SECRET)):
        raise HTTPException(status_code=401, detail="Unauthorized")

    with _listen_lock:
        if _listen_job.get("status") == "running":
            return JSONResponse({"ok": True, "job_id": _listen_job["id"], "status": "running", "note": "already running"})
        job_id = str(uuid.uuid4())[:8]
        _listen_job = {"id": job_id, "status": "running", "chunks": [], "started_at": datetime.utcnow().isoformat(), "stop_reason": None}

    t = threading.Thread(target=_run_listen_job, args=(job_id,), daemon=True)
    t.start()
    print(f"[LISTEN] Started job={job_id}")
    return JSONResponse({"ok": True, "job_id": job_id, "status": "running"})


@app.get("/listen/status")
async def listen_status(req: Request):
    """Poll listen job status. Returns current chunks + done/running/error status."""
    secret = req.headers.get("X-MCP-Secret", "")
    if not secrets.compare_digest(str(secret), str(MCP_SECRET)):
        raise HTTPException(status_code=401, detail="Unauthorized")
    with _listen_lock:
        job = dict(_listen_job)
    chunks = job.get("chunks", [])
    cold_runs = [c for c in chunks if isinstance(c, dict) and c.get("type") == "cold_run"]
    return JSONResponse({
        "ok": True,
        "job_id": job.get("id"),
        "status": job.get("status", "idle"),
        "stop_reason": job.get("stop_reason"),
        "started_at": job.get("started_at"),
        "stopped_at": job.get("stopped_at"),
        "chunk_count": len(chunks),
        "cycles": len(cold_runs),
        "total_patterns_found": sum(c.get("patterns_found", 0) for c in cold_runs),
        "total_evolutions_queued": sum(c.get("evolutions_queued", 0) for c in cold_runs),
        "chunks": chunks,
    })


# -- Backfill job background state -------------------------------------------
_backfill_job: dict = {}   # {id, status, inserted, checked, started_at, stopped_at, error}
_backfill_lock = threading.Lock()

def _run_backfill_job(job_id: str, batch_size: int):
    """Background thread: run _backfill_patterns(), update _backfill_job."""
    from core_train import _backfill_patterns
    try:
        inserted = _backfill_patterns(batch_size=batch_size)
        with _backfill_lock:
            _backfill_job["status"] = "done"
            _backfill_job["inserted"] = inserted
            _backfill_job["stopped_at"] = datetime.utcnow().isoformat()
        print(f"[BACKFILL-JOB] job={job_id} done inserted={inserted}")
    except Exception as e:
        print(f"[BACKFILL-JOB] job={job_id} error: {e}")
        with _backfill_lock:
            _backfill_job["status"] = "error"
            _backfill_job["error"] = str(e)
            _backfill_job["stopped_at"] = datetime.utcnow().isoformat()


@app.get("/backfill")
async def backfill_start(req: Request, batch_size: int = 20):
    """Start backfill job in background. Returns job_id immediately. Poll /backfill/status."""
    global _backfill_job
    secret = req.headers.get("X-MCP-Secret", "")
    if not secrets.compare_digest(str(secret), str(MCP_SECRET)):
        raise HTTPException(status_code=401, detail="Unauthorized")
    with _backfill_lock:
        if _backfill_job.get("status") == "running":
            return JSONResponse({"ok": True, "job_id": _backfill_job["id"], "status": "running", "note": "already running"})
        job_id = str(uuid.uuid4())[:8]
        _backfill_job = {"id": job_id, "status": "running", "inserted": 0, "started_at": datetime.utcnow().isoformat(), "stopped_at": None, "error": None}
    t = threading.Thread(target=_run_backfill_job, args=(job_id, batch_size), daemon=True)
    t.start()
    print(f"[BACKFILL-JOB] Started job={job_id} batch_size={batch_size}")
    return JSONResponse({"ok": True, "job_id": job_id, "status": "running", "batch_size": batch_size})


@app.get("/backfill/status")
async def backfill_status(req: Request):
    """Poll backfill job status. Returns inserted count + done/running/error."""
    secret = req.headers.get("X-MCP-Secret", "")
    if not secrets.compare_digest(str(secret), str(MCP_SECRET)):
        raise HTTPException(status_code=401, detail="Unauthorized")
    with _backfill_lock:
        job = dict(_backfill_job)
    return JSONResponse({
        "ok": True,
        "job_id": job.get("id"),
        "status": job.get("status", "idle"),
        "inserted": job.get("inserted", 0),
        "started_at": job.get("started_at"),
        "stopped_at": job.get("stopped_at"),
        "error": job.get("error"),
    })


@app.post("/webhook")
async def webhook(req: Request):
    try:
        if not _telegram_webhook_ok(req):
            raise HTTPException(status_code=401, detail="Unauthorized")
        u = await req.json()
        keys = list(u.keys())
        print(f"[WEBHOOK] update keys={keys} update_id={u.get('update_id','?')}")
        if "message" in u:
            msg = u["message"]
            text = msg.get("text", "")
            cid = msg.get("chat", {}).get("id", "?")
            uname = msg.get("from", {}).get("username", "?")
            print(f"[WEBHOOK] message: chat_id={cid} user=@{uname} text={text!r:.80}")
            # Fire-and-forget in a thread â€” webhook must return 200 immediately
            threading.Thread(target=handle_msg, args=(msg,), daemon=True).start()
        else:
            # Non-message update (edited_message, callback_query, etc) â€” log and ignore
            print(f"[WEBHOOK] non-message update ignored: keys={keys}")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[WEBHOOK] error: {e}")
    return {"ok": True}  # Always return 200 immediately â€” never block here


# ---------------------------------------------------------------------------
# Telegram message handler
# ---------------------------------------------------------------------------
def handle_msg(msg):
    cid  = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "").strip()
    if not text:
        return
    if not _is_owner_chat(cid):
        print(f"[WEBHOOK] unauthorized chat rejected: chat_id={cid}")
        return
    # Strip bot username suffix from commands (e.g. /status@reinvagnarbot -> /status)
    if text.startswith("/") and "@" in text:
        text = text.split("@")[0]
    cmd, _, arg_str = text.partition(" ")
    cmd = cmd.lower()
    arg_str = arg_str.strip()

    if cmd == "/start":
        counts = get_system_counts()
        resume = get_resume_task()
        task_auto = autonomy_status() if AUTONOMY_ENABLED else {}
        code_auto = code_autonomy_status() if CODE_AUTONOMY_ENABLED else {}
        integration_auto = integration_autonomy_status() if INTEGRATION_AUTONOMY_ENABLED else {}
        evo_auto = evolution_autonomy_status() if EVOLUTION_AUTONOMY_ENABLED else {}
        notify(
            _render_section(
                "CORE Online",
                [
                    f"Resume: {_tg_escape(resume or 'No active tasks', 180)}",
        f"KB: {counts.get('knowledge_base', 0)} | Mistakes: {counts.get('mistakes', 0)} | Sessions: {counts.get('sessions', 0)}",
        f"Repo map: components {counts.get('repo_components', 0)} | chunks {counts.get('repo_component_chunks', 0)} | edges {counts.get('repo_component_edges', 0)} | scans {counts.get('repo_scan_runs', 0)}",
        f"Manual work audit: {'enabled' if core_gap_audit_status().get('enabled') else 'disabled'} | gaps {core_gap_audit_status().get('last_report', {}).get('summary', {}).get('gap_count', 0)} | last_run {_tg_escape(core_gap_audit_status().get('last_run_at') or 'n/a', 40)}",
        f"Task queue: pending {counts.get('task_queue_pending', 0)} | in_progress {counts.get('task_queue_in_progress', 0)} | done {counts.get('task_queue_done', 0)} | failed {counts.get('task_queue_failed', 0)}",
                    f"Evolution queue: pending {counts.get('evolution_pending', 0)} | applied {counts.get('evolution_applied', 0)} | rejected {counts.get('evolution_rejected', 0)}",
                    f"Task autonomy: {'enabled' if task_auto.get('enabled') else 'disabled'} | pending {task_auto.get('pending', 0)}",
                    f"Code autonomy: {'enabled' if code_auto.get('enabled') else 'disabled'} | pending {code_auto.get('pending_code_tasks', 0)}",
                    f"Integration autonomy: {'enabled' if integration_auto.get('enabled') else 'disabled'} | pending {integration_auto.get('pending_integration_tasks', 0)}",
                    f"Evolution autonomy: {'enabled' if evo_auto.get('enabled') else 'disabled'} | pending {evo_auto.get('pending_evolutions', 0)}",
                    "",
                    "<b>Quick actions</b>",
                    "/status, /queues, /tasks, /code, /integration, /evolutions, /review, /audit",
                    "/memory, /autonomy, /evolution, /semantic, /repo",
                    "/health, /deploycheck, /project, /restart, /kill",
                ],
            ),
            cid,
        )

    elif cmd == "/help":
        notify(_render_command_catalog(), cid)

    elif cmd == "/status":
        from core_tools import t_state_packet
        counts = get_system_counts()
        task_auto = autonomy_status() if AUTONOMY_ENABLED else {}
        code_auto = code_autonomy_status() if CODE_AUTONOMY_ENABLED else {}
        integration_auto = integration_autonomy_status() if INTEGRATION_AUTONOMY_ENABLED else {}
        evo_auto = evolution_autonomy_status() if EVOLUTION_AUTONOMY_ENABLED else {}
        sem = semantic_projection_status() if SEMANTIC_PROJECTION_ENABLED else {}
        audit_status = core_gap_audit_status()
        state_packet = t_state_packet(session_id="default")
        state_verification = state_packet.get("verification") or {}
        lines = [
            f"Runtime: {'enabled' if AUTONOMY_ENABLED else 'disabled'} task autonomy | {'enabled' if CODE_AUTONOMY_ENABLED else 'disabled'} code autonomy | {'enabled' if INTEGRATION_AUTONOMY_ENABLED else 'disabled'} integration autonomy | {'enabled' if EVOLUTION_AUTONOMY_ENABLED else 'disabled'} evolution autonomy",
            f"Queues: task pending {counts.get('task_queue_pending', 0)} | in_progress {counts.get('task_queue_in_progress', 0)} | done {counts.get('task_queue_done', 0)} | failed {counts.get('task_queue_failed', 0)} | evolution pending {counts.get('evolution_pending', 0)} | applied {counts.get('evolution_applied', 0)} | rejected {counts.get('evolution_rejected', 0)}",
            f"Memory: KB {counts.get('knowledge_base', 0)} | Mistakes {counts.get('mistakes', 0)} | Sessions {counts.get('sessions', 0)} | Repo map {counts.get('repo_components', 0)} comps / {counts.get('repo_component_chunks', 0)} chunks / {counts.get('repo_component_edges', 0)} edges",
        f"Manual work audit: {format_core_gap_audit_status(audit_status)}",
            f"State continuity: {'verified' if state_verification.get('verified') else 'degraded'} | score {state_verification.get('verification_score', 0):.2f} | warnings {len(state_verification.get('warnings') or [])}",
            f"Workers: task {task_auto.get('pending', 0)} pending / {task_auto.get('in_progress', 0)} in progress | code {code_auto.get('pending_code_tasks', 0)} pending / {code_auto.get('pending_review_proposals', 0)} review proposals | integration {integration_auto.get('pending_integration_tasks', 0)} pending / {integration_auto.get('pending_review_proposals', 0)} review proposals | evolution {evo_auto.get('pending_evolutions', 0)} pending",
            f"Semantic projection: {'enabled' if SEMANTIC_PROJECTION_ENABLED else 'disabled'} | last_run {_tg_escape(sem.get('last_run_at') or 'n/a', 40)}",
        ]
        notify(_render_section("Status", lines), cid)

    elif cmd == "/health":
        notify(_render_health_report(), cid)

    elif cmd == "/queues":
        counts = get_system_counts()
        task_auto = autonomy_status() if AUTONOMY_ENABLED else {}
        evo_auto = evolution_autonomy_status() if EVOLUTION_AUTONOMY_ENABLED else {}
        notify(_render_queue_report(counts, task_auto, evo_auto), cid)

    elif cmd in {"/task", "/tasks"}:
        task_auto = autonomy_status() if AUTONOMY_ENABLED else {}
        notify(_render_task_status_report(task_auto), cid)

    elif cmd == "/research":
        research = research_autonomy_status() if RESEARCH_AUTONOMY_ENABLED else {}
        if arg_str.lower().startswith("run"):
            parts = arg_str.split()
            max_tasks = 2
            if len(parts) > 1:
                try:
                    max_tasks = max(1, min(10, int(parts[1])))
                except Exception:
                    max_tasks = 2
            result = run_research_autonomy_cycle(max_tasks=max_tasks)
            notify(_render_section("Research Autonomy", [
                f"Processed: {result.get('processed', 0)} | Completed: {result.get('completed', 0)} | Failed: {result.get('failed', 0)}",
                f"Pending: {result.get('pending', 0)} | Follow-up queued: {result.get('follow_up_queued', 0)}",
            ]), cid)
        else:
            notify(render_research_status_report(research), cid)

    elif cmd == "/code":
        code_auto = code_autonomy_status() if CODE_AUTONOMY_ENABLED else {}
        if arg_str.lower().startswith("run"):
            parts = arg_str.split()
            max_tasks = 1
            if len(parts) > 1:
                try:
                    max_tasks = max(1, min(10, int(parts[1])))
                except Exception:
                    max_tasks = 1
            result = run_code_autonomy_cycle(max_tasks=max_tasks)
            notify(_render_section("Code Autonomy", [
                f"Processed: {result.get('processed', 0)} | Proposed: {result.get('proposed', 0)} | Duplicates: {result.get('duplicates', 0)} | Deferred: {result.get('deferred', 0)} | Failures: {result.get('failures', 0)}",
                f"Pending code tasks: {result.get('pending_now', 0)} | Pending review proposals: {result.get('pending_proposals_now', 0)}",
            ]), cid)
        else:
            notify(_render_code_status_report(code_auto), cid)

    elif cmd == "/integration":
        integration_auto = integration_autonomy_status() if INTEGRATION_AUTONOMY_ENABLED else {}
        if arg_str.lower().startswith("run"):
            parts = arg_str.split()
            max_tasks = 1
            if len(parts) > 1:
                try:
                    max_tasks = max(1, min(10, int(parts[1])))
                except Exception:
                    max_tasks = 1
            result = run_integration_autonomy_cycle(max_tasks=max_tasks)
            notify(_render_section("Integration Autonomy", [
                f"Processed: {result.get('processed', 0)} | Proposed: {result.get('proposed', 0)} | Duplicates: {result.get('duplicates', 0)} | Deferred: {result.get('deferred', 0)} | Failures: {result.get('failures', 0)}",
                f"Pending integration tasks: {result.get('pending_now', 0)} | Pending review proposals: {result.get('pending_proposals_now', 0)}",
            ]), cid)
        else:
            notify(_render_integration_status_report(integration_auto), cid)

    elif cmd == "/autonomy":
        counts = get_system_counts()
        task_auto = autonomy_status() if AUTONOMY_ENABLED else {}
        evo_auto = evolution_autonomy_status() if EVOLUTION_AUTONOMY_ENABLED else {}
        notify(_render_autonomy_overview_report(counts, task_auto, evo_auto), cid)

    elif cmd in {"/evolution", "/evolutions"}:
        evo_auto = evolution_autonomy_status() if EVOLUTION_AUTONOMY_ENABLED else {}
        if arg_str.lower().startswith("run"):
            parts = arg_str.split()
            max_evos = 3
            if len(parts) > 1:
                try:
                    max_evos = max(1, min(10, int(parts[1])))
                except Exception:
                    max_evos = 3
            result = run_evolution_autonomy_cycle(max_evolutions=max_evos)
            notify(_render_evolution_status_report(result if isinstance(result, dict) else evo_auto), cid)
        else:
            notify(_render_evolution_status_report(evo_auto), cid)

    elif cmd in {"/review", "/proposals"}:
        rows = _fetch_pending_reviews(limit=5)
        note = "Read-only owner queue. Workers auto-handle safe tracks; Telegram review is for inspection only."
        if arg_str.strip() and arg_str.strip().split()[0].lower() not in {"", "list", "status", "dashboard", "help"}:
            note = "Read-only owner queue. Actions are disabled on Telegram; use desktop/operator flows for any manual decision."
        notify(_render_review_dashboard(rows, summary_note=note), cid)

    elif cmd == "/memory":
        notify(_render_memory_report(), cid)

    elif cmd == "/semantic":
        sem = semantic_projection_status() if SEMANTIC_PROJECTION_ENABLED else {}
        if arg_str.lower().startswith("run"):
            parts = arg_str.split()
            max_rows = 20
            if len(parts) > 1:
                try:
                    max_rows = max(1, min(50, int(parts[1])))
                except Exception:
                    max_rows = 20
            result = run_semantic_projection_cycle(max_rows=max_rows)
            sem = result if isinstance(result, dict) else sem
            lines = [
            f"Status: {'enabled' if sem.get('enabled', SEMANTIC_PROJECTION_ENABLED) else 'disabled'} | running={sem.get('running', False)}",
            f"Last run: {_tg_escape(sem.get('last_run_at', 'n/a'), 40)}",
            f"Pending raw rows: {_tg_escape(sem.get('pending_rows', sem.get('pending', 'n/a')), 40)}",
        ]
        notify(_render_section("Semantic Projection", lines), cid)

    elif cmd == "/repo":
        parts = arg_str.split()
        if not parts or parts[0].lower() in {"status", "view", "dashboard"}:
            notify(_render_repo_report(), cid)
        elif parts[0].lower() in {"sync", "run"}:
            result = run_repo_map_cycle(trigger="telegram")
            lines = [
                f"Sync: {'ok' if result.get('ok') else 'failed'}",
                f"Files total: {result.get('summary', {}).get('files_total', 0)} | changed: {result.get('summary', {}).get('files_changed', 0)}",
                f"Components: {result.get('summary', {}).get('components_upserted', 0)} | chunks: {result.get('summary', {}).get('chunks_upserted', 0)} | edges: {result.get('summary', {}).get('edges_upserted', 0)}",
                f"Removed: {result.get('summary', {}).get('removed', 0)} | duration_sec: {result.get('duration_sec', 0)}",
            ]
            if result.get("error"):
                lines.append(f"Error: {_tg_escape(result.get('error'), 220)}")
            notify(_render_section("Repo Map", lines), cid)
        else:
            query = " ".join(parts[1:]).strip()
            if not query:
                notify(_render_repo_report(), cid)
            else:
                from core_repo_map import build_repo_component_packet, build_repo_graph_packet
                packet = build_repo_component_packet(query=query, limit=10)
                graph = build_repo_graph_packet(query=query, depth=2, limit=10)
                lines = [
                    f"Query: {_tg_escape(query, 180)}",
                    f"Components: {len(packet.get('components', []) or [])} | Chunks: {len(packet.get('chunks', []) or [])} | Edges: {len(packet.get('edges', []) or [])}",
                    f"Graph nodes: {graph.get('count_nodes', 0)} | edges: {graph.get('count_edges', 0)}",
                ]
                if packet.get("summary"):
                    lines.append(f"Summary: {_tg_escape(packet.get('summary'), 260)}")
                notify(_render_section("Repo Packet", lines), cid)

    elif cmd in {"/audit", "/gaps"}:
        force = arg_str.lower().strip() in {"run", "force", "now", "full"}
        result = build_core_gap_audit(force=force)
        if result.get("ok"):
            if force or arg_str.lower().strip() in {"status", "summary"}:
                notify(format_core_gap_audit(result), cid)
            elif result.get("gaps") and len(result.get("gaps") or []):
                notify(format_core_gap_audit(result), cid)
            else:
                notify(_render_section("Manual Work Audit", ["No manual work gaps detected."]), cid)
        else:
            notify(_render_section("Manual Work Audit", [f"Error: {_tg_escape(result.get('error') or 'unknown', 220)}"]), cid)

    elif cmd == "/deploycheck":
        notify(_render_deployment_report(), cid)

    elif cmd == "/restart":
        notify(_render_section("Restart", ["Restarting CORE service...", "Any active loop will be interrupted by systemd restart."]), cid)
        import subprocess as _sp
        import time as _time
        def _do_restart():
            _time.sleep(1)
            _sp.run(["sudo", "systemctl", "restart", "core-agi"], check=False)
        import threading as _thr
        _thr.Thread(target=_do_restart, daemon=True).start()

    elif cmd == "/kill":
        notify(_render_section("Kill", ["Stopping active agentic loops and marking live sessions aborted."]), cid)
        try:
            import httpx as _hx
            from core_config import SUPABASE_URL, _sbh
            _hx.patch(
                f"{SUPABASE_URL}/rest/v1/agentic_sessions?status=eq.active",
                headers={**_sbh(True), "Prefer": "return=minimal"},
                json={"status": "aborted"},
                timeout=5
            )
            notify("âœ… Active sessions marked aborted. Use /restart if CORE is still stuck.", cid)
        except Exception as e:
            notify(f"âš ï¸ Kill failed: {e}", cid)

    elif cmd == "/project":
        from core_tools import t_project_list, t_project_prepare
        parts = arg_str.split()
        if not parts or parts[0] == "list":
            result = t_project_list()
            projects = result.get("projects", [])
            if projects:
                lines = [f"- {_tg_escape(p['name'], 60)} ({_tg_escape(p['project_id'], 40)}) â€” {_tg_escape(p['status'], 40)}" for p in projects]
                notify(_render_section("Projects", lines), cid)
            else:
                notify("No projects registered. Use Claude Desktop to register first.", cid)
        else:
            ids = ",".join(parts)
            result = t_project_prepare(ids)
            prepared = result.get("prepared", [])
            if prepared:
                notify(_render_section("Project Context", [f"Prepared: {', '.join(prepared)}", "Open Claude Desktop to activate."]), cid)
            else:
                notify(f"Could not prepare: {ids}. Check project IDs with /project list.", cid)

    else:
        # â”€â”€ Orchestrator v2: all freeform messages routed through L0â†’L10 pipeline
        # Pass raw msg dict â€” core_orch_main wraps it into {"message": msg} internally.
        # Use sync wrapper (handle_telegram_message) â€” creates its own event loop safely.
        from core_orch_main import handle_telegram_message
        threading.Thread(
            target=handle_telegram_message,
            args=(msg,),
            daemon=True
        ).start()


# ---------------------------------------------------------------------------
# Background pollers
# ---------------------------------------------------------------------------
def queue_poller():
    """Notify-only mode â€” no auto-execution without owner approval.
    Polls task_queue for pending tasks and notifies owner via Telegram."""
    print("[QUEUE] Started - notify-only mode (no auto-execution)")
    notify_sources = ("core_v6_registry", "mcp_session")
    _notified: set = set()
    while True:
        try:
            tasks = sb_get(
                "task_queue",
                "status=eq.pending"
                f"&source=in.({','.join(notify_sources)})"
                "&order=priority.asc&limit=5",
            )
            if tasks:
                for t in tasks:
                    tid = t["id"]
                    if tid in _notified:
                        continue
                    task_text = t.get("task", "")[:200]
                    priority = t.get("priority", 0)
                    source = t.get("source", "unknown")
                    notify(
                        f"Pending task (P{priority}) from {source}:\n"
                        f"`{task_text}`\n"
                        f"ID: `{tid}`\n"
                        f"Review via Claude Desktop â†’ task_queue"
                    )
                    _notified.add(tid)
                    if len(_notified) > 200:
                        _notified.clear()
        except Exception as e:
            print(f"[QUEUE] {e}")
        time.sleep(60)


# ---------------------------------------------------------------------------
# Deploy webhook â€” triggered by GitHub push to auto-pull + restart
# ---------------------------------------------------------------------------
@app.post("/deploy-webhook")
async def deploy_webhook(req: Request):
    """Auto-deploy: git pull latest from GitHub then restart core-agi service.
    Auth: X-MCP-Secret header (same secret as MCP).
    Call from GitHub Actions or manually after pushing code.
    Returns immediately â€” restart happens in background thread.
    """
    secret = req.headers.get("X-MCP-Secret", "")
    if not secrets.compare_digest(str(secret), str(MCP_SECRET)):
        raise HTTPException(status_code=401, detail="Unauthorized")

    def _do_deploy():
        import subprocess
        try:
            pull = subprocess.run(
                ["git", "pull", "origin", "main"],
                cwd="/home/ubuntu/core-agi",
                capture_output=True, text=True, timeout=60
            )
            print(f"[DEPLOY] git pull: {pull.stdout.strip()} {pull.stderr.strip()}")
            if pull.returncode != 0:
                notify(
                    f"âš ï¸ <b>CORE Auto-Deploy Failed</b>\n"
                    f"git pull returned {pull.returncode}\n{(pull.stderr or pull.stdout)[:300]}"
                )
                return
            notify(f"ðŸš€ <b>CORE Auto-Deploy</b>\n{pull.stdout.strip() or 'already up to date'}")
            if "Already up to date" not in pull.stdout:
                restart = subprocess.run(
                    ["sudo", "-n", "systemctl", "restart", "core-agi.service"],
                    capture_output=True, text=True, timeout=15
                )
                if restart.returncode != 0:
                    notify(
                        f"âš ï¸ <b>CORE Auto-Deploy Restart Failed</b>\n"
                        f"{(restart.stderr or restart.stdout)[:300]}"
                    )
                    return
                current = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd="/home/ubuntu/core-agi",
                    capture_output=True, text=True, timeout=10
                )
                notify(
                    f"âœ… <b>CORE Auto-Deploy Restarted</b>\n"
                    f"commit={current.stdout.strip()[:12] if current.returncode == 0 else 'unknown'}"
                )
        except Exception as e:
            print(f"[DEPLOY] error: {e}")
            notify(f"âš ï¸ CORE Deploy error: {e}")

    threading.Thread(target=_do_deploy, daemon=True).start()
    return {"ok": True, "status": "deploy_started"}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_start():
    try:
        bootstrap_result = bootstrap_core_supabase()
        if isinstance(bootstrap_result, dict) and not bootstrap_result.get("ok", False):
            print(f"[CORE] Supabase bootstrap warning: {bootstrap_result.get('errors') or bootstrap_result.get('error')}")
    except Exception as e:
        print(f"[CORE] Supabase bootstrap failed (non-fatal): {e}")
    try:
        set_webhook()
    except Exception as e:
        print(f"[CORE] webhook setup failed (non-fatal): {e}")
    try:
        set_telegram_commands(CORE_TELEGRAM_COMMANDS)
    except Exception as e:
        print(f"[CORE] telegram command setup failed (non-fatal): {e}")
    orch = startup_v2() or {}
    # Auto-embed sync — patches sb_post to embed all semantic table inserts
    try:
        from core_embed_sync import install, install_critical
        install()
        install_critical()
    except Exception as _es_e:
        print(f"[EMBED_SYNC] install failed (non-fatal): {_es_e}")
    threading.Thread(target=queue_poller, daemon=True).start()
    threading.Thread(target=cold_processor_loop, daemon=True).start()
    # self_sync_check disabled -- CORE_SELF.md is tombstoned, superseded by system_map
    threading.Thread(target=background_researcher, daemon=True).start()
    threading.Thread(target=repo_map_loop, daemon=True).start()
    threading.Thread(target=core_gap_audit_loop, daemon=True).start()
    if AUTONOMY_ENABLED:
        threading.Thread(target=autonomy_loop, daemon=True).start()
    if CODE_AUTONOMY_ENABLED:
        threading.Thread(target=code_autonomy_loop, daemon=True).start()
    if INTEGRATION_AUTONOMY_ENABLED:
        threading.Thread(target=integration_autonomy_loop, daemon=True).start()
    if RESEARCH_AUTONOMY_ENABLED:
        threading.Thread(target=research_autonomy_loop, daemon=True).start()
    if EVOLUTION_AUTONOMY_ENABLED:
        threading.Thread(target=evolution_autonomy_loop, daemon=True).start()
    if SEMANTIC_PROJECTION_ENABLED:
        threading.Thread(target=semantic_projection_loop, daemon=True).start()

    def _publish_startup_brief():
        counts = {}
        resume = "No active tasks"
        task_auto = {}
        evo_auto = {}
        try:
            counts = get_system_counts() or {}
        except Exception as e:
            print(f"[CORE] startup counts unavailable: {e}")
        try:
            resume = get_resume_task() or "No active tasks"
        except Exception as e:
            print(f"[CORE] startup resume unavailable: {e}")
        try:
            task_auto = autonomy_status() if AUTONOMY_ENABLED else {}
        except Exception as e:
            print(f"[CORE] startup task autonomy unavailable: {e}")
        try:
            evo_auto = evolution_autonomy_status() if EVOLUTION_AUTONOMY_ENABLED else {}
        except Exception as e:
            print(f"[CORE] startup evolution autonomy unavailable: {e}")
        try:
            in_progress = sb_get(
                "task_queue",
                "select=task,priority,status&source=in.(core_v6_registry,mcp_session)"
                "&status=eq.in_progress&order=priority.desc&limit=3"
            ) or []
            if in_progress:
                lines = []
                for t in in_progress:
                    raw = t.get("task", "")
                    try:
                        parsed = json.loads(raw) if isinstance(raw, str) else raw
                        title = parsed.get("title") or str(parsed)[:60]
                    except Exception:
                        title = str(raw)[:60]
                    lines.append(f"  ▶ {title} (P{t.get('priority','?')})")
                task_line = "In progress:\n" + "\n".join(lines)
            else:
                pending = sb_get(
                    "task_queue",
                    "select=task,priority&source=in.(core_v6_registry,mcp_session)"
                    "&status=eq.pending&order=priority.desc&limit=1"
                ) or []
                if pending:
                    raw = pending[0].get("task", "")
                    try:
                        parsed = json.loads(raw) if isinstance(raw, str) else raw
                        title = parsed.get("title") or str(parsed)[:60]
                    except Exception:
                        title = str(raw)[:60]
                    task_line = f"Next up: {title}"
                else:
                    task_line = "No active tasks"
        except Exception as e:
            task_line = f"Tasks: unavailable ({e})"
        try:
            brief = _build_startup_brief(resume if resume != "No active tasks" else task_line, counts, orch, task_auto, evo_auto)
        except Exception as e:
            print(f"[CORE] startup brief build failed: {e}")
            brief = (
                "🧠 <b>CORE Online</b>\n"
                f"Orchestrator: <b>{orch.get('model', 'unknown')}</b>\n"
                f"Startup note: brief rendering degraded ({e})"
            )
            notify_ok = notify(brief)
            print(f"[CORE] startup notify sent={notify_ok}")
        except Exception as e:
            print(f"[CORE] startup notify failed (non-fatal): {e}")
        print(f"[CORE] v6.0 online :{PORT} - {resume}")

    threading.Thread(target=_publish_startup_brief, daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    # Tambahkan path sertifikat yang tadi sudah terdaftar di certbot
    uvicorn.run(
        "core_main:app", 
        host="0.0.0.0", 
        port=443,             # Pindah ke port 443
        reload=False,
        ssl_keyfile="/etc/letsencrypt/live/core-agi.duckdns.org/privkey.pem",
        ssl_certfile="/etc/letsencrypt/live/core-agi.duckdns.org/fullchain.pem"
    )

