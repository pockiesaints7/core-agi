"""core_research_autonomy.py -- specialized research worker for CORE.

This worker consumes research-class tasks and turns them into durable memory.
It does not patch code directly. It:

- validates the task against CORE memory;
- writes a knowledge_base entry with the research outcome;
- optionally queues a follow-up evolution when implementation work is needed;
- leaves a clear audit trail on the task row.
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from typing import Any
from core_config import _env_int, sb_get, sb_patch, sb_post, groq_chat, GROQ_MODEL
from core_github import notify
from core_queue_cursor import build_seek_filter, cursor_from_row
from core_tools import t_add_knowledge, t_reasoning_packet, t_agent_session_init, t_agent_state_set, t_agent_step_done
from core_work_taxonomy import build_autonomy_contract

AUTONOMY_ENABLED = os.getenv("CORE_RESEARCH_AUTONOMY_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off"
}
AUTONOMY_INTERVAL_S = max(300, _env_int("CORE_RESEARCH_AUTONOMY_INTERVAL_S", "600"))
AUTONOMY_BATCH_LIMIT = max(1, _env_int("CORE_RESEARCH_AUTONOMY_BATCH_LIMIT", "3"))
AUTONOMY_NOTIFY = os.getenv("CORE_RESEARCH_AUTONOMY_NOTIFY", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
TASK_SOURCES = tuple(
    s.strip() for s in os.getenv("CORE_RESEARCH_TASK_SOURCES", "mcp_session,self_assigned,improvement").split(",") if s.strip()
)

_lock = threading.Lock()
_state = {
    "running": False,
    "last_run_at": "",
    "last_summary": {},
    "last_error": "",
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


def _escape(value: Any, limit: int = 240) -> str:
    return html.escape(_safe_text(value, limit), quote=False)


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
    description = _safe_text(task.get("description") or "", 1200)
    task.setdefault("title", title)
    task.setdefault("description", description)
    return task


def _task_strategy(task: dict, title: str, description: str, source: str = "") -> dict:
    autonomy = task.get("autonomy") if isinstance(task, dict) else {}
    return build_autonomy_contract(title, description, source=source, autonomy=autonomy, context="research_autonomy")


def _task_rows(limit: int = 1, cursor: dict | None = None) -> list[dict]:
    source_list = [s.strip() for s in TASK_SOURCES if s.strip()] or ["mcp_session", "self_assigned", "improvement"]
    cursor_filter = build_seek_filter(cursor, QUEUE_ORDER)
    rows = sb_get(
        "task_queue",
        "select=id,task,status,priority,source,created_at,updated_at,next_step,blocked_by,checkpoint,checkpoint_at,checkpoint_draft"
        f"&status=eq.pending&source=in.({','.join(source_list)})"
        f"{('&' + cursor_filter) if cursor_filter else ''}"
        f"&order=priority.desc,created_at.asc,id.asc&limit={max(1, min(limit, 1500))}",
        svc=True,
    ) or []
    rows.sort(key=lambda row: (-int(row.get("priority") or 0), row.get("created_at") or "", str(row.get("id") or "")))
    return rows


def _research_rows(limit: int = 1) -> list[dict]:
    rows = _task_rows(limit=max(limit, 250))
    picked = []
    for row in rows:
        task = _parse_task_blob(row)
        strategy = _task_strategy(task, task.get("title") or "", task.get("description") or "", source=row.get("source") or "")
        if strategy.get("work_track") == "research":
            picked.append(row)
        if len(picked) >= limit:
            break
    return picked


def _research_pending_rows(limit: int = 500) -> list[dict]:
    rows = _task_rows(limit=max(1, min(limit, 1500)))
    picked = []
    for row in rows:
        task = _parse_task_blob(row)
        strategy = _task_strategy(task, task.get("title") or "", task.get("description") or "", source=row.get("source") or "")
        if strategy.get("work_track") == "research":
            picked.append(row)
    return picked


def _count_rows(table: str, qs: str) -> int:
    try:
        rows = sb_get(table, qs, svc=True) or []
        return len(rows)
    except Exception:
        return 0


def _memory_context(query: str, domain: str = "") -> dict:
    try:
        from core_tools import t_reasoning_packet
        return t_reasoning_packet(
            query=query,
            domain=domain or "general",
            limit="8",
            per_table="2",
            tables="knowledge_base,mistakes,behavioral_rules,hot_reflections,output_reflections,evolution_queue,conversation_episodes",
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _memory_excerpt(packet: dict) -> str:
    pkt = packet.get("packet") if isinstance(packet, dict) else {}
    if not isinstance(pkt, dict):
        pkt = {}
    lines = []
    for hit in (pkt.get("top_hits") or [])[:8]:
        table = _safe_text(hit.get("table") or "unknown", 40)
        title = _safe_text(hit.get("title") or hit.get("body") or "", 180)
        score = hit.get("score")
        lines.append(f"- {table} ({score}): {title}")
    return "\n".join(lines) or "none"


def _synthesize_research(task: dict, strategy: dict, memory_packet: dict) -> dict:
    title = _safe_text(task.get("title") or "", 200)
    description = _safe_text(task.get("description") or "", 1200)
    task_group = _safe_text(strategy.get("task_group") or "research", 40)
    domain = _safe_text(strategy.get("domain") or strategy.get("artifact_domain") or "research", 80)
    prompt = (
        "You are CORE's research autonomy worker.\n"
        "Use the task and memory context to produce a concise research synthesis.\n"
        "Return ONLY valid JSON.\n\n"
        f"TASK TITLE: {title}\n"
        f"TASK DESCRIPTION: {description}\n"
        f"TRACK: {strategy.get('work_track')}\n"
        f"TASK GROUP: {task_group}\n"
        f"DOMAIN: {domain}\n\n"
        f"MEMORY CONTEXT:\n{_memory_excerpt(memory_packet)}\n\n"
        "Return JSON:\n"
        "{\n"
        '  "kb_topic": "short unique topic",\n'
        '  "kb_instruction": "one actionable knowledge entry CORE should remember",\n'
        '  "kb_content": "supporting details and evidence summary",\n'
        '  "confidence": "high|medium|low",\n'
        '  "summary": "one sentence research result",\n'
        '  "follow_up_needed": true|false,\n'
        '  "follow_up_work_track": "code_patch|new_module|integration|behavioral_rule|db_only|",\n'
        '  "follow_up_summary": "optional follow-up suggestion",\n'
        '  "follow_up_reason": "why follow-up is needed",\n'
        '  "evidence_notes": "short evidence notes"\n'
        "}\n"
    )
    try:
        raw = groq_chat(
            system="You are a precise research planner. Return JSON only.",
            user=prompt,
            model=GROQ_MODEL,
            max_tokens=900,
        )
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("non-dict result")
        return result
    except Exception as e:
        return {
            "kb_topic": f"research_{re.sub(r'[^a-z0-9]+', '_', title.lower())[:40] or 'task'}",
            "kb_instruction": f"Research task completed for: {title}",
            "kb_content": f"Task: {description}\nMemory: {_memory_excerpt(memory_packet)}",
            "confidence": "medium",
            "summary": f"Research synthesis for {title}",
            "follow_up_needed": False,
            "follow_up_work_track": "",
            "follow_up_summary": "",
            "follow_up_reason": f"LLM fallback used: {str(e)[:120]}",
            "evidence_notes": "Fallback synthesis",
        }


def _queue_follow_up(task: dict, strategy: dict, result: dict) -> dict | None:
    follow_up = str(result.get("follow_up_work_track") or "").strip()
    if follow_up not in {"code_patch", "new_module", "integration", "behavioral_rule", "db_only"}:
        return None
    follow_up_summary = _safe_text(result.get("follow_up_summary") or "", 500)
    if not follow_up_summary:
        return None
    task_id = str(task.get("id") or "")
    follow_title = f"[RESEARCH] {follow_up_summary[:120]}"
    follow_description = (
        f"Derived from research task #{task_id}\n"
        f"Original task: {_safe_text(task.get('task') or '', 800)}\n"
        f"Research summary: {_safe_text(result.get('summary') or '', 500)}\n"
        f"Reason: {_safe_text(result.get('follow_up_reason') or '', 600)}"
    )
    autonomy = build_autonomy_contract(
        follow_title,
        description=follow_description,
        source="research_autonomy",
        autonomy={
            "kind": "architecture_proposal",
            "origin": "research_autonomy",
            "source": "research_autonomy",
            "task_id": task_id,
            "work_track": follow_up,
            "execution_mode": "proposal" if follow_up in {"code_patch", "new_module", "integration"} else "db_write",
            "verification": "evolution_queue artifact exists",
            "expected_artifact": "evolution_queue",
            "task_group": "research" if follow_up == "db_only" else "code" if follow_up in {"code_patch", "new_module", "integration"} else "behavior",
            "specialized_worker": "proposal_router",
            "route": "proposal_router",
            "priority": 2,
        },
        context="research_autonomy",
    )
    # This follow-up is an evolution proposal. Tag it explicitly so routers can
    # keep owner-only items out of worker-only queues and vice versa.
    approval_tier = "owner_review" if bool(autonomy.get("owner_only")) else "worker"
    return {
        "change_type": "knowledge" if follow_up == "db_only" else "behavior" if follow_up == "behavioral_rule" else "code",
        "change_summary": follow_up_summary,
        "pattern_key": f"research:{task_id}:{follow_up_summary[:60]}",
        "confidence": 0.75,
        "status": "pending",
        "source": "research_autonomy",
        "impact": strategy.get("domain") or "research",
        "recommendation": _safe_text(result.get("follow_up_reason") or "Review and decide whether to route to the next specialized worker.", 600),
        "approval_tier": approval_tier,
        "diff_content": json.dumps({
            "origin_task_id": task_id,
            "source": "research_autonomy",
            "autonomy": autonomy,
            "research_result": _jsonable(result),
        }, default=str),
    }


def _claim_task(task: dict, strategy: dict) -> dict:
    task_id = str(task.get("id") or "")
    payload = _parse_task_blob(task)
    title = _safe_text(payload.get("title") or task_id, 200)
    description = _safe_text(payload.get("description") or "", 1200)
    claim_id = str(uuid.uuid4())
    try:
        t_agent_session_init(session_id=claim_id, goal=title, chat_id="")
        t_agent_state_set(session_id=claim_id, key="task_id", value=task_id)
        t_agent_state_set(session_id=claim_id, key="task_title", value=title)
        t_agent_state_set(session_id=claim_id, key="task_work_track", value="research")
        t_agent_step_done(session_id=claim_id, step_name="claim", result="research task claimed")
    except Exception:
        pass

    memory_packet = _memory_context(f"{title}\n{description}", domain=strategy.get("domain") or "")
    result = _synthesize_research(payload, strategy, memory_packet)

    topic = _safe_text(result.get("kb_topic") or f"research:{title}", 120)
    instruction = _safe_text(result.get("kb_instruction") or "", 1200)
    content = _safe_text(result.get("kb_content") or "", 2000)
    confidence = _safe_text(result.get("confidence") or "medium", 20)
    source_ref = f"task_queue:{task_id}"

    kb_res = t_add_knowledge(
        domain=_safe_text(strategy.get("domain") or strategy.get("artifact_domain") or "research", 80),
        topic=topic,
        instruction=instruction,
        content=content,
        confidence=confidence,
        source_type="research_autonomy",
        source_ref=source_ref,
    )

    follow_up = _queue_follow_up(task, strategy, result)
    if follow_up:
        sb_post("evolution_queue", follow_up)

    task_patch = {
        "status": "done" if kb_res.get("ok") else "failed",
        "result": json.dumps({
            "summary": _safe_text(result.get("summary") or "", 500),
            "kb_topic": topic,
            "kb_write_ok": bool(kb_res.get("ok")),
            "follow_up_queued": bool(follow_up),
        }, default=str),
        "error": "" if kb_res.get("ok") else _safe_text(kb_res.get("error") or "research write failed", 500),
        "updated_at": _utcnow(),
        "next_step": "owner_review" if follow_up else "",
        "checkpoint": {
            "claim_id": claim_id,
            "stage": "research_complete",
            "summary": _safe_text(result.get("summary") or "", 500),
            "memory": memory_packet.get("ok", False),
            "kb_topic": topic,
            "follow_up": bool(follow_up),
        },
        "checkpoint_at": _utcnow(),
        "checkpoint_draft": json.dumps({
            "claim_id": claim_id,
            "stage": "research_complete",
            "summary": _safe_text(result.get("summary") or "", 500),
            "kb_topic": topic,
            "follow_up": bool(follow_up),
        }, default=str)[:4000],
    }
    sb_patch("task_queue", f"id=eq.{task_id}", task_patch)

    try:
        t_agent_state_set(session_id=claim_id, key="artifact_type", value="knowledge_base")
        t_agent_state_set(session_id=claim_id, key="artifact_pointer", value=source_ref)
        t_agent_step_done(session_id=claim_id, step_name="verify", result=f"kb_write_ok={bool(kb_res.get('ok'))}")
    except Exception:
        pass

    return {
        "task_id": task_id,
        "claim_id": claim_id,
        "title": title,
        "domain": strategy.get("domain") or strategy.get("artifact_domain") or "research",
        "kb_ok": bool(kb_res.get("ok")),
        "kb_topic": topic,
        "follow_up_queued": bool(follow_up),
        "summary": _safe_text(result.get("summary") or "", 500),
        "track": strategy.get("work_track") or "research",
    }


def _notify_cycle(summary: dict) -> None:
    if not AUTONOMY_NOTIFY:
        return
    lines = [
        "<b>RESEARCH AUTONOMY CYCLE</b>",
        f"Window: {summary.get('started_at', '?')} -> {summary.get('finished_at', '?')}",
        f"Processed: {summary.get('processed', 0)} | Completed: {summary.get('completed', 0)} | Failed: {summary.get('failed', 0)} | Skipped: {summary.get('skipped', 0)}",
        f"Pending research tasks: {summary.get('pending', 0)} | Follow-up evolutions: {summary.get('follow_up_queued', 0)}",
    ]
    if summary.get("track_counts"):
        lines.append("Tracks: " + ", ".join(f"{_escape(k)}={v}" for k, v in sorted(summary["track_counts"].items())))
    for item in (summary.get("details") or [])[:5]:
        status = "done" if item.get("ok") else "failed"
        lines.append(
            f"- #{item.get('task_id')} [{status}] {_escape(item.get('title') or '', 100)} "
            f"({_escape(item.get('track') or 'research', 20)} -> kb:{'ok' if item.get('kb_ok') else 'fail'})"
        )
        if item.get("follow_up_queued"):
            lines.append("  follow-up: queued")
    try:
        notify("\n".join(lines))
    except Exception:
        pass


def run_research_autonomy_cycle(max_tasks: int = AUTONOMY_BATCH_LIMIT) -> dict:
    started_at = _utcnow()
    with _lock:
        if _state["running"]:
            return {"ok": False, "busy": True, "message": "research autonomy cycle already running"}
        _state["running"] = True
    try:
        cursor = _state.get("queue_cursor") or {}
        page_limit = max(100, min(max_tasks * 80, QUEUE_PAGE_LIMIT))
        processed = []
        skipped = 0
        failed = 0
        follow_up_queued = 0
        track_counts = Counter()
        claimed = 0
        inspected = 0
        while True:
            rows = _task_rows(limit=page_limit, cursor=cursor)
            if not rows:
                if cursor:
                    cursor = {}
                    _state["queue_cursor"] = {}
                break
            for row in rows:
                task = _parse_task_blob(row)
                strategy = _task_strategy(task, task.get("title") or "", task.get("description") or "", source=row.get("source") or "")
                track_counts[strategy.get("work_track") or "unknown"] += 1
                inspected += 1
                cursor = cursor_from_row(row, QUEUE_ORDER)
                _state["queue_cursor"] = cursor
                if claimed >= max_tasks:
                    break
                if strategy.get("work_track") != "research":
                    skipped += 1
                    continue
                try:
                    result = _claim_task(row, strategy)
                    processed.append(result)
                    if result.get("kb_ok"):
                        claimed += 1
                    if result.get("follow_up_queued"):
                        follow_up_queued += 1
                    if not result.get("kb_ok"):
                        failed += 1
                except Exception as e:
                    failed += 1
                    processed.append({
                        "task_id": row.get("id"),
                        "title": _safe_text(task.get("title") or "", 120),
                        "ok": False,
                        "kb_ok": False,
                        "track": "research",
                        "error": str(e),
                    })
            if claimed >= max_tasks:
                break
            if len(rows) < page_limit:
                cursor = {}
                _state["queue_cursor"] = {}
                break

        pending = len(_research_pending_rows(limit=1000))
        status = {
            "ok": True,
            "enabled": AUTONOMY_ENABLED,
            "running": False,
            "started_at": started_at,
            "finished_at": _utcnow(),
            "processed": len(processed),
            "inspected": inspected,
            "completed": sum(1 for item in processed if item.get("kb_ok")),
            "failed": failed,
            "skipped": skipped,
            "follow_up_queued": follow_up_queued,
            "pending": pending,
            "track_counts": dict(sorted(track_counts.items())),
            "details": processed,
            "queue_cursor": _state.get("queue_cursor") or {},
        }
        with _lock:
            _state["last_run_at"] = status["finished_at"]
            _state["last_summary"] = status
            _state["last_error"] = ""
        _notify_cycle(status)
        return status
    except Exception as e:
        with _lock:
            _state["last_error"] = str(e)
        return {
            "ok": False,
            "enabled": AUTONOMY_ENABLED,
            "running": False,
            "started_at": started_at,
            "finished_at": _utcnow(),
            "error": str(e),
            "processed": 0,
            "completed": 0,
            "failed": 1,
            "skipped": 0,
            "follow_up_queued": 0,
            "pending": _count_rows("task_queue", "select=id&status=eq.pending&source=in.(mcp_session,self_assigned,improvement)"),
            "track_counts": {},
            "details": [],
        }
    finally:
        with _lock:
            _state["running"] = False


def research_autonomy_loop() -> None:
    while True:
        try:
            if AUTONOMY_ENABLED:
                run_research_autonomy_cycle(max_tasks=AUTONOMY_BATCH_LIMIT)
        except Exception as e:
            with _lock:
                _state["last_error"] = str(e)
        time.sleep(AUTONOMY_INTERVAL_S)


def research_autonomy_status() -> dict:
    pending = len(_research_pending_rows(limit=1000))
    summary = _state.get("last_summary") or {}
    return {
        "enabled": AUTONOMY_ENABLED,
        "running": _state.get("running", False),
        "interval_seconds": AUTONOMY_INTERVAL_S,
        "batch_limit": AUTONOMY_BATCH_LIMIT,
        "sources": list(TASK_SOURCES),
        "pending": pending,
        "last_run_at": _state.get("last_run_at", ""),
        "last_summary": summary,
        "last_error": _state.get("last_error", ""),
        "completed_tasks": int(summary.get("completed") or 0),
        "follow_up_queued": int(summary.get("follow_up_queued") or 0),
        "track_counts": summary.get("track_counts") or {},
        "recent_tasks": summary.get("details") or [],
        "queue_cursor": _state.get("queue_cursor") or {},
    }


def render_research_status_report(status: dict) -> str:
    last = status.get("last_summary") or {}
    lines = [
        f"Status: <b>{'enabled' if status.get('enabled') else 'disabled'}</b> | running={status.get('running')} | interval={status.get('interval_seconds')}s | batch={status.get('batch_limit')}",
        f"Sources: {_escape(', '.join(status.get('sources') or []), 120)}",
        f"Queue: pending {status.get('pending', 0)} | completed {status.get('completed_tasks', 0)} | follow-up queued {status.get('follow_up_queued', 0)}",
    ]
    if status.get("track_counts"):
        lines.append("Tracks: " + ", ".join(f"{_escape(k)}={v}" for k, v in sorted(status["track_counts"].items())))
    if last.get("finished_at"):
        lines.append(f"Last run: {_escape(last.get('finished_at') or status.get('last_run_at') or 'n/a', 40)}")
    tasks = last.get("details") or []
    if tasks:
        lines.append("")
        lines.append("<b>Recent cycle</b>")
        for item in tasks[:3]:
            lines.append(
                f"- #{item.get('task_id')} [{'done' if item.get('kb_ok') else 'failed'}] "
                f"{_escape(item.get('title') or '', 90)} "
                f"(kb={'ok' if item.get('kb_ok') else 'fail'}, follow-up={'yes' if item.get('follow_up_queued') else 'no'})"
            )
    return "\n".join(["<b>Research Autonomy</b>"] + lines)


def research_autonomy_summary(limit: int = 5) -> dict:
    return {
        "ok": True,
        "status": research_autonomy_status(),
        "report": render_research_status_report(research_autonomy_status()),
    }


def register_tools() -> None:
    try:
        from core_tools import TOOLS
    except Exception:
        return

    if "research_autonomy_status" not in TOOLS:
        TOOLS["research_autonomy_status"] = {
            "fn": lambda: research_autonomy_status(),
            "desc": "Return research autonomy worker status, queue depth, and recent cycle summary.",
            "args": [],
        }
    if "research_autonomy_run" not in TOOLS:
        TOOLS["research_autonomy_run"] = {
            "fn": lambda max_tasks="2": run_research_autonomy_cycle(max_tasks=int(max_tasks or 2)),
            "desc": "Run the research autonomy worker for a bounded batch.",
            "args": [{"name": "max_tasks", "type": "string", "description": "Maximum number of research tasks to process."}],
        }


register_tools()


