"""core_task_taxonomy.py -- human work-intent taxonomy and cowork packets.

This module adds a first-class work-mode layer on top of CORE's speech-act
classification. It is designed for cowork-style inputs such as:
- analyze this spreadsheet
- make a presentation
- inspect a repo diff
- research a topic
- coordinate/batch owner-review items

The goal is not to replace task execution tools. The goal is to give the
orchestrator a structured, multi-label work packet so it can choose the right
mode, first tool family, and output shape before agentic execution begins.
"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Iterable


WORK_INTENT_MATRIX = {
    "analyze": {
        "subintents": [
            "data_analysis",
            "spreadsheet_analysis",
            "document_analysis",
            "code_analysis",
            "incident_analysis",
            "semantic_analysis",
        ],
        "detail_intents": {
            "spreadsheet_analysis": [
                "financial_analysis",
                "kpi_review",
                "reconciliation",
                "cleaning",
                "forecasting",
                "variance",
            ],
            "document_analysis": [
                "summary",
                "fact_check",
                "compare",
                "extract_actions",
                "rewrite",
            ],
            "code_analysis": [
                "root_cause",
                "architecture",
                "diff_review",
                "dependency_trace",
                "refactor_scope",
            ],
        },
        "default_execution_mode": "agentic",
        "artifact_expected": "analysis_report",
        "preferred_tool_families": ["document", "repo_code", "knowledge"],
    },
    "transform": {
        "subintents": ["summarize", "rewrite", "convert", "normalize", "extract"],
        "detail_intents": {
            "summarize": ["executive_summary", "technical_summary"],
            "convert": ["document_conversion", "format_migration"],
            "extract": ["action_items", "tables", "entities", "highlights"],
        },
        "default_execution_mode": "tool",
        "artifact_expected": "transformed_artifact",
        "preferred_tool_families": ["document", "knowledge"],
    },
    "create": {
        "subintents": [
            "draft",
            "build",
            "design",
            "compose",
            "presentation_creation",
            "document_creation",
            "spreadsheet_creation",
            "code_creation",
        ],
        "detail_intents": {
            "presentation_creation": [
                "executive_deck",
                "technical_deck",
                "status_update",
                "sales_pitch",
                "training_deck",
            ],
            "document_creation": ["proposal", "brief", "report", "email", "spec"],
            "spreadsheet_creation": ["model", "tracker", "budget", "table", "dashboard"],
        },
        "default_execution_mode": "agentic",
        "artifact_expected": "new_artifact",
        "preferred_tool_families": ["document", "repo_code", "state"],
    },
    "inspect": {
        "subintents": ["review", "audit", "validate", "test", "forensics"],
        "detail_intents": {
            "review": ["diff_review", "proposal_review", "cluster_review", "quality_review"],
            "audit": ["compliance", "consistency", "risk", "coverage"],
        },
        "default_execution_mode": "agentic",
        "artifact_expected": "verdict",
        "preferred_tool_families": ["review", "repo_code", "state"],
    },
    "operate": {
        "subintents": ["deploy", "monitor", "recover", "maintain", "sync", "status"],
        "detail_intents": {
            "deploy": ["push", "restart", "release"],
            "recover": ["rollback", "outage", "restore"],
            "monitor": ["health", "logs", "alerts"],
        },
        "default_execution_mode": "agentic",
        "artifact_expected": "operational_action",
        "preferred_tool_families": ["state", "deploy", "repo_code"],
    },
    "research": {
        "subintents": [
            "internal_research",
            "public_research",
            "mixed_research",
            "market_research",
        ],
        "detail_intents": {
            "public_research": ["docs", "papers", "community", "news", "web"],
            "market_research": ["funding", "sentiment", "dominance", "signals", "price"],
        },
        "default_execution_mode": "agentic",
        "artifact_expected": "evidence_summary",
        "preferred_tool_families": ["knowledge", "web"],
    },
    "coordinate": {
        "subintents": ["triage", "batch", "delegate", "track"],
        "detail_intents": {
            "batch": ["cluster_close", "bulk_apply", "bulk_update"],
            "delegate": ["worker_assignment", "queue_routing"],
        },
        "default_execution_mode": "agentic",
        "artifact_expected": "coordination_plan",
        "preferred_tool_families": ["task", "review", "knowledge"],
    },
    "learn": {
        "subintents": ["pattern_learning", "policy_learning", "memory_projection", "cluster_learning"],
        "detail_intents": {
            "pattern_learning": ["wins", "losses", "recurrence", "heuristics"],
            "policy_learning": ["routing", "threshold", "gate", "tool_choice"],
        },
        "default_execution_mode": "agentic",
        "artifact_expected": "learning_update",
        "preferred_tool_families": ["knowledge", "review", "state"],
    },
    "decide": {
        "subintents": ["select", "rank", "approve_reject", "prioritize"],
        "detail_intents": {
            "approve_reject": ["go_no_go", "batch_approval", "rejection"],
            "prioritize": ["ranking", "triage", "queue_order"],
        },
        "default_execution_mode": "tool",
        "artifact_expected": "decision",
        "preferred_tool_families": ["review", "state", "knowledge"],
    },
    "clarify": {
        "subintents": ["missing_target", "missing_file", "missing_goal", "conflicting_instruction"],
        "detail_intents": {
            "missing_target": ["path", "url", "commit", "file", "row"],
        },
        "default_execution_mode": "clarify",
        "artifact_expected": "clarification_request",
        "preferred_tool_families": ["state", "knowledge"],
    },
    "interrupt": {
        "subintents": ["pause", "stop", "abort", "switch_direction", "cancel"],
        "detail_intents": {
            "pause": ["hold", "wait"],
            "stop": ["halt", "cease"],
        },
        "default_execution_mode": "stop",
        "artifact_expected": "stop_acknowledgement",
        "preferred_tool_families": ["state"],
    },
}


def _safe_text(value: Any, limit: int = 400) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _normalize_items(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                value = parsed
        except Exception:
            return [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = item.get("type") or item.get("name") or item.get("filename") or item.get("path") or ""
        else:
            text = item
        text = _safe_text(text, 120)
        if text:
            out.append(text)
    return out


def _signal(text: str, phrases: Iterable[str]) -> bool:
    lower = (text or "").lower()
    return any(phrase in lower for phrase in phrases)


def _word_signal(text: str, phrases: Iterable[str]) -> bool:
    lower = f" {text.lower()} "
    return any(re.search(rf"(?<!\w){re.escape(phrase.lower())}(?!\w)", lower) for phrase in phrases)


def _count_numeric(values: list[Any]) -> int:
    count = 0
    for value in values:
        if value in (None, ""):
            continue
        try:
            float(str(value).replace(",", ""))
            count += 1
        except Exception:
            continue
    return count


def _parse_table_like(content: str) -> dict[str, Any]:
    """Parse JSON rows or CSV-like text into a compact table profile."""
    text = content or ""
    rows: list[list[Any]] = []
    format_kind = "text"
    try:
        parsed = json.loads(text)
        format_kind = "json"
        if isinstance(parsed, list):
            if parsed and all(isinstance(row, dict) for row in parsed):
                headers = sorted({key for row in parsed for key in row.keys()})
                rows = [headers]
                for row in parsed:
                    rows.append([row.get(h) for h in headers])
            elif parsed and all(isinstance(row, list) for row in parsed):
                rows = parsed
            elif parsed and isinstance(parsed[0], (str, int, float)):
                rows = [parsed]
            else:
                rows = []
        elif isinstance(parsed, dict):
            headers = sorted(parsed.keys())
            rows = [headers, [parsed.get(h) for h in headers]]
    except Exception:
        try:
            format_kind = "csv"
            rows = list(csv.reader(io.StringIO(text)))
        except Exception:
            rows = []
    if not rows:
        return {
            "format_kind": format_kind,
            "rows": 0,
            "cols": 0,
            "header_detected": False,
            "numeric_columns": [],
            "blank_cells": 0,
            "duplicate_rows": 0,
            "sample_headers": [],
        }

    col_count = max(len(r) for r in rows)
    padded = [list(r) + [""] * (col_count - len(r)) for r in rows]
    header = padded[0] if padded else []
    data_rows = padded[1:] if len(padded) > 1 else []
    header_detected = bool(header and any(isinstance(cell, str) and cell.strip() for cell in header))
    numeric_columns: list[int] = []
    for idx in range(col_count):
        column_values = [row[idx] if idx < len(row) else "" for row in data_rows]
        if column_values and _count_numeric(column_values) >= max(1, len(column_values) // 2):
            numeric_columns.append(idx)
    blank_cells = sum(1 for row in padded for cell in row if cell in (None, ""))
    duplicate_rows = len({tuple(row) for row in padded}) and max(0, len(padded) - len({tuple(row) for row in padded}))
    return {
        "format_kind": format_kind,
        "rows": len(padded),
        "cols": col_count,
        "header_detected": header_detected,
        "numeric_columns": numeric_columns[:12],
        "blank_cells": blank_cells,
        "duplicate_rows": duplicate_rows,
        "sample_headers": [str(cell)[:60] for cell in header[:8]],
    }


def _infer_top_level(text: str, goal: str, input_profile: dict[str, Any] | None = None) -> str:
    hay = f"{goal}\n{text}".lower()
    if input_profile and input_profile.get("request_kind") in {"status", "self_assessment"}:
        return "analyze"
    if _signal(hay, ("stop", "pause", "abort", "cancel", "halt", "don't continue", "do not continue")):
        return "interrupt"
    if _signal(hay, ("clarify", "need more detail", "missing file", "missing target", "can't find", "cannot find")):
        return "clarify"
    if _signal(hay, ("research", "evidence", "source", "latest", "public", "web", "internet", "paper", "docs", "documentation")):
        return "research"
    if _signal(hay, ("deploy", "restart", "sync", "monitor", "recover", "service", "log", "health", "status")):
        return "operate"
    if _signal(hay, ("review", "audit", "validate", "test", "verify", "check", "judge", "approve", "reject")):
        return "inspect" if not _signal(hay, ("batch close", "cluster", "queue", "triage")) else "coordinate"
    if _signal(hay, ("triage", "batch", "cluster", "delegate", "route", "queue", "prioritize", "assign")):
        return "coordinate"
    if _signal(hay, ("learn", "improve", "policy", "memory", "pattern", "cluster learning", "update rules")):
        return "learn"
    if _signal(hay, ("choose", "select", "rank", "prefer", "should i", "approve", "reject", "prioritize")):
        return "decide"
    if _signal(hay, ("rewrite", "summarize", "convert", "normalize", "extract", "reformat")):
        return "transform"
    if _signal(hay, ("make", "build", "create", "draft", "design", "prepare", "outline", "generate", "compose")):
        return "create"
    if _signal(hay, ("analyze", "analyse", "inspect", "compare", "diagnose", "explain", "understand")):
        return "analyze"
    if _signal(hay, ("fix", "bug", "error", "crash", "trace", "root cause", "commit status", "repo", "code", "function", "diff")):
        return "analyze"
    if input_profile and input_profile.get("top_level_class") in {"interrupt", "correct", "approve", "constrain", "meta"}:
        return "clarify" if input_profile.get("requires_clarification") else "coordinate"
    return "analyze" if len(hay) > 40 else "clarify"


def _infer_subintent(top_level: str, text: str, goal: str, artifact_hint: str = "", content_profile: dict[str, Any] | None = None) -> str:
    hay = f"{goal}\n{text}\n{artifact_hint}".lower()
    content_profile = content_profile or {}
    if top_level == "analyze":
        if _signal(hay, ("sheet", "spreadsheet", "excel", "xlsx", "csv", "worksheet", "pivot", "formula", "rows", "columns")) or content_profile.get("cols", 0) >= 2:
            return "spreadsheet_analysis"
        if _signal(hay, ("doc", "docx", "pdf", "report", "memo", "proposal", "brief", "notes", "paper", "document")):
            return "document_analysis"
        if _word_signal(hay, ("repo", "code", "codebase", "function", "module", "bug", "patch", "commit", "diff", "trace", "stack trace")):
            return "code_analysis"
        if _signal(hay, ("log", "trace", "incident", "outage", "crash", "error", "failure", "exception")):
            return "incident_analysis"
        if _signal(hay, ("metric", "trend", "kpi", "dashboard", "chart", "table", "dataset", "data")):
            return "data_analysis"
        return "semantic_analysis"
    if top_level == "transform":
        if _signal(hay, ("summary", "summarize", "shorten", "condense")):
            return "summarize"
        if _signal(hay, ("rewrite", "rephrase", "tone", "style")):
            return "rewrite"
        if _signal(hay, ("convert", "change format", "xlsx", "csv", "pptx", "docx", "pdf")):
            return "convert"
        if _signal(hay, ("normalize", "clean", "standardize", "dedupe")):
            return "normalize"
        return "extract"
    if top_level == "create":
        if _signal(hay, ("slide", "slides", "deck", "presentation", "powerpoint", "ppt", "speaker notes", "keynote")):
            return "presentation_creation"
        if _signal(hay, ("sheet", "spreadsheet", "excel", "xlsx", "csv", "table", "tracker", "dashboard", "model")):
            return "spreadsheet_creation"
        if _signal(hay, ("doc", "docx", "pdf", "report", "proposal", "brief", "spec", "email", "memo")):
            return "document_creation"
        if _signal(hay, ("code", "module", "function", "tool", "script", "worker", "patch")):
            return "code_creation"
        return "draft"
    if top_level == "inspect":
        if _signal(hay, ("diff", "patch", "commit", "repo", "code", "function")):
            return "review"
        if _signal(hay, ("test", "smoke", "regression", "verify", "validate")):
            return "test"
        if _signal(hay, ("audit", "compliance", "risk", "coverage")):
            return "audit"
        return "forensics"
    if top_level == "operate":
        if _signal(hay, ("deploy", "release", "push")):
            return "deploy"
        if _signal(hay, ("restart", "rollback", "restore", "recover")):
            return "recover"
        if _signal(hay, ("monitor", "health", "log", "status")):
            return "monitor"
        return "maintain"
    if top_level == "research":
        if _signal(hay, ("funding", "sentiment", "dominance", "market", "trade", "binance", "pnl", "crypto")):
            return "market_research"
        if _signal(hay, ("public", "web", "internet", "news", "latest", "current", "docs", "article", "paper")):
            return "public_research"
        if _signal(hay, ("internal", "supabase", "kb", "mistake", "session")):
            return "internal_research"
        return "mixed_research"
    if top_level == "coordinate":
        if _signal(hay, ("batch", "cluster", "close", "apply")):
            return "batch"
        if _signal(hay, ("delegate", "assign", "route")):
            return "delegate"
        if _signal(hay, ("triage", "sort", "prioritize", "rank")):
            return "triage"
        return "track"
    if top_level == "learn":
        if _signal(hay, ("pattern", "repeat", "recurring", "habit", "trend")):
            return "pattern_learning"
        if _signal(hay, ("policy", "routing", "threshold", "gate", "tool choice")):
            return "policy_learning"
        if _signal(hay, ("memory", "kb", "knowledge", "semantic")):
            return "memory_projection"
        return "cluster_learning"
    if top_level == "decide":
        if _signal(hay, ("approve", "reject", "yes/no", "go/no-go")):
            return "approve_reject"
        if _signal(hay, ("rank", "priority", "prioritize", "order")):
            return "prioritize"
        if _signal(hay, ("select", "choose", "pick")):
            return "select"
        return "rank"
    if top_level == "clarify":
        if _signal(hay, ("file", "path", "url", "commit")):
            return "missing_file"
        if _signal(hay, ("goal", "outcome", "expected", "target")):
            return "missing_goal"
        return "missing_target"
    if top_level == "interrupt":
        if _signal(hay, ("stop", "halt", "abort", "cancel")):
            return "stop"
        if _signal(hay, ("pause", "wait", "hold")):
            return "pause"
        return "switch_direction"
    return "generic"


def _infer_detail_intents(top_level: str, subintent: str, text: str, goal: str) -> list[str]:
    hay = f"{goal}\n{text}".lower()
    details: list[str] = []
    if top_level == "analyze" and subintent == "spreadsheet_analysis":
        for key, label in (
            ("financial", "financial_analysis"),
            ("kpi", "kpi_review"),
            ("forecast", "forecasting"),
            ("variance", "variance"),
            ("clean", "cleaning"),
            ("reconcile", "reconciliation"),
        ):
            if key in hay:
                details.append(label)
    elif top_level == "analyze" and subintent == "document_analysis":
        for key, label in (
            ("summary", "summary"),
            ("compare", "compare"),
            ("extract", "extract_actions"),
            ("fact", "fact_check"),
            ("rewrite", "rewrite"),
        ):
            if key in hay:
                details.append(label)
    elif top_level == "analyze" and subintent == "code_analysis":
        for key, label in (
            ("root cause", "root_cause"),
            ("diff", "diff_review"),
            ("architecture", "architecture"),
            ("dependency", "dependency_trace"),
            ("refactor", "refactor_scope"),
        ):
            if key in hay:
                details.append(label)
    elif top_level == "create" and subintent == "presentation_creation":
        for key, label in (
            ("executive", "executive_deck"),
            ("technical", "technical_deck"),
            ("status", "status_update"),
            ("training", "training_deck"),
            ("sales", "sales_pitch"),
        ):
            if key in hay:
                details.append(label)
    elif top_level == "research" and subintent == "public_research":
        for key, label in (
            ("docs", "docs"),
            ("paper", "papers"),
            ("community", "community"),
            ("news", "news"),
            ("web", "web"),
        ):
            if key in hay:
                details.append(label)
    elif top_level == "operate":
        for key, label in (
            ("deploy", "deploy"),
            ("restart", "restart"),
            ("rollback", "rollback"),
            ("monitor", "monitor"),
            ("recover", "recover"),
        ):
            if key in hay:
                details.append(label)
    return details[:6]


def _artifact_expected(top_level: str, subintent: str) -> str:
    if top_level == "analyze":
        return {
            "spreadsheet_analysis": "analysis_report",
            "document_analysis": "analysis_summary",
            "code_analysis": "root_cause_or_diff_review",
            "incident_analysis": "incident_report",
        }.get(subintent, "analysis_report")
    if top_level == "transform":
        return "transformed_artifact"
    if top_level == "create":
        return {
            "presentation_creation": "slide_deck",
            "document_creation": "document",
            "spreadsheet_creation": "spreadsheet",
            "code_creation": "code_artifact",
        }.get(subintent, "new_artifact")
    if top_level == "inspect":
        return "verdict"
    if top_level == "operate":
        return "operational_action"
    if top_level == "research":
        return "evidence_summary"
    if top_level == "coordinate":
        return "coordination_plan"
    if top_level == "learn":
        return "learning_update"
    if top_level == "decide":
        return "decision"
    if top_level == "clarify":
        return "clarification_request"
    if top_level == "interrupt":
        return "stop_acknowledgement"
    return "response"


def _preferred_tool_families(top_level: str, subintent: str, task_hint: str, evidence_gate: dict[str, Any] | None = None) -> list[str]:
    families = list(WORK_INTENT_MATRIX.get(top_level, {}).get("preferred_tool_families", []))
    if top_level in {"analyze", "transform", "create", "inspect"} and subintent in {
        "spreadsheet_analysis", "document_analysis", "presentation_creation",
        "spreadsheet_creation", "document_creation", "code_analysis",
    }:
        if "document" not in families:
            families.insert(0, "document")
    if top_level == "research" and "web" not in families:
        families.append("web")
    if top_level == "operate" and "state" not in families:
        families.insert(0, "state")
    if top_level == "coordinate" and "review" not in families:
        families.insert(0, "review")
    if evidence_gate and evidence_gate.get("public_research_needed") and "web" not in families:
        families.append("web")
    if task_hint and task_hint in {"spreadsheet", "presentation", "document"} and "document" not in families:
        families.insert(0, "document")
    # stable dedupe
    out: list[str] = []
    for family in families:
        if family and family not in out:
            out.append(family)
    return out[:6]


def _preferred_tools(top_level: str, subintent: str, text: str, goal: str, artifact_hint: str = "") -> list[str]:
    hay = f"{goal}\n{text}\n{artifact_hint}".lower()
    tools: list[str] = []
    if top_level == "analyze":
        if _signal(hay, ("current price in usd", "current_price_in_usd", "spot price", "btc price", "crypto price")):
            tools.extend(["current_price_in_usd", "crypto_price", "reasoning_packet"])
        if _signal(hay, ("architecture", "architectural review", "framework", "hcpn", "cgcan")):
            tools.extend(["architecture_review_packet", "module_assessment_packet", "knowledge_state_packet", "state_packet"])
        if _signal(hay, ("world model", "world_model", "hierarchical", "neuro symbolic", "neuro-symbolic", "symbolic memory", "causal memory", "gating")):
            tools.extend(["domain_invariant_feature_packet", "hierarchical_gated_neuro_symbolic_world_model", "causal_principle_discovery", "causal_graph_data_generator", "generate_synthetic_data", "module_assessment_packet", "state_reconciliation_buffer"])
        elif _signal(hay, ("causal", "principle", "symbolic regression", "regression", "discovery", "abstraction")):
            tools.extend(["causal_principle_discovery", "causal_graph_data_generator", "generate_synthetic_data", "module_assessment_packet", "state_reconciliation_buffer"])
        elif subintent == "spreadsheet_analysis":
            tools.extend(["spreadsheet_work_packet", "read_document", "reasoning_packet", "search_memory"])
        elif subintent == "document_analysis":
            tools.extend(["document_work_packet", "read_document", "reasoning_packet", "search_memory"])
        elif subintent == "code_analysis":
            tools.extend(["code_read_packet", "repo_component_packet", "repo_graph_packet", "search_in_file"])
        elif subintent == "incident_analysis":
            tools.extend(["logs", "railway_logs_live", "state_packet", "search_mistakes"])
        else:
            tools.extend(["reasoning_packet", "search_memory", "state_packet"])
    elif top_level == "transform":
        if "slide" in hay or "presentation" in hay or "deck" in hay:
            tools.extend(["presentation_work_packet", "create_presentation", "create_document"])
        elif "sheet" in hay or "spreadsheet" in hay or "excel" in hay or "csv" in hay:
            tools.extend(["spreadsheet_work_packet", "create_spreadsheet", "convert_document"])
        else:
            tools.extend(["document_work_packet", "create_document", "convert_document"])
    elif top_level == "create":
        if subintent == "presentation_creation":
            tools.extend(["presentation_work_packet", "create_presentation", "create_document"])
        elif subintent == "spreadsheet_creation":
            tools.extend(["spreadsheet_work_packet", "create_spreadsheet", "create_document"])
        elif subintent == "document_creation":
            tools.extend(["document_work_packet", "create_document"])
        elif subintent == "code_creation":
            tools.extend(["code_read_packet", "repo_component_packet", "patch_file", "multi_patch"])
        else:
            tools.extend(["document_work_packet", "create_document", "reasoning_packet"])
    elif top_level == "inspect":
        if "code" in hay or "repo" in hay or "diff" in hay:
            tools.extend(["repo_review_packet", "code_read_packet", "repo_component_packet", "search_in_file", "gh_read_lines"])
        elif "sheet" in hay or "spreadsheet" in hay or "excel" in hay or "csv" in hay:
            tools.extend(["spreadsheet_review_packet", "spreadsheet_work_packet", "read_document", "convert_document"])
        elif "slide" in hay or "deck" in hay or "presentation" in hay:
            tools.extend(["presentation_review_packet", "presentation_work_packet", "document_work_packet"])
        elif "doc" in hay or "document" in hay or "report" in hay or "proposal" in hay:
            tools.extend(["document_review_packet", "document_work_packet", "read_document", "search_memory"])
        else:
            if _signal(hay, ("architecture", "architectural review", "framework", "hcpn", "cgcan")):
                tools.extend(["architecture_review_packet", "module_assessment_packet", "knowledge_state_packet"])
            if _signal(hay, ("world model", "world_model", "hierarchical", "neuro symbolic", "neuro-symbolic", "symbolic memory", "causal memory", "gating", "meta-controller", "meta controller", "feature extraction", "domain invariant")):
                tools.extend(["domain_invariant_feature_packet", "hierarchical_gated_neuro_symbolic_world_model", "causal_principle_discovery", "causal_graph_data_generator", "module_assessment_packet", "state_reconciliation_buffer"])
            if _signal(hay, ("module", "performance", "cost", "overfit", "robustness", "generalization", "readiness")):
                tools.extend(["module_assessment_packet", "state_packet", "reasoning_packet"])
            if _signal(hay, ("verify", "verification", "side effect", "side effects", "counterfactual", "completeness", "schema", "required field", "required fields", "reward", "success", "checkpoint", "complete")):
                tools.extend(["completeness_monitor_packet", "task_verification_bundle", "task_state_packet", "system_verification_packet"])
            tools.extend(["review_work_packet", "document_review_packet", "search_memory", "reasoning_packet"])
    elif top_level == "operate":
        tools.extend(["state_packet", "deploy_status", "build_status", "railway_logs_live", "service"])
    elif top_level == "research":
        tools.extend(["search_kb", "public_evidence_packet", "ingest_knowledge", "web_search", "web_fetch"])
    elif top_level == "coordinate":
        if _signal(hay, ("architecture", "architectural review", "framework", "hcpn", "cgcan")):
            tools.extend(["architecture_review_packet", "module_assessment_packet", "knowledge_state_packet"])
        if _signal(hay, ("world model", "world_model", "hierarchical", "neuro symbolic", "neuro-symbolic", "symbolic memory", "causal memory", "gating", "meta-controller", "meta controller", "feature extraction", "domain invariant")):
            tools.extend(["domain_invariant_feature_packet", "hierarchical_gated_neuro_symbolic_world_model", "task_verification_bundle"])
        tools.extend(["owner_review_cluster_packet", "completeness_monitor_packet", "task_verification_bundle", "task_tracking_packet", "task_packet", "state_packet"])
    elif top_level == "learn":
        if _signal(hay, ("architecture", "architectural review", "framework", "hcpn", "cgcan")):
            tools.extend(["architecture_review_packet", "module_assessment_packet", "knowledge_state_packet"])
        if _signal(hay, ("world model", "world_model", "hierarchical", "neuro symbolic", "neuro-symbolic", "symbolic memory", "causal memory", "gating")):
            tools.extend(["domain_invariant_feature_packet", "hierarchical_gated_neuro_symbolic_world_model", "causal_principle_discovery", "causal_graph_data_generator", "generate_synthetic_data", "module_assessment_packet", "state_reconciliation_buffer"])
        elif _signal(hay, ("causal", "principle", "symbolic regression", "regression", "discovery", "abstraction")):
            tools.extend(["causal_principle_discovery", "causal_graph_data_generator", "generate_synthetic_data", "module_assessment_packet", "state_reconciliation_buffer"])
        tools.extend(["search_memory", "reasoning_packet", "completeness_monitor_packet", "tool_reliance_assessor", "state_packet"])
    elif top_level == "decide":
        tools.extend(["evaluate_state", "reasoning_packet", "tool_reliance_assessor", "state_packet"])
    elif top_level == "clarify":
        tools.extend(["prompt_scaffold_packet", "reasoning_packet", "search_memory", "state_packet"])
    elif top_level == "interrupt":
        tools.extend(["state_packet"])
    # preserve order while deduping
    out: list[str] = []
    for tool in tools:
        if tool and tool not in out:
            out.append(tool)
    return out[:8]


def _coverage_status(preferred_tools: list[str], top_level: str, subintent: str) -> str:
    if top_level in {"analyze", "transform", "create", "inspect", "operate", "research", "coordinate", "learn", "decide"}:
        return "covered" if preferred_tools else "partial"
    return "narrow"


def build_task_mode_packet(
    text: str = "",
    goal: str = "",
    source: str = "",
    message_type: str = "message",
    route: str = "conversation",
    attachments: Any = None,
    input_profile: dict[str, Any] | None = None,
    decision_packet: dict[str, Any] | None = None,
    evidence_gate: dict[str, Any] | None = None,
    artifact_hint: str = "",
    content: str = "",
) -> dict[str, Any]:
    """Return a structured work-intent packet for cowork/agentic tasks."""
    input_profile = input_profile or {}
    decision_packet = decision_packet or {}
    evidence_gate = evidence_gate or {}
    attachments_list = _normalize_items(attachments)
    text = _safe_text(text, 1200)
    goal = _safe_text(goal, 1200) or text
    content = _safe_text(content, 4000)
    artifact_hint = _safe_text(artifact_hint, 120)
    hay = f"{goal}\n{text}\n{artifact_hint}\n{content}".lower()
    content_profile = _parse_table_like(content) if content else {}

    top_level = _infer_top_level(text=hay, goal=goal, input_profile=input_profile)
    subintent = _infer_subintent(top_level=top_level, text=text, goal=goal, artifact_hint=artifact_hint, content_profile=content_profile)
    detail_intents = _infer_detail_intents(top_level=top_level, subintent=subintent, text=text, goal=goal)
    if not detail_intents and artifact_hint:
        detail_intents = [artifact_hint]

    vague_action = _signal(hay, (
        "make it better",
        "improve it",
        "fix it",
        "change it",
        "make better",
        "do better",
        "make it work",
    ))
    if top_level in {"analyze", "create", "transform"} and vague_action and not artifact_hint and not content and len(hay) < 80:
        top_level = "clarify"
        subintent = "missing_goal"
        detail_intents = ["missing_goal"]

    artifact_expected = _artifact_expected(top_level, subintent)
    task_hint = artifact_hint or content_profile.get("format_kind", "")
    preferred_tool_families = _preferred_tool_families(top_level, subintent, task_hint, evidence_gate)
    preferred_tools = _preferred_tools(top_level, subintent, text, goal, artifact_hint=artifact_hint)
    if input_profile.get("request_kind") in {"status", "self_assessment"} or _signal(hay, ("how advanced", "capability", "what can you do", "strengths", "weaknesses", "limitations")):
        preferred_tool_families = ["state", "knowledge"]
        preferred_tools = ["get_state", "search_kb", "state_packet", "session_snapshot"]
        artifact_expected = "capability_summary"
        execution_mode = "tool"
    if top_level == "research" and evidence_gate.get("public_family"):
        if evidence_gate.get("public_family") == "public_trading":
            preferred_tools = ["search_kb", "public_evidence_packet", "ingest_knowledge", "web_search", "web_fetch"]
    if top_level == "coordinate" and "owner_review" in hay:
        preferred_tools = ["owner_review_cluster_packet", "owner_review_cluster_close", "task_tracking_packet", "state_packet"]

    prompt_len = len((text or goal or "").strip())
    explicit_agentic = top_level in {"analyze", "create", "inspect", "operate", "coordinate", "learn"} and (
        prompt_len > 120 or bool(attachments_list) or subintent in {
            "spreadsheet_analysis", "document_analysis", "code_analysis", "incident_analysis",
            "presentation_creation", "document_creation", "spreadsheet_creation", "code_creation",
            "public_research", "mixed_research", "market_research", "batch", "delegate", "track",
        }
    )
    if top_level == "research":
        explicit_agentic = bool(
            prompt_len > 140
            or bool(attachments_list)
            or subintent in {"mixed_research", "market_research"}
            or _signal(hay, ("deep dive", "multi step", "step by step", "compare sources", "sweep", "cross-check"))
        )
    if input_profile.get("request_kind") in {"status", "self_assessment"}:
        explicit_agentic = False
    if decision_packet.get("clarification_needed"):
        explicit_agentic = False

    if top_level in {"clarify", "interrupt"}:
        execution_mode = "clarify" if top_level == "clarify" else "stop"
    else:
        execution_mode = "agentic" if explicit_agentic else WORK_INTENT_MATRIX.get(top_level, {}).get("default_execution_mode", "tool")
    if input_profile.get("request_kind") in {"status", "self_assessment"}:
        execution_mode = "tool"
    elif top_level == "research" and not explicit_agentic:
        execution_mode = "tool"
    elif top_level == "analyze" and subintent == "semantic_analysis" and prompt_len < 120 and not attachments_list:
        execution_mode = "tool"

    stop_condition = {
        "analyze": "Stop when enough evidence explains the pattern and the next best action is clear.",
        "transform": "Stop when the target format is produced and the content is internally consistent.",
        "create": "Stop when the requested deliverable is assembled and verified.",
        "inspect": "Stop when a defensible verdict can be stated with evidence.",
        "operate": "Stop when the operational action is confirmed or a safe blocker is identified.",
        "research": "Stop when evidence is strong enough or the sweep becomes thin and clarification is required.",
        "coordinate": "Stop when the batch/cluster is grouped or closed and the next action is explicit.",
        "learn": "Stop when the lesson, pattern, or policy update is durable and specific.",
        "decide": "Stop when a reasoned recommendation or verdict is possible.",
        "clarify": "Stop immediately and ask for the missing target, file, URL, or expected output.",
        "interrupt": "Stop immediately and acknowledge the pause or cancel request.",
    }.get(top_level, "Stop when the work is complete or evidence is insufficient.")

    # When the request is clearly document-like, bias toward document tools.
    if content_profile.get("rows", 0) and top_level in {"analyze", "transform", "create"}:
        if "document" not in preferred_tool_families:
            preferred_tool_families.insert(0, "document")

    return {
        "ok": True,
        "matrix_version": "1.0",
        "matrix": WORK_INTENT_MATRIX,
        "work_intent": top_level,
        "work_subintent": subintent,
        "work_detail_intents": detail_intents,
        "execution_mode": execution_mode,
        "agentic_recommended": execution_mode == "agentic",
        "artifact_expected": artifact_expected,
        "artifact_hint": artifact_hint or content_profile.get("format_kind", ""),
        "source": source,
        "message_type": message_type,
        "route": route,
        "goal": goal,
        "text": text,
        "attachments_present": bool(attachments_list),
        "attachment_types": attachments_list[:8],
        "preferred_tool_families": preferred_tool_families,
        "preferred_tools": preferred_tools,
        "coverage_status": _coverage_status(preferred_tools, top_level, subintent),
        "stop_condition": stop_condition,
        "content_profile": content_profile,
        "decision_hint": decision_packet.get("route_reason") or decision_packet.get("request_kind") or "",
        "evidence_hint": evidence_gate.get("retrieval_mode") or evidence_gate.get("public_family") or "",
        "route_hint": "clarify" if top_level == "clarify" else "execute" if top_level not in {"interrupt"} else "stop",
        "needs_clarification": top_level == "clarify" or bool(decision_packet.get("clarification_needed")),
        "confidence": 0.78 if explicit_agentic else 0.64,
        "notes": (
            "CORE should classify cowork work by task intent first, then subintent/detail, "
            "then choose the first specialized tool family. This packet is designed to keep "
            "agentic mode aligned with the actual work type rather than raw wording."
        ),
    }


def build_spreadsheet_work_packet(
    content: str = "",
    goal: str = "",
    filename: str = "",
    sheet_name: str = "Sheet1",
    format_hint: str = "",
) -> dict[str, Any]:
    base = build_task_mode_packet(
        text=goal or content,
        goal=goal,
        artifact_hint="spreadsheet",
        content=content,
    )
    profile = _parse_table_like(content) if content else {}
    rows = profile.get("rows", 0)
    cols = profile.get("cols", 0)
    numeric_columns = profile.get("numeric_columns", [])
    analysis_focus = []
    if rows and cols:
        if cols >= 2:
            analysis_focus.append("column_relationships")
        if rows > 20:
            analysis_focus.append("outlier_review")
        if numeric_columns:
            analysis_focus.append("numeric_trends")
        if profile.get("duplicate_rows"):
            analysis_focus.append("duplicate_rows")
        if profile.get("blank_cells", 0) > 0:
            analysis_focus.append("missing_values")
    else:
        analysis_focus.append("need_table_input")

    recommended_next = "use create_spreadsheet" if content else "upload_or_provide_table"
    if goal and _signal(goal, ("analyze", "inspect", "audit")):
        recommended_next = "analyze_table_then_summarize"
    elif goal and _signal(goal, ("create", "make", "build")):
        recommended_next = "prepare_workbook_from_structure"

    return {
        "ok": True,
        "artifact_kind": "spreadsheet",
        "filename": filename,
        "sheet_name": sheet_name,
        "format_hint": format_hint or profile.get("format_kind", ""),
        "table_profile": profile,
        "analysis_focus": analysis_focus,
        "recommended_next_step": recommended_next,
        "preferred_tools": ["spreadsheet_work_packet", "create_spreadsheet", "read_document", "convert_document"],
        "task_mode_packet": base,
    }


def build_document_work_packet(
    content: str = "",
    goal: str = "",
    audience: str = "",
    format_hint: str = "",
) -> dict[str, Any]:
    base = build_task_mode_packet(
        text=goal or content,
        goal=goal,
        artifact_hint="document",
        content=content,
    )
    text = content or goal
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    bullets = [line.strip("-• \t") for line in text.splitlines() if line.strip().startswith(("-", "•"))]
    action_items = [line for line in bullets if _signal(line, ("must", "should", "do ", "fix", "update", "follow up", "next step"))]
    summary_focus = []
    if len(paragraphs) > 1:
        summary_focus.append("section_summary")
    if action_items:
        summary_focus.append("action_extraction")
    if sentences:
        summary_focus.append("fact_extraction")
    if len(text) > 2000:
        summary_focus.append("long_document")
    recommended_next = "summarize_and_extract_actions" if text else "provide_document_text"
    if goal and _signal(goal, ("rewrite", "convert", "format")):
        recommended_next = "transform_document"

    return {
        "ok": True,
        "artifact_kind": "document",
        "audience": audience,
        "format_hint": format_hint,
        "paragraph_count": len(paragraphs),
        "sentence_count": len(sentences),
        "action_items": action_items[:12],
        "summary_focus": summary_focus,
        "recommended_next_step": recommended_next,
        "preferred_tools": ["document_work_packet", "read_document", "create_document", "convert_document"],
        "task_mode_packet": base,
    }


def build_presentation_work_packet(
    content: str = "",
    goal: str = "",
    audience: str = "",
    slide_target: str = "",
    theme: str = "default",
) -> dict[str, Any]:
    base = build_task_mode_packet(
        text=goal or content,
        goal=goal,
        artifact_hint="presentation",
        content=content,
    )
    text = content or goal
    headings = [line.strip("# ").strip() for line in text.splitlines() if line.lstrip().startswith("#")]
    bullets = [line.strip("-• \t") for line in text.splitlines() if line.strip().startswith(("-", "•"))]
    sections = headings or [line for line in bullets[:12] if line]
    if not sections and text:
        parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        sections = [part[:60] for part in parts[:8]]
    if not sections and goal:
        sections = [goal[:60]]
    slide_count = len(sections) or 5
    slide_count = max(3, min(12, slide_count))
    outline = []
    if sections:
        for idx, section in enumerate(sections[:slide_count], start=1):
            outline.append({
                "slide": idx,
                "title": section[:60],
                "bullets": bullets[(idx - 1) * 2:(idx - 1) * 2 + 3] if bullets else [],
            })
    else:
        outline = [
            {"slide": 1, "title": "Title", "bullets": [goal[:80] or "Why this matters"]},
            {"slide": 2, "title": "Current State", "bullets": []},
            {"slide": 3, "title": "Proposal", "bullets": []},
            {"slide": 4, "title": "Risks", "bullets": []},
            {"slide": 5, "title": "Next Steps", "bullets": []},
        ]

    recommended_next = "create_presentation" if text else "provide_outline_or_notes"
    if goal and _signal(goal, ("status", "update", "review")):
        recommended_next = "status_deck_outline"

    return {
        "ok": True,
        "artifact_kind": "presentation",
        "audience": audience,
        "slide_target": slide_target,
        "theme": theme,
        "slide_count_suggested": slide_count,
        "outline": outline,
        "recommended_next_step": recommended_next,
        "preferred_tools": ["presentation_work_packet", "create_presentation", "document_work_packet", "create_document"],
        "task_mode_packet": base,
    }


def build_review_work_packet(
    content: str = "",
    goal: str = "",
    artifact_type: str = "generic",
    focus: str = "",
    rubric: str = "",
) -> dict[str, Any]:
    base = build_task_mode_packet(
        text=goal or content,
        goal=goal,
        artifact_hint="review",
        content=content,
    )
    text = content or goal
    table_profile = _parse_table_like(content) if content else {}
    issues: list[str] = []
    findings: list[str] = []
    checklist: list[str] = []
    severity = "medium"

    hay = f"{goal}\n{content}\n{focus}\n{rubric}".lower()
    if artifact_type == "code":
        checklist = [
            "check correctness",
            "check edge cases",
            "check regressions",
            "check safety",
        ]
        if _signal(hay, ("diff", "patch", "commit", "bug", "risk", "safety", "regression", "refactor")):
            findings.append("code_or_diff_context_present")
        recommended_tools = ["repo_review_packet", "code_read_packet", "repo_component_packet", "search_in_file", "gh_read_lines"]
    elif artifact_type == "spreadsheet":
        checklist = [
            "check totals",
            "check duplicates",
            "check blanks",
            "check formulas",
            "check anomalies",
        ]
        if table_profile.get("duplicate_rows"):
            issues.append("duplicate_rows")
        if table_profile.get("blank_cells", 0) > 0:
            issues.append("blank_cells")
        if table_profile.get("numeric_columns"):
            findings.append("numeric_columns_present")
        recommended_tools = ["spreadsheet_review_packet", "spreadsheet_work_packet", "read_document", "convert_document"]
    elif artifact_type == "presentation":
        checklist = [
            "check story flow",
            "check slide count",
            "check evidence",
            "check audience fit",
            "check next steps",
        ]
        if _signal(hay, ("slide", "deck", "presentation", "speaker notes", "visual")):
            findings.append("presentation_context_present")
        recommended_tools = ["presentation_review_packet", "presentation_work_packet", "document_work_packet", "create_presentation"]
    elif artifact_type == "document":
        checklist = [
            "check clarity",
            "check completeness",
            "check actions",
            "check facts",
            "check structure",
        ]
        if _signal(hay, ("summary", "action", "rewrite", "proposal", "report", "spec")):
            findings.append("document_context_present")
        recommended_tools = ["document_review_packet", "document_work_packet", "read_document", "create_document"]
    else:
        checklist = [
            "check intent",
            "check evidence",
            "check gaps",
            "check next action",
        ]
        if _signal(hay, ("review", "audit", "validate", "check", "verify")):
            findings.append("review_context_present")
        recommended_tools = ["review_work_packet", "owner_review_cluster_packet", "repo_component_packet", "state_packet"]

    if not issues and table_profile.get("rows", 0) > 0 and artifact_type in {"spreadsheet", "document"}:
        severity = "low"
    if len(findings) > 1 or len(issues) > 1:
        severity = "high"

    recommended_next = "perform_review_then_verdict" if text else "provide_artifact"
    return {
        "ok": True,
        "artifact_kind": artifact_type,
        "review_focus": focus,
        "rubric": rubric,
        "table_profile": table_profile,
        "findings": findings,
        "issues": issues,
        "severity": severity,
        "checklist": checklist,
        "recommended_next_step": recommended_next,
        "preferred_tools": recommended_tools,
        "task_mode_packet": base,
    }


def build_repo_review_packet(
    content: str = "",
    goal: str = "",
    focus: str = "repo_diff",
) -> dict[str, Any]:
    return build_review_work_packet(content=content, goal=goal, artifact_type="code", focus=focus)


def build_document_review_packet(
    content: str = "",
    goal: str = "",
    focus: str = "document_quality",
) -> dict[str, Any]:
    return build_review_work_packet(content=content, goal=goal, artifact_type="document", focus=focus)


def build_spreadsheet_review_packet(
    content: str = "",
    goal: str = "",
    focus: str = "spreadsheet_quality",
) -> dict[str, Any]:
    return build_review_work_packet(content=content, goal=goal, artifact_type="spreadsheet", focus=focus)


def build_presentation_review_packet(
    content: str = "",
    goal: str = "",
    focus: str = "presentation_quality",
) -> dict[str, Any]:
    return build_review_work_packet(content=content, goal=goal, artifact_type="presentation", focus=focus)


def t_task_mode_packet(
    text: str = "",
    goal: str = "",
    source: str = "",
    message_type: str = "message",
    route: str = "conversation",
    attachments: str = "",
    artifact_hint: str = "",
    content: str = "",
) -> dict:
    try:
        attach = _normalize_items(attachments)
        return build_task_mode_packet(
            text=text,
            goal=goal,
            source=source,
            message_type=message_type,
            route=route,
            attachments=attach,
            artifact_hint=artifact_hint,
            content=content,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def t_spreadsheet_work_packet(
    content: str = "",
    goal: str = "",
    filename: str = "",
    sheet_name: str = "Sheet1",
    format_hint: str = "",
) -> dict:
    try:
        return build_spreadsheet_work_packet(
            content=content,
            goal=goal,
            filename=filename,
            sheet_name=sheet_name,
            format_hint=format_hint,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def t_document_work_packet(
    content: str = "",
    goal: str = "",
    audience: str = "",
    format_hint: str = "",
) -> dict:
    try:
        return build_document_work_packet(
            content=content,
            goal=goal,
            audience=audience,
            format_hint=format_hint,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def t_presentation_work_packet(
    content: str = "",
    goal: str = "",
    audience: str = "",
    slide_target: str = "",
    theme: str = "default",
) -> dict:
    try:
        return build_presentation_work_packet(
            content=content,
            goal=goal,
            audience=audience,
            slide_target=slide_target,
            theme=theme,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def t_review_work_packet(
    content: str = "",
    goal: str = "",
    artifact_type: str = "generic",
    focus: str = "",
    rubric: str = "",
) -> dict:
    try:
        return build_review_work_packet(
            content=content,
            goal=goal,
            artifact_type=artifact_type,
            focus=focus,
            rubric=rubric,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def t_repo_review_packet(
    content: str = "",
    goal: str = "",
    focus: str = "repo_diff",
) -> dict:
    try:
        return build_repo_review_packet(content=content, goal=goal, focus=focus)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def t_document_review_packet(
    content: str = "",
    goal: str = "",
    focus: str = "document_quality",
) -> dict:
    try:
        return build_document_review_packet(content=content, goal=goal, focus=focus)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def t_spreadsheet_review_packet(
    content: str = "",
    goal: str = "",
    focus: str = "spreadsheet_quality",
) -> dict:
    try:
        return build_spreadsheet_review_packet(content=content, goal=goal, focus=focus)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def t_presentation_review_packet(
    content: str = "",
    goal: str = "",
    focus: str = "presentation_quality",
) -> dict:
    try:
        return build_presentation_review_packet(content=content, goal=goal, focus=focus)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def t_review_quality_packet(
    content: str = "",
    goal: str = "",
    artifact_type: str = "generic",
    focus: str = "",
    rubric: str = "",
) -> dict:
    try:
        return build_review_work_packet(
            content=content,
            goal=goal,
            artifact_type=artifact_type,
            focus=focus,
            rubric=rubric,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
