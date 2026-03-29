"""core_tools_task.py -- canonical task packets, tracking, and task error handling.

This module keeps task management logic out of the huge facade file while still
giving CORE one authoritative place to:
- read task state in a structured packet
- track ongoing task progress across checkpoints and sessions
- terminalize task failures with consistent error metadata
- verify that task rows landed in the expected state
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any

from core_config import sb_get, sb_patch


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _safe_text(value: Any, limit: int = 240) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _coerce_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_task(row: dict) -> dict:
    raw = row.get("task", "")
    if isinstance(raw, dict):
        task = dict(raw)
    else:
        try:
            task = json.loads(raw) if raw else {}
        except Exception:
            task = {"title": _safe_text(raw, 200), "description": ""}
    task.setdefault("title", _safe_text(task.get("title") or task.get("task_id") or raw, 200))
    task.setdefault("description", _safe_text(task.get("description") or "", 1000))
    return task


def _resolve_rows(task_id: str) -> list[dict]:
    import re as _re

    if not task_id:
        return []
    is_uuid = bool(_re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", task_id.lower()))
    cols = "id,task,status,result,checkpoint,checkpoint_draft,next_step,created_at,updated_at,priority,source,error"
    if is_uuid:
        rows = sb_get("task_queue", f"select={cols}&id=eq.{task_id}&limit=1", svc=True) or []
        if rows:
            return rows

    rows = sb_get(
        "task_queue",
        f"select={cols}&order=updated_at.desc&limit=500",
        svc=True,
    ) or []
    matches: list[dict] = []
    needle = task_id.strip().lower()
    for row in rows:
        task = _parse_task(row)
        blob = json.dumps(task, default=str).lower()
        if needle == str(row.get("id", "")).lower() or needle in blob:
            matches.append(row)
    return matches


@dataclass
class TaskPacket:
    task_id: str
    row_id: Any = ""
    title: str = ""
    current_status: str = ""
    expected_status: str = ""
    kind: str = "verification"
    phase: str = ""
    error: str = ""
    retryable: bool = False
    next_step: str = ""
    source: str = ""
    priority: Any = ""
    result_present: bool = False
    checkpoint_present: bool = False
    error_present: bool = False
    verification_score: float = 0.0
    blocked: bool = False
    passed_checks: list[str] | None = None
    failed_checks: list[str] | None = None
    warnings: list[str] | None = None
    summary: str = ""
    context_vector: dict | None = None
    principle_abstraction_score: float = 0.0
    principle_abstraction_history: list[dict] | None = None
    row: dict | None = None
    task: dict | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["passed_checks"] = list(self.passed_checks or [])
        data["failed_checks"] = list(self.failed_checks or [])
        data["warnings"] = list(self.warnings or [])
        return data


def _resolve_task_history(task_id: str, history_limit: int = 10) -> list[dict]:
    """Return recent session checkpoints linked to a task_id."""
    if not task_id:
        return []
    try:
        limit = max(1, min(int(history_limit or 10), 25))
    except Exception:
        limit = 10

    try:
        rows = sb_get(
            "sessions",
            "select=*&order=created_at.desc&limit=200",
            svc=True,
        ) or []
    except Exception:
        return []

    needle = str(task_id).strip().lower()
    matches: list[dict] = []
    for row in rows:
        checkpoint = row.get("checkpoint_data")
        if isinstance(checkpoint, str):
            try:
                checkpoint = json.loads(checkpoint)
            except Exception:
                checkpoint = {}
        if not isinstance(checkpoint, dict):
            checkpoint = {}

        blob = json.dumps(checkpoint, default=str).lower()
        active_task_id = str(checkpoint.get("active_task_id") or checkpoint.get("task_id") or "").strip().lower()
        resume_task_id = str(row.get("resume_task") or "").strip().lower()
        summary = str(row.get("summary") or "").strip().lower()

        if needle in {active_task_id, resume_task_id} or needle in blob or needle in summary:
            matches.append(row)

    return matches[:limit]


def _token_set(text: str) -> set[str]:
    return {part for part in re.split(r"[^A-Za-z0-9_]+", (text or "").lower()) if len(part) >= 3}


def _build_context_vector(task: dict, row: dict) -> dict:
    title = _safe_text(task.get("title") or "", 200)
    description = _safe_text(task.get("description") or "", 1000)
    status = _safe_text(row.get("status") or "", 40)
    checkpoint = _safe_text(row.get("checkpoint") or row.get("checkpoint_draft") or "", 1000)
    result = _safe_text(row.get("result") or "", 1000)
    combined = " ".join([title, description, status, checkpoint, result]).strip()
    tokens = sorted(_token_set(combined))
    abstract_tokens = {"principle", "causal", "abstract", "abstraction", "verify", "verification", "memory", "gating", "replay", "symbolic"}
    overlap = len(set(tokens) & abstract_tokens)
    token_count = len(tokens)
    return {
        "title_tokens": sorted(_token_set(title)),
        "description_tokens": sorted(_token_set(description)),
        "status_tokens": sorted(_token_set(status)),
        "checkpoint_tokens": sorted(_token_set(checkpoint)),
        "result_tokens": sorted(_token_set(result)),
        "signal_tokens": tokens[:24],
        "token_count": token_count,
        "abstract_overlap": overlap,
        "summary": f"context_vector: tokens={token_count} | abstract_overlap={overlap}",
    }


@dataclass
class TaskTrackingPacket:
    task_id: str
    row_id: Any = ""
    title: str = ""
    current_status: str = ""
    source: str = ""
    priority: Any = ""
    next_step: str = ""
    result_present: bool = False
    checkpoint_present: bool = False
    checkpoint_draft_present: bool = False
    error_present: bool = False
    session_matches: int = 0
    last_session_id: Any = ""
    last_checkpoint_ts: str = ""
    last_action: str = ""
    last_result: str = ""
    age_hours: float = 0.0
    tracking_state: str = ""
    stalled: bool = False
    tracking_score: float = 0.0
    passed_checks: list[str] | None = None
    failed_checks: list[str] | None = None
    warnings: list[str] | None = None
    summary: str = ""
    row: dict | None = None
    task: dict | None = None
    checkpoint_history: list[dict] | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["passed_checks"] = list(self.passed_checks or [])
        data["failed_checks"] = list(self.failed_checks or [])
        data["warnings"] = list(self.warnings or [])
        data["checkpoint_history"] = list(self.checkpoint_history or [])
        return data


@dataclass
class TaskVerificationBundle:
    task_id: str
    session_id: str = "default"
    kind: str = "integrated_verification"
    task_state: dict | None = None
    system_verification: dict | None = None
    action_verification: dict | None = None
    causal_mapping: dict | None = None
    causal_graph_packet: dict | None = None
    principle_search: dict | None = None
    search_tree: dict | None = None
    critic: dict | None = None
    verification_score: float = 0.0
    blocked: bool = False
    passed_checks: list[str] | None = None
    failed_checks: list[str] | None = None
    warnings: list[str] | None = None
    domain_invariant_features: dict | None = None
    summary: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["passed_checks"] = list(self.passed_checks or [])
        data["failed_checks"] = list(self.failed_checks or [])
        data["warnings"] = list(self.warnings or [])
        return data


def build_task_tracking_packet(
    task_id: str = "",
    include_history: str = "true",
    history_limit: str = "10",
) -> dict:
    if not task_id:
        return {"ok": False, "error": "task_id required"}

    try:
        rows = _resolve_rows(task_id)
        if not rows:
            packet = TaskTrackingPacket(
                task_id=task_id,
                tracking_state="not_found",
                stalled=True,
                tracking_score=0.0,
                failed_checks=["task_not_found"],
                warnings=[],
                summary=f"Task not found: {task_id}",
            )
            return {"ok": True, "blocked": True, "packet": packet.to_dict(), "summary": packet.summary}

        row = rows[0]
        task = _parse_task(row)
        current_status = str(row.get("status") or "")
        include_history_bool = _coerce_bool(include_history)
        try:
            limit = max(1, min(int(history_limit or 10), 25))
        except Exception:
            limit = 10

        history_rows = _resolve_task_history(task_id, history_limit=limit) if include_history_bool else []
        checkpoint_history: list[dict] = []
        for session_row in history_rows:
            checkpoint = session_row.get("checkpoint_data") or {}
            if isinstance(checkpoint, str):
                try:
                    checkpoint = json.loads(checkpoint)
                except Exception:
                    checkpoint = {}
            if not isinstance(checkpoint, dict):
                checkpoint = {}
            checkpoint_history.append({
                "session_id": session_row.get("id"),
                "created_at": session_row.get("created_at"),
                "checkpoint_ts": session_row.get("checkpoint_ts"),
                "active_task_id": checkpoint.get("active_task_id") or checkpoint.get("task_id"),
                "last_action": _safe_text(checkpoint.get("last_action") or "", 160),
                "last_result": _safe_text(checkpoint.get("last_result") or "", 240),
                "interface": session_row.get("interface"),
            })

        row_checkpoint = row.get("checkpoint") or row.get("checkpoint_draft")
        row_checkpoint_present = bool(row_checkpoint)
        current_checkpoint_present = row_checkpoint_present or bool(checkpoint_history)
        warnings: list[str] = []
        passed = ["task_found", f"current_status={current_status or 'unknown'}"]
        failed: list[str] = []

        if current_status == "in_progress" and not current_checkpoint_present:
            warnings.append("missing_checkpoint_history")
        if current_status == "in_progress":
            try:
                ts_str = row.get("updated_at") or row.get("created_at") or ""
                if ts_str:
                    ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    age_hours = max(0.0, round((now - ts).total_seconds() / 3600.0, 2))
                    if ts < now - timedelta(hours=24):
                        warnings.append("in_progress_stale>24h")
                else:
                    age_hours = 0.0
            except Exception:
                age_hours = 0.0
        else:
            try:
                ts_str = row.get("updated_at") or row.get("created_at") or ""
                if ts_str:
                    ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    age_hours = max(0.0, round((now - ts).total_seconds() / 3600.0, 2))
                else:
                    age_hours = 0.0
            except Exception:
                age_hours = 0.0

        if current_checkpoint_present:
            passed.append("checkpoint_tracked")
        else:
            failed.append("missing_checkpoint")

        if row.get("result"):
            passed.append("result_present")
        if row.get("error"):
            passed.append("error_present")

        if current_status in {"done", "failed"}:
            passed.append(f"terminal_status:{current_status}")
        elif current_status == "in_progress":
            passed.append("ongoing_task")
        else:
            passed.append("non_terminal_task")

        session_matches = len(checkpoint_history)
        last_checkpoint = checkpoint_history[0] if checkpoint_history else {}
        tracking_state = "tracked" if current_checkpoint_present else "untracked"
        stalled = current_status == "in_progress" and age_hours >= 24.0
        if stalled:
            warnings.append("task_stalled>24h")
        if session_matches:
            tracking_state = "tracked"

        score = 0.55
        if current_checkpoint_present:
            score += 0.2
        if session_matches:
            score += min(0.2, 0.05 * session_matches)
        if row.get("result"):
            score += 0.1
        if row.get("error"):
            score += 0.05
        if stalled:
            score -= 0.2
        score = max(0.0, min(1.0, round(score, 2)))

        summary = (
            f"TRACKED: task {task_id} is {current_status or 'unknown'}"
            f" | checkpoints {session_matches}"
            f" | age {age_hours:.2f}h"
            if current_status
            else f"TRACKED: task {task_id}"
        )

        packet = TaskTrackingPacket(
            task_id=task_id,
            row_id=row.get("id"),
            title=task.get("title") or "",
            current_status=current_status,
            source=_safe_text(row.get("source") or "", 80),
            priority=row.get("priority"),
            next_step=_safe_text(row.get("next_step") or "", 120),
            result_present=bool(row.get("result")),
            checkpoint_present=current_checkpoint_present,
            checkpoint_draft_present=bool(row.get("checkpoint_draft")),
            error_present=bool(row.get("error")),
            session_matches=session_matches,
            last_session_id=last_checkpoint.get("session_id", ""),
            last_checkpoint_ts=_safe_text(last_checkpoint.get("checkpoint_ts") or "", 80),
            last_action=_safe_text(last_checkpoint.get("last_action") or "", 160),
            last_result=_safe_text(last_checkpoint.get("last_result") or "", 240),
            age_hours=age_hours,
            tracking_state=tracking_state,
            stalled=stalled,
            tracking_score=score,
            passed_checks=passed,
            failed_checks=failed,
            warnings=warnings,
            summary=summary,
            row={
                "id": row.get("id"),
                "status": current_status,
                "priority": row.get("priority"),
                "source": row.get("source"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            },
            task=task,
            checkpoint_history=checkpoint_history if include_history_bool else [],
        )

        return {
            "ok": True,
            "task_id": task_id,
            "row_id": row.get("id"),
            "current_status": current_status,
            "tracking_state": tracking_state,
            "session_matches": session_matches,
            "tracking_score": score,
            "stalled": stalled,
            "warnings": warnings,
            "packet": packet.to_dict(),
            "row": packet.row,
            "checkpoint_history": packet.checkpoint_history,
            "message": packet.summary,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "blocked": True}


def t_task_tracking_packet(
    task_id: str = "",
    include_history: str = "true",
    history_limit: str = "10",
) -> dict:
    return build_task_tracking_packet(
        task_id=task_id,
        include_history=include_history,
        history_limit=history_limit,
    )


def build_task_packet(
    task_id: str = "",
    expected_status: str = "",
    require_result: str = "false",
    require_checkpoint: str = "false",
) -> dict:
    if not task_id:
        return {"ok": False, "error": "task_id required"}

    try:
        rows = _resolve_rows(task_id)
        if not rows:
            packet = TaskPacket(
                task_id=task_id,
                blocked=True,
                verification_score=0.0,
                failed_checks=["task_not_found"],
                warnings=[],
                summary=f"Task not found: {task_id}",
            )
            return {"ok": True, "blocked": True, "packet": packet.to_dict(), "summary": packet.summary}

        row = rows[0]
        task = _parse_task(row)
        current_status = str(row.get("status") or "")
        expected_status = str(expected_status or "").strip()
        require_result_bool = _coerce_bool(require_result)
        require_checkpoint_bool = _coerce_bool(require_checkpoint)
        context_vector = _build_context_vector(task, row)
        principle_tokens = _token_set(" ".join([
            task.get("title") or "",
            task.get("description") or "",
            row.get("next_step") or "",
            row.get("checkpoint") or row.get("checkpoint_draft") or "",
        ]))
        principle_signals = {
            "principle": len(principle_tokens & {"principle", "principles", "verify", "verification", "causal", "abstract", "abstraction", "replay"}),
            "memory": len(principle_tokens & {"memory", "history", "replay"}),
            "graph": len(principle_tokens & {"causal", "graph", "symbolic"}),
            "task": len(principle_tokens & {"task", "queue", "checkpoint", "result"}),
        }
        principle_abstraction_score = round(min(1.0, 0.15 + (0.12 * principle_signals["principle"]) + (0.08 * principle_signals["memory"]) + (0.05 * principle_signals["graph"])), 3)
        principle_abstraction_history = [
            {
                "task_id": task_id,
                "score": principle_abstraction_score,
                "signals": principle_signals,
                "context_vector": context_vector,
            }
        ]

        passed = ["task_found", f"current_status={current_status or 'unknown'}"]
        failed: list[str] = []
        warnings: list[str] = []

        if expected_status:
            if current_status == expected_status:
                passed.append(f"status_matches:{expected_status}")
            else:
                failed.append(f"status_mismatch:{current_status or 'missing'}!= {expected_status}")

        if require_result_bool or current_status in {"done", "failed"}:
            if row.get("result"):
                passed.append("result_present")
            else:
                failed.append("missing_result")

        if require_checkpoint_bool or current_status in {"in_progress", "done", "failed"}:
            if row.get("checkpoint") or row.get("checkpoint_draft"):
                passed.append("checkpoint_present")
            else:
                failed.append("missing_checkpoint")

        if row.get("error"):
            passed.append("error_present")

        try:
            ts_str = row.get("updated_at") or row.get("created_at") or ""
            if ts_str:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                if current_status == "in_progress" and ts < now - timedelta(hours=24):
                    warnings.append("in_progress_stale>24h")
                if current_status == "pending" and ts < now - timedelta(days=7):
                    warnings.append("pending_stale>7d")
        except Exception:
            pass

        score = len(passed) / max(1, len(passed) + len(failed))
        score = max(0.0, round(score - min(0.15, 0.02 * len(warnings)), 2))
        blocked = bool(failed) or score < 0.8
        packet = TaskPacket(
            task_id=task_id,
            row_id=row.get("id"),
            title=task.get("title") or "",
            current_status=current_status,
            expected_status=expected_status,
            kind="verification",
            next_step=_safe_text(row.get("next_step") or "", 120),
            source=_safe_text(row.get("source") or "", 80),
            priority=row.get("priority"),
            result_present=bool(row.get("result")),
            checkpoint_present=bool(row.get("checkpoint") or row.get("checkpoint_draft")),
            error_present=bool(row.get("error")),
            verification_score=score,
            blocked=blocked,
            passed_checks=passed,
            failed_checks=failed,
            warnings=warnings,
            summary=(
                f"BLOCKED: task verification failed for {task_id}"
                if blocked else
                f"CLEAR: task {task_id} verified"
            ),
            context_vector=context_vector,
            principle_abstraction_score=principle_abstraction_score,
            principle_abstraction_history=principle_abstraction_history,
            row={
                "id": row.get("id"),
                "priority": row.get("priority"),
                "source": row.get("source"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            },
            task=task,
        )
        return {
            "ok": True,
            "task_id": task_id,
            "row_id": row.get("id"),
            "current_status": current_status,
            "expected_status": expected_status or None,
            "verification_score": score,
            "blocked": blocked,
            "passed_checks": passed,
            "failed_checks": failed,
            "warnings": warnings,
            "packet": packet.to_dict(),
            "row": packet.row,
            "context_vector": context_vector,
            "principle_abstraction_score": principle_abstraction_score,
            "principle_abstraction_history": principle_abstraction_history,
            "message": packet.summary,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "blocked": True}


def t_task_packet(
    task_id: str = "",
    expected_status: str = "",
    require_result: str = "false",
    require_checkpoint: str = "false",
) -> dict:
    return build_task_packet(
        task_id=task_id,
        expected_status=expected_status,
        require_result=require_result,
        require_checkpoint=require_checkpoint,
    )


def build_task_error_packet(
    task_id: str = "",
    error: str = "",
    phase: str = "runtime",
    summary: str = "",
    retryable: str = "false",
    next_step: str = "owner_review",
    checkpoint: str | dict = "",
) -> dict:
    if not task_id or not error:
        return {"ok": False, "error": "task_id and error required"}

    try:
        rows = _resolve_rows(task_id)
        if not rows:
            return {
                "ok": False,
                "blocked": True,
                "error": f"task not found: {task_id}",
                "summary": f"Task not found while recording error: {task_id}",
            }

        row = rows[0]
        task = _parse_task(row)
        retryable_bool = _coerce_bool(retryable)
        now = _utcnow()

        packet = TaskPacket(
            task_id=task_id,
            row_id=row.get("id"),
            title=task.get("title") or "",
            current_status=str(row.get("status") or ""),
            expected_status="failed",
            kind="error",
            phase=_safe_text(phase, 80),
            error=_safe_text(error, 500),
            retryable=retryable_bool,
            next_step=_safe_text(next_step or "owner_review", 120),
            source=_safe_text(row.get("source") or "", 80),
            priority=row.get("priority"),
            result_present=True,
            checkpoint_present=bool(checkpoint) or bool(row.get("checkpoint") or row.get("checkpoint_draft")),
            error_present=True,
            verification_score=1.0,
            blocked=False,
            passed_checks=["task_found", "failure_recorded"],
            failed_checks=[],
            warnings=[],
            summary=_safe_text(summary or error, 240),
            row={
                "id": row.get("id"),
                "status": row.get("status"),
                "source": row.get("source"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            },
            task=task,
        )

        checkpoint_payload = checkpoint if isinstance(checkpoint, dict) and checkpoint else None
        if checkpoint_payload is None:
            checkpoint_payload = {
                "task_id": task_id,
                "row_id": row.get("id"),
                "phase": packet.phase,
                "error": packet.error,
                "retryable": retryable_bool,
                "next_step": packet.next_step,
                "status": "failed",
                "ts": now,
            }
        patch = {
            "status": "failed",
            "error": packet.error,
            "result": json.dumps(packet.to_dict(), default=str)[:4000],
            "next_step": packet.next_step,
            "checkpoint": checkpoint_payload,
            "checkpoint_at": now,
            "checkpoint_draft": json.dumps(checkpoint_payload, default=str)[:4000],
            "updated_at": now,
        }

        ok = sb_patch("task_queue", f"id=eq.{row['id']}", patch)
        verification = build_task_packet(
            task_id=task_id,
            expected_status="failed",
            require_result="true",
            require_checkpoint="true" if checkpoint else "false",
        )
        return {
            "ok": bool(ok) and bool(verification.get("ok") and not verification.get("blocked")),
            "task_id": task_id,
            "row_id": row.get("id"),
            "phase": packet.phase,
            "retryable": retryable_bool,
            "next_step": packet.next_step,
            "packet": packet.to_dict(),
            "verification_packet": verification,
            "verified": bool(verification.get("ok") and not verification.get("blocked")),
            "message": f"Task failure recorded for {task_id}",
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "blocked": True}


def t_task_error_packet(
    task_id: str = "",
    error: str = "",
    phase: str = "runtime",
    summary: str = "",
    retryable: str = "false",
    next_step: str = "owner_review",
    checkpoint: str | dict = "",
) -> dict:
    return build_task_error_packet(
        task_id=task_id,
        error=error,
        phase=phase,
        summary=summary,
        retryable=retryable,
        next_step=next_step,
        checkpoint=checkpoint,
    )


def t_task_verification_bundle(
    task_id: str = "",
    expected_status: str = "",
    require_result: str = "false",
    require_checkpoint: str = "false",
    include_history: str = "true",
    history_limit: str = "8",
    session_id: str = "default",
    strict: str = "false",
    require_system_checkpoint: str = "false",
    operation: str = "",
    target_file: str = "",
    context: str = "",
    assumed_state: str = "",
    sources: str = "supabase",
    action_type: str = "deploy",
    owner_token: str = "",
    sequence: str = "",
    reward_signal: str = "",
    side_effects: str = "",
    principles: str = "",
    task_context: str = "",
    goal: str = "",
    current_state: str = "",
    hwm_levels: str = "",
    candidate_actions: str = "",
    horizon: str = "3",
    rollouts: str = "8",
    exploration_weight: str = "1.2",
    causal_graph: str = "",
    domain: str = "general",
    state_hint: str = "",
) -> dict:
    """Integrated verification bundle for task/state/action/counterfactual work."""
    try:
        from core_tools import t_task_state_packet as _t_task_state_packet, t_verification_packet as _t_verification_packet
        from core_tools_memory import t_system_verification_packet
        from core_tools_world_model import (
            t_causal_mapping_module,
            t_causal_graph_data_generator,
            t_hierarchical_search_tree,
            t_domain_invariant_feature_packet,
            t_principle_search_module,
            t_simulated_critic,
        )

        task_state = _t_task_state_packet(
            task_id=task_id or "",
            expected_status=expected_status or "",
            require_result=require_result,
            require_checkpoint=require_checkpoint,
            include_history=include_history,
            history_limit=history_limit,
        )
        system_verification = t_system_verification_packet(
            session_id=session_id or "default",
            strict=strict,
            require_checkpoint=require_system_checkpoint,
            task_sample_limit="5",
            changelog_limit="5",
        )

        action_verification = None
        if task_id or any(str(v).strip() for v in (operation, target_file, context, assumed_state, owner_token)):
            try:
                action_verification = _t_verification_packet(
                    operation=operation or task_id or "",
                    target_file=target_file or "",
                    context=context or task_context or "",
                    assumed_state=assumed_state or "",
                    sources=sources or "supabase",
                    action_type=action_type or "deploy",
                    owner_token=owner_token or "",
                )
            except Exception as exc:
                action_verification = {
                    "ok": False,
                    "blocked": True,
                    "verification_score": 0.0,
                    "error": str(exc),
                }

        causal_graph_packet = None
        causal_graph_payload = causal_graph or "{}"
        if not causal_graph_payload or causal_graph_payload == "{}":
            try:
                causal_graph_packet = t_causal_graph_data_generator(
                    context=task_context or context or current_state or state_hint or "",
                    goal=goal or operation or expected_status or task_id or "",
                    modules=sources or "",
                    symbols=principles or "",
                    actions=candidate_actions or "",
                    domain=domain,
                    state_hint=state_hint,
                    limit="6",
                )
                if isinstance(causal_graph_packet, dict) and causal_graph_packet.get("ok"):
                    graph_obj = causal_graph_packet.get("graph")
                    causal_graph_payload = json.dumps(graph_obj, default=str) if graph_obj is not None else "{}"
            except Exception as exc:
                causal_graph_packet = {"ok": False, "error": str(exc), "summary": f"causal_graph_data_generator=error | {exc}"}

        critic = t_simulated_critic(
            sequence=sequence or task_context or operation or task_id or "",
            reward_signal=reward_signal or goal or expected_status or operation or "",
            side_effects=side_effects or context or target_file or "",
            domain=domain,
            state_hint=state_hint,
        )
        causal_mapping = t_causal_mapping_module(
            causal_graph=causal_graph_payload,
            context_embedding=context or state_hint or task_context or "",
            goal=goal or operation or expected_status or task_id or "",
            domain=domain,
            state_hint=state_hint,
        )
        principle_search = t_principle_search_module(
            principles=principles or "verify_before_close,check_side_effects,prefer_evidence,do_not_guess",
            state=current_state or (task_state.get("summary") or ""),
            goal=goal or operation or expected_status or task_id or "",
            task_context=task_context or context or sequence or "",
            domain=domain,
            state_hint=state_hint,
        )
        invariant_features = t_domain_invariant_feature_packet(
            current_state=current_state or (task_state.get("summary") or ""),
            goal=goal or operation or expected_status or task_id or "",
            modules=sources or "",
            symbols=principles or "",
            actions=candidate_actions or "",
            task_context=task_context or context or sequence or "",
            hwm_levels=hwm_levels or "",
            domain=domain,
            state_hint=state_hint,
            limit="8",
        )
        search_tree = t_hierarchical_search_tree(
            current_state=current_state or (task_state.get("summary") or ""),
            goal=goal or operation or expected_status or task_id or "",
            hwm_levels=hwm_levels or "low,medium,high",
            candidate_actions=candidate_actions or "inspect,implement,verify,close",
            horizon=horizon,
            rollouts=rollouts,
            exploration_weight=exploration_weight,
            domain=domain,
            state_hint=state_hint,
        ) if (hwm_levels or candidate_actions or goal or operation or task_id) else {"ok": True, "summary": "search_tree skipped"}

        scores: list[float] = []
        for pkt, key in (
            (task_state.get("verification") or {}, "verification_score"),
            (system_verification, "verification_score"),
            (action_verification or {}, "verification_score"),
            (critic, "score"),
            (invariant_features or {}, "feature_score"),
        ):
            try:
                val = float(pkt.get(key) or 0.0)
            except Exception:
                val = 0.0
            if val > 0:
                scores.append(max(0.0, min(1.0, val)))

        bundle_score = round(sum(scores) / len(scores), 3) if scores else 0.0
        task_ver = task_state.get("verification") or {}
        passed = []
        failed = []
        warnings = []

        if task_state.get("ok") and not task_ver.get("blocked"):
            passed.append("task_state_verified")
        else:
            failed.append("task_state_blocked")

        if system_verification.get("ok") and not system_verification.get("blocked"):
            passed.append("system_verified")
        else:
            failed.append("system_blocked")

        if action_verification and action_verification.get("ok") and not action_verification.get("blocked"):
            passed.append("action_verified")
        elif action_verification is not None:
            failed.append("action_blocked")

        critic_score = float(critic.get("score") or 0.0)
        if critic_score >= 0.55:
            passed.append("critic_supportive")
        else:
            warnings.append("critic_low_confidence")
        if invariant_features.get("ok"):
            passed.append("domain_invariant_features_extracted")
        else:
            warnings.append("domain_invariant_features_failed")

        if float(task_ver.get("verification_score") or 0.0) < 0.8:
            warnings.append("task_verification_below_threshold")
        if float(system_verification.get("verification_score") or 0.0) < 0.75:
            warnings.append("system_verification_below_threshold")
        if action_verification is not None and float(action_verification.get("verification_score") or 0.0) < 0.75 and action_verification.get("ok") is not False:
            warnings.append("action_verification_below_threshold")
        if isinstance(causal_mapping, dict) and not causal_mapping.get("ok", True):
            warnings.append("causal_mapping_failed")
        if isinstance(causal_graph_packet, dict) and not causal_graph_packet.get("ok", True):
            warnings.append("causal_graph_generation_failed")
        if isinstance(principle_search, dict) and not principle_search.get("ok", True):
            warnings.append("principle_search_failed")
        if isinstance(search_tree, dict) and not search_tree.get("ok", True):
            warnings.append("search_tree_failed")

        blocked = bool(
            task_ver.get("blocked")
            or system_verification.get("blocked")
            or (action_verification.get("blocked") if action_verification is not None else False)
            or bundle_score < 0.75
        )
        summary = (
            f"integrated_verification={'blocked' if blocked else 'ok'} | "
            f"task={float(task_ver.get('verification_score') or 0.0):.2f} | "
            f"system={float(system_verification.get('verification_score') or 0.0):.2f} | "
            f"action={(float(action_verification.get('verification_score') or 0.0) if action_verification is not None else 0.0):.2f} | "
            f"critic={critic_score:.2f} | feature={(invariant_features.get('feature_signature') or 'none')[:60]} | score={bundle_score:.2f}"
        )

        packet = TaskVerificationBundle(
            task_id=task_id or "",
            session_id=session_id or "default",
            task_state=task_state,
            system_verification=system_verification,
            action_verification=action_verification,
            causal_mapping=causal_mapping,
            causal_graph_packet=causal_graph_packet,
            principle_search=principle_search,
            search_tree=search_tree,
            critic=critic,
            domain_invariant_features=invariant_features,
            verification_score=bundle_score,
            blocked=blocked,
            passed_checks=passed,
            failed_checks=failed,
            warnings=warnings,
            summary=summary,
        )
        return {
            "ok": True,
            "task_id": task_id or "",
            "session_id": session_id or "default",
            "verification_score": bundle_score,
            "blocked": blocked,
            "passed_checks": passed,
            "failed_checks": failed,
            "warnings": warnings,
            "task_state": task_state,
            "system_verification": system_verification,
            "action_verification": action_verification,
            "causal_mapping": causal_mapping,
            "causal_graph_packet": causal_graph_packet,
            "principle_search": principle_search,
            "search_tree": search_tree,
            "critic": critic,
            "domain_invariant_features": invariant_features,
            "packet": packet.to_dict(),
            "summary": summary,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "blocked": True, "task_id": task_id or ""}
