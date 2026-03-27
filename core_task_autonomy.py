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

from core_config import sb_get, sb_patch, sb_post
from core_github import notify
from core_reflection_audit import (
    finalize_reflection_event,
    note_reflection_stage,
    register_reflection_event,
)
from core_tools import (
    t_add_knowledge,
    t_agent_session_init,
    t_agent_state_set,
    t_agent_step_done,
    t_add_behavioral_rule,
)

AUTONOMY_ENABLED = os.getenv("CORE_AUTONOMY_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off"
}
AUTONOMY_INTERVAL_S = max(60, int(os.getenv("CORE_AUTONOMY_INTERVAL_S", "300")))
AUTONOMY_BATCH_LIMIT = max(1, int(os.getenv("CORE_AUTONOMY_BATCH_LIMIT", "1")))
AUTONOMY_SOURCE = os.getenv("CORE_AUTONOMY_SOURCE", "self_assigned").strip() or "self_assigned"
AUTONOMY_NOTIFY = os.getenv("CORE_AUTONOMY_NOTIFY", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

_lock = threading.Lock()
_state = {
    "running": False,
    "last_run_at": "",
    "last_summary": {},
    "last_error": "",
    "last_claimed_task_id": "",
}


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
    hay = f"{title}\n{description}\n{source}".lower()
    domain = _extract_domain(title) if "domain:" in title.lower() else _extract_domain(description)
    if "expand kb coverage" in hay or "knowledge base is shallow" in hay or "kb coverage" in hay:
        return {
            "kind": "kb_expand",
            "domain": domain,
            "trigger": "session_open",
            "artifact_domain": domain,
            "artifact_topic": f"autonomy:{_slugify(title)}",
        }
    if "recurring" in hay and "failure" in hay:
        return {
            "kind": "behavioral_remediation",
            "domain": "code",
            "trigger": "post_mistake",
            "artifact_domain": "code",
            "artifact_topic": f"autonomy:{_slugify(title)}",
        }
    if "quality score decline" in hay or "quality declining" in hay or "quality decline" in hay:
        return {
            "kind": "behavioral_remediation",
            "domain": "reasoning",
            "trigger": "session_open",
            "artifact_domain": "reasoning",
            "artifact_topic": f"autonomy:{_slugify(title)}",
        }
    if "stale pending tasks" in hay or "stale tasks" in hay or "no progress" in hay:
        return {
            "kind": "behavioral_remediation",
            "domain": "project",
            "trigger": "on_blocked",
            "artifact_domain": "project",
            "artifact_topic": f"autonomy:{_slugify(title)}",
        }
    if "[rarl]" in hay or "modify core_train.py" in hay or "meta_replay_update" in hay or "architecture" in hay:
        return {
            "kind": "architecture_proposal",
            "domain": "code",
            "artifact_domain": "code",
            "artifact_topic": f"autonomy:{_slugify(title)}",
        }
    return {
        "kind": "architecture_proposal",
        "domain": "reasoning",
        "artifact_domain": "code",
        "artifact_topic": f"autonomy:{_slugify(title)}",
    }


def _task_rows(limit: int = 1, source: str = AUTONOMY_SOURCE) -> list[dict]:
    if limit <= 0:
        return []
    rows = sb_get(
        "task_queue",
        f"select=id,task,status,priority,source,created_at,updated_at,next_step,blocked_by,checkpoint,checkpoint_at,checkpoint_draft"
        f"&status=eq.pending&source=eq.{source}&order=priority.desc&limit={max(1, min(limit, 10))}",
        svc=True,
    ) or []
    # Prefer oldest among the highest-priority slice to avoid starvation.
    def _priority_key(row: dict) -> tuple:
        created = row.get("created_at") or ""
        return (-int(row.get("priority") or 0), created)

    return sorted(rows, key=_priority_key)


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


def _build_rule_text(title: str, description: str, kind: str) -> str:
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
    domain = strategy.get("domain", "reasoning")
    artifact_domain = strategy.get("artifact_domain", domain)
    artifact_topic = strategy.get("artifact_topic", f"autonomy:{_slugify(title)}")

    _init_agentic_session(task_id, claim_id, title, strategy)
    t_agent_step_done(session_id=claim_id, step_name="classify", result=json.dumps(strategy, default=str))

    if kind == "kb_expand":
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
                    },
                    default=str,
                ),
                "pattern_key": f"autonomy:{task_id}",
                "confidence": 0.82,
                "status": "pending",
                "source": "autonomy",
                "impact": "medium",
                "recommendation": "Review the proposal and apply only after owner approval.",
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
        return {
            "ok": False,
            "task_id": task_id,
            "title": title,
            "error": "failed_to_claim",
        }

    _state["last_claimed_task_id"] = task_id
    _task_checkpoint(task_id, claim_id, "claimed", title, strategy, {"row": task_row}, "plan", "task claimed")

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
        _patch_task(task_id, {
            "status": "failed",
            "error": fail["summary"],
            "result": json.dumps(fail, default=str)[:4000],
            "updated_at": _utcnow(),
        })
        return fail

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
    errors: list[dict] = []
    try:
        rows = _task_rows(limit=max_tasks, source=source)
        for row in rows[:max(1, min(int(max_tasks or 1), 10))]:
            try:
                processed.append(process_task_row(row))
            except Exception as e:
                errors.append({"task_id": row.get("id"), "error": str(e)})
                try:
                    task_id = str(row.get("id") or "")
                    _patch_task(task_id, {
                        "status": "failed",
                        "error": _safe_text(str(e), 500),
                        "result": json.dumps({"error": str(e)}, default=str),
                        "updated_at": _utcnow(),
                    })
                except Exception:
                    pass
        summary = {
            "started_at": started_at,
            "finished_at": _utcnow(),
            "processed": len(processed),
            "errors": len(errors),
            "processed_tasks": processed,
            "error_details": errors,
        }
        _state["last_run_at"] = summary["finished_at"]
        _state["last_summary"] = summary
        _state["last_error"] = ""
        try:
            sb_post("sessions", {
                "summary": f"[state_update] task_autonomy_last_run: {_state['last_run_at']}",
                "actions": [
                    f"task_autonomy cycle processed={len(processed)} errors={len(errors)}",
                ],
                "interface": "task_autonomy",
            })
        except Exception:
            pass
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
        pending = sb_get(
            "task_queue",
            f"select=id&status=eq.pending&source=eq.{AUTONOMY_SOURCE}",
            svc=True,
        ) or []
        in_progress = sb_get(
            "task_queue",
            f"select=id&status=eq.in_progress&source=eq.{AUTONOMY_SOURCE}",
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
        "source": AUTONOMY_SOURCE,
        "last_run_at": _state["last_run_at"],
        "last_claimed_task_id": _state["last_claimed_task_id"],
        "last_error": _state["last_error"],
        "pending": len(pending),
        "in_progress": len(in_progress),
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
                {"name": "source", "type": "string", "description": "task_queue source filter (default self_assigned)"},
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


def t_task_autonomy_run(max_tasks: str = "1", source: str = AUTONOMY_SOURCE) -> dict:
    try:
        lim = int(max_tasks) if max_tasks else AUTONOMY_BATCH_LIMIT
    except Exception:
        lim = AUTONOMY_BATCH_LIMIT
    return run_autonomy_cycle(max_tasks=lim, source=source or AUTONOMY_SOURCE)


def t_task_autonomy_status() -> dict:
    return autonomy_status()


register_tools()
