"""
core_orch_context.py — shared request/evidence/decision helpers for CORE ORC.
Keeps the orchestrator pipeline cohesive by building structured packets
instead of ad hoc dicts at each layer.
"""
from __future__ import annotations

import re
import hashlib
from dataclasses import asdict
from typing import Any, Dict, Iterable, List
from datetime import datetime
from pathlib import Path

import httpx

from core_config import SUPABASE_URL, _sbh_count_svc
from core_public_evidence import classify_public_evidence
from core_task_taxonomy import build_task_mode_packet


# ── Basic helpers ────────────────────────────────────────────────────────────
def _safe_text(value: Any, limit: int = 500) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _keyword_hits(text: str, keywords: Iterable[str]) -> int:
    lower = (text or "").lower()
    return sum(1 for kw in keywords if kw in lower)


def _extract_code_targets(text: str) -> List[str]:
    """Pull likely repo/file targets from a request string."""
    if not text:
        return []
    candidates = set()
    for match in re.findall(r"[\w./:-]+\.(?:py|ts|js|jsx|tsx|json|md|yml|yaml|toml)", text):
        candidates.add(match.strip(" ,;:()[]{}<>"))
    for match in re.findall(r"(?:/[\w.-]+)+", text):
        if "." not in match and match.count("/") <= 1:
            continue
        if "." in match or "/" in match:
            candidates.add(match.strip(" ,;:()[]{}<>"))
    return sorted(candidates)[:5]


def _missing_code_targets(text: str) -> List[str]:
    """Return extracted code/file targets that do not exist on disk."""
    missing: List[str] = []
    for target in _extract_code_targets(text):
        try:
            if not Path(target).expanduser().exists():
                missing.append(target)
        except Exception:
            missing.append(target)
    return missing


def _signal(text: str, phrases: Iterable[str]) -> bool:
    lower = (text or "").lower()
    return any(p in lower for p in phrases)


def classify_human_input(
    text: str,
    command: str = "",
    message_type: str = "message",
    route: str = "conversation",
    attachments: list | None = None,
) -> Dict[str, Any]:
    """Build a structured human-input packet before intent classification."""
    text = text or ""
    lower = text.lower().strip()
    cmd = (command or "").lower().strip()
    attachments = attachments or []
    missing_targets = _missing_code_targets(text)

    signals: list[str] = []
    classes: list[str] = []
    score_map: dict[str, int] = {
        "interrupt": 0,
        "correct": 0,
        "approve": 0,
        "constrain": 0,
        "meta": 0,
        "act": 0,
        "evaluate": 0,
        "inform": 0,
        "ask": 0,
    }

    def bump(cls: str, amount: int = 1, signal: str = "") -> None:
        score_map[cls] = score_map.get(cls, 0) + amount
        if signal:
            signals.append(signal)

    # Interrupt / cancel / pause
    if _signal(lower, ("stop", "pause", "hold on", "abort", "cancel", "wait", "don't continue", "do not continue", "pause here")) or cmd in {"/stop", "/pause", "/abort"}:
        bump("interrupt", 4, "interrupt")
        classes.append("interrupt")

    # Corrections / fixes
    if _signal(lower, ("no,", "no ", "wrong", "that's wrong", "not correct", "actually", "i meant", "what i meant", "correction", "fix this", "you said", "you used the wrong")):
        bump("correct", 4, "correction")
        classes.append("correct")

    # Approval / consent
    if _signal(lower, ("approved", "proceed", "go ahead", "yes", "correct", "looks good", "sounds good", "greenlight")):
        bump("approve", 3, "approval")
        classes.append("approve")

    # Constraints / policy
    if _signal(lower, ("only ", "must ", "must not", "don't ", "do not ", "never ", "without ", "keep ", "limit ", "strictly", "no need")):
        bump("constrain", 3, "constraint")
        classes.append("constrain")

    # Meta / orchestration guidance
    if _signal(lower, ("plan", "strategy", "architecture", "how should", "what should", "route", "layer", "intent", "schema", "matrix", "classify", "pipeline")):
        bump("meta", 2, "meta")
        classes.append("meta")

    # Active instructions / tasks
    if route == "command" or cmd:
        bump("act", 3, "command")
        classes.append("act")
    if _signal(lower, (
        "do ", "make ", "build ", "create ", "update ", "fix ", "change ", "add ", "remove ",
        "implement ", "proceed", "run ", "restart", "sync", "close ", "push ", "pull ", "test ",
        "verify ",
    )):
        bump("act", 2, "instruction")
        classes.append("act")
    if _signal(lower, ("step by step", "investigate", "research", "analyze", "inspect", "diagnose", "trace", "break down", "deep dive", "keep going", "until")):
        bump("act", 2, "analysis_instruction")
        classes.append("act")

    # Evaluation / review
    if _signal(lower, ("review", "judge", "evaluate", "compare", "rank", "triage", "approve", "reject", "batch close", "cluster close")):
        bump("evaluate", 2, "evaluation")
        classes.append("evaluate")

    # Missing file/path targets should be clarified instead of guessed.
    if missing_targets:
        bump("ask", 4, "missing_target")
        bump("meta", 1, "missing_target_meta")
        classes.append("ask")

    # Informational / status updates
    if _signal(lower, ("i did", "i added", "i changed", "i fixed", "here is", "this is", "fyi", "for your info", "update:", "status:", "result:")):
        bump("inform", 2, "status_update")
        classes.append("inform")

    # Questions / asks
    if "?" in text or _signal(lower, ("what", "how", "why", "when", "where", "who", "which", "can you", "could you", "would you", "please")):
        bump("ask", 3, "question")
        classes.append("ask")

    # Attachments usually imply a task or clarification
    if attachments:
        bump("act", 1, "attachment")
        if any(a.get("type") == "document" for a in attachments if isinstance(a, dict)):
            bump("ask", 1, "document")

    # Short acknowledgements lean conversational unless command/ask already present.
    if len(lower) <= 20 and _signal(lower, ("ok", "okay", "thanks", "thank you", "got it", "nice", "cool", "yes", "no")):
        bump("inform", 1, "ack")

    if not classes:
        classes = ["ask"] if ("?" in text or _signal(lower, ("what", "how", "why", "when", "where", "who", "which"))) else ["inform"]

    ordered = sorted(score_map.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    primary_class = ordered[0][0] if ordered and ordered[0][1] > 0 else classes[0]
    secondary_classes = [cls for cls in classes if cls != primary_class]
    multi_label = len(set(classes)) > 1
    confidence = min(1.0, 0.42 + (score_map.get(primary_class, 0) * 0.12) + (0.08 if multi_label else 0.0))

    route_hint = "clarify"
    if primary_class in {"interrupt"}:
        route_hint = "stop"
    elif primary_class in {"act", "approve", "constrain", "meta"}:
        route_hint = "execute"
    elif primary_class in {"evaluate"}:
        route_hint = "review"
    elif primary_class in {"ask"}:
        route_hint = "answer"
    elif primary_class in {"correct", "inform"}:
        route_hint = "store"

    request_kind = classify_request_kind(text, command=command, message_type=message_type, route=route, intent=None)
    response_mode = {
        "status": "status",
        "self_assessment": "capability",
        "owner_review": "review",
        "debug": "debug",
        "task": "task",
        "conversation": "conversation",
    }.get(request_kind, "tool")

    if primary_class == "interrupt" and request_kind in {"question", "conversation", "general_query"}:
        request_kind = "command"
        response_mode = "conversation"
    elif primary_class == "correct" and request_kind in {"question", "conversation", "general_query"}:
        request_kind = "debug"
        response_mode = "debug"
    elif primary_class == "evaluate" and request_kind in {"question", "conversation", "general_query"}:
        request_kind = "owner_review"
        response_mode = "review"
    elif primary_class == "approve" and request_kind in {"question", "conversation", "general_query"}:
        request_kind = "command"
        response_mode = "conversation"
    elif primary_class == "constrain" and request_kind in {"question", "conversation", "general_query"}:
        request_kind = "command"
        response_mode = "tool"
    elif primary_class == "meta" and request_kind in {"question", "conversation", "general_query"}:
        request_kind = "command"
        response_mode = "tool"
    elif primary_class == "inform" and request_kind in {"question", "general_query"}:
        request_kind = "conversation"
        response_mode = "conversation"
    elif primary_class == "act" and request_kind in {"question", "conversation"} and route == "command":
        request_kind = "command"
        response_mode = "tool"

    vague_action = _signal(lower, (
        "make it better",
        "improve it",
        "fix it",
        "change it",
        "make better",
        "do better",
        "make it work",
    ))
    if primary_class == "act" and vague_action and not _extract_code_targets(text) and len(lower) < 60:
        route_hint = "clarify"
        requires_clarification = True

    requires_tools = primary_class in {"ask", "act", "evaluate"} or route == "command" or bool(cmd)
    requires_clarification = confidence < 0.5 and primary_class in {"ask", "act", "meta"}
    if missing_targets:
        requires_clarification = True
        route_hint = "clarify"
    if primary_class == "act" and vague_action and not _extract_code_targets(text) and len(lower) < 60:
        requires_clarification = True
        route_hint = "clarify"

    return {
        "top_level_class": primary_class,
        "primary_class": primary_class,
        "secondary_classes": secondary_classes[:5],
        "speech_acts": list(dict.fromkeys(classes))[:8],
        "multi_label": multi_label,
        "confidence": round(confidence, 3),
        "route_hint": route_hint,
        "request_kind": request_kind,
        "response_mode": response_mode,
        "requires_tools": requires_tools,
        "requires_clarification": requires_clarification,
        "urgency": "high" if _signal(lower, ("now", "immediately", "urgent", "asap", "right now")) or primary_class == "interrupt" else "normal",
        "actionability": "actionable" if primary_class in {"act", "approve", "interrupt"} else "informational" if primary_class in {"inform"} else "mixed" if multi_label else "contextual",
        "constraints": [c for c in ("only", "must", "don't", "do not", "never", "without", "keep") if c in lower],
        "signals": list(dict.fromkeys(signals))[:12],
        "attachments_present": bool(attachments),
        "message_type": message_type,
        "route": route,
        "command": command,
        "missing_targets": missing_targets,
    }


def _pick_public_sources(text: str) -> List[str]:
    """Choose public ingestion sources that fit the request."""
    lower = (text or "").lower()
    sources: List[str] = []
    if any(k in lower for k in ("paper", "arxiv", "research", "study", "scientific", "academic", "benchmark")):
        sources.extend(["arxiv"])
    if any(k in lower for k in ("docs", "documentation", "api", "reference", "manual", "guide", "how to", "official")):
        sources.extend(["docs", "stackoverflow"])
    if any(k in lower for k in ("news", "latest", "current", "today", "release", "update", "announce", "trending")):
        sources.extend(["hackernews", "reddit", "medium"])
    if any(k in lower for k in ("blog", "article", "tutorial", "explain", "learn", "overview")):
        sources.extend(["medium", "stackoverflow"])
    if any(k in lower for k in ("community", "discussion", "forum")):
        sources.extend(["reddit", "stackoverflow"])
    if not sources:
        sources = ["arxiv", "docs", "medium", "reddit", "hackernews", "stackoverflow"]
    # Preserve order while deduping.
    out: List[str] = []
    for src in sources:
        if src not in out:
            out.append(src)
    return out[:6]


def _tool_family_for_name(tool_name: str, tool_desc: str = "") -> str:
    """Classify a tool into a broad capability family."""
    name = (tool_name or "").lower()
    desc = (tool_desc or "").lower()
    combined = f"{name} {desc}"

    def has(*needles: str) -> bool:
        return any(n in combined for n in needles)

    if has("owner_review_cluster_packet", "owner_review_cluster_close", "review_cluster", "cluster_close", "review_work_packet", "repo_review_packet", "document_review_packet", "spreadsheet_review_packet", "presentation_review_packet"):
        return "review"
    if has(
        "reasoning_packet", "tool_reliance_assessor", "dynamic_relational_graph", "causal_graph",
        "causal_graph_inference", "meta_contextual_router", "adaptive_temporal_filter",
        "temporal_attention", "monte_carlo_tree_search", "hierarchical_search_controller",
        "temporal_hierarchical_world_model", "dynamic_router", "meta_representation",
        "novelty_assessment", "consolidation_manager", "active_learning_strategy",
        "register_tool", "contradiction_check", "negative_space", "circuit_breaker",
        "loop_detect", "partial_complete", "verify_external_state", "verification_packet",
        "system_verification_packet", "mid_task_correct", "resolve_ambiguity", "assert_source",
    ):
        return "self_improve"
    if has(
        "evaluate_state", "state_packet", "state_consistency_check", "session_snapshot",
        "get_state", "get_system_health", "get_active_goals", "get_quality_trend",
        "get_time", "datetime_now", "get_constitution", "vm_info", "system_map_scan",
        "get_capability_model", "trigger_capability_calibration", "tool_health_scan",
        "core_gap_audit", "gap_audit",
        "tool_stats", "load_arch_context", "task_health", "crash_report", "verify_live",
    ):
        return "state"
    if has(
        "semantic_kb_search", "semantic_episode_search", "get_owner_profile",
        "add_owner_observation", "mistakes_since", "changelog_verification_packet",
        "changelog_tracking_packet", "mistake_tracking_packet", "core_py_fn",
        "core_py_validate", "diff", "project_list", "project_get", "project_search",
        "project_context_check", "project_register", "project_update_kb",
        "project_update_index", "project_consume", "public_evidence_packet", "ask",
    ):
        return "knowledge"
    if has("repo_map", "repo_component", "repo_graph", "code_read_packet", "file_list", "file_read", "file_write", "read_file", "write_file", "search_in_file", "gh_", "multi_patch", "smart_patch", "shell", "run_python", "run_script", "git"):
        return "repo_code"
    if has("task_mode_packet"):
        return "task"
    if has("spreadsheet_work_packet", "document_work_packet", "presentation_work_packet", "create_document", "create_spreadsheet", "create_presentation", "read_document", "convert_document", "read_pdf_content", "read_image_content", "image_process", "generate_image"):
        return "document"
    if has("search_kb", "add_knowledge", "kb_update", "get_mistakes", "log_mistake", "get_behavioral_rules", "ingest_knowledge", "knowledge"):
        return "knowledge"
    if has("web_search", "web_fetch", "summarize_url", "browser", "fetch_url"):
        return "web"
    if has("get_state", "get_system_health", "state_packet", "state_consistency_check", "session_snapshot", "get_time", "datetime_now", "get_active_goals", "get_quality_trend", "get_constitution"):
        return "state"
    if has("task_add", "task_update", "checkpoint", "set_goal", "update_goal_progress", "goal"):
        return "task"
    if has("list_evolutions", "approve_evolution", "reject_evolution", "trigger_cold_processor", "get_training_pipeline", "evolution"):
        return "training"
    if has("deploy_status", "railway_logs_live", "redeploy", "build_status", "ping_health", "service_info", "env_get", "env_set"):
        return "deploy"
    if has("notify_owner", "notify"):
        return "notify"
    if has("sb_query", "sb_insert", "sb_patch", "sb_upsert", "sb_delete", "get_table_schema"):
        return "database"
    if has("crypto_price", "crypto_balance", "crypto_trade"):
        return "crypto"
    if has("reason_chain", "decompose_task", "lookahead", "impact_model"):
        return "self_improve"
    if has("agent_session_init", "agent_state_get", "agent_state_set", "agent_step_done"):
        return "agent_ops"
    if has("weather", "currency", "translate", "generate_image", "calc", "list_tools", "datetime_now", "get_time"):
        return "utility"
    if has("sb_bulk_insert", "set_simulation", "backfill_patterns"):
        return "training"
    if has("task_error_packet", "task_similarity_metric", "consolidation_manager"):
        return "task"
    if has("list_templates", "run_template", "debug_fn", "listen", "install_package", "validate_tool_output", "test_gemini", "maintenance_purge"):
        return "utility"
    if has("vm_info", "update_behavioral_rule", "log_quality_metrics", "get_quality_alert", "log_reasoning"):
        return "state"
    if has("predict_failure"):
        return "self_improve"
    if has("service", "install_package", "vm_info"):
        return "deploy"
    return "other"


def build_tool_policy_packet(msg) -> Dict[str, Any]:
    """Fingerprint the live tool registry and surface capability-aware tool policy."""
    try:
        from core_tools import TOOLS
    except Exception as exc:
        return {
            "ok": False,
            "error": f"tool registry unavailable: {exc}",
            "registry_size": 0,
            "registry_signature": "",
            "family_counts": {},
            "preferred_families": [],
            "preferred_tools": [],
            "avoid_first": ["file_list", "shell", "list_tools"],
        }

    decision = (msg.context or {}).get("decision_packet", {}) if hasattr(msg, "context") else {}
    input_profile = (msg.context or {}).get("input_profile", {}) if hasattr(msg, "context") else {}
    task_mode_packet = (msg.context or {}).get("task_mode_packet", {}) if hasattr(msg, "context") else {}
    evidence_gate = (msg.context or {}).get("evidence_gate", {}) if hasattr(msg, "context") else {}
    request_kind = getattr(msg, "request_kind", "") or decision.get("request_kind") or input_profile.get("request_kind") or "question"
    response_mode = getattr(msg, "response_mode", "") or decision.get("response_mode") or input_profile.get("response_mode") or "tool"
    primary_class = input_profile.get("primary_class") or input_profile.get("top_level_class") or ""
    lower_text = (msg.text or "").lower()
    cluster_query = any(marker in lower_text for marker in (
        "owner-review cluster",
        "owner review cluster",
        "cluster packet",
        "batch-close cluster",
        "batch close cluster",
        "cluster close",
        "cluster_id",
        "cluster_key",
        "cluster member",
    )) or ("cluster" in lower_text and ("owner" in lower_text or "review" in lower_text))
    audit_query = any(marker in lower_text for marker in (
        "audit", "manual work", "manual gap", "taxonomy update", "capability family",
        "what can't you do", "what can you not do", "what you cannot do", "cannot do itself",
    ))

    tool_rows = []
    for name in sorted(TOOLS):
        entry = TOOLS.get(name)
        desc = ""
        if isinstance(entry, dict):
            desc = str(entry.get("desc") or entry.get("description") or "")
        tool_rows.append((name, desc, _tool_family_for_name(name, desc)))

    family_map: Dict[str, List[str]] = {}
    for name, desc, family in tool_rows:
        family_map.setdefault(family, []).append(name)

    registry_names = [name for name, _, _ in tool_rows]
    registry_signature = hashlib.sha1("\n".join(registry_names).encode("utf-8")).hexdigest()[:12]
    family_counts = {family: len(names) for family, names in sorted(family_map.items())}

    gate_tools = list(dict.fromkeys((evidence_gate or {}).get("preferred_tools", []) or []))
    task_tools = []
    if isinstance(task_mode_packet, dict):
        task_tools = list(dict.fromkeys(task_mode_packet.get("preferred_tools", []) or []))
    for tool in task_tools:
        if tool not in gate_tools:
            gate_tools.append(tool)
    gate_families = [_tool_family_for_name(tool) for tool in gate_tools]
    gate_families = [family for family in gate_families if family != "other"]

    preferred_families: List[str] = []
    work_intent = task_mode_packet.get("work_intent") if isinstance(task_mode_packet, dict) else ""
    work_subintent = task_mode_packet.get("work_subintent") if isinstance(task_mode_packet, dict) else ""
    work_detail = task_mode_packet.get("work_detail_intents") if isinstance(task_mode_packet, dict) else []
    codeish = bool(any(k in lower_text for k in ("codebase", " repo ", "diff", "commit", " patch ", "function", "module", "bug", "stack trace", ".py", " file ")))
    if work_intent in {"analyze", "inspect", "create", "transform"} and codeish:
        preferred_families.extend(["repo_code", "state", "knowledge"])
    if work_intent in {"analyze", "transform", "create", "inspect", "operate", "research", "coordinate", "learn", "decide", "clarify", "interrupt"}:
        if work_intent == "analyze":
            if work_subintent in {"spreadsheet_analysis", "document_analysis", "presentation_creation", "spreadsheet_creation", "document_creation"}:
                preferred_families.extend(["document", "knowledge", "state"])
            elif work_subintent in {"code_analysis", "incident_analysis"}:
                preferred_families.extend(["repo_code", "state", "knowledge"])
            else:
                preferred_families.extend(["knowledge", "state", "document"])
        elif work_intent == "transform":
            preferred_families.extend(["document", "knowledge", "state"])
        elif work_intent == "create":
            if work_subintent in {"presentation_creation", "document_creation", "spreadsheet_creation"}:
                preferred_families.extend(["document", "state", "knowledge"])
            elif work_subintent == "code_creation":
                preferred_families.extend(["repo_code", "state", "knowledge"])
            else:
                preferred_families.extend(["document", "knowledge", "state"])
        elif work_intent == "inspect":
            if work_subintent in {"review", "audit", "test"}:
                preferred_families.extend(["review", "repo_code", "state"])
            else:
                preferred_families.extend(["repo_code", "document", "state"])
        elif work_intent == "operate":
            preferred_families.extend(["state", "deploy", "repo_code"])
        elif work_intent == "research":
            preferred_families.extend(["knowledge", "web", "state"])
        elif work_intent == "coordinate":
            preferred_families.extend(["review", "task", "state"])
        elif work_intent == "learn":
            preferred_families.extend(["knowledge", "state", "review"])
        elif work_intent == "decide":
            preferred_families.extend(["review", "knowledge", "state"])
        elif work_intent == "clarify":
            preferred_families.extend(["state", "knowledge"])
        elif work_intent == "interrupt":
            preferred_families.extend(["state"])

    if request_kind in {"status", "self_assessment"}:
        has_code_evidence = bool(
            evidence_gate.get("code_targets")
            or evidence_gate.get("repo_map_needed")
            or codeish
            or any(k in (msg.text or "").lower() for k in ("code", "repo", "file", "commit", "git", "patch"))
        )
        if has_code_evidence:
            preferred_families = ["repo_code", "state", "knowledge"] + preferred_families
        else:
            preferred_families = ["state", "knowledge"] + [fam for fam in preferred_families if fam != "repo_code"]
    elif request_kind in {"debug"}:
        preferred_families.extend(["repo_code", "state", "knowledge"])
    elif request_kind in {"owner_review"}:
        preferred_families.extend(["review", "repo_code", "knowledge", "state"])
    elif request_kind in {"task"}:
        preferred_families.extend(["task", "repo_code", "state", "knowledge"])
    elif request_kind in {"conversation"}:
        preferred_families.extend(["state", "knowledge", "utility"])
    else:
        preferred_families.extend(["knowledge", "state", "repo_code"])

    if evidence_gate.get("public_research_needed"):
        if evidence_gate.get("repo_map_needed") or codeish:
            preferred_families = preferred_families + ["knowledge", "web"]
        else:
            preferred_families = [fam for fam in preferred_families if fam != "repo_code"]
            if request_kind in {"status", "self_assessment"}:
                preferred_families = [fam for fam in preferred_families if fam != "task"]
            preferred_families += ["knowledge", "web"]

    # Keep ordering stable and dedupe.
    preferred_families = [fam for i, fam in enumerate(preferred_families) if fam and fam not in preferred_families[:i]]

    preferred_tools: List[str] = []
    for tool in gate_tools:
        if tool in registry_names and tool not in preferred_tools:
            preferred_tools.append(tool)
    for family in preferred_families:
        preferred_tools.extend(family_map.get(family, [])[:4])
    for tool in gate_tools:
        if tool in registry_names and tool not in preferred_tools:
            preferred_tools.append(tool)

    avoid_first = ["file_list", "shell", "list_tools"]
    if request_kind in {"status", "self_assessment"} and "repo_map_status" in registry_names:
        avoid_first = ["file_list", "shell", "list_tools", "web_search"]
    if request_kind in {"debug", "owner_review"}:
        avoid_first = ["file_list", "shell", "list_tools", "web_search"]

    family_examples = {family: names[:5] for family, names in family_map.items() if names}
    best_fit_family = preferred_families[0] if preferred_families else "other"
    best_first_tool = ""
    best_first_args: Dict[str, Any] = {}
    best_first_reason = ""

    query = _safe_text((msg.text or ""), 240)
    if audit_query and "core_gap_audit" in registry_names:
        best_first_tool = "core_gap_audit"
        best_first_args = {"force": False, "notify_owner": True}
        best_first_reason = "audit and gap queries should start from the dedicated CORE-wide manual work audit."
    if cluster_query:
        if "owner_review_cluster_packet" in registry_names:
            best_first_tool = "owner_review_cluster_packet"
            best_first_args = {"query": query, "limit": 8}
            best_first_reason = "owner-review cluster inspection is the safest first action."
    elif work_intent == "inspect" and work_subintent in {"review", "audit", "test"}:
        if "repo_review_packet" in registry_names and (evidence_gate.get("code_targets") or any(k in lower_text for k in ("code", "repo", "diff", "commit", "patch", ".py"))):
            best_first_tool = "repo_review_packet"
            best_first_args = {"content": query, "goal": query, "focus": "repo_diff"}
            best_first_reason = "repo review should start with a specialized review packet."
        elif "repo_component_packet" in registry_names and (evidence_gate.get("code_targets") or any(k in lower_text for k in ("code", "repo", "diff", "commit", "patch", ".py"))):
            best_first_tool = "repo_component_packet"
            best_first_args = {"query": query, "limit": 8}
            best_first_reason = "code review should start from the repository map, not the owner-review queue."
        elif "code_read_packet" in registry_names and any(k in lower_text for k in ("code", "repo", "diff", "patch")):
            best_first_tool = "code_read_packet"
            best_first_args = {"query": query}
            best_first_reason = "code review should start by reading the relevant code packet."
        elif "document_review_packet" in registry_names and any(k in lower_text for k in ("doc", "document", "proposal", "report", "memo", "brief", "pdf", "notes")):
            best_first_tool = "document_review_packet"
            best_first_args = {"content": query, "goal": query, "focus": "document_quality"}
            best_first_reason = "document review should start with a specialized review packet."
        elif "spreadsheet_review_packet" in registry_names and any(k in lower_text for k in ("sheet", "spreadsheet", "excel", "csv", "table")):
            best_first_tool = "spreadsheet_review_packet"
            best_first_args = {"content": query, "goal": query, "focus": "spreadsheet_quality"}
            best_first_reason = "spreadsheet review should start with a specialized review packet."
        elif "presentation_review_packet" in registry_names and any(k in lower_text for k in ("slide", "deck", "presentation", "speaker notes")):
            best_first_tool = "presentation_review_packet"
            best_first_args = {"content": query, "goal": query, "focus": "presentation_quality"}
            best_first_reason = "presentation review should start with a specialized review packet."
        elif "document_work_packet" in registry_names:
            best_first_tool = "document_work_packet"
            best_first_args = {"content": query, "goal": query}
            best_first_reason = "document review should start with a document work packet."
    elif work_intent == "analyze" and work_subintent == "spreadsheet_analysis":
        if "spreadsheet_work_packet" in registry_names:
            best_first_tool = "spreadsheet_work_packet"
            best_first_args = {"content": query, "goal": query}
            best_first_reason = "spreadsheet analysis should start with a spreadsheet work packet."
        elif "read_document" in registry_names:
            best_first_tool = "read_document"
            best_first_args = {"base64_content": "", "filename": query, "format": "xlsx"}
            best_first_reason = "spreadsheet analysis should start by extracting document text if available."
    elif work_intent == "create" and work_subintent == "presentation_creation":
        if "presentation_work_packet" in registry_names:
            best_first_tool = "presentation_work_packet"
            best_first_args = {"content": query, "goal": query}
            best_first_reason = "presentation creation should start with a presentation work packet."
        elif "create_presentation" in registry_names:
            best_first_tool = "create_presentation"
            best_first_args = {"slides": "[]"}
            best_first_reason = "presentation creation should start with the presentation builder."
    elif work_intent in {"analyze", "transform", "create"} and work_subintent in {"document_analysis", "document_creation"}:
        if "document_work_packet" in registry_names:
            best_first_tool = "document_work_packet"
            best_first_args = {"content": query, "goal": query}
            best_first_reason = "document work should start with a document work packet."
        elif "read_document" in registry_names:
            best_first_tool = "read_document"
            best_first_args = {"base64_content": "", "filename": query, "format": "md"}
            best_first_reason = "document work should start with read_document when source text exists."
    elif request_kind in {"debug", "status"} and (evidence_gate.get("repo_map_needed") or evidence_gate.get("code_targets") or any(k in lower_text for k in ("code", "repo", "file", "commit", "git", "patch", ".py"))):
        if "repo_component_packet" in registry_names:
            best_first_tool = "repo_component_packet"
            best_first_args = {"query": query, "limit": 8}
            best_first_reason = "repo/component evidence is the best first grounding for code/status checks."
    elif request_kind in {"task"} and (evidence_gate.get("repo_map_needed") or evidence_gate.get("code_targets") or any(k in lower_text for k in ("code", "repo", "file", "commit", "git", "patch", ".py"))):
        if "repo_component_packet" in registry_names:
            best_first_tool = "repo_component_packet"
            best_first_args = {"query": query, "limit": 8}
            best_first_reason = "task investigation should start from the repo map before generic discovery."
    elif best_fit_family == "state":
        if "get_state" in registry_names:
            best_first_tool = "get_state"
            best_first_args = {}
            best_first_reason = "state queries should start from live CORE state."
    elif best_fit_family == "knowledge":
        if "search_kb" in registry_names:
            best_first_tool = "search_kb"
            best_first_args = {"query": query, "limit": 5}
            best_first_reason = "knowledge queries should start from the KB."
    elif best_fit_family == "document":
        if "document_work_packet" in registry_names and work_intent in {"analyze", "create", "transform", "inspect"}:
            best_first_tool = "document_work_packet"
            best_first_args = {"content": query, "goal": query}
            best_first_reason = "document-heavy work should start from the document work packet."
        elif "task_mode_packet" in registry_names and work_intent and work_intent in {"analyze", "create", "transform", "inspect"}:
            best_first_tool = "task_mode_packet"
            best_first_args = {"text": query, "goal": query, "artifact_hint": work_subintent or "document"}
            best_first_reason = "document-heavy work should first classify the task mode and then use the right artifact tool."
        elif "read_document" in registry_names:
            best_first_tool = "read_document"
            best_first_args = {"base64_content": "", "filename": query, "format": "md"}
            best_first_reason = "document-heavy work should start with document extraction if available."
    elif best_fit_family == "web" and "web_search" in registry_names:
        best_first_tool = "web_search"
        best_first_args = {"query": query, "max_results": 5}
        best_first_reason = "public questions should start from web research."
    elif best_fit_family == "task" and "repo_component_packet" in registry_names:
        best_first_tool = "repo_component_packet"
        best_first_args = {"query": query, "limit": 8}
        best_first_reason = "task work should start from repo evidence."
    if not best_first_tool and gate_tools:
        best_first_tool = gate_tools[0]
        best_first_args = {}
        best_first_reason = "preferred by current evidence gate."
    growth_hint = "updates automatically when TOOLS changes"

    return {
        "ok": True,
        "registry_size": len(registry_names),
        "registry_signature": registry_signature,
        "registry_sample": registry_names[:24],
        "family_counts": family_counts,
        "family_examples": family_examples,
        "request_kind": request_kind,
        "response_mode": response_mode,
        "primary_class": primary_class,
        "task_mode_packet": task_mode_packet,
        "evidence_gate_mode": evidence_gate.get("retrieval_mode", ""),
        "preferred_families": preferred_families,
        "preferred_tools": preferred_tools[:16],
        "avoid_first": avoid_first,
        "best_fit_family": best_fit_family,
        "best_first_tool": best_first_tool,
        "best_first_args": best_first_args,
        "best_first_reason": best_first_reason,
        "gate_families": gate_families,
        "growth_hint": growth_hint,
        "capability_summary": (
            f"{len(registry_names)} live tools across {len(family_map)} capability families. "
            f"Best fit family: {best_fit_family}. Registry signature: {registry_signature}."
        ),
        "timestamp": datetime.utcnow().isoformat(),
    }


def _count_table(table: str, where: str = "") -> int:
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?select=id&limit=1"
        if where:
            url += f"&{where}"
        r = httpx.get(url, headers=_sbh_count_svc(), timeout=10)
        cr = r.headers.get("content-range", "*/0")
        return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return -1


# ── Request profile ──────────────────────────────────────────────────────────
def classify_request_kind(
    text: str,
    command: str = "",
    message_type: str = "message",
    route: str = "conversation",
    intent: str | None = None,
) -> str:
    t = (text or "").lower()
    cmd = (command or "").lower()

    if cmd in {"/health", "/status", "/state"} or any(k in t for k in ("health", "status", "system state", "system health")):
        return "status"
    if any(k in t for k in (
        "verify the current git commit",
        "verify git commit",
        "git status",
        "cleanliness of",
        "synced with github",
        "synced with git",
        "current git commit",
        "current commit",
    )):
        return "status"
    if any(k in t for k in ("how advanced", "capability", "capabilities", "what can you do", "strengths", "weaknesses", "limitations")):
        return "self_assessment"
    if t.strip().startswith((
        "what is ",
        "what are ",
        "what's ",
        "how is ",
        "how are ",
        "why is ",
        "why are ",
        "where is ",
        "where are ",
        "when is ",
        "when are ",
        "who is ",
        "who are ",
        "which is ",
        "which are ",
    )):
        return "question"
    if cmd in {"/review"} or any(k in t for k in (
        "review queue", "owner review", "proposal queue", "owner only",
        "owner queue", "batch close", "cluster close", "close cluster",
        "review cluster", "cluster packet", "owner-review cluster",
        "manual queue", "proposal review",
    )):
        return "owner_review"
    if any(k in t for k in ("debug", "bug", "error", "broken", "crash", "stack trace")):
        return "debug"
    if any(k in t for k in (
        "analyze", "analyse", "inspect", "compare", "diagnose", "explain", "understand",
        "summarize", "summary", "rewrite", "convert", "normalize", "extract",
        "make ", "make a", "build ", "create ", "draft ", "design ", "prepare ", "outline ", "generate ",
        "review", "audit", "validate", "test", "verify", "check",
        "deploy", "restart", "sync", "monitor", "recover", "status",
        "research", "triage", "batch", "cluster", "delegate", "prioritize",
        "learn", "improve", "update rules", "memory", "pattern",
    )):
        return "task"
    if intent in ("task_execution",):
        return "task"
    if intent in ("conversation", "greeting"):
        return "conversation"
    if route == "command":
        return "command"
    return "question"


def initial_request_profile(msg) -> Dict[str, Any]:
    cmd = msg.context.get("command", "") if hasattr(msg, "context") else ""
    input_profile = classify_human_input(
        msg.text,
        command=cmd,
        message_type=msg.message_type,
        route=msg.route,
        attachments=getattr(msg, "attachments", []),
    )
    task_mode_packet = build_task_mode_packet(
        text=msg.text,
        goal=msg.text,
        source=getattr(msg, "source", ""),
        message_type=getattr(msg, "message_type", "message"),
        route=getattr(msg, "route", "conversation"),
        attachments=getattr(msg, "attachments", []),
        input_profile=input_profile,
    )
    return {
        "request_kind": input_profile.get("request_kind", "question"),
        "response_mode": input_profile.get("response_mode", "tool"),
        "route_reason": "initial_profile",
        "clarification_needed": bool(input_profile.get("requires_clarification", False)),
        "input_profile": input_profile,
        "task_mode_packet": task_mode_packet,
        "speech_act_packet": {
            "top_level_class": input_profile.get("top_level_class", "ask"),
            "primary_class": input_profile.get("primary_class", "ask"),
            "secondary_classes": input_profile.get("secondary_classes", []),
            "speech_acts": input_profile.get("speech_acts", []),
            "multi_label": input_profile.get("multi_label", False),
            "confidence": input_profile.get("confidence", 0.0),
            "route_hint": input_profile.get("route_hint", "clarify"),
            "urgency": input_profile.get("urgency", "normal"),
            "actionability": input_profile.get("actionability", "contextual"),
        },
    }


def build_decision_packet(msg) -> Dict[str, Any]:
    classification = msg.context.get("intent_classification", {}) if hasattr(msg, "context") else {}
    input_profile = msg.context.get("input_profile", {}) if hasattr(msg, "context") else {}
    task_mode_packet = msg.context.get("task_mode_packet", {}) if hasattr(msg, "context") else {}
    intent = classification.get("intent") or msg.intent or "general_query"
    confidence = float(classification.get("confidence", 0.0) or 0.0)
    cmd = msg.context.get("command", "") if hasattr(msg, "context") else ""
    primary_class = input_profile.get("primary_class") or input_profile.get("top_level_class") or ""
    missing_targets = list(input_profile.get("missing_targets", []) or [])

    request_kind = input_profile.get("request_kind") or classify_request_kind(
        msg.text,
        command=cmd,
        message_type=msg.message_type,
        route=msg.route,
        intent=intent,
    )

    response_mode = {
        "status": "status",
        "self_assessment": "capability",
        "owner_review": "review",
        "debug": "debug",
        "task": "task",
        "conversation": "conversation",
    }.get(request_kind, "tool")

    if isinstance(task_mode_packet, dict) and task_mode_packet.get("work_intent"):
        work_intent = task_mode_packet.get("work_intent")
        if work_intent in {"analyze", "transform", "create", "inspect", "operate", "coordinate", "learn", "decide"}:
            if request_kind in {"question", "conversation", "general_query", "command"}:
                if not (
                    work_intent == "analyze"
                    and task_mode_packet.get("work_subintent") == "semantic_analysis"
                    and not task_mode_packet.get("agentic_recommended")
                ):
                    request_kind = "task" if work_intent not in {"inspect"} else "owner_review"
                    if response_mode in {"tool", "conversation"}:
                        response_mode = "task"

    if isinstance(task_mode_packet, dict) and task_mode_packet.get("needs_clarification"):
        if request_kind in {"question", "conversation", "general_query"}:
            request_kind = "task" if task_mode_packet.get("work_intent") not in {"clarify", "interrupt"} else request_kind
        response_mode = "clarify" if task_mode_packet.get("work_intent") == "clarify" else response_mode

    explicit_agentic = any(
        trigger in (msg.text or "").lower()
        for trigger in (
            "step by step",
            "keep going",
            "until",
            "comprehensive",
            "full analysis",
            "iterate",
            "repeat until",
            "scan all",
            "work until",
            "multi-step",
            "multi step",
            "deep dive",
            "investigate",
            "research",
        )
    )

    clarification_needed = bool(input_profile.get("requires_clarification", False)) and not explicit_agentic
    clarification_prompt = ""
    if clarification_needed:
        clarification_prompt = "I need a bit more detail to proceed. Give me the exact target, file, URL, commit, or outcome."
    elif request_kind in {"question", "conversation", "general_query"} and confidence < 0.45 and not explicit_agentic:
        clarification_needed = True
        clarification_prompt = "I need a bit more detail. What exactly should I do, and what is the expected outcome?"
    elif missing_targets:
        clarification_needed = True
        clarification_prompt = (
            "I could not find the file or path you referenced. "
            "Send the correct file path, repo path, URL, commit hash, or upload the missing file."
        )

    hard_non_agentic = {"interrupt", "correct", "constrain", "inform"}
    agentic_hint = False
    if primary_class not in hard_non_agentic and (request_kind in ("task", "owner_review") or explicit_agentic):
        lower = (msg.text or "").lower()
        if explicit_agentic:
            agentic_hint = True
        if not agentic_hint and bool(input_profile.get("multi_label", False)) and input_profile.get("actionability") == "actionable":
            agentic_hint = len(msg.text or "") > 160
        if not agentic_hint and len(msg.text or "") > 240 and primary_class in {"act", "evaluate", "ask"}:
            agentic_hint = True

    if explicit_agentic and request_kind in {"question", "conversation", "general_query", "command"} and primary_class in {"act", "ask", "evaluate", "meta"}:
        request_kind = "task"
        response_mode = "agentic"
        clarification_needed = False
        clarification_prompt = ""

    if isinstance(task_mode_packet, dict) and task_mode_packet.get("agentic_recommended"):
        if task_mode_packet.get("work_intent") not in {"clarify", "interrupt"}:
            if response_mode in {"tool", "task", "conversation"} or request_kind == "task":
                response_mode = "agentic"

    tool_policy_packet = build_tool_policy_packet(msg)

    return {
        "request_kind": request_kind,
        "response_mode": response_mode,
        "route_reason": "decision_packet",
        "clarification_needed": clarification_needed,
        "clarification_prompt": clarification_prompt,
        "agentic_hint": agentic_hint,
        "intent": intent,
        "confidence": confidence,
        "requires_tools": bool(classification.get("requires_tools", False)),
        "domain": classification.get("domain") or msg.context.get("current_domain", "general"),
        "command": cmd,
        "input_profile": input_profile,
        "primary_class": primary_class,
        "task_mode_packet": task_mode_packet if isinstance(task_mode_packet, dict) else {},
        "tool_policy_packet": tool_policy_packet,
        "response_style_packet": build_response_style_packet(
            msg,
            request_kind=request_kind,
            primary_class=primary_class,
            agentic_hint=agentic_hint,
        ),
    }


def build_response_style_packet(
    msg,
    request_kind: str = "",
    primary_class: str = "",
    agentic_hint: bool = False,
) -> Dict[str, Any]:
    """Turn structured human input into output-shaping instructions for L9/L10."""
    ctx = msg.context if hasattr(msg, "context") else {}
    input_profile = ctx.get("input_profile", {}) or {}
    task_mode_packet = ctx.get("task_mode_packet", {}) or {}
    decision = ctx.get("decision_packet", {}) or {}
    delivery_channel = getattr(msg, "source", "telegram") or "telegram"
    request_kind = request_kind or decision.get("request_kind") or input_profile.get("request_kind") or "question"
    primary_class = primary_class or input_profile.get("primary_class") or input_profile.get("top_level_class") or ""
    work_intent = task_mode_packet.get("work_intent", "")
    work_subintent = task_mode_packet.get("work_subintent", "")
    explicit_agentic = bool(agentic_hint or decision.get("agentic_hint", False))
    if primary_class not in {"interrupt", "correct", "constrain", "inform"}:
        if not explicit_agentic and request_kind in {"task", "owner_review", "command"}:
            explicit_agentic = bool(input_profile.get("multi_label", False) and input_profile.get("actionability") == "actionable")

    mode = "answer"
    lead = "direct_answer"
    verbosity = "medium"
    structure: list[str] = ["answer_first"]
    tone = "direct"
    use_html = delivery_channel == "telegram"
    must_include: list[str] = []
    must_avoid: list[str] = ["guessing", "filler", "hedging"]
    channel_notes: list[str] = []

    if request_kind in {"status", "self_assessment"}:
        mode = "capability"
        lead = "capability_summary"
        verbosity = "medium"
        structure = ["direct_answer", "strengths", "gaps", "confidence"]
        must_include = ["current capability", "strengths", "gaps", "what is safe to trust"]
    elif work_intent == "analyze":
        mode = "analysis"
        lead = "evidence_first"
        verbosity = "medium"
        structure = ["findings", "evidence", "interpretation", "next_steps"]
        must_include = ["evidence", "findings", "interpretation", "next steps"]
        if work_subintent in {"spreadsheet_analysis", "document_analysis", "code_analysis", "incident_analysis"}:
            must_include.append("artifact-specific details")
    elif work_intent == "transform":
        mode = "transform"
        lead = "transformed_output"
        verbosity = "medium"
        structure = ["transformed_output", "key_changes", "verification"]
        must_include = ["what changed", "verification"]
    elif work_intent == "create":
        mode = "deliverable"
        lead = "deliverable_first"
        verbosity = "medium"
        structure = ["deliverable", "decisions", "next_steps", "verification"]
        must_include = ["deliverable", "verification", "next steps"]
    elif work_intent == "inspect":
        mode = "review"
        lead = "verdict"
        verbosity = "short"
        structure = ["verdict", "evidence", "gaps", "next_action"]
        must_include = ["verdict", "evidence", "next action"]
    elif work_intent == "operate":
        mode = "ops"
        lead = "state_first"
        verbosity = "short"
        structure = ["state", "action", "verification"]
        must_include = ["state", "action", "verification"]
    elif work_intent == "research":
        if request_kind in {"question", "conversation", "general_query"} and not explicit_agentic:
            mode = "answer"
            lead = "evidence_summary"
            verbosity = "medium"
            structure = ["answer_first", "sources", "uncertainty", "next_steps"]
            must_include = ["sources", "uncertainty"]
        else:
            mode = "research"
            lead = "evidence_summary"
            verbosity = "medium"
            structure = ["sources", "synthesis", "uncertainty", "next_steps"]
            must_include = ["sources", "synthesis", "uncertainty"]
    elif work_intent == "coordinate":
        mode = "coordination"
        lead = "cluster_summary"
        verbosity = "short"
        structure = ["clusters", "routing", "next_actions"]
        must_include = ["cluster", "routing", "next actions"]
    elif work_intent == "learn":
        mode = "learning"
        lead = "pattern_summary"
        verbosity = "medium"
        structure = ["pattern", "lesson", "policy_update"]
        must_include = ["pattern", "lesson", "policy update"]
    elif work_intent == "decide":
        mode = "decision"
        lead = "recommendation"
        verbosity = "short"
        structure = ["recommendation", "rationale", "tradeoffs"]
        must_include = ["recommendation", "tradeoffs"]
    elif request_kind in {"owner_review"} or primary_class == "evaluate":
        mode = "review"
        lead = "verdict"
        verbosity = "short"
        structure = ["verdict", "reason", "next_action"]
        must_include = ["verdict first", "short reasons", "next step"]
    elif request_kind in {"debug"} or primary_class == "correct":
        mode = "debug"
        lead = "root_cause"
        verbosity = "medium"
        structure = ["root_cause", "evidence", "fix_path"]
        must_include = ["what failed", "why", "fix path", "evidence"]
    elif request_kind in {"task"} or primary_class == "act":
        mode = "task"
        lead = "action_summary"
        verbosity = "medium"
        structure = ["what_was_done", "what_next", "blockers", "verification"]
        must_include = ["what was done", "verification", "blockers"]
    elif primary_class == "interrupt":
        mode = "interrupt"
        lead = "acknowledge_stop"
        verbosity = "short"
        structure = ["acknowledge", "stop_state", "next_step"]
        must_include = ["acknowledge the stop", "state current status"]
    elif primary_class == "approve":
        mode = "approval"
        lead = "acknowledge_approval"
        verbosity = "short"
        structure = ["acknowledge", "apply_next"]
        must_include = ["acknowledge approval", "next step"]
    elif primary_class == "constrain":
        mode = "constraints"
        lead = "constraints_summary"
        verbosity = "short"
        structure = ["constraints", "impact", "next_action"]
        must_include = ["respect constraints", "restated constraints"]
    elif primary_class == "inform":
        mode = "inform"
        lead = "store_and_ack"
        verbosity = "short"
        structure = ["acknowledge", "store", "impact"]
        must_include = ["acknowledge", "what changes"]
    elif request_kind in {"conversation"}:
        mode = "conversation"
        lead = "answer_first"
        verbosity = "medium"
        structure = ["answer_first", "context", "follow_up"]
        must_include = ["direct answer", "minimal filler"]

    if delivery_channel == "telegram":
        channel_notes = [
            "write for a human in chat",
            "prefer short paragraphs and bullets when useful",
            "keep it concise but complete",
            "use HTML only when it materially improves readability",
            "avoid raw JSON unless explicitly requested",
        ]
        if mode in {"review", "interrupt"}:
            verbosity = "short"
    elif delivery_channel == "mcp":
        channel_notes = [
            "write for a machine/desktop caller",
            "prefer explicit structure over chatty prose",
            "include sections and exact values when relevant",
            "avoid Telegram-style phrasing and emojis",
            "keep the same evidence, but phrase it more formally",
        ]
        use_html = False
        if mode in {"conversation", "answer"}:
            verbosity = "medium"
            structure = ["direct_answer", "evidence", "next_step"]
        if mode in {"review", "debug", "task", "capability"}:
            structure = ["summary", "evidence", "action", "risk"] if mode != "interrupt" else structure

    if explicit_agentic:
        if mode not in {"capability", "interrupt"}:
            mode = "agentic"
            if "steps" not in structure:
                if mode == "agentic":
                    structure = ["answer_first", "evidence", "steps"] if structure == ["answer_first"] else structure + ["steps"]
        if verbosity == "short":
            verbosity = "medium"

    return {
        "mode": mode,
        "lead": lead,
        "verbosity": verbosity,
        "structure": structure,
        "tone": tone,
        "use_html": use_html,
        "must_include": must_include,
        "must_avoid": must_avoid,
        "channel_notes": channel_notes,
        "delivery_channel": delivery_channel,
        "explicit_agentic": explicit_agentic,
        "input_class": primary_class,
        "request_kind": request_kind,
    }


def should_use_agentic_mode(msg) -> bool:
    """Hard gate for agentic escalation based on structured input."""
    decision = msg.context.get("decision_packet", {}) if hasattr(msg, "context") else {}
    input_profile = msg.context.get("input_profile", {}) if hasattr(msg, "context") else {}
    task_mode_packet = msg.context.get("task_mode_packet", {}) if hasattr(msg, "context") else {}
    primary_class = input_profile.get("primary_class") or input_profile.get("top_level_class") or ""
    request_kind = decision.get("request_kind") or input_profile.get("request_kind") or classify_request_kind(
        msg.text,
        command=(msg.context.get("command", "") if hasattr(msg, "context") else ""),
        message_type=getattr(msg, "message_type", "message"),
        route=getattr(msg, "route", "conversation"),
        intent=getattr(msg, "intent", None),
    )

    # Pure control / correction / acknowledgment inputs should not escalate.
    if primary_class in {"interrupt", "correct", "constrain", "inform"}:
        return False
    if request_kind in {"owner_review"}:
        return False
    if input_profile.get("route_hint") == "stop":
        return False
    if isinstance(task_mode_packet, dict) and task_mode_packet.get("execution_mode") == "agentic":
        if task_mode_packet.get("work_intent") not in {"clarify", "interrupt"}:
            return True

    text = (msg.text or "").lower()
    explicit_agentic = any(
        trigger in text
        for trigger in (
            "step by step",
            "keep going",
            "until",
            "comprehensive",
            "full analysis",
            "iterate",
            "repeat until",
            "scan all",
            "work until",
            "multi-step",
            "multi step",
            "deep dive",
            "investigate",
            "research",
        )
    )

    if explicit_agentic and primary_class not in {"interrupt", "correct", "constrain", "inform"}:
        return True

    if request_kind in {"status", "self_assessment", "debug", "conversation"}:
        return False

    if request_kind in {"task", "owner_review", "command"}:
        if explicit_agentic:
            return True
        if bool(decision.get("agentic_hint", False)):
            return True
        if bool(input_profile.get("multi_label", False)) and input_profile.get("actionability") == "actionable":
            return len(msg.text or "") > 180
        return False

    return explicit_agentic and bool(decision.get("agentic_hint", False))


# ── Evidence / capability packets ────────────────────────────────────────────
def build_evidence_packet(msg) -> Dict[str, Any]:
    ctx = msg.context or {}
    packet = {
        "request": {
            "text": _safe_text(msg.text, 800),
            "intent": msg.intent,
            "request_kind": getattr(msg, "request_kind", ""),
            "response_mode": getattr(msg, "response_mode", ""),
            "source": msg.source,
            "message_type": msg.message_type,
            "route": msg.route,
            "input_profile": ctx.get("input_profile", {}),
            "speech_act_packet": ctx.get("speech_act_packet", {}),
            "task_mode_packet": ctx.get("task_mode_packet", {}),
        },
        "domain": ctx.get("current_domain", "general"),
        "session": ctx.get("session", {}),
        "behavioral_rules": ctx.get("behavioral_rules", [])[:10],
        "domain_mistakes": ctx.get("domain_mistakes", [])[:10],
        "kb_snippets": ctx.get("kb_snippets", [])[:10],
        "conversation_history": ctx.get("conversation_history", [])[-10:],
        "health": ctx.get("health", {}),
        "timestamp": datetime.utcnow().isoformat(),
    }
    try:
        from core_reasoning_packet import build_reasoning_packet
        if msg.text and len(msg.text.strip()) > 2:
            sem = build_reasoning_packet(msg.text, domain=ctx.get("current_domain", "general"))
            if isinstance(sem, dict) and sem.get("ok"):
                packet["semantic"] = sem.get("packet", {})
                repo_counts = (packet["semantic"].get("memory_by_table") or {})
                if repo_counts:
                    packet["repo_map"] = {
                        "counts": {
                            "repo_components": repo_counts.get("repo_components", 0),
                            "repo_component_chunks": repo_counts.get("repo_component_chunks", 0),
                            "repo_component_edges": repo_counts.get("repo_component_edges", 0),
                        },
                        "focus": packet["semantic"].get("focus", ""),
                    }
    except Exception:
        pass
    return packet


def build_capability_packet(msg) -> Dict[str, Any]:
    # Counts
    counts = {
        "knowledge_base": _count_table("knowledge_base"),
        "mistakes": _count_table("mistakes"),
        "sessions": _count_table("sessions"),
        "task_pending": _count_table("task_queue", "status=eq.pending"),
        "task_in_progress": _count_table("task_queue", "status=eq.in_progress"),
        "task_done": _count_table("task_queue", "status=eq.done"),
        "task_failed": _count_table("task_queue", "status=eq.failed"),
        "evo_pending": _count_table("evolution_queue", "status=eq.pending"),
        "evo_applied": _count_table("evolution_queue", "status=eq.applied"),
        "evo_rejected": _count_table("evolution_queue", "status=eq.rejected"),
        "repo_components": _count_table("repo_components", "active=eq.true"),
        "repo_component_chunks": _count_table("repo_component_chunks", "active=eq.true"),
        "repo_component_edges": _count_table("repo_component_edges", "active=eq.true"),
        "repo_scan_runs": _count_table("repo_scan_runs"),
    }
    owner_only = _count_table("evolution_queue", "status=eq.pending&review_scope=eq.owner_only")
    if owner_only >= 0:
        counts["owner_review_pending"] = owner_only

    workers = {}
    try:
        from core_task_autonomy import autonomy_status
        workers["task_autonomy"] = autonomy_status()
    except Exception:
        pass
    try:
        from core_research_autonomy import research_autonomy_status
        workers["research_autonomy"] = research_autonomy_status()
    except Exception:
        pass
    try:
        from core_code_autonomy import code_autonomy_status
        workers["code_autonomy"] = code_autonomy_status()
    except Exception:
        pass
    try:
        from core_integration_autonomy import integration_autonomy_status
        workers["integration_autonomy"] = integration_autonomy_status()
    except Exception:
        pass
    try:
        from core_evolution_autonomy import evolution_autonomy_status
        workers["evolution_autonomy"] = evolution_autonomy_status()
    except Exception:
        pass
    try:
        from core_semantic_projection import semantic_projection_status
        workers["semantic_projection"] = semantic_projection_status()
    except Exception:
        pass
    try:
        from core_repo_map import repo_map_status
        workers["repo_map"] = repo_map_status()
    except Exception:
        pass

    strengths = []
    gaps = []
    if counts.get("task_done", 0) >= 0 and counts.get("task_failed", 0) >= 0:
        strengths.append("task worker lane is measurable and continuously reporting")
    if counts.get("evo_applied", 0) >= 0:
        strengths.append("evolution lane is continuously applying approved changes")
    if counts.get("knowledge_base", 0) >= 0:
        strengths.append("core memory stores KB, mistakes, sessions, and reflections")
    if counts.get("repo_components", 0) >= 0:
        strengths.append("repo map tracks file meaning, chunks, and wiring")
    if counts.get("owner_review_pending", 0) and counts.get("owner_review_pending", 0) > 0:
        gaps.append(f"owner review has {counts['owner_review_pending']} pending cluster rows")
    if counts.get("task_pending", 0) and counts.get("task_pending", 0) > 0:
        gaps.append(f"task queue still has {counts['task_pending']} pending rows")
    if counts.get("evo_pending", 0) and counts.get("evo_pending", 0) > 0:
        gaps.append(f"evolution queue still has {counts['evo_pending']} pending rows")

    return {
        "counts": counts,
        "workers": workers,
        "headline": (
            "CORE has live orchestrator coverage across task, research, code, integration, evolution, "
            "and semantic projection lanes."
        ),
        "strengths": strengths,
        "gaps": gaps,
        "timestamp": datetime.utcnow().isoformat(),
    }


def build_evidence_gate(msg) -> Dict[str, Any]:
    """Codex-style gate: prefer evidence, then external search, then clarification."""
    ctx = msg.context or {}
    evidence = ctx.get("evidence_packet", {}) or {}
    decision = ctx.get("decision_packet", {}) or {}
    request_kind = getattr(msg, "request_kind", "") or decision.get("request_kind", "")
    intent = getattr(msg, "intent", "") or decision.get("intent", "")
    text = msg.text or ""
    if not request_kind:
        request_kind = classify_request_kind(
            text,
            command=ctx.get("command", "") if isinstance(ctx, dict) else "",
            message_type=getattr(msg, "message_type", "message"),
            route=getattr(msg, "route", "conversation"),
            intent=intent or None,
        )

    kb = list(evidence.get("kb_snippets", []) or [])
    rules = list(evidence.get("behavioral_rules", []) or [])
    mistakes = list(evidence.get("domain_mistakes", []) or [])
    session = evidence.get("session", {}) or {}
    semantic = evidence.get("semantic", {}) or {}
    sem_hits = 0
    if isinstance(semantic, dict):
        mem = semantic.get("memory_by_table", {}) or {}
        if isinstance(mem, dict):
            sem_hits = sum(int(v or 0) for v in mem.values() if isinstance(v, (int, float)))
        if semantic.get("results"):
            sem_hits += len(semantic.get("results") or [])

    kb_hits = len(kb)
    rule_hits = len(rules)
    mistake_hits = len(mistakes)
    session_hits = 1 if session else 0
    evidence_score = min(1.0, (kb_hits * 0.18) + (rule_hits * 0.06) + (mistake_hits * 0.05) + (sem_hits * 0.01) + (session_hits * 0.15))

    code_markers = (
        "code", "repo", "repository", "file", "commit", "branch", "diff", "patch", "function",
        "variable", "line", "traceback", "stack trace", "git", "pull", "push", "status", "module",
        "python", ".py", "fix", "refactor", "review", "implement"
    )
    web_markers = (
        "latest", "current", "today", "news", "public", "internet", "web", "docs", "documentation",
        "api", "how to", "what is", "who is", "price", "weather", "search", "look up", "find"
    )
    public_markers = (
        "research", "paper", "arxiv", "study", "benchmark", "official", "docs", "documentation",
        "api", "current", "latest", "news", "public", "internet", "web", "blog", "tutorial",
        "guide", "community", "forum", "reddit", "hackernews", "stackoverflow"
    )
    cluster_markers = (
        "owner-review cluster",
        "owner review cluster",
        "cluster packet",
        "batch-close cluster",
        "batch close cluster",
        "cluster close",
        "cluster_id",
        "cluster_key",
        "cluster member",
    )

    lower_text = (text or "").lower()
    code_hits = _keyword_hits(text, tuple(kw for kw in code_markers if kw != "code"))
    if re.search(r"\bcode\b", lower_text) and "codex" not in lower_text:
        code_hits += 1
    if "codebase" in lower_text:
        code_hits += 1
    web_hits = _keyword_hits(text, web_markers)
    public_hits = _keyword_hits(text, public_markers)
    code_targets = _extract_code_targets(text)
    public_plan = classify_public_evidence(
        query=text,
        domain=ctx.get("current_domain", "general"),
        request_kind=request_kind,
        code_targets=code_targets,
    )
    public_sources = list(public_plan.get("public_sources") or _pick_public_sources(text))
    public_family = public_plan.get("public_family") or "public_general"
    repo_map_needed = bool(code_hits or code_targets or request_kind in {"debug", "owner_review"})
    cluster_query = any(marker in lower_text for marker in cluster_markers) or (
        "cluster" in lower_text and ("owner" in lower_text or "review" in lower_text)
    )
    audit_query = any(marker in lower_text for marker in (
        "audit", "manual work", "manual gap", "taxonomy update", "capability family",
        "what can't you do", "what can you not do", "what you cannot do", "cannot do itself",
    ))

    if request_kind in {"status", "self_assessment"}:
        if code_hits >= 1 or code_targets or web_hits >= 1:
            retrieval_mode = "code_then_web" if web_hits else "code"
            preferred_tools = ["repo_map_status", "repo_component_packet", "repo_graph_packet", "git", "search_in_file", "read_file"]
            if web_hits or evidence_score < 0.25:
                preferred_tools.append("web_search")
        else:
            retrieval_mode = "state_only"
            preferred_tools = []
    elif code_hits >= 1 or code_targets:
        retrieval_mode = "code"
        preferred_tools = ["repo_map_status", "repo_component_packet", "repo_graph_packet", "git", "search_in_file", "read_file"]
        if web_hits or evidence_score < 0.25:
            preferred_tools.append("web_search")
    elif request_kind in {"owner_review", "debug"}:
        retrieval_mode = "supabase_then_web"
        preferred_tools = ["repo_map_status", "repo_component_packet", "search_kb", "web_search"]
        if cluster_query:
            preferred_tools = ["owner_review_cluster_packet", "owner_review_cluster_close"] + preferred_tools
    elif public_hits >= 1 or web_hits >= 1:
        retrieval_mode = "public_research_then_web" if web_hits else "public_research"
        preferred_tools = ["search_kb", "ingest_knowledge", "web_search"]
    else:
        retrieval_mode = "supabase_then_web"
        preferred_tools = ["search_kb", "web_search"]

    if audit_query and "core_gap_audit" in registry_names:
        retrieval_mode = "state_only" if request_kind in {"status", "self_assessment"} else retrieval_mode
        preferred_tools = ["core_gap_audit", "repo_map_status", "get_quality_alert", "get_capability_model", "state_packet"]

    if cluster_query and "owner_review_cluster_packet" not in preferred_tools:
        preferred_tools = ["owner_review_cluster_packet", "owner_review_cluster_close"] + preferred_tools

    needs_retrieval = retrieval_mode != "state_only" and (evidence_score < 0.45 or retrieval_mode in {"code", "code_then_web", "public_research", "public_research_then_web"})

    # Keep the gate strict: if no local evidence and no web intent, clarification is the last resort.
    if request_kind not in {"status", "self_assessment"} and evidence_score < 0.12 and not code_targets and web_hits == 0:
        needs_retrieval = True

    clarification_prompt = (
        "I checked CORE memory and external evidence, but I still do not have enough context. "
        "Upload the missing file, repo path, URL, commit hash, or the exact details you want me to verify."
    )

    explicit_clarification = bool(
        ctx.get("clarification_needed")
        or getattr(msg, "clarification_needed", False)
        or decision.get("clarification_needed", False)
    )

    return {
        "request_kind": request_kind,
        "intent": intent,
        "score": round(evidence_score, 3),
        "state": "rich" if evidence_score >= 0.8 else "moderate" if evidence_score >= 0.45 else "sparse" if evidence_score >= 0.15 else "empty",
        "needs_retrieval": needs_retrieval,
        "retrieval_mode": retrieval_mode,
        "preferred_tools": preferred_tools,
        "search_query": _safe_text(text, 220),
        "code_targets": code_targets,
        "clarification_prompt": clarification_prompt,
        "needs_clarification_after_retrieval": explicit_clarification or (retrieval_mode != "state_only" and evidence_score < 0.25),
        "public_research_needed": bool(public_plan.get("public_research_needed") or public_hits or (web_hits and request_kind not in {"status", "self_assessment"} and not code_targets)),
        "public_family": public_family,
        "public_sources": public_sources if (public_plan.get("public_research_needed") or public_hits or web_hits) else [],
        "repo_map_needed": repo_map_needed,
        "repo_map_targets": code_targets,
        "source_counts": {
            "kb_hits": kb_hits,
            "rule_hits": rule_hits,
            "mistake_hits": mistake_hits,
            "session_hits": session_hits,
            "semantic_hits": sem_hits,
        },
    }


def tool_result_has_evidence(tool_name: str, result: Any) -> bool:
    """Best-effort check whether a tool result meaningfully answered the request."""
    if not isinstance(result, dict):
        return bool(str(result).strip())
    if result.get("ok") is False:
        return False
    if tool_name == "search_kb":
        return bool(result.get("results") or result.get("matches") or result.get("rows") or result.get("items"))
    if tool_name in {"web_search"}:
        return bool(result.get("results") or result.get("items"))
    if tool_name in {"ingest_knowledge"}:
        return any(
            result.get(k)
            for k in ("records_inserted", "records_updated", "raw_count", "deduped_count", "concepts_found", "hot_reflections_injected")
        )
    if tool_name in {"web_fetch", "summarize_url", "read_file", "gh_read_lines", "search_in_file"}:
        for key in ("content", "text", "snippet", "summary", "lines", "result"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                return True
            if isinstance(val, list) and val:
                return True
        return False
    if tool_name in {"repo_map_status", "repo_map_sync", "repo_component_packet", "repo_graph_packet", "public_evidence_packet"}:
        return any(result.get(k) for k in ("summary", "counts", "components", "chunks", "edges", "nodes", "packet", "families"))
    if tool_name in {"git"}:
        return any(result.get(k) for k in ("stdout", "status", "diff", "log", "commit", "branch"))
    if tool_name in {"get_state", "state_packet", "session_snapshot", "system_verification_packet"}:
        return True
    return any(
        result.get(k)
        for k in ("content", "text", "summary", "result", "data", "rows", "state", "status", "details")
    )
