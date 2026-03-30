"""core_code_autonomy.py -- code planning autonomy for CORE.

This worker does not auto-edit code. It turns code-class tasks into a
production-grade review packet, stores the packet as an evolution proposal,
and leaves a durable audit trail on the originating task row.
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
from pathlib import Path
from typing import Any
from core_config import sb_get, sb_post, sb_patch
from core_github import notify
from core_queue_cursor import build_seek_filter, cursor_from_row
from core_reflection_audit import (
    finalize_reflection_event,
    note_reflection_stage,
    register_reflection_event,
)
from core_tools import t_agent_session_init, t_agent_state_set, t_agent_step_done, t_reasoning_packet
from core_work_taxonomy import build_autonomy_contract


def _env_clean(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    if value is None:
        return default
    value = value.strip()
    if "#" in value:
        value = value.split("#", 1)[0].strip()
    return value


def _env_int(name: str, default: str) -> int:
    try:
        return int(_env_clean(name, default))
    except Exception:
        return int(default)


def _env_float(name: str, default: str) -> float:
    try:
        return float(_env_clean(name, default))
    except Exception:
        return float(default)
AUTONOMY_ENABLED = os.getenv("CORE_CODE_AUTONOMY_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off"
}
AUTONOMY_INTERVAL_S = max(300, _env_int("CORE_CODE_AUTONOMY_INTERVAL_S", "900"))
AUTONOMY_BATCH_LIMIT = max(1, _env_int("CORE_CODE_AUTONOMY_BATCH_LIMIT", "1"))
AUTONOMY_NOTIFY = os.getenv("CORE_CODE_AUTONOMY_NOTIFY", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
TASK_SOURCES = tuple(
    s.strip() for s in os.getenv("CORE_CODE_TASK_SOURCES", "mcp_session,self_assigned,improvement").split(",") if s.strip()
)
CODE_TRACKS = {"code_patch", "new_module"}

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

_REPO_ROOT = Path(__file__).resolve().parent
_CODE_FILE_HINTS = [
    {
        "path": "core_main.py",
        "focus": ["telegram", "command", "review", "autonomy", "startup", "webhook", "deploy", "health"],
        "anchors": [
            "CORE_TELEGRAM_COMMANDS",
            "def handle_msg",
            "def _render_autonomy_overview_report",
            "def _render_review_dashboard",
            "def _render_health_report",
            "@app.on_event(\"startup\")",
            "def _deployment_manifest",
        ],
    },
    {
        "path": "core_github.py",
        "focus": ["telegram", "webhook", "menu", "notify", "commands"],
        "anchors": [
            "set_telegram_commands",
            "set_webhook",
            "notify",
        ],
    },
    {
        "path": "core_tools.py",
        "focus": ["tool", "mcp", "memory", "reasoning", "packet", "search"],
        "anchors": [
            "def t_reasoning_packet",
            "def t_search_memory",
            "def t_search_kb",
            "TOOLS = {",
            "def handle_jsonrpc",
        ],
    },
    {
        "path": "core_work_taxonomy.py",
        "focus": ["taxonomy", "track", "worker", "route", "verification", "contract"],
        "anchors": [
            "def build_autonomy_contract",
            "def _infer_work_track",
            "def _infer_execution_mode",
            "def _infer_verification",
            "def _infer_specialized_worker",
        ],
    },
    {
        "path": "core_task_autonomy.py",
        "focus": ["task", "checkpoint", "claim", "verify", "behavior", "knowledge"],
        "anchors": [
            "def process_task_row",
            "def _execute_strategy",
            "def run_autonomy_cycle",
            "def _render_task_status_report",
            "def autonomy_status",
        ],
    },
    {
        "path": "core_evolution_autonomy.py",
        "focus": ["evolution", "proposal", "improvement", "synthesize", "queue"],
        "anchors": [
            "def process_evolution_row",
            "def run_evolution_autonomy_cycle",
            "def evolution_autonomy_status",
            "def render_evolution_status_report",
        ],
    },
    {
        "path": "core_proposal_router.py",
        "focus": ["proposal", "review", "reroute", "route", "approval"],
        "anchors": [
            "def queue_review_reroute",
            "def proposal_router_status",
            "def render_proposal_router_dashboard",
            "def proposal_router_summary",
        ],
    },
    {
        "path": "core_research_autonomy.py",
        "focus": ["research", "knowledge", "follow-up", "validate"],
        "anchors": [
            "def _synthesize_research",
            "def _queue_follow_up",
            "def run_research_autonomy_cycle",
            "def research_autonomy_status",
        ],
    },
    {
        "path": "core_orch_main.py",
        "focus": ["orchestrator", "startup", "telegram", "layer"],
        "anchors": [
            "def startup_v2",
            "def handle_telegram_message_v2",
            "def handle_telegram_message",
        ],
    },
    {
        "path": "core_reflection_audit.py",
        "focus": ["reflection", "event", "audit", "stage"],
        "anchors": [
            "def register_reflection_event",
            "def note_reflection_stage",
            "def finalize_reflection_event",
            "def fetch_reflection_events",
        ],
    },
    {
        "path": "core_semantic_projection.py",
        "focus": ["semantic", "projection", "memory", "index"],
        "anchors": [
            "def semantic_projection_status",
            "def run_semantic_projection_cycle",
            "def semantic_projection_loop",
        ],
    },
    {
        "path": "core_train.py",
        "focus": ["train", "task", "evolution", "proposal", "autonomy"],
        "anchors": [
            "def self_diagnosis",
            "def _create_evolution",
            "def process_cycle",
            "def background_researcher",
        ],
    },
]


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
    return build_autonomy_contract(title, description, source=source, autonomy=autonomy, context="code_autonomy")


def _task_rows(limit: int = 1, cursor: dict | None = None) -> list[dict]:
    source_list = [s.strip() for s in TASK_SOURCES if s.strip()] or ["mcp_session", "self_assigned", "improvement"]
    cursor_filter = build_seek_filter(cursor, QUEUE_ORDER)
    rows = sb_get(
        "task_queue",
        "select=id,task,status,priority,source,created_at,updated_at,next_step,blocked_by,checkpoint,checkpoint_at,checkpoint_draft"
        f"&status=eq.pending&source=in.({','.join(source_list)})"
        f"{('&' + cursor_filter) if cursor_filter else ''}"
        f"&order=priority.desc,created_at.asc,id.asc&limit={max(1, min(limit, QUEUE_PAGE_LIMIT))}",
        svc=True,
    ) or []
    rows.sort(key=lambda row: (-int(row.get("priority") or 0), row.get("created_at") or "", str(row.get("id") or "")))
    return rows


def _code_rows(limit: int = 1) -> list[dict]:
    rows = _task_rows(limit=max(limit, 250))
    picked = []
    for row in rows:
        task = _parse_task_blob(row)
        strategy = _task_strategy(task, task.get("title") or "", task.get("description") or "", source=row.get("source") or "")
        if strategy.get("work_track") in CODE_TRACKS:
            picked.append(row)
        if len(picked) >= limit:
            break
    return picked


def _code_pending_rows(limit: int = 500) -> list[dict]:
    rows = _task_rows(limit=max(1, min(limit, 500)))
    picked = []
    for row in rows:
        task = _parse_task_blob(row)
        strategy = _task_strategy(task, task.get("title") or "", task.get("description") or "", source=row.get("source") or "")
        if strategy.get("work_track") in CODE_TRACKS:
            picked.append(row)
    return picked


def _count_rows(table: str, qs: str) -> int:
    try:
        rows = sb_get(table, qs, svc=True) or []
        return len(rows)
    except Exception:
        return 0


def _patch_task(task_id: str, patch: dict) -> bool:
    data = dict(patch or {})
    data.setdefault("updated_at", _utcnow())
    try:
        return bool(sb_patch("task_queue", f"id=eq.{task_id}", data))
    except Exception:
        return False


def _repo_root() -> Path:
    return _REPO_ROOT


def _file_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        try:
            return path.read_text(errors="ignore")
        except Exception:
            return ""


def _line_anchor(text: str, anchors: list[str]) -> dict:
    lines = text.splitlines()
    for anchor in anchors:
        anchor_l = anchor.lower().strip()
        for idx, line in enumerate(lines, start=1):
            if anchor_l in line.lower():
                start = max(1, idx - 2)
                end = min(len(lines), idx + 2)
                snippet = "\n".join(lines[start - 1 : end])
                return {"line": idx, "anchor": anchor, "snippet": snippet}
    return {"line": 0, "anchor": "", "snippet": ""}


def _task_terms(text: str) -> list[str]:
    raw = re.findall(r"[a-zA-Z][a-zA-Z0-9_+.-]{2,}", text.lower())
    stop = {
        "the", "and", "for", "with", "from", "this", "that", "into", "when", "what",
        "need", "make", "more", "have", "will", "only", "also", "about", "your",
        "code", "worker", "task", "propose", "proposal", "review", "update", "change",
    }
    out = []
    for token in raw:
        if token not in stop and token not in out:
            out.append(token)
    return out[:12]


def _candidate_file_contexts(task: dict, strategy: dict) -> list[dict]:
    title = _safe_text(task.get("title") or "", 200)
    description = _safe_text(task.get("description") or "", 1200)
    hay = f"{title}\n{description}\n{json.dumps(strategy, default=str)}".lower()
    terms = set(_task_terms(hay))
    contexts: list[dict] = []
    for spec in _CODE_FILE_HINTS:
        path = _repo_root() / spec["path"]
        if not path.exists():
            continue
        focus_hits = sum(1 for term in spec["focus"] if term in hay)
        anchor_hits = sum(1 for term in spec["anchors"] if term.lower().replace("def ", "") in hay)
        keyword_hits = sum(1 for term in terms if term in " ".join(spec["focus"] + spec["anchors"]).lower())
        score = focus_hits * 3 + anchor_hits * 2 + keyword_hits
        if score <= 0:
            if spec["path"] in {"core_main.py", "core_tools.py", "core_work_taxonomy.py"}:
                score = 1
            else:
                continue
        text = _file_text(path)
        anchor = _line_anchor(text, spec["anchors"])
        contexts.append(
            {
                "path": spec["path"],
                "score": score,
                "focus": spec["focus"],
                "anchor": anchor,
            }
        )
    contexts.sort(key=lambda item: (-int(item.get("score") or 0), item.get("path") or ""))
    return contexts[:4]


def _memory_context(query: str, domain: str = "") -> dict:
    try:
        return t_reasoning_packet(
            query=query,
            domain=domain or "code",
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


def _code_change_type(work_track: str) -> str:
    if work_track == "new_module":
        return "new_tool"
    return "code"


def _fallback_code_packet(task: dict, strategy: dict, memory_packet: dict, file_contexts: list[dict]) -> dict:
    title = _safe_text(task.get("title") or "", 200)
    description = _safe_text(task.get("description") or "", 1200)
    work_track = _safe_text(strategy.get("work_track") or "code_patch", 40)
    change_type = _code_change_type(work_track)
    files = []
    for ctx in file_contexts[:3]:
        path = ctx.get("path")
        anchor = ctx.get("anchor") or {}
        line = int(anchor.get("line") or 0)
        files.append(
            {
                "path": path,
                "line": line,
                "anchor": anchor.get("anchor") or "",
                "why": f"Integration point for {work_track} in {path}",
                "change": "Inspect the surrounding block and apply the minimal bounded patch needed for the proposal.",
                "integration": "Owner review will use this file as the primary integration point.",
                "tests": [
                    "py_compile for touched Python files",
                    "restart CORE service after review-approved implementation",
                    "confirm operator endpoint or command output still renders",
                ],
                "risk": "medium" if path == "core_main.py" else "low",
                "rollback": "Revert the bounded file patch and rerun syntax checks.",
            }
        )
    return {
        "goal": title,
        "summary": f"Code planning packet for {title}",
        "decision": "route to owner review",
        "change_type": change_type,
        "work_track": work_track,
        "owner_review": "Required before any implementation.",
        "files": files,
        "verification": [
            "proposal exists in evolution_queue",
            "task row marked done only after verification",
        ],
        "rollback": [
            "Delete or reject the proposal row",
            "Restore the prior task state if implementation later fails",
        ],
        "notes": [
            f"Task: {description[:240]}",
            f"Memory: {_memory_excerpt(memory_packet)}",
        ],
    }


def _merge_file_contexts(packet: dict, file_contexts: list[dict], work_track: str) -> dict:
    if not isinstance(packet, dict):
        packet = {}
    files = packet.get("files") or []
    if not isinstance(files, list):
        files = []

    index = {ctx.get("path"): ctx for ctx in file_contexts or [] if ctx.get("path")}
    normalized = []
    for idx, file_entry in enumerate(files):
        if not isinstance(file_entry, dict):
            continue
        merged = dict(file_entry)
        ctx = index.get(merged.get("path"))
        if ctx:
            anchor = ctx.get("anchor") or {}
            line = anchor.get("line") or 0
            if not merged.get("line") or str(merged.get("line")).upper().startswith("TBD"):
                merged["line"] = int(line or 0)
            if not merged.get("anchor") or str(merged.get("anchor")).upper().startswith("TBD"):
                merged["anchor"] = anchor.get("anchor") or ""
            if not merged.get("why"):
                merged["why"] = f"Primary integration point for {work_track} in {merged.get('path')}"
            if not merged.get("change"):
                merged["change"] = "Apply the smallest bounded patch that satisfies the owner-reviewed plan."
            if not merged.get("integration"):
                merged["integration"] = "Integrate through the existing control plane and verify the owning route."
            if not merged.get("tests"):
                merged["tests"] = [
                    "py_compile for touched Python files",
                    "restart CORE service after implementation",
                    "confirm the related command/operator route still works",
                ]
            if not merged.get("risk"):
                merged["risk"] = "medium" if merged.get("path") == "core_main.py" else "low"
            if not merged.get("rollback"):
                merged["rollback"] = "Revert the bounded patch and rerun syntax checks."
        normalized.append(merged)

    if not normalized:
        normalized = _fallback_code_packet(
            {"title": packet.get("goal") or packet.get("summary") or "code task", "description": "generated packet"},
            {"work_track": work_track},
            {"packet": {"top_hits": []}},
            file_contexts,
        ).get("files", [])

    packet["files"] = normalized
    if not packet.get("change_type") or packet.get("change_type") not in {"code", "new_tool"}:
        packet["change_type"] = _code_change_type(work_track)
    if not packet.get("summary"):
        packet["summary"] = f"Code planning packet for {packet.get('goal') or 'code task'}"
    if not packet.get("decision"):
        packet["decision"] = "route to owner review"
    if not packet.get("owner_review"):
        packet["owner_review"] = "Required before implementation."
    return packet


def _synthesize_code_packet(task: dict, strategy: dict, memory_packet: dict, file_contexts: list[dict]) -> dict:
    title = _safe_text(task.get("title") or "", 200)
    description = _safe_text(task.get("description") or "", 1200)
    work_track = _safe_text(strategy.get("work_track") or "code_patch", 40)
    needs_clarification = _needs_clarification(task)
    prompt = {
        "task_title": title,
        "task_description": description,
        "work_track": work_track,
        "needs_clarification": needs_clarification,
        "task_group": _safe_text(strategy.get("task_group") or "code", 40),
        "domain": _safe_text(strategy.get("domain") or strategy.get("artifact_domain") or "code", 80),
        "expected_artifact": _safe_text(strategy.get("expected_artifact") or "evolution_queue", 80),
        "verification": _safe_text(strategy.get("verification") or "proposal queued for owner review", 120),
        "memory_excerpt": _memory_excerpt(memory_packet),
        "file_contexts": file_contexts,
    }
    system = (
        "You are CORE's code autonomy planner.\n"
        "Do not write code. Do not invent file paths.\n"
        "Return ONLY valid JSON with precise, reviewable code change guidance.\n"
        "The JSON must include: goal, summary, decision, change_type, work_track, owner_review, files, "
        "verification, rollback, notes.\n"
        "Each file entry should include: path, line, anchor, why, change, integration, tests, risk, rollback.\n"
        "Prefer conservative plans with minimal bounded edits."
    )
    user = (
        f"PLAN INPUT:\n{json.dumps(prompt, default=str, indent=2)}\n\n"
        "Return JSON only. If the task is too vague, produce a proposal-only plan that asks for owner review."
    )
    try:
        raw = groq_chat(system=system, user=user, model=GROQ_MODEL, max_tokens=1100)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("non-dict result")
        result.setdefault("work_track", work_track)
        result.setdefault("verification", ["proposal exists in evolution_queue"])
        result.setdefault("rollback", ["Reject the proposal row and restore prior state."])
        result.setdefault("files", [])
        if needs_clarification:
            result["decision"] = "request clarification"
            result["owner_review"] = "Required before implementation."
            result.setdefault("change_type", "proposal_only")
            result.setdefault("work_track", "proposal_only")
            result.setdefault("clarifying_questions", [
                "Which file or module should be changed?",
                "What exact behavior should change after the patch?",
                "What tests or acceptance criteria should pass?",
            ])
            result.setdefault("verification", ["owner confirms implementation scope and acceptance criteria"])
        return _merge_file_contexts(result, file_contexts, work_track)
    except Exception:
        return _merge_file_contexts(_fallback_code_packet(task, strategy, memory_packet, file_contexts), file_contexts, work_track)


def _proposal_exists(task_id: str) -> dict:
    rows = sb_get(
        "evolution_queue",
        f"select=id,status,change_type,change_summary,pattern_key,source,created_at&pattern_key=eq.code:{task_id}&limit=1",
        svc=True,
    ) or []
    return rows[0] if rows else {}


def _needs_clarification(task: dict) -> bool:
    title = _safe_text(task.get("title") or "", 220).lower()
    description = _safe_text(task.get("description") or "", 1200).lower()
    combined = f"{title} {description}"
    vague_markers = [
        "too vague",
        "need more information",
        "request further information",
        "unclear",
        "not enough context",
        "clarify",
        "owner review",
    ]
    if any(marker in combined for marker in vague_markers):
        return True
    has_anchor = any(token in combined for token in [
        "core_tools.py", "core_train.py", "core_main.py", "core_",
        ".py", "line", "anchor", "file", "module", "function",
    ])
    return not has_anchor or len(combined.strip()) < 80


def _init_agentic_session(task_id: str, claim_id: str, title: str, strategy: dict) -> None:
    try:
        t_agent_session_init(session_id=claim_id, goal=title, chat_id="")
        t_agent_state_set(session_id=claim_id, key="task_id", value=task_id)
        t_agent_state_set(session_id=claim_id, key="task_title", value=title)
        t_agent_state_set(session_id=claim_id, key="task_strategy", value=json.dumps(strategy, default=str))
        t_agent_step_done(session_id=claim_id, step_name="claim", result="agentic session initialized")
    except Exception as e:
        print(f"[CODE_AUTONOMY] agentic init failed: {e}")


def _notify_task_event(stage: str, task_id: str, title: str, claim_id: str, strategy: dict, detail: str = "") -> None:
    if not AUTONOMY_NOTIFY:
        return
    kind = _safe_text(strategy.get("kind"), 80)
    work_track = _safe_text(strategy.get("work_track") or "proposal_only", 40)
    execution_mode = _safe_text(strategy.get("execution_mode") or "proposal", 40)
    source = _safe_text(strategy.get("origin") or strategy.get("source") or "self_assigned", 40)
    worker = _safe_text(strategy.get("specialized_worker") or strategy.get("route") or "proposal_router", 40)
    stage_label = {
        "claimed": "CLAIMED",
        "plan": "PLANNED",
        "proposed": "PROPOSED",
        "verified": "VERIFIED",
        "completed": "COMPLETED",
        "failed": "FAILED",
    }.get(stage, stage.upper())
    message = (
        f"<b>CODE AUTONOMY</b>\n"
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
        message += f"Detail: {detail[:900]}\n"
    try:
        notify(message)
    except Exception as e:
        print(f"[CODE_AUTONOMY] notify failed: {e}")


def _notify_cycle(summary: dict) -> None:
    if not AUTONOMY_NOTIFY:
        return
    processed = summary.get("processed", 0)
    proposed = summary.get("proposed", 0)
    duplicates = summary.get("duplicates", 0)
    failures = summary.get("failures", 0)
    deferred = summary.get("deferred", 0)
    track_counts = summary.get("track_counts") or {}
    parts = [
        "<b>CODE AUTONOMY CYCLE</b>",
        f"Window: {summary.get('started_at', '?')} -> {summary.get('finished_at', '?')}",
        f"Processed: {processed} | Proposed: {proposed} | Duplicates: {duplicates} | Deferred: {deferred} | Failures: {failures}",
        f"Pending code tasks: {summary.get('pending_now', '?')} | Pending review proposals: {summary.get('pending_proposals_now', '?')}",
    ]
    if track_counts:
        parts.append("Tracks: " + ", ".join(f"{k}={v}" for k, v in sorted(track_counts.items())))
    details = summary.get("details") or []
    for item in details[:5]:
        outcome = item.get("outcome") or ("created" if item.get("proposal_created") else "duplicate")
        label = _safe_text(item.get("task_title") or item.get("summary") or "", 180)
        parts.append(
            f"- #{item.get('task_id')} [{item.get('work_track') or 'unknown'}] {outcome}: {label}"
        )
        reason = _safe_text(item.get("reason") or item.get("error") or "", 220)
        if reason:
            parts.append(f"  reason: {reason}")
    try:
        notify("\n".join(parts))
    except Exception as e:
        print(f"[CODE_AUTONOMY] cycle notify failed: {e}")


def _build_review_payload(task: dict, task_id: str, strategy: dict, memory_packet: dict, file_contexts: list[dict]) -> dict:
    title = _safe_text(task.get("title") or "", 200)
    description = _safe_text(task.get("description") or "", 1200)
    work_track = _safe_text(strategy.get("work_track") or "code_patch", 40)
    change_type = _code_change_type(work_track)
    code_packet = _synthesize_code_packet(task, strategy, memory_packet, file_contexts)
    impact = "high" if work_track == "new_module" else "medium"
    recommendation = (
        "Review the code packet with the owner before implementation. "
        "If approved, convert this plan into a bounded patch set."
    )
    return {
        "change_type": change_type,
        "change_summary": code_packet.get("summary") or f"Code plan for {title}",
        "impact": impact,
        "recommendation": recommendation,
        "confidence": 0.84 if work_track == "code_patch" else 0.78,
        "pattern_key": f"code:{task_id}",
        "code_packet": code_packet,
        "description": description,
    }


def process_code_row(task_row: dict) -> dict:
    task_id = str(task_row.get("id") or "")
    task = _parse_task_blob(task_row)
    title = _safe_text(task.get("title"), 200)
    description = _safe_text(task.get("description"), 1200)
    claim_id = str(uuid.uuid4())
    strategy = _task_strategy(task, title, description, source=_safe_text(task_row.get("source"), 80))
    work_track = _safe_text(strategy.get("work_track") or "proposal_only", 40)
    if work_track not in CODE_TRACKS:
        deferred_to = "integration_autonomy" if work_track == "integration" else "task_autonomy"
        return {
            "ok": True,
            "task_id": task_id,
            "title": title,
            "claim_id": claim_id,
            "strategy": strategy,
            "blocked": True,
            "deferred": True,
            "deferred_to": deferred_to,
            "summary": f"Non-code task deferred to {deferred_to}",
        }

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
        _notify_task_event("failed", task_id, title, claim_id, strategy, detail="failed_to_claim")
        return {"ok": False, "task_id": task_id, "title": title, "error": "failed_to_claim"}

    _state["last_claimed_task_id"] = task_id
    _init_agentic_session(task_id, claim_id, title, strategy)
    _notify_task_event("claimed", task_id, title, claim_id, strategy, detail="claim accepted")

    event_context = {
        "source": "core_autonomy",
        "source_domain": "core",
        "source_branch": "code_autonomy",
        "source_service": "core-agi",
        "event_type": "code_autonomy",
        "trace_id": f"code:{task_id}:{claim_id}",
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
        "output_text": f"Code plan task claimed: {title}",
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
        _patch_task(task_id, {
            "status": "failed",
            "error": fail["summary"],
            "result": json.dumps(fail, default=str)[:4000],
        })
        _notify_task_event("failed", task_id, title, claim_id, strategy, detail=json.dumps(fail, default=str))
        return fail

    event_id = ingress["event_id"]
    note_reflection_stage(event_id, "critic", source="core_autonomy", status="done", payload={
        "title": title,
        "strategy": strategy,
        "claim_id": claim_id,
    })
    t_agent_step_done(session_id=claim_id, step_name="critic", result=json.dumps(strategy, default=str))
    file_contexts = _candidate_file_contexts(task, strategy)
    t_agent_state_set(session_id=claim_id, key="task_work_track", value=work_track)
    t_agent_state_set(session_id=claim_id, key="task_execution_mode", value=_safe_text(strategy.get("execution_mode") or "plan", 40))
    t_agent_state_set(session_id=claim_id, key="candidate_files", value=json.dumps(file_contexts, default=str)[:3500])
    _notify_task_event(
        "plan",
        task_id,
        title,
        claim_id,
        strategy,
        detail=json.dumps({"candidate_files": file_contexts[:4]}, default=str),
    )

    memory_packet = _memory_context(query=f"{title}\n{description}", domain=strategy.get("domain") or "code")
    review_payload = _build_review_payload(task, task_id, strategy, memory_packet, file_contexts)
    packet = review_payload["code_packet"]
    note_reflection_stage(event_id, "causal", source="core_autonomy", status="done", payload={
        "review_payload": review_payload,
    })

    diff_content = {
        "task_id": task_id,
        "claim_id": claim_id,
        "title": title,
        "description": description,
        "strategy": strategy,
        "source": "code_autonomy",
        "generated_at": _utcnow(),
        "review_payload": review_payload,
        "code_packet": packet,
        "autonomy": {
            "kind": "architecture_proposal",
            "origin": "code_autonomy",
            "source": "code_autonomy",
            "task_id": task_id,
            "claim_id": claim_id,
            "work_track": work_track,
            "execution_mode": "proposal",
            "review_scope": _safe_text(strategy.get("review_scope") or "owner", 40),
            "owner_only": bool(strategy.get("owner_only", True)),
            "verification": "evolution_queue row exists and is visible to proposal_router",
            "route": "proposal_router",
            "specialized_worker": "proposal_router",
            "expected_artifact": "evolution_queue",
            "task_group": "code",
        },
    }
    pattern_key = f"code:{task_id}"
    existing = _proposal_exists(task_id)
    proposal_created = False
    proposal_id = ""
    if existing:
        proposal_id = str(existing.get("id") or "")
        outcome = "duplicate"
        reason = "proposal already exists"
    else:
        proposal_created = bool(sb_post(
            "evolution_queue",
            {
                "change_type": review_payload["change_type"],
                "change_summary": review_payload["change_summary"],
                "diff_content": json.dumps(diff_content, default=str),
                "pattern_key": pattern_key,
                "confidence": review_payload["confidence"],
                "status": "pending",
                "source": "code_autonomy",
                "impact": review_payload["impact"],
                "recommendation": review_payload["recommendation"],
                "approval_tier": "owner_review",
                "created_at": _utcnow(),
            },
        ))
        if proposal_created:
            proposal_rows = sb_get(
                "evolution_queue",
                f"select=id&pattern_key=eq.{pattern_key}&source=eq.code_autonomy&limit=1",
                svc=True,
            ) or []
            if proposal_rows:
                proposal_id = str(proposal_rows[0].get("id") or "")
        outcome = "created" if proposal_created else "error"
        reason = "" if proposal_created else "failed to queue proposal"

    note_reflection_stage(event_id, "reflect", source="core_autonomy", status="done" if proposal_created or existing else "failed", payload={
        "proposal_created": proposal_created,
        "proposal_id": proposal_id,
        "pattern_key": pattern_key,
    }, error=None if (proposal_created or existing) else "proposal_queue_failed")
    t_agent_step_done(session_id=claim_id, step_name="execute", result=json.dumps({"proposal_created": proposal_created, "proposal_id": proposal_id}, default=str))

    execution = {
        "proposal_created": proposal_created or bool(existing),
        "proposal_id": proposal_id,
        "pattern_key": pattern_key,
        "artifact_type": "evolution_queue",
        "verification": {
            "rows": 1 if proposal_created or existing else 0,
            "pattern_key": pattern_key,
            "proposal_exists": bool(existing),
        },
        "summary": packet.get("summary") or f"Code proposal prepared for {title}",
        "blocked": False if (proposal_created or existing) else True,
        "reason": reason,
        "code_packet": packet,
        "review_payload": review_payload,
    }

    status = "done" if (proposal_created or existing) else "failed"
    task_result = {
        "task_id": task_id,
        "title": title,
        "strategy": strategy,
        "execution": execution,
        "claim_id": claim_id,
        "event_id": event_id,
        "completed_at": _utcnow(),
    }
    task_ok = _patch_task(task_id, {
        "status": status,
        "result": json.dumps(task_result, default=str)[:4000],
        "error": None if status == "done" else _safe_text(reason or "code planning failed", 500),
        "checkpoint": {
            "claim_id": claim_id,
            "phase": "complete" if status == "done" else "blocked",
            "title": title,
            "strategy": strategy,
            "execution": execution,
            "event_id": event_id,
            "ts": _utcnow(),
        },
        "checkpoint_at": _utcnow(),
        "checkpoint_draft": json.dumps(task_result, default=str)[:4000],
        "next_step": "owner_review",
    })
    if task_ok:
        try:
            verify = sb_get(
                "task_queue",
                f"select=id,status,next_step,updated_at&id=eq.{task_id}&limit=1",
                svc=True,
            ) or []
            if verify and str(verify[0].get("status") or "") != status:
                task_ok = False
        except Exception:
            pass

    if status == "done" and task_ok:
        note_reflection_stage(event_id, "meta", source="core_autonomy", status="done", payload={
            "task_status": status,
            "proposal_id": proposal_id,
        })
        finalize_reflection_event(event_id, status="complete", current_stage="meta", current_stage_status="done")
        t_agent_step_done(session_id=claim_id, step_name="verify", result="proposal queued for owner review")
        t_agent_step_done(session_id=claim_id, step_name="complete", result="task marked done")
    else:
        note_reflection_stage(event_id, "meta", source="core_autonomy", status="failed", payload={
            "task_status": status,
            "proposal_id": proposal_id,
        }, error=reason or "proposal queue failed")
        finalize_reflection_event(event_id, status="error", current_stage="meta", current_stage_status="failed", last_error=reason or "proposal queue failed")
        t_agent_step_done(session_id=claim_id, step_name="verify", result="failed")
        t_agent_step_done(session_id=claim_id, step_name="complete", result="finalization_failed")
        status = "failed"

    _notify_task_event(
        "completed" if status == "done" and task_ok else "failed",
        task_id,
        title,
        claim_id,
        strategy,
        detail=json.dumps({
            "status": status if task_ok else "failed",
            "proposal_id": proposal_id,
            "pattern_key": pattern_key,
            "summary": execution.get("summary"),
        }, default=str),
    )

    return {
        "ok": status == "done" and task_ok,
        "task_id": task_id,
        "title": title,
        "claim_id": claim_id,
        "event_id": event_id,
        "strategy": strategy,
        "execution": execution,
        "final": task_result,
        "artifact_type": execution.get("artifact_type"),
        "blocked": bool(execution.get("blocked")) or not task_ok,
        "proposal_id": proposal_id,
        "proposal_created": proposal_created or bool(existing),
        "outcome": "duplicate" if existing else "created" if proposal_created else "error",
        "work_track": work_track,
    }


def run_code_autonomy_cycle(max_tasks: int = AUTONOMY_BATCH_LIMIT) -> dict:
    if not AUTONOMY_ENABLED:
        return {"ok": False, "enabled": False, "message": "CORE_CODE_AUTONOMY_ENABLED=false"}

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
            "proposed": 0,
            "duplicates": 0,
            "deferred": 0,
            "pending_now": 0,
            "pending_proposals_now": 0,
            "details": [],
            "track_counts": {},
            "note": "No-op cycle requested (max_tasks <= 0).",
        }

    with _lock:
        if _state["running"]:
            return {"ok": False, "busy": True, "message": "code autonomy cycle already running"}
        _state["running"] = True

    started_at = _utcnow()
    details: list[dict] = []
    proposed = 0
    duplicates = 0
    deferred = 0
    failures = 0
    errors: list[dict] = []
    track_counts: dict[str, int] = {}
    try:
        cursor = _state.get("queue_cursor") or {}
        page_limit = max(100, min(max_tasks * 80, QUEUE_PAGE_LIMIT))
        attempted = 0
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
                try:
                    if claimed >= max_tasks:
                        break
                    inspected += 1
                    cursor = cursor_from_row(row, QUEUE_ORDER)
                    _state["queue_cursor"] = cursor
                    result = process_code_row(row)
                    details.append(result)
                    attempted += 1
                    track = _safe_text((result.get("strategy") or {}).get("work_track") or result.get("work_track") or "unknown", 40)
                    track_counts[track] = track_counts.get(track, 0) + 1
                    if result.get("deferred"):
                        deferred += 1
                    elif result.get("proposal_created"):
                        proposed += 1
                        claimed += 1
                    elif result.get("outcome") == "duplicate":
                        duplicates += 1
                    else:
                        failures += 1
                except Exception as e:
                    errors.append({"task_id": row.get("id"), "error": str(e)})
                    failures += 1
            if claimed >= max_tasks:
                break
            if len(rows) < page_limit:
                cursor = {}
                _state["queue_cursor"] = {}
                break
        pending_now = _count_rows("task_queue", "select=id&status=eq.pending")
        pending_props = _count_rows("evolution_queue", "select=id&status=eq.pending&source=eq.code_autonomy")
        summary = {
            "started_at": started_at,
            "finished_at": _utcnow(),
            "processed": attempted,
            "inspected": inspected,
            "proposed": proposed,
            "duplicates": duplicates,
            "deferred": deferred,
            "failures": failures,
            "pending_now": pending_now,
            "pending_proposals_now": pending_props,
            "track_counts": track_counts,
            "details": details,
            "errors": errors,
            "queue_cursor": _state.get("queue_cursor") or {},
        }
        _state["last_run_at"] = summary["finished_at"]
        _state["last_summary"] = summary
        _state["last_error"] = ""
        try:
            sb_post("sessions", {
                "summary": f"[state_update] code_autonomy_last_run: {_state['last_run_at']}",
                "actions": [
                    f"code_autonomy cycle processed={len(rows)} proposed={proposed} duplicates={duplicates} deferred={deferred} failures={failures}",
                ],
                "interface": "code_autonomy",
            })
        except Exception:
            pass
        _notify_cycle(summary)
        return {"ok": True, "enabled": True, **summary}
    except Exception as e:
        _state["last_error"] = str(e)
        return {"ok": False, "enabled": True, "error": str(e), "details": details, "errors": errors}
    finally:
        with _lock:
            _state["running"] = False


def code_autonomy_loop() -> None:
    while AUTONOMY_ENABLED:
        try:
            cycle = run_code_autonomy_cycle(max_tasks=AUTONOMY_BATCH_LIMIT)
            if not cycle.get("ok") and cycle.get("busy"):
                time.sleep(min(60, AUTONOMY_INTERVAL_S))
            else:
                time.sleep(AUTONOMY_INTERVAL_S)
        except Exception as e:
            _state["last_error"] = str(e)
            time.sleep(min(120, AUTONOMY_INTERVAL_S))


def code_autonomy_status() -> dict:
    pending = len(_code_pending_rows(limit=500))
    pending_props = _count_rows("evolution_queue", "select=id&status=eq.pending&source=eq.code_autonomy")
    return {
        "ok": True,
        "enabled": AUTONOMY_ENABLED,
        "running": _state["running"],
        "interval_seconds": AUTONOMY_INTERVAL_S,
        "batch_limit": AUTONOMY_BATCH_LIMIT,
        "last_run_at": _state["last_run_at"],
        "last_error": _state["last_error"],
        "pending_code_tasks": pending,
        "pending_review_proposals": pending_props,
        "track_counts": _state["last_summary"].get("track_counts", {}),
        "last_summary": _state["last_summary"],
        "deferred": _state["last_summary"].get("deferred", 0),
        "queue_cursor": _state.get("queue_cursor") or {},
    }


def render_code_status_report(status: dict | None = None) -> str:
    status = status or code_autonomy_status()
    last = status.get("last_summary") or {}
    lines = [
        f"Status: <b>{'enabled' if status.get('enabled') else 'disabled'}</b> | running={status.get('running')} | interval={status.get('interval_seconds')}s | batch={status.get('batch_limit')}",
        f"Queue: pending code tasks {status.get('pending_code_tasks', 0)} | pending review proposals {status.get('pending_review_proposals', 0)}",
    ]
    if status.get("track_counts"):
        lines.append("Tracks: " + ", ".join(f"{_escape(k)}={v}" for k, v in sorted(status.get("track_counts", {}).items())))
    if last.get("last_run_at"):
        lines.append(f"Last run: {_escape(last.get('finished_at') or status.get('last_run_at'), 40)}")
    details = last.get("details") or []
    if details:
        lines.append("")
        lines.append("<b>Recent cycle</b>")
        for item in details[:3]:
            outcome = item.get("outcome") or ("created" if item.get("proposal_created") else "duplicate")
            lines.append(
                f"- #{item.get('task_id')} [{_escape(item.get('strategy', {}).get('work_track') or item.get('work_track') or 'unknown', 20)}] {outcome} "
                f"({_escape(item.get('artifact_type') or 'evolution_queue', 40)})"
            )
            reason = item.get("reason") or item.get("error")
            if reason:
                lines.append(f"  reason: {_escape(reason, 180)}")
    return "\n".join(["<b>Code Autonomy</b>"] + lines)


def code_autonomy_summary() -> dict:
    status = code_autonomy_status()
    return {
        "ok": True,
        "enabled": status.get("enabled", True),
        "pending_code_tasks": status.get("pending_code_tasks", 0),
        "pending_review_proposals": status.get("pending_review_proposals", 0),
        "track_counts": status.get("track_counts", {}),
        "dashboard": render_code_status_report(status),
        "last_run_at": status.get("last_run_at", ""),
        "error": status.get("last_error", ""),
    }


def register_tools() -> None:
    try:
        from core_tools import TOOLS
    except Exception:
        return

    if "code_autonomy_status" not in TOOLS:
        TOOLS["code_autonomy_status"] = {
            "fn": lambda: code_autonomy_summary(),
            "desc": "Return the code autonomy dashboard, pending code tasks, and review proposal backlog.",
            "args": [],
        }
    if "code_autonomy_run" not in TOOLS:
        TOOLS["code_autonomy_run"] = {
            "fn": lambda max_tasks="1": run_code_autonomy_cycle(max_tasks=int(max_tasks or 1)),
            "desc": "Run one code autonomy cycle to convert code-class tasks into review-ready proposals.",
            "args": [
                {"name": "max_tasks", "type": "string", "description": "Maximum code tasks to process (default 1)."},
            ],
        }


register_tools()





