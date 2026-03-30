"""core_task_autonomy.py -- autonomous task lifecycle for CORE.

This module turns task_queue rows into a bounded claim -> checkpoint -> verify ->
complete loop. It is intentionally conservative:

- only auto-processes tasks that can produce a durable artifact;
- writes task-native checkpoints on every phase;
- records a canonical reflection event for the task run;
- marks done only after a live verification query succeeds.

The goal is not full unsupervised action on every task. The goal is to make the
task system autonomous where verification is possible and to leave a durable
paper trail everywhere else.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any

 param($m)
        $line = $m.Groups[1].Value
        if ($line -match '_env_int' -or $line -match '_env_float') { return $m.Value }
        return 'from core_config import ' + $line + ', _env_int, _env_float'
    
from core_github import notify
from core_reflection_audit import (
    finalize_reflection_event,
    note_reflection_stage,
    register_reflection_event,
)
from core_work_taxonomy import build_autonomy_contract
from core_tools import (
    t_add_knowledge,
    t_agent_session_init,
    t_agent_state_set,
    t_agent_step_done,
    t_add_behavioral_rule,
    t_task_error_packet,
)
from core_queue_cursor import build_seek_filter, cursor_from_row

AUTONOMY_ENABLED = os.getenv("CORE_AUTONOMY_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off"
}
AUTONOMY_INTERVAL_S = max(60, _env_int("CORE_AUTONOMY_INTERVAL_S", "60")))
AUTONOMY_BATCH_LIMIT = max(1, _env_int("CORE_AUTONOMY_BATCH_LIMIT", "10")))
AUTONOMY_SOURCES = tuple(
    s.strip() for s in os.getenv("CORE_AUTONOMY_SOURCES", "self_assigned,improvement").split(",") if s.strip()
)
AUTONOMY_SOURCE = ",".join(AUTONOMY_SOURCES) if AUTONOMY_SOURCES else "self_assigned"
AUTONOMY_NOTIFY = os.getenv("CORE_AUTONOMY_NOTIFY", "true").strip().lower() in {
    "1", "true", "yes", "on"
}

_lock = threading.Lock()
_state = {
    "running": False,
    "last_run_at": "",
    "last_summary": {},
    "last_error": "",
    "last_claimed_task_id": "",
    "queue_cursor": {},
}

QUEUE_ORDER = (("priority", "desc"), ("created_at", "asc"), ("id", "asc"))
QUEUE_PAGE_LIMIT = 250


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _safe_text(value: Any, limit: int = 240) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        return str(value)


def _parse_task_blob(row: dict) -> dict:
    raw = row.get("task", "")
    if isinstance(raw, dict):
        task = dict(raw)
    else:
        try:
            task = json.loads(raw) if raw else {}
        except Exception:
            task = {"title": _safe_text(raw, 200), "description": ""}
    title = _safe_text(task.get("title") or task.get("task_id") or raw, 200)
    description = _safe_text(task.get("description") or "", 600)
    task.setdefault("title", title)
    task.setdefault("description", description)
    return task


def _task_kind(task: dict, title: str, description: str, source: str = "") -> dict:
    autonomy = task.get("autonomy") if isinstance(task, dict) else {}
    return build_autonomy_contract(title, description, source=source, autonomy=autonomy, context="task_autonomy")


def _deferred_worker(strategy: dict) -> str:
    track = _safe_text(strategy.get("work_track") or "", 40)
    specialized = _safe_text(strategy.get("specialized_worker") or strategy.get("route") or "", 40)
    if track == "integration":
        return specialized or "integration_autonomy"
    if track in {"code_patch", "new_module"}:
        return specialized or "code_autonomy"
    if track == "research":
        return specialized or "research_autonomy"
    return ""


def _is_deferred_track(strategy: dict) -> bool:
    return bool(_deferred_worker(strategy))


def _slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:80] or "task"


def _extract_domain(text: str) -> str:
    if not text:
        return "reasoning"
    patterns = [
        r"domain:\s*([^\]\n]+)",
        r"for\s+domain:\s*([^\]\n]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            domain = _safe_text(m.group(1), 120)
            return domain.rstrip(" .,:;")
    return "reasoning"


def _classify_task(title: str, description: str, source: str = "") -> dict:
    return build_autonomy_contract(title, description, source=source, context="task_autonomy")


def _task_rows(limit: int = 1, source: str = AUTONOMY_SOURCE, cursor: dict | None = None) -> list[dict]:
    if limit <= 0:
        return []
    source_list = [s.strip() for s in str(source or ",".join(AUTONOMY_SOURCES)).split(",") if s.strip()] or list(AUTONOMY_SOURCES)
    cursor_filter = build_seek_filter(cursor, QUEUE_ORDER)
    rows = sb_get(
        "task_queue",
        f"select=id,task,status,priority,source,created_at,updated_at,next_step,blocked_by,checkpoint,checkpoint_at,checkpoint_draft"
        f"&status=eq.pending&source=in.({','.join(source_list)})"
        f"{('&' + cursor_filter) if cursor_filter else ''}"
        f"&order=priority.desc,created_at.asc,id.asc&limit={max(1, min(limit, QUEUE_PAGE_LIMIT))}",
        svc=True,
    ) or []
    # Prefer oldest among the highest-priority slice to avoid starvation.
    def _priority_key(row: dict) -> tuple:
        created = row.get("created_at") or ""
        return (-int(row.get("priority") or 0), created, str(row.get("id") or ""))

    return sorted(rows, key=_priority_key)


def _close_duplicate_noop_tasks(source: str = AUTONOMY_SOURCE, limit: int = 50) -> dict:
    """Terminalize pending task rows that already resolved as duplicate no-ops.

    Some rerouted tasks write a downstream duplicate/no-op result (e.g. KB entry already
    existed) before the parent task row is terminalized. Those rows are not live work and
    should not remain pending.
    """
    source_list = [s.strip() for s in str(source or ",".join(AUTONOMY_SOURCES)).split(",") if s.strip()] or list(AUTONOMY_SOURCES)
    try:
        rows = sb_get(
            "task_queue",
            (
                "select=id,status,source,task,result,checkpoint,next_step,updated_at"
                f"&status=eq.pending&source=in.({','.join(source_list)})"
                "&or=(result.ilike.*skipped_duplicate*,result.ilike.*duplicate_of*,result.ilike.*existing_status*)"
                f"&limit={max(1, min(int(limit or 50), 100))}"
            ),
            svc=True,
        ) or []
    except Exception:
        rows = []

    closed: list[str] = []
    for row in rows:
        task_id = str(row.get("id") or "")
        if not task_id:
            continue
        now = _utcnow()
        checkpoint = {
            "phase": "complete",
            "reason": "duplicate_noop_terminalized",
            "ts": now,
        }
        patch = {
            "status": "done",
            "error": None,
            "next_step": "",
            "updated_at": now,
            "checkpoint": checkpoint,
            "checkpoint_at": now,
            "checkpoint_draft": json.dumps(checkpoint, default=str)[:4000],
        }
        if sb_patch("task_queue", f"id=eq.{task_id}&status=eq.pending", patch):
            closed.append(task_id)

    return {
        "checked": len(rows),
        "closed": len(closed),
        "closed_ids": closed[:20],
    }


def _backfill_autonomy_metadata(source: str = AUTONOMY_SOURCE, limit: int = 1000) -> dict:
    """Normalize legacy task rows so autonomy metadata is explicit.

    Older evolution-synthesized rows may have a valid worker route but missing
    review_scope/owner_only/route fields in the task JSON. This sweep backfills
    those fields in place so the queue stops showing unlabeled legacy noise.
    """
    source_list = [s.strip() for s in str(source or ",".join(AUTONOMY_SOURCES)).split(",") if s.strip()] or list(AUTONOMY_SOURCES)
    patched: list[str] = []
    inspected = 0
    page_size = max(1, min(int(limit or 1000), 1000))
    cursor: dict[str, Any] = {}
    while True:
        try:
            cursor_filter = build_seek_filter(cursor, QUEUE_ORDER)
            rows = sb_get(
                "task_queue",
                (
                    "select=id,task,status,source,created_at,updated_at"
                    f"&status=eq.pending&source=in.({','.join(source_list)})"
                    f"{('&' + cursor_filter) if cursor_filter else ''}"
                    f"&order=priority.desc,created_at.asc,id.asc&limit={page_size}"
                ),
                svc=True,
            ) or []
        except Exception:
            rows = []
        if not rows:
            break

        for row in rows:
            inspected += 1
            cursor = cursor_from_row(row, QUEUE_ORDER)
            task = _parse_task_blob(row)
            auto = task.get("autonomy") if isinstance(task, dict) else {}
            if isinstance(auto, str):
                try:
                    auto = json.loads(auto)
                except Exception:
                    auto = {}
            if not isinstance(auto, dict):
                auto = {}

            title = _safe_text(task.get("title"), 200)
            description = _safe_text(task.get("description"), 1000)
            normalized = build_autonomy_contract(
                title,
                description,
                source=_safe_text(row.get("source") or task.get("source") or "", 80),
                autonomy=auto,
                context="task_autonomy_backfill",
            )

            needs_patch = False
            for key in ("work_track", "route", "review_scope", "owner_only", "task_group", "specialized_worker", "execution_mode", "verification", "expected_artifact"):
                if auto.get(key) in (None, "") and normalized.get(key) not in (None, ""):
                    auto[key] = normalized.get(key)
                    needs_patch = True

            if needs_patch:
                task["autonomy"] = auto
                patch = {
                    "task": json.dumps(task, default=str),
                    "updated_at": _utcnow(),
                }
                if sb_patch("task_queue", f"id=eq.{row.get('id')}&status=eq.pending", patch):
                    patched.append(str(row.get("id") or ""))

        if len(rows) < page_size:
            break

    return {
        "inspected": inspected,
        "patched": len(patched),
        "patched_ids": patched[:20],
    }


def _patch_task(task_id: str, patch: dict) -> bool:
    data = dict(patch or {})
    data.setdefault("updated_at", _utcnow())
    return bool(sb_patch("task_queue", f"id=eq.{task_id}", data))


def _task_checkpoint(
    task_id: str,
    claim_id: str,
    phase: str,
    title: str,
    strategy: dict,
    details: dict | None = None,
    next_step: str = "",
    result: str = "",
) -> bool:
    payload = {
        "claim_id": claim_id,
        "phase": phase,
        "title": title,
        "strategy": strategy,
        "details": _jsonable(details or {}),
        "result": _safe_text(result, 800),
        "ts": _utcnow(),
    }
    patch = {
        "checkpoint": payload,
        "checkpoint_at": payload["ts"],
        "checkpoint_draft": json.dumps(payload, default=str)[:4000],
        "next_step": _safe_text(next_step, 240),
    }
    return _patch_task(task_id, patch)


def _init_agentic_session(task_id: str, claim_id: str, title: str, strategy: dict) -> None:
    try:
        t_agent_session_init(session_id=claim_id, goal=title, chat_id="")
        t_agent_state_set(session_id=claim_id, key="task_id", value=task_id)
        t_agent_state_set(session_id=claim_id, key="task_title", value=title)
        t_agent_state_set(session_id=claim_id, key="task_strategy", value=json.dumps(strategy, default=str))
        t_agent_step_done(session_id=claim_id, step_name="claim", result="agentic session initialized")
    except Exception as e:
        print(f"[TASK_AUTONOMY] agentic init failed: {e}")


def _notify_task_event(stage: str, task_id: str, title: str, claim_id: str, strategy: dict, detail: str = "") -> None:
    if not AUTONOMY_NOTIFY:
        return
    kind = _safe_text(strategy.get("kind"), 80)
    work_track = _safe_text(strategy.get("work_track") or "proposal_only", 40)
    execution_mode = _safe_text(strategy.get("execution_mode") or "proposal", 40)
    source = _safe_text(strategy.get("origin") or strategy.get("source") or "self_assigned", 40)
    worker = _safe_text(strategy.get("specialized_worker") or strategy.get("route") or "evolution_autonomy", 40)
    stage_label = {
        "claimed": "CLAIMED",
        "plan": "PLANNED",
        "completed": "COMPLETED",
        "failed": "FAILED",
    }.get(stage, stage.upper())
    message = (
        f"<b>TASK AUTONOMY</b>\n"
        f"State: {stage_label}\n"
        f"Task: {title}\n"
        f"Task ID: {task_id}\n"
        f"Claim: {claim_id}\n"
        f"Kind: {kind}\n"
        f"Track: {work_track}\n"
        f"Mode: {execution_mode}\n"
        f"Worker: {worker}\n"
        f"Source: {source}\n"
    )
    if detail:
        message += f"Detail: {detail[:800]}\n"
    try:
        notify(message)
    except Exception as e:
        print(f"[TASK_AUTONOMY] notify failed: {e}")


def _notify_cycle(summary: dict) -> None:
    if not AUTONOMY_NOTIFY:
        return
    processed_tasks = summary.get("processed_tasks") or []
    success = 0
    blocked = 0
    deferred = 0
    failures = 0
    artifact_counts: dict[str, int] = {}
    track_counts: dict[str, int] = {}
    deferred_counts: dict[str, int] = {}
    deferred_worker_counts: dict[str, int] = {}
    for item in processed_tasks:
        execution = item.get("execution") or {}
        final = item.get("final") or {}
        artifact_type = _safe_text(execution.get("artifact_type") or item.get("artifact_type") or "unknown", 80)
        artifact_counts[artifact_type] = artifact_counts.get(artifact_type, 0) + 1
        track = _safe_text((item.get("strategy") or {}).get("work_track") or "unknown", 40)
        track_counts[track] = track_counts.get(track, 0) + 1
        if item.get("deferred"):
            deferred += 1
            deferred_counts[track] = deferred_counts.get(track, 0) + 1
            worker = _safe_text(item.get("deferred_to") or _deferred_worker(item.get("strategy") or {}), 40) or "unknown"
            deferred_worker_counts[worker] = deferred_worker_counts.get(worker, 0) + 1
            continue
        if final.get("status") == "done" and item.get("ok"):
            success += 1
        else:
            failures += 1
            if execution.get("blocked") or item.get("blocked"):
                blocked += 1

    def _fmt_item(item: dict) -> str:
        execution = item.get("execution") or {}
        final = item.get("final") or {}
        status = "done" if item.get("ok") else "failed"
        artifact = _safe_text(execution.get("artifact_type") or item.get("artifact_type") or "unknown", 80)
        summary_text = _safe_text(execution.get("summary") or final.get("result", {}).get("summary") or "", 240)
        if not summary_text:
            summary_text = _safe_text((execution.get("verification") or {}).get("rows") and "verified", 240)
        return (
            f"- #{item.get('task_id')} [{status}] {item.get('title')} "
            f"({artifact})"
            + (f" -> {summary_text}" if summary_text else "")
        )

    parts = [
        "<b>TASK AUTONOMY</b>",
        "Cycle summary",
        f"Window: {summary.get('started_at', '?')} -> {summary.get('finished_at', '?')}",
        f"Processed: {summary.get('processed', 0)} | Completed: {success} | Deferred: {summary.get('deferred', deferred)} | Failed: {failures} | Blocked: {blocked} | Errors: {summary.get('errors', 0)}",
        f"Current queue: pending {summary.get('pending_now', '?')} | in_progress {summary.get('in_progress_now', '?')}",
    ]
    if track_counts:
        parts.append("Tracks: " + ", ".join(f"{k}={v}" for k, v in sorted(track_counts.items())))
    if artifact_counts:
        parts.append(
            "Artifacts: " + ", ".join(f"{k}={v}" for k, v in sorted(artifact_counts.items()))
        )
    if deferred_counts:
        parts.append("Deferred tracks: " + ", ".join(f"{k}={v}" for k, v in sorted(deferred_counts.items())))
    if deferred_worker_counts:
        parts.append("Deferred workers: " + ", ".join(f"{k}={v}" for k, v in sorted(deferred_worker_counts.items())))
    for item in processed_tasks[:5]:
        parts.append(_fmt_item(item))
    deferred_tasks = summary.get("deferred_tasks") or []
    if deferred_tasks:
        parts.append("Deferred tasks:")
        for item in deferred_tasks[:3]:
            parts.append(
                f"  - #{item.get('task_id') or '?'} {item.get('title') or 'unknown'} -> "
                f"{_safe_text(item.get('deferred_to') or _deferred_worker(item.get('strategy') or {}), 40) or 'evolution_autonomy'}"
            )
    error_details = summary.get("error_details") or []
    if error_details:
        parts.append("Errors:")
        for err in error_details[:3]:
            parts.append(
                f"  - #{err.get('task_id') or '?'} { _safe_text(err.get('error') or '', 220)}"
            )
    try:
        notify("\n".join(parts))
    except Exception as e:
        print(f"[TASK_AUTONOMY] cycle notify failed: {e}")


def _build_rule_text(title: str, description: str, kind: str) -> str:
    track = ""
    try:
        track = build_autonomy_contract(title, description, context="task_autonomy").get("work_track", "")
    except Exception:
        track = ""
    if kind == "behavioral_remediation":
        return (
            f"When CORE sees the task '{title}', it must checkpoint after each verified substep, "
            f"produce a durable artifact, and mark the task done only after a live readback confirms "
            f"the artifact exists. Context: {description[:240]}"
        )
    if kind == "architecture_proposal":
        return (
            f"Task '{title}' should be treated as a proposal, not an auto-apply change. "
            f"CORE must emit an evolution_queue artifact, verify it exists, then close the task. "
            f"Track: {track or 'proposal_only'}. "
            f"Context: {description[:240]}"
        )
    return (
        f"Task '{title}' should be resolved through a verified artifact and live readback. "
        f"Context: {description[:240]}"
    )


def _execute_strategy(task_id: str, claim_id: str, task: dict, strategy: dict) -> dict:
    title = _safe_text(task.get("title"), 200)
    description = _safe_text(task.get("description"), 1000)
    kind = strategy.get("kind", "analysis_only")
    work_track = _safe_text(strategy.get("work_track") or "proposal_only", 40)
    execution_mode = _safe_text(strategy.get("execution_mode") or "proposal", 40)
    domain = strategy.get("domain", "reasoning")
    artifact_domain = strategy.get("artifact_domain", domain)
    artifact_topic = strategy.get("artifact_topic", f"autonomy:{_slugify(title)}")

    _init_agentic_session(task_id, claim_id, title, strategy)
    t_agent_step_done(session_id=claim_id, step_name="classify", result=json.dumps(strategy, default=str))
    t_agent_state_set(session_id=claim_id, key="task_work_track", value=work_track)
    t_agent_state_set(session_id=claim_id, key="task_execution_mode", value=execution_mode)

    if kind == "kb_expand" or execution_mode == "db_write":
        rule = _build_rule_text(title, description, kind)
        created = t_add_knowledge(
            domain=artifact_domain,
            topic=artifact_topic,
            instruction=rule,
            content=description or title,
            confidence="high",
            source_type="autonomy",
            source_ref=task_id,
        )
        t_agent_state_set(session_id=claim_id, key="artifact_type", value="knowledge_base")
        t_agent_state_set(session_id=claim_id, key="artifact_topic", value=artifact_topic)
        t_agent_step_done(session_id=claim_id, step_name="execute", result=json.dumps(created, default=str))
        verified = sb_get(
            "knowledge_base",
            f"select=id,domain,topic,source_type,source_ref&domain=eq.{artifact_domain}&topic=eq.{artifact_topic}&limit=1",
            svc=True,
        ) or []
        return {
            "ok": bool(created.get("ok")) and bool(verified),
            "artifact_type": "knowledge_base",
            "artifact": created,
            "verification": {"rows": len(verified), "topic": artifact_topic, "domain": artifact_domain},
            "summary": f"Knowledge entry written for {artifact_domain}/{artifact_topic}",
        }

    if kind == "behavioral_remediation":
        trigger = strategy.get("trigger", "session_open")
        rule = _build_rule_text(title, description, kind)
        created = t_add_behavioral_rule(
            trigger=trigger,
            pointer=f"autonomy:{task_id}",
            full_rule=rule,
            domain=artifact_domain if artifact_domain in {
                "universal", "postgres", "railway", "github", "supabase", "groq", "powershell",
                "zapier", "project", "auth", "code", "reasoning", "failure_recovery", "local_pc",
                "telegram"
            } else "reasoning",
            priority="2",
            source="autonomy",
            confidence="0.9",
        )
        t_agent_state_set(session_id=claim_id, key="artifact_type", value="behavioral_rules")
        t_agent_state_set(session_id=claim_id, key="artifact_pointer", value=f"autonomy:{task_id}")
        t_agent_step_done(session_id=claim_id, step_name="execute", result=json.dumps(created, default=str))
        verified = sb_get(
            "behavioral_rules",
            f"select=id,trigger,pointer,domain,source&source=eq.autonomy&pointer=eq.autonomy:{task_id}&limit=1",
            svc=True,
        ) or []
        return {
            "ok": bool(created.get("ok")) and bool(verified),
            "artifact_type": "behavioral_rules",
            "artifact": created,
            "verification": {"rows": len(verified), "pointer": f"autonomy:{task_id}"},
            "summary": f"Behavioral rule written for {title}",
        }

    if kind == "architecture_proposal":
        summary = _build_rule_text(title, description, kind)
        review_scope = _safe_text(strategy.get("review_scope") or ("worker" if work_track in {"db_only", "behavioral_rule", "research"} else "owner"), 40)
        owner_only = bool(strategy.get("owner_only", review_scope == "owner"))
        evo_ok = sb_post(
            "evolution_queue",
            {
                "change_type": "knowledge",
                "change_summary": summary,
                "diff_content": json.dumps(
                    {
                        "task_id": task_id,
                        "claim_id": claim_id,
                        "title": title,
                        "description": description,
                        "strategy": strategy,
                        "source": "task_autonomy",
                        "generated_at": _utcnow(),
                        "autonomy": {
                            "kind": "architecture_proposal",
                            "origin": "task_autonomy",
                            "source": "task_autonomy",
                            "task_id": task_id,
                            "claim_id": claim_id,
                            "work_track": work_track,
                            "execution_mode": execution_mode,
                            "review_scope": review_scope,
                            "owner_only": owner_only,
                            "verification": strategy.get("verification") or "evolution_queue artifact exists",
                            "route": "evolution_autonomy",
                            "specialized_worker": strategy.get("specialized_worker") or "evolution_autonomy",
                            "expected_artifact": "evolution_queue",
                            "next_worker": "evolution_autonomy",
                        },
                    },
                    default=str,
                ),
                "pattern_key": f"autonomy:{task_id}",
                "confidence": 0.82,
                "status": "pending",
                "source": "autonomy",
                "impact": "medium",
                "recommendation": "Review the proposal and apply only after owner approval.",
                "approval_tier": "owner_review" if owner_only else "worker",
                "created_at": _utcnow(),
            },
        )
        t_agent_state_set(session_id=claim_id, key="artifact_type", value="evolution_queue")
        t_agent_state_set(session_id=claim_id, key="artifact_key", value=f"autonomy:{task_id}")
        t_agent_step_done(session_id=claim_id, step_name="execute", result=f"evolution_queue insert ok={evo_ok}")
        verified = sb_get(
            "evolution_queue",
            f"select=id,pattern_key,source,status&source=eq.autonomy&pattern_key=eq.autonomy:{task_id}&limit=1",
            svc=True,
        ) or []
        return {
            "ok": bool(evo_ok) and bool(verified),
            "artifact_type": "evolution_queue",
            "artifact": {"ok": bool(evo_ok), "pattern_key": f"autonomy:{task_id}"},
            "verification": {"rows": len(verified), "pattern_key": f"autonomy:{task_id}"},
            "summary": f"Proposal queued for {title}",
        }

    return {
        "ok": False,
        "artifact_type": "none",
        "artifact": {"ok": False, "reason": "unsupported strategy"},
        "verification": {"rows": 0},
        "summary": f"Unsupported strategy for {title}",
        "blocked": True,
    }


def _finalize_task(
    task_row: dict,
    task: dict,
    claim_id: str,
    strategy: dict,
    execution: dict,
    event_id: str,
) -> dict:
    task_id = str(task_row.get("id"))
    title = _safe_text(task.get("title"), 200)
    now = _utcnow()
    checkpoint = {
        "claim_id": claim_id,
        "phase": "complete" if execution.get("ok") else "blocked",
        "title": title,
        "strategy": strategy,
        "execution": execution,
        "event_id": event_id,
        "ts": now,
    }
    status = "done" if execution.get("ok") else "failed"
    result = {
        "task_id": task_id,
        "title": title,
        "strategy": strategy,
        "execution": execution,
        "claim_id": claim_id,
        "event_id": event_id,
        "completed_at": now,
    }
    patch = {
        "status": status,
        "result": json.dumps(result, default=str)[:4000],
        "error": None if execution.get("ok") else _safe_text(execution.get("summary") or "autonomy failed", 500),
        "checkpoint": checkpoint,
        "checkpoint_at": now,
        "checkpoint_draft": json.dumps(checkpoint, default=str)[:4000],
        "next_step": "" if execution.get("ok") else "owner_review",
        "updated_at": now,
    }
    ok = _patch_task(task_id, patch)
    return {"ok": ok, "status": status, "result": result, "checkpoint": checkpoint}


def process_task_row(task_row: dict) -> dict:
    task_id = str(task_row.get("id") or "")
    task = _parse_task_blob(task_row)
    title = _safe_text(task.get("title"), 200)
    description = _safe_text(task.get("description"), 1000)
    claim_id = str(uuid.uuid4())
    strategy = _classify_task(title, description, _safe_text(task_row.get("source"), 80))

    claim_patch = {
        "status": "in_progress",
        "next_step": "claim_and_plan",
        "checkpoint": {
            "claim_id": claim_id,
            "phase": "claimed",
            "title": title,
            "description": description,
            "strategy": strategy,
            "ts": _utcnow(),
        },
        "checkpoint_at": _utcnow(),
        "checkpoint_draft": json.dumps(
            {
                "claim_id": claim_id,
                "phase": "claimed",
                "title": title,
                "strategy": strategy,
            },
            default=str,
        )[:4000],
        "updated_at": _utcnow(),
    }
    if not _patch_task(task_id, claim_patch):
        failure = t_task_error_packet(
            task_id=task_id,
            error="failed_to_claim",
            phase="claim",
            summary="Failed to claim task row",
            retryable="true",
            next_step="owner_review",
        )
        _notify_task_event("failed", task_id, title, claim_id, strategy, detail=json.dumps(failure, default=str))
        return {
            "ok": False,
            "task_id": task_id,
            "title": title,
            "error": "failed_to_claim",
            "error_packet": failure,
        }

    _state["last_claimed_task_id"] = task_id
    _task_checkpoint(task_id, claim_id, "claimed", title, strategy, {"row": task_row}, "plan", "task claimed")
    _notify_task_event(
        "claimed",
        task_id,
        title,
        claim_id,
        strategy,
        detail=json.dumps({
            "stage": "claimed",
            "next_step": "plan",
            "strategy": strategy,
        }, default=str),
    )

    event_context = {
        "source": "core_autonomy",
        "source_domain": "core",
        "source_branch": "task_autonomy",
        "source_service": "core-agi",
        "event_type": "task_autonomy",
        "trace_id": f"task:{task_id}:{claim_id}",
        "decision_id": task_id,
        "position_id": task_id,
        "session_id": claim_id,
        "status": "received",
        "current_stage": "ingress",
        "current_stage_status": "received",
        "task_title": title,
        "task_strategy": strategy,
        "task_source": task_row.get("source", ""),
        "task_priority": task_row.get("priority", 0),
        "task_work_track": strategy.get("work_track", ""),
        "task_execution_mode": strategy.get("execution_mode", ""),
        "output_text": f"Autonomy task claimed: {title}",
    }
    ingress = register_reflection_event(event_context, event_context["output_text"])
    if ingress is None:
        fail = {
            "ok": False,
            "task_id": task_id,
            "title": title,
            "claim_id": claim_id,
            "error": "reflection_ingress_failed",
            "summary": "Failed to persist canonical reflection event",
        }
        _task_checkpoint(task_id, claim_id, "claimed", title, strategy, fail, "owner_review", fail["summary"])
        failure = t_task_error_packet(
            task_id=task_id,
            error=fail["summary"],
            phase="reflection_ingress",
            summary=fail["summary"],
            retryable="false",
            next_step="owner_review",
            checkpoint=fail,
        )
        _notify_task_event("failed", task_id, title, claim_id, strategy, detail=json.dumps(failure, default=str))
        return {**fail, "error_packet": failure}

    event_id = ingress["event_id"]
    note_reflection_stage(event_id, "critic", source="core_autonomy", status="done", payload={
        "title": title,
        "strategy": strategy,
        "claim_id": claim_id,
    })
    t_agent_step_done(session_id=claim_id, step_name="critic", result=json.dumps(strategy, default=str))
    _task_checkpoint(task_id, claim_id, "plan", title, strategy, {"event_id": event_id}, "execute", "task plan confirmed")

    execution = _execute_strategy(task_id, claim_id, task, strategy)
    note_reflection_stage(event_id, "causal", source="core_autonomy", status="done", payload={
        "execution": execution,
    })
    t_agent_step_done(session_id=claim_id, step_name="execute", result=json.dumps(execution, default=str))

    if execution.get("ok"):
        note_reflection_stage(event_id, "reflect", source="core_autonomy", status="done", payload={
            "verification": execution.get("verification", {}),
        })
        t_agent_step_done(session_id=claim_id, step_name="verify", result=json.dumps(execution.get("verification", {}), default=str))
        finalize_reflection_event(event_id, status="complete", current_stage="meta", current_stage_status="done")
        _task_checkpoint(task_id, claim_id, "verify", title, strategy, execution.get("verification", {}), "complete", execution.get("summary", "verified"))
    else:
        note_reflection_stage(event_id, "reflect", source="core_autonomy", status="failed", payload={
            "execution": execution,
        }, error=execution.get("summary") or "execution_failed")
        finalize_reflection_event(event_id, status="error", current_stage="reflect", current_stage_status="failed", last_error=execution.get("summary") or "execution_failed")
        t_agent_step_done(session_id=claim_id, step_name="verify", result="failed")

    final = _finalize_task(task_row, task, claim_id, strategy, execution, event_id)
    note_reflection_stage(event_id, "meta", source="core_autonomy", status="done" if final.get("ok") else "failed", payload={
        "task_status": final.get("status"),
        "task_id": task_id,
    })
    if final.get("ok"):
        finalize_reflection_event(event_id, status="complete", current_stage="meta", current_stage_status="done")
        t_agent_step_done(session_id=claim_id, step_name="complete", result="task marked done")
    else:
        finalize_reflection_event(event_id, status="error", current_stage="meta", current_stage_status="failed", last_error="task finalization failed")
        t_agent_step_done(session_id=claim_id, step_name="complete", result="finalization_failed")

    _notify_task_event(
        "completed" if final.get("ok") else "failed",
        task_id,
        title,
        claim_id,
        strategy,
        detail=json.dumps({
            "status": final.get("status"),
            "artifact_type": execution.get("artifact_type"),
            "verification": execution.get("verification", {}),
            "summary": execution.get("summary"),
        }, default=str),
    )

    if AUTONOMY_NOTIFY and execution.get("ok"):
        notify(f"[TASK-AUTONOMY] {title}\n{execution.get('summary', '')}")

    return {
        "ok": bool(final.get("ok")),
        "task_id": task_id,
        "title": title,
        "claim_id": claim_id,
        "event_id": event_id,
        "strategy": strategy,
        "execution": execution,
        "final": final,
        "artifact_type": execution.get("artifact_type"),
        "blocked": bool(execution.get("blocked")),
    }


def run_autonomy_cycle(max_tasks: int = AUTONOMY_BATCH_LIMIT, source: str = AUTONOMY_SOURCE) -> dict:
    if not AUTONOMY_ENABLED:
        return {"ok": False, "enabled": False, "message": "CORE_AUTONOMY_ENABLED=false"}

    try:
        max_tasks = int(max_tasks)
    except Exception:
        max_tasks = AUTONOMY_BATCH_LIMIT
    if max_tasks <= 0:
        return {
            "ok": True,
            "enabled": True,
            "started_at": _utcnow(),
            "finished_at": _utcnow(),
            "processed": 0,
            "errors": 0,
            "processed_tasks": [],
            "error_details": [],
            "note": "No-op cycle requested (max_tasks <= 0).",
        }

    with _lock:
        if _state["running"]:
            return {"ok": False, "busy": True, "message": "autonomy cycle already running"}
        _state["running"] = True

    started_at = _utcnow()
    processed: list[dict] = []
    deferred_tasks: list[dict] = []
    errors: list[dict] = []
    track_counts: dict[str, int] = {}
    try:
        metadata_backfill = _backfill_autonomy_metadata(source=source, limit=max_tasks * 20)
        duplicate_cleanup = _close_duplicate_noop_tasks(source=source, limit=max_tasks * 5)
        cursor = _state.get("queue_cursor") or {}
        page_limit = max(100, min(max_tasks * 60, QUEUE_PAGE_LIMIT))
        inspected = 0
        while True:
            rows = _task_rows(limit=page_limit, source=source, cursor=cursor)
            if not rows:
                if cursor:
                    cursor = {}
                    _state["queue_cursor"] = {}
                break
            for row in rows:
                inspected += 1
                cursor = cursor_from_row(row, QUEUE_ORDER)
                _state["queue_cursor"] = cursor
                try:
                    task = _parse_task_blob(row)
                    strategy = _classify_task(_safe_text(task.get("title"), 200), _safe_text(task.get("description"), 1000), _safe_text(row.get("source"), 80))
                    if _is_deferred_track(strategy):
                        deferred_to = _deferred_worker(strategy) or "evolution_autonomy"
                        deferred_tasks.append({
                            "task_id": row.get("id"),
                            "title": _safe_text(task.get("title"), 200),
                            "strategy": strategy,
                            "deferred_to": deferred_to,
                        })
                        continue
                    result = process_task_row(row)
                    processed.append(result)
                    track = _safe_text((result.get("strategy") or {}).get("work_track") or "unknown", 40)
                    track_counts[track] = track_counts.get(track, 0) + 1
                    if len(processed) >= max_tasks:
                        break
                except Exception as e:
                    errors.append({"task_id": row.get("id"), "error": str(e)})
                    try:
                        task_id = str(row.get("id") or "")
                        t_task_error_packet(
                            task_id=task_id,
                            error=str(e),
                            phase="cycle",
                            summary="Unhandled task autonomy exception",
                            retryable="false",
                            next_step="owner_review",
                        )
                    except Exception:
                        pass
            if len(processed) >= max_tasks:
                break
            if len(rows) < page_limit:
                cursor = {}
                _state["queue_cursor"] = {}
                break
        summary = {
            "started_at": started_at,
            "finished_at": _utcnow(),
            "processed": len(processed),
            "deferred": len(deferred_tasks),
            "errors": len(errors),
            "processed_tasks": processed,
            "deferred_tasks": deferred_tasks,
            "error_details": errors,
            "track_counts": track_counts,
            "inspected": inspected,
            "metadata_backfill": metadata_backfill,
            "duplicate_cleanup": duplicate_cleanup,
            "queue_cursor": _state.get("queue_cursor") or {},
        }
        try:
            source_list = list(AUTONOMY_SOURCES)
            pending_now = sb_get(
                "task_queue",
                f"select=id&status=eq.pending&source=in.({','.join(source_list)})",
                svc=True,
            ) or []
            in_progress_now = sb_get(
                "task_queue",
                f"select=id&status=eq.in_progress&source=in.({','.join(source_list)})",
                svc=True,
            ) or []
            summary["pending_now"] = len(pending_now)
            summary["in_progress_now"] = len(in_progress_now)
        except Exception:
            summary["pending_now"] = "?"
            summary["in_progress_now"] = "?"
        _state["last_run_at"] = summary["finished_at"]
        _state["last_summary"] = summary
        _state["last_error"] = ""
        try:
            sb_post("sessions", {
                "summary": f"[state_update] task_autonomy_last_run: {_state['last_run_at']}",
                "actions": [
                    f"task_autonomy cycle processed={len(processed)} errors={len(errors)} tracks={track_counts}",
                ],
                "interface": "task_autonomy",
            })
        except Exception:
            pass
        _notify_cycle(summary)
        return {"ok": True, "enabled": True, **summary}
    except Exception as e:
        _state["last_error"] = str(e)
        return {"ok": False, "enabled": True, "error": str(e), "processed": processed, "errors": errors}
    finally:
        with _lock:
            _state["running"] = False


def autonomy_loop() -> None:
    while AUTONOMY_ENABLED:
        try:
            cycle = run_autonomy_cycle(max_tasks=AUTONOMY_BATCH_LIMIT, source=AUTONOMY_SOURCE)
            if not cycle.get("ok") and cycle.get("busy"):
                time.sleep(min(30, AUTONOMY_INTERVAL_S))
            else:
                time.sleep(AUTONOMY_INTERVAL_S)
        except Exception as e:
            _state["last_error"] = str(e)
            time.sleep(min(60, AUTONOMY_INTERVAL_S))


def autonomy_status() -> dict:
    try:
        source_list = list(AUTONOMY_SOURCES)
        pending = sb_get(
            "task_queue",
            f"select=id&status=eq.pending&source=in.({','.join(source_list)})",
            svc=True,
        ) or []
        in_progress = sb_get(
            "task_queue",
            f"select=id&status=eq.in_progress&source=in.({','.join(source_list)})",
            svc=True,
        ) or []
    except Exception:
        pending = []
        in_progress = []
    return {
        "ok": True,
        "enabled": AUTONOMY_ENABLED,
        "running": _state["running"],
        "interval_seconds": AUTONOMY_INTERVAL_S,
        "batch_limit": AUTONOMY_BATCH_LIMIT,
        "sources": source_list,
        "last_run_at": _state["last_run_at"],
        "last_claimed_task_id": _state["last_claimed_task_id"],
        "last_error": _state["last_error"],
        "pending": len(pending),
        "in_progress": len(in_progress),
        "deferred": _state["last_summary"].get("deferred", 0),
        "track_counts": _state["last_summary"].get("track_counts", {}),
        "queue_cursor": _state.get("queue_cursor") or {},
        "last_summary": _state["last_summary"],
    }


def register_tools() -> None:
    try:
        from core_tools import TOOLS
    except Exception as e:
        print(f"[TASK_AUTONOMY] tool registration skipped: {e}")
        return

    if "task_autonomy_run" not in TOOLS:
        TOOLS["task_autonomy_run"] = {
            "fn": t_task_autonomy_run,
            "perm": "WRITE",
            "args": [
                {"name": "max_tasks", "type": "string", "description": "Max pending tasks to process in this cycle (default 1)"},
                {"name": "source", "type": "string", "description": "task_queue source filter (comma-separated; default self_assigned,improvement)"},
            ],
            "desc": "Run one autonomous claim -> checkpoint -> verify -> complete cycle over pending tasks. Safe by design: only marks done after durable artifact verification.",
        }
    if "task_autonomy_status" not in TOOLS:
        TOOLS["task_autonomy_status"] = {
            "fn": t_task_autonomy_status,
            "perm": "READ",
            "args": [],
            "desc": "Return autonomous worker status, queue depth, and last cycle summary.",
        }


def t_task_autonomy_run(max_tasks: str = str(AUTONOMY_BATCH_LIMIT), source: str = ",".join(AUTONOMY_SOURCES)) -> dict:
    try:
        lim = int(max_tasks) if max_tasks else AUTONOMY_BATCH_LIMIT
    except Exception:
        lim = AUTONOMY_BATCH_LIMIT
    return run_autonomy_cycle(max_tasks=lim, source=source or ",".join(AUTONOMY_SOURCES))


def t_task_autonomy_status() -> dict:
    return autonomy_status()


register_tools()

