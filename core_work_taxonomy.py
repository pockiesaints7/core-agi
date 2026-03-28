"""core_work_taxonomy.py -- shared taxonomy for CORE autonomy tasks.

This module keeps the current execution conservative while giving future
specialized workers a stable contract for routing.
"""
from __future__ import annotations

import json
import re
from typing import Any


def _safe_text(value: Any, limit: int = 240) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _normalize_autonomy(autonomy: Any) -> dict:
    if isinstance(autonomy, str):
        try:
            autonomy = json.loads(autonomy)
        except Exception:
            autonomy = {}
    return autonomy if isinstance(autonomy, dict) else {}


def _slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:80] or "task"


def _infer_domain(kind: str, work_track: str, title: str, description: str) -> str:
    hay = f"{title}\n{description}".lower()
    if work_track == "db_only" or kind == "kb_expand":
        return "knowledge_base"
    if work_track == "behavioral_rule" or kind == "behavioral_remediation":
        return "reasoning"
    if work_track == "integration":
        return "integration"
    if any(key in hay for key in ("telegram", "webhook")):
        return "telegram"
    if any(key in hay for key in ("supabase", "postgres", "database", "table", "row")):
        return "data"
    if any(key in hay for key in ("trade", "binance", "position", "pnl", "risk")):
        return "trading"
    return "code" if work_track in {"code_patch", "new_module"} else "project"


def _infer_kind(work_track: str) -> str:
    if work_track == "db_only":
        return "kb_expand"
    if work_track == "behavioral_rule":
        return "behavioral_remediation"
    return "architecture_proposal"


def _infer_work_track(kind: str, title: str, description: str) -> str:
    hay = f"{title}\n{description}".lower()
    if kind == "kb_expand" or any(
        key in hay for key in ("kb coverage", "knowledge base", "knowledge", "ingest", "domain:")
    ):
        return "db_only"
    if kind == "behavioral_remediation" or any(
        key in hay for key in ("behavior", "rule", "policy", "stale", "quality decline", "recurring failure")
    ):
        return "behavioral_rule"
    if any(key in hay for key in ("research", "benchmark", "evaluate hypothesis", "evidence")):
        return "research"
    if any(key in hay for key in ("wire", "wiring", "integrat", "connect", "hook up", "plumb")):
        return "integration"
    if any(key in hay for key in ("new module", "new worker", "add module", "create module")):
        return "new_module"
    if any(key in hay for key in ("tool", "module", "py", "patch", "refactor", "code", "script", "function", "worker")):
        return "code_patch"
    return "proposal_only"


def _infer_execution_mode(work_track: str) -> str:
    if work_track in {"db_only", "behavioral_rule"}:
        return "db_write"
    if work_track == "research":
        return "research"
    if work_track in {"code_patch", "new_module", "integration"}:
        return "plan"
    return "proposal"


def _infer_verification(work_track: str) -> str:
    if work_track == "db_only":
        return "knowledge_base row exists"
    if work_track == "behavioral_rule":
        return "behavioral_rules row exists"
    if work_track == "research":
        return "knowledge capture queued"
    if work_track in {"code_patch", "new_module"}:
        return "code proposal queued for owner review"
    if work_track == "integration":
        return "integration proposal queued for owner review"
    return "evolution_queue artifact exists"


def _infer_expected_artifact(work_track: str) -> str:
    if work_track == "db_only":
        return "knowledge_base"
    if work_track == "behavioral_rule":
        return "behavioral_rules"
    if work_track in {"code_patch", "new_module", "integration"}:
        return "evolution_queue"
    return "evolution_queue"


def _infer_specialized_worker(work_track: str) -> str:
    if work_track in {"db_only", "behavioral_rule"}:
        return "task_autonomy"
    if work_track == "research":
        return "research_autonomy"
    if work_track in {"code_patch", "new_module"}:
        return "code_autonomy"
    if work_track == "integration":
        return "integration_autonomy"
    return "evolution_autonomy"


def _infer_task_group(work_track: str) -> str:
    if work_track == "db_only":
        return "knowledge"
    if work_track == "behavioral_rule":
        return "behavior"
    if work_track == "research":
        return "research"
    if work_track in {"code_patch", "new_module"}:
        return "code"
    if work_track == "integration":
        return "integration"
    return "architecture"


def _infer_review_scope(work_track: str) -> str:
    """Explicitly separate owner-only review items from worker-handled items.

    This is used by routers/dashboards to keep manual review clean:
    - worker: db_only, behavioral_rule, research
    - owner:  code_patch, new_module, integration, proposal_only
    """
    if work_track in {"db_only", "behavioral_rule", "research"}:
        return "worker"
    if work_track in {"code_patch", "new_module", "integration", "proposal_only"}:
        return "owner"
    # Default conservative: if we don't know, require owner review.
    return "owner"


def _infer_owner_only(review_scope: str) -> bool:
    return str(review_scope or "").strip().lower() == "owner"


def build_autonomy_contract(
    title: str,
    description: str = "",
    source: str = "",
    autonomy: Any = None,
    context: str = "",
) -> dict:
    auto = _normalize_autonomy(autonomy)
    title_s = _safe_text(title, 200)
    description_s = _safe_text(description, 1200)
    source_s = _safe_text(source or auto.get("source") or auto.get("origin") or "", 80)

    kind = _safe_text(auto.get("kind"), 80)
    work_track = _safe_text(auto.get("work_track"), 40)
    if not work_track:
        work_track = _infer_work_track(kind or "", title_s, description_s)
    if not kind:
        kind = _infer_kind(work_track)

    artifact_domain = _safe_text(auto.get("artifact_domain") or auto.get("domain"), 80)
    if not artifact_domain:
        artifact_domain = _infer_domain(kind, work_track, title_s, description_s)

    domain = _safe_text(auto.get("domain"), 80) or artifact_domain
    execution_mode = _safe_text(auto.get("execution_mode"), 40) or _infer_execution_mode(work_track)
    verification = _safe_text(auto.get("verification"), 120) or _infer_verification(work_track)
    expected_artifact = _safe_text(auto.get("expected_artifact"), 120) or _infer_expected_artifact(work_track)
    task_group = _safe_text(auto.get("task_group"), 80) or _infer_task_group(work_track)
    specialized_worker = _safe_text(auto.get("specialized_worker"), 80) or _infer_specialized_worker(work_track)
    review_scope = _safe_text(auto.get("review_scope"), 40) or _infer_review_scope(work_track)
    owner_only = auto.get("owner_only")
    if owner_only in (None, ""):
        owner_only = _infer_owner_only(review_scope)
    else:
        owner_only = str(owner_only).strip().lower() in {"1", "true", "yes", "on"}
    trigger = _safe_text(auto.get("trigger"), 80)
    evolution_id = _safe_text(auto.get("evolution_id"), 80)
    task_id = _safe_text(auto.get("task_id"), 80)
    claim_id = _safe_text(auto.get("claim_id"), 80)
    route = _safe_text(auto.get("route"), 80) or specialized_worker
    priority = auto.get("priority")
    try:
        priority = int(priority) if priority is not None else 3
    except Exception:
        priority = 3

    contract = {
        "kind": kind,
        "origin": _safe_text(auto.get("origin") or source_s, 80) or source_s,
        "source": source_s,
        "domain": domain,
        "artifact_domain": artifact_domain,
        "artifact_topic": _safe_text(auto.get("artifact_topic") or f"{context or 'autonomy'}:{_slugify(title_s)}", 120),
        "work_track": work_track,
        "execution_mode": execution_mode,
        "verification": verification,
        "expected_artifact": expected_artifact,
        "task_group": task_group,
        "specialized_worker": specialized_worker,
        "route": route,
        "review_scope": review_scope,
        "owner_only": owner_only,
        "trigger": trigger,
        "evolution_id": evolution_id,
        "task_id": task_id,
        "claim_id": claim_id,
        "priority": priority,
        "context": _safe_text(context, 80),
    }
    for key, value in auto.items():
        if value not in (None, ""):
            contract[key] = value
    contract.setdefault("work_track", work_track)
    contract.setdefault("execution_mode", execution_mode)
    contract.setdefault("verification", verification)
    contract.setdefault("expected_artifact", expected_artifact)
    contract.setdefault("task_group", task_group)
    contract.setdefault("specialized_worker", specialized_worker)
    contract.setdefault("route", route)
    contract.setdefault("review_scope", review_scope)
    contract.setdefault("owner_only", owner_only)
    contract.setdefault("artifact_domain", artifact_domain)
    contract.setdefault("domain", domain)
    contract.setdefault("kind", kind)
    contract.setdefault("source", source_s)
    contract.setdefault("origin", _safe_text(auto.get("origin") or source_s, 80) or source_s)
    contract.setdefault("artifact_topic", _safe_text(auto.get("artifact_topic") or f"{context or 'autonomy'}:{_slugify(title_s)}", 120))
    contract.setdefault("priority", priority)
    return contract
