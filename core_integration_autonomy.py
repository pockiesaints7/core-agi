"""core_integration_autonomy.py -- specialized integration worker for CORE.

This worker handles wiring-class work: endpoint surface changes, module
plumbing, and cross-repo contracts between core-agi and core-trading-bot.
It does not auto-edit code. It:

- claims integration-class tasks;
- builds a detailed integration review packet from CORE memory and repo context;
- writes the proposal to evolution_queue for owner review;
- leaves a durable audit trail on the task row and reflection ledger.
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
from core_config import _env_int, sb_get, sb_patch, sb_post, groq_chat, GROQ_MODEL
from core_github import notify
from core_queue_cursor import build_seek_filter, cursor_from_row
from core_reflection_audit import finalize_reflection_event, note_reflection_stage, register_reflection_event
from core_tools import t_agent_session_init, t_agent_state_set, t_agent_step_done, t_reasoning_packet
from core_work_taxonomy import build_autonomy_contract

AUTONOMY_ENABLED = os.getenv("CORE_INTEGRATION_AUTONOMY_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off"
}
AUTONOMY_INTERVAL_S = max(300, _env_int("CORE_INTEGRATION_AUTONOMY_INTERVAL_S", "1200"))
AUTONOMY_BATCH_LIMIT = max(1, _env_int("CORE_INTEGRATION_AUTONOMY_BATCH_LIMIT", "1"))
AUTONOMY_NOTIFY = os.getenv("CORE_INTEGRATION_AUTONOMY_NOTIFY", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
TASK_SOURCES = tuple(
    s.strip()
    for s in os.getenv("CORE_INTEGRATION_TASK_SOURCES", "mcp_session,self_assigned,improvement").split(",")
    if s.strip()
)

_REPO_ROOT = Path(__file__).resolve().parent
_TRADING_REPO_ROOT = _REPO_ROOT.parent / "core-trading-bot"

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

_FILE_HINTS = [
    {"repo": "core-agi", "path": "core_main.py", "focus": ["telegram", "command", "review", "autonomy", "startup", "webhook", "deploy", "health", "operator"], "anchors": [
        "CORE_TELEGRAM_COMMANDS",
        "def handle_msg",
        "def _render_autonomy_overview_report",
        "def _render_review_dashboard",
        "def _render_health_report",
        "@app.on_event(\"startup\")",
        "def _deployment_manifest",
    ]},
    {"repo": "core-agi", "path": "core_github.py", "focus": ["telegram", "webhook", "menu", "notify", "commands"], "anchors": [
        "set_telegram_commands",
        "set_webhook",
        "notify",
    ]},
    {"repo": "core-agi", "path": "core_tools.py", "focus": ["tool", "mcp", "memory", "reasoning", "packet", "search"], "anchors": [
        "def t_reasoning_packet",
        "def t_search_memory",
        "def handle_jsonrpc",
    ]},
    {"repo": "core-agi", "path": "core_work_taxonomy.py", "focus": ["taxonomy", "track", "worker", "route", "verification", "contract"], "anchors": [
        "def build_autonomy_contract",
        "def _infer_work_track",
        "def _infer_execution_mode",
        "def _infer_verification",
        "def _infer_specialized_worker",
    ]},
    {"repo": "core-agi", "path": "core_proposal_router.py", "focus": ["proposal", "review", "reroute", "route", "approval"], "anchors": [
        "def queue_review_reroute",
        "def proposal_router_status",
        "def render_proposal_router_dashboard",
    ]},
    {"repo": "core-agi", "path": "core_code_autonomy.py", "focus": ["code", "proposal", "packet", "review"], "anchors": [
        "def process_code_row",
        "def run_code_autonomy_cycle",
        "def render_code_status_report",
    ]},
    {"repo": "core-agi", "path": "core_research_autonomy.py", "focus": ["research", "knowledge", "follow-up", "validate"], "anchors": [
        "def _synthesize_research",
        "def _queue_follow_up",
        "def research_autonomy_status",
    ]},
    {"repo": "core-trading-bot", "path": "trading_bot.py", "focus": ["telegram", "status", "health", "command", "operator", "review", "webhook", "decision", "execution"], "anchors": [
        "def handle_command",
        "def status",
        "def health",
        "def deployment_check",
        "def operator_breaker",
        "def operator_resume",
        "def _evaluate_live_rollout_guard",
    ]},
    {"repo": "core-trading-bot", "path": "core_bridge.py", "focus": ["bridge", "core", "reflection", "contract", "trade"], "anchors": [
        "def fire_trading",
        "def get_trading_rules",
        "def build_core_context",
        "def push_strategy_promotion",
    ]},
    {"repo": "core-trading-bot", "path": "executor.py", "focus": ["open", "close", "decision", "market", "guard", "contract"], "anchors": [
        "def _check_market_gate",
        "def _validate_order_size",
        "def open_directional_trade",
        "def close_directional_trade",
        "def check_directional_exits",
    ]},
    {"repo": "core-trading-bot", "path": "brain.py", "focus": ["reasoning", "packet", "decision", "market", "signal", "contract"], "anchors": [
        "def _build_reasoning_packet",
        "def make_decision",
        "def _store_decision",
    ]},
    {"repo": "core-trading-bot", "path": "graduation.py", "focus": ["graduate", "status", "report", "performance"], "anchors": [
        "def calculate_performance",
        "def format_status_message",
        "def format_full_report_message",
        "def check_graduation",
    ]},
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
    return build_autonomy_contract(title, description, source=source, autonomy=autonomy, context="integration_autonomy")


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


def _integration_rows(limit: int = 1) -> list[dict]:
    rows = _task_rows(limit=max(limit, 250))
    picked = []
    for row in rows:
        task = _parse_task_blob(row)
        strategy = _task_strategy(task, task.get("title") or "", task.get("description") or "", source=row.get("source") or "")
        if strategy.get("work_track") == "integration":
            picked.append(row)
        if len(picked) >= limit:
            break
    return picked


def _integration_pending_rows(limit: int = 500) -> list[dict]:
    rows = _task_rows(limit=max(1, min(limit, 500)))
    picked = []
    for row in rows:
        task = _parse_task_blob(row)
        strategy = _task_strategy(task, task.get("title") or "", task.get("description") or "", source=row.get("source") or "")
        if strategy.get("work_track") == "integration":
            picked.append(row)
    return picked


def _count_rows(table: str, qs: str) -> int:
    try:
        rows = sb_get(table, qs, svc=True) or []
        return len(rows)
    except Exception:
        return 0


def _repo_root(repo: str) -> Path:
    return _TRADING_REPO_ROOT if repo == "core-trading-bot" else _REPO_ROOT


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
                return {"line": idx, "anchor": anchor, "snippet": "\n".join(lines[start - 1 : end])}
    return {"line": 0, "anchor": "", "snippet": ""}


def _task_terms(text: str) -> list[str]:
    raw = re.findall(r"[a-zA-Z][a-zA-Z0-9_+.-]{2,}", text.lower())
    stop = {
        "the", "and", "for", "with", "from", "this", "that", "into", "when", "what",
        "need", "make", "more", "have", "will", "only", "also", "about", "your",
        "code", "worker", "task", "propose", "proposal", "review", "update", "change",
        "integration", "wire", "wiring", "connected",
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
    important = {"core_main.py", "core_work_taxonomy.py", "trading_bot.py", "core_bridge.py"}
    contexts: list[dict] = []
    for spec in _FILE_HINTS:
        path = _repo_root(spec["repo"]) / spec["path"]
        if not path.exists():
            continue
        focus_hits = sum(1 for term in spec["focus"] if term in hay)
        anchor_hits = sum(1 for term in spec["anchors"] if term.lower().replace("def ", "") in hay)
        keyword_hits = sum(1 for term in terms if term in " ".join(spec["focus"] + spec["anchors"]).lower())
        score = focus_hits * 3 + anchor_hits * 2 + keyword_hits
        if score <= 0 and spec["path"] not in important:
            continue
        if score <= 0:
            score = 1
        contexts.append(
            {
                "repo": spec["repo"],
                "path": spec["path"],
                "full_path": str(path),
                "score": score,
                "focus": spec["focus"],
                "anchor": _line_anchor(_file_text(path), spec["anchors"]),
            }
        )
    contexts.sort(key=lambda item: (-int(item.get("score") or 0), item.get("repo") or "", item.get("path") or ""))
    return contexts[:6]


def _memory_context(query: str, domain: str = "") -> dict:
    try:
        return t_reasoning_packet(
            query=query,
            domain=domain or "integration",
            limit="8",
            per_table="2",
            tables="knowledge_base,mistakes,behavioral_rules,hot_reflections,output_reflections,evolution_queue,conversation_episodes,project_context",
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


def _integration_change_type() -> str:
    return "integration"


def _fallback_integration_packet(task: dict, strategy: dict, memory_packet: dict, file_contexts: list[dict]) -> dict:
    title = _safe_text(task.get("title") or "", 200)
    description = _safe_text(task.get("description") or "", 1200)
    files = []
    contracts = []
    for ctx in file_contexts[:4]:
        anchor = ctx.get("anchor") or {}
        line = int(anchor.get("line") or 0)
        repo = ctx.get("repo") or "core-agi"
        path = ctx.get("path")
        why = f"Primary integration point for {repo}:{path}"
        files.append(
            {
                "repo": repo,
                "path": path,
                "line": line,
                "anchor": anchor.get("anchor") or "",
                "why": why,
                "change": "Review the surrounding control flow and wire the smallest bounded contract needed.",
                "integration": "Tie the new wiring into the existing service, command, or cross-repo handoff.",
                "tests": [
                    "py_compile for touched Python files",
                    "restart the affected service after implementation",
                    "verify the command, endpoint, or integration route still responds",
                ],
                "risk": "high" if repo == "core-trading-bot" or path == "core_main.py" else "medium",
                "rollback": "Revert the bounded integration patch and rerun syntax checks.",
            }
        )
        contracts.append(
            {
                "name": f"{repo}:{path}",
                "source": why,
                "verification": "Contract preserved by runtime smoke test and owner review.",
            }
        )
    return {
        "goal": title,
        "summary": f"Integration planning packet for {title}",
        "decision": "route to owner review",
        "change_type": _integration_change_type(),
        "work_track": "integration",
        "owner_review": "Required before any implementation.",
        "files": files,
        "contracts": contracts,
        "verification": [
            "proposal exists in evolution_queue",
            "task row marked done only after verification",
            "affected endpoints and cross-repo contract points are explicitly reviewed",
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


def _merge_file_contexts(packet: dict, file_contexts: list[dict]) -> dict:
    if not isinstance(packet, dict):
        packet = {}
    files = packet.get("files") or []
    if not isinstance(files, list):
        files = []

    index = {(ctx.get("repo"), ctx.get("path")): ctx for ctx in file_contexts or [] if ctx.get("path")}
    normalized = []
    for file_entry in files:
        if not isinstance(file_entry, dict):
            continue
        merged = dict(file_entry)
        ctx = index.get((merged.get("repo"), merged.get("path")))
        if ctx:
            anchor = ctx.get("anchor") or {}
            line = anchor.get("line") or 0
            if not merged.get("line") or str(merged.get("line")).upper().startswith("TBD"):
                merged["line"] = int(line or 0)
            if not merged.get("anchor") or str(merged.get("anchor")).upper().startswith("TBD"):
                merged["anchor"] = anchor.get("anchor") or ""
            if not merged.get("why"):
                merged["why"] = f"Primary integration point for {ctx.get('repo')}:{merged.get('path')}"
            if not merged.get("change"):
                merged["change"] = "Apply the smallest bounded integration change that satisfies the owner-reviewed plan."
            if not merged.get("integration"):
                merged["integration"] = "Wire through the existing service boundary and verify the handoff end to end."
            if not merged.get("tests"):
                merged["tests"] = [
                    "py_compile for touched Python files",
                    "restart affected services after implementation",
                    "confirm the command, endpoint, or cross-repo route still works",
                ]
            if not merged.get("risk"):
                merged["risk"] = "high" if merged.get("repo") == "core-trading-bot" or merged.get("path") == "core_main.py" else "medium"
            if not merged.get("rollback"):
                merged["rollback"] = "Revert the bounded integration patch and rerun syntax checks."
        normalized.append(merged)

    if not normalized:
        normalized = _fallback_integration_packet(
            {"title": packet.get("goal") or packet.get("summary") or "integration task", "description": "generated packet"},
            {"work_track": "integration"},
            {"packet": {"top_hits": []}},
            file_contexts,
        ).get("files", [])

    packet["files"] = normalized
    packet["change_type"] = _integration_change_type()
    packet["work_track"] = "integration"
    if not isinstance(packet.get("goal"), str) or not packet.get("goal"):
        packet["goal"] = _safe_text(packet.get("goal") or packet.get("summary") or "integration task", 200)
    if not isinstance(packet.get("summary"), str) or not packet.get("summary"):
        packet["summary"] = f"Integration planning packet for {packet.get('goal') or 'integration task'}"
    if not isinstance(packet.get("decision"), str) or not packet.get("decision"):
        packet["decision"] = "route to owner review"
    if not isinstance(packet.get("owner_review"), str) or not packet.get("owner_review"):
        packet["owner_review"] = "Required before implementation."
    if not isinstance(packet.get("contracts"), list):
        packet["contracts"] = []
    if not isinstance(packet.get("verification"), list):
        packet["verification"] = ["proposal exists in evolution_queue"]
    if not isinstance(packet.get("rollback"), list):
        packet["rollback"] = ["Reject the proposal row and restore prior state."]
    packet["specialized_worker"] = "proposal_router"
    return packet


def _synthesize_integration_packet(task: dict, strategy: dict, memory_packet: dict, file_contexts: list[dict]) -> dict:
    title = _safe_text(task.get("title") or "", 200)
    description = _safe_text(task.get("description") or "", 1200)
    prompt = {
        "task_title": title,
        "task_description": description,
        "work_track": "integration",
        "task_group": _safe_text(strategy.get("task_group") or "integration", 40),
        "domain": _safe_text(strategy.get("domain") or strategy.get("artifact_domain") or "integration", 80),
        "expected_artifact": _safe_text(strategy.get("expected_artifact") or "evolution_queue", 80),
        "verification": _safe_text(strategy.get("verification") or "integration proposal queued for owner review", 120),
        "memory_excerpt": _memory_excerpt(memory_packet),
        "file_contexts": file_contexts,
        "instruction": (
            "Create an owner-review integration packet. "
            "Focus on endpoint wiring, module boundaries, and cross-repo contracts. "
            "When two repos interact, state the contract boundary explicitly."
        ),
    }
    system = (
        "You are CORE's integration autonomy planner.\n"
        "Do not write code. Do not invent file paths.\n"
        "Return ONLY valid JSON with precise, reviewable integration guidance.\n"
        "The JSON must include: goal, summary, decision, change_type, work_track, owner_review, files, contracts, "
        "verification, rollback, notes.\n"
        "Each file entry should include: repo, path, line, anchor, why, change, integration, tests, risk, rollback.\n"
        "Prefer conservative plans with minimal bounded edits and explicit cross-repo contracts."
    )
    user = f"PLAN INPUT:\n{json.dumps(prompt, default=str, indent=2)}\n\nReturn JSON only."
    try:
        raw = groq_chat(system=system, user=user, model=GROQ_MODEL, max_tokens=1200)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("non-dict result")
        return _merge_file_contexts(result, file_contexts)
    except Exception:
        return _merge_file_contexts(_fallback_integration_packet(task, strategy, memory_packet, file_contexts), file_contexts)


def _proposal_exists(task_id: str) -> dict:
    rows = sb_get(
        "evolution_queue",
        f"select=id,status,change_type,change_summary,pattern_key,source,created_at&pattern_key=eq.integration:{task_id}&limit=1",
        svc=True,
    ) or []
    return rows[0] if rows else {}


def _init_agentic_session(task_id: str, claim_id: str, title: str, strategy: dict) -> None:
    try:
        t_agent_session_init(session_id=claim_id, goal=title, chat_id="")
        t_agent_state_set(session_id=claim_id, key="task_id", value=task_id)
        t_agent_state_set(session_id=claim_id, key="task_title", value=title)
        t_agent_state_set(session_id=claim_id, key="task_strategy", value=json.dumps(strategy, default=str))
        t_agent_step_done(session_id=claim_id, step_name="claim", result="agentic session initialized")
    except Exception as e:
        print(f"[INTEGRATION_AUTONOMY] agentic init failed: {e}")


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
        f"<b>INTEGRATION AUTONOMY</b>\n"
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
        print(f"[INTEGRATION_AUTONOMY] notify failed: {e}")


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
        "<b>INTEGRATION AUTONOMY CYCLE</b>",
        f"Window: {summary.get('started_at', '?')} -> {summary.get('finished_at', '?')}",
        f"Processed: {processed} | Proposed: {proposed} | Duplicates: {duplicates} | Deferred: {deferred} | Failures: {failures}",
        f"Pending integration tasks: {summary.get('pending_now', '?')} | Pending review proposals: {summary.get('pending_proposals_now', '?')}",
    ]
    if track_counts:
        parts.append("Tracks: " + ", ".join(f"{k}={v}" for k, v in sorted(track_counts.items())))
    for item in (summary.get("details") or [])[:5]:
        outcome = item.get("outcome") or ("created" if item.get("proposal_created") else "duplicate")
        label = _safe_text(item.get("task_title") or item.get("summary") or "", 180)
        parts.append(f"- #{item.get('task_id')} [{item.get('work_track') or 'unknown'}] {outcome}: {label}")
        reason = _safe_text(item.get("reason") or item.get("error") or "", 220)
        if reason:
            parts.append(f"  reason: {reason}")
    try:
        notify("\n".join(parts))
    except Exception as e:
        print(f"[INTEGRATION_AUTONOMY] cycle notify failed: {e}")


def _build_review_payload(task: dict, task_id: str, strategy: dict, memory_packet: dict, file_contexts: list[dict]) -> dict:
    title = _safe_text(task.get("title") or "", 200)
    description = _safe_text(task.get("description") or "", 1200)
    packet = _synthesize_integration_packet(task, strategy, memory_packet, file_contexts)
    return {
        "change_type": _integration_change_type(),
        "change_summary": packet.get("summary") or f"Integration plan for {title}",
        "impact": "high",
        "recommendation": (
            "Review the integration packet with the owner before implementation. "
            "If approved, convert this plan into a bounded patch set."
        ),
        "confidence": 0.8,
        "pattern_key": f"integration:{task_id}",
        "integration_packet": packet,
        "description": description,
    }


def _claim_task(task_row: dict) -> dict:
    task_id = str(task_row.get("id") or "")
    task = _parse_task_blob(task_row)
    title = _safe_text(task.get("title"), 200)
    description = _safe_text(task.get("description"), 1200)
    claim_id = str(uuid.uuid4())
    strategy = _task_strategy(task, title, description, source=_safe_text(task_row.get("source"), 80))
    work_track = _safe_text(strategy.get("work_track") or "proposal_only", 40)
    if work_track != "integration":
        return {
            "ok": True,
            "task_id": task_id,
            "title": title,
            "claim_id": claim_id,
            "strategy": strategy,
            "blocked": True,
            "deferred": True,
            "deferred_to": _safe_text(strategy.get("specialized_worker") or "proposal_router", 40) or "proposal_router",
            "summary": "Non-integration task deferred",
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
    if not sb_patch("task_queue", f"id=eq.{task_id}", claim_patch):
        _notify_task_event("failed", task_id, title, claim_id, strategy, detail="failed_to_claim")
        return {"ok": False, "task_id": task_id, "title": title, "error": "failed_to_claim"}

    _state["last_claimed_task_id"] = task_id
    _init_agentic_session(task_id, claim_id, title, strategy)
    _notify_task_event("claimed", task_id, title, claim_id, strategy, detail="claim accepted")

    event_context = {
        "source": "core_autonomy",
        "source_domain": "core",
        "source_branch": "integration_autonomy",
        "source_service": "core-agi",
        "event_type": "integration_autonomy",
        "trace_id": f"integration:{task_id}:{claim_id}",
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
        "output_text": f"Integration plan task claimed: {title}",
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
        sb_patch("task_queue", f"id=eq.{task_id}", {
            "status": "failed",
            "error": fail["summary"],
            "result": json.dumps(fail, default=str)[:4000],
            "updated_at": _utcnow(),
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
    _notify_task_event("plan", task_id, title, claim_id, strategy, detail=json.dumps({"candidate_files": file_contexts[:4]}, default=str))

    memory_packet = _memory_context(query=f"{title}\n{description}", domain="integration")
    review_payload = _build_review_payload(task, task_id, strategy, memory_packet, file_contexts)
    packet = review_payload["integration_packet"]
    note_reflection_stage(event_id, "causal", source="core_autonomy", status="done", payload={"review_payload": review_payload})

    diff_content = {
        "task_id": task_id,
        "claim_id": claim_id,
        "title": title,
        "description": description,
        "strategy": strategy,
        "source": "integration_autonomy",
        "generated_at": _utcnow(),
        "review_payload": review_payload,
        "integration_packet": packet,
        "autonomy": {
            "kind": "architecture_proposal",
            "origin": "integration_autonomy",
            "source": "integration_autonomy",
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
            "task_group": "integration",
        },
    }
    pattern_key = f"integration:{task_id}"
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
                "source": "integration_autonomy",
                "impact": review_payload["impact"],
                "recommendation": review_payload["recommendation"],
                "approval_tier": "owner_review",
                "created_at": _utcnow(),
            },
        ))
        if proposal_created:
            proposal_rows = sb_get(
                "evolution_queue",
                f"select=id&pattern_key=eq.{pattern_key}&source=eq.integration_autonomy&limit=1",
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
        "summary": packet.get("summary") or f"Integration proposal prepared for {title}",
        "blocked": False if (proposal_created or existing) else True,
        "reason": reason,
        "integration_packet": packet,
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
    task_ok = bool(sb_patch("task_queue", f"id=eq.{task_id}", {
        "status": status,
        "result": json.dumps(task_result, default=str)[:4000],
        "error": None if status == "done" else _safe_text(reason or "integration planning failed", 500),
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
        "updated_at": _utcnow(),
    }))
    if task_ok:
        try:
            verify = sb_get("task_queue", f"select=id,status,next_step,updated_at&id=eq.{task_id}&limit=1", svc=True) or []
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


def run_integration_autonomy_cycle(max_tasks: int = AUTONOMY_BATCH_LIMIT) -> dict:
    if not AUTONOMY_ENABLED:
        return {"ok": False, "enabled": False, "message": "CORE_INTEGRATION_AUTONOMY_ENABLED=false"}

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
            return {"ok": False, "busy": True, "message": "integration autonomy cycle already running"}
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
                    result = _claim_task(row)
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
        pending_props = _count_rows("evolution_queue", "select=id&status=eq.pending&source=eq.integration_autonomy")
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
                "summary": f"[state_update] integration_autonomy_last_run: {_state['last_run_at']}",
                "actions": [
                    f"integration_autonomy cycle processed={len(rows)} proposed={proposed} duplicates={duplicates} deferred={deferred} failures={failures}",
                ],
                "interface": "integration_autonomy",
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


def integration_autonomy_loop() -> None:
    while AUTONOMY_ENABLED:
        try:
            cycle = run_integration_autonomy_cycle(max_tasks=AUTONOMY_BATCH_LIMIT)
            if not cycle.get("ok") and cycle.get("busy"):
                time.sleep(min(60, AUTONOMY_INTERVAL_S))
            else:
                time.sleep(AUTONOMY_INTERVAL_S)
        except Exception as e:
            _state["last_error"] = str(e)
            time.sleep(min(120, AUTONOMY_INTERVAL_S))


def integration_autonomy_status() -> dict:
    pending = len(_integration_pending_rows(limit=500))
    pending_props = _count_rows("evolution_queue", "select=id&status=eq.pending&source=eq.integration_autonomy")
    return {
        "ok": True,
        "enabled": AUTONOMY_ENABLED,
        "running": _state["running"],
        "interval_seconds": AUTONOMY_INTERVAL_S,
        "batch_limit": AUTONOMY_BATCH_LIMIT,
        "sources": list(TASK_SOURCES),
        "last_run_at": _state["last_run_at"],
        "last_claimed_task_id": _state["last_claimed_task_id"],
        "last_error": _state["last_error"],
        "pending_integration_tasks": pending,
        "pending_review_proposals": pending_props,
        "track_counts": _state["last_summary"].get("track_counts", {}),
        "last_summary": _state["last_summary"],
        "deferred": _state["last_summary"].get("deferred", 0),
        "queue_cursor": _state.get("queue_cursor") or {},
    }


def render_integration_status_report(status: dict | None = None) -> str:
    status = status or integration_autonomy_status()
    last = status.get("last_summary") or {}
    lines = [
        f"Status: <b>{'enabled' if status.get('enabled') else 'disabled'}</b> | running={status.get('running')} | interval={status.get('interval_seconds')}s | batch={status.get('batch_limit')}",
        f"Sources: {_escape(', '.join(status.get('sources') or []), 120)}",
        f"Queue: pending integration tasks {status.get('pending_integration_tasks', 0)} | pending review proposals {status.get('pending_review_proposals', 0)}",
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
                f"- #{item.get('task_id')} [{'done' if item.get('proposal_created') else 'failed'}] "
                f"{_escape(item.get('title') or '', 90)} "
                f"(proposal={'ok' if item.get('proposal_created') else 'fail'}, follow-up={'yes' if item.get('deferred') else 'no'})"
            )
            reason = item.get("reason") or item.get("error")
            if reason:
                lines.append(f"  reason: {_escape(reason, 180)}")
    return "\n".join(["<b>Integration Autonomy</b>"] + lines)


def integration_autonomy_summary() -> dict:
    status = integration_autonomy_status()
    return {
        "ok": True,
        "enabled": status.get("enabled", True),
        "pending_integration_tasks": status.get("pending_integration_tasks", 0),
        "pending_review_proposals": status.get("pending_review_proposals", 0),
        "track_counts": status.get("track_counts", {}),
        "dashboard": render_integration_status_report(status),
        "last_run_at": status.get("last_run_at", ""),
        "error": status.get("last_error", ""),
    }


def register_tools() -> None:
    try:
        from core_tools import TOOLS
    except Exception:
        return

    if "integration_autonomy_status" not in TOOLS:
        TOOLS["integration_autonomy_status"] = {
            "fn": lambda: integration_autonomy_summary(),
            "desc": "Return the integration autonomy dashboard, pending integration tasks, and review proposal backlog.",
            "args": [],
        }
    if "integration_autonomy_run" not in TOOLS:
        TOOLS["integration_autonomy_run"] = {
            "fn": lambda max_tasks="1": run_integration_autonomy_cycle(max_tasks=int(max_tasks or 1)),
            "desc": "Run one integration autonomy cycle to convert integration-class tasks into review-ready proposals.",
            "args": [
                {"name": "max_tasks", "type": "string", "description": "Maximum integration tasks to process (default 1)."},
            ],
        }


register_tools()


