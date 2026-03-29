#!/usr/bin/env python3
"""
CORE ORC stress matrix.

This is a concrete, executable test matrix for the full input→context→decision
→evidence→style pipeline. It is intentionally dependency-light and uses the
real orchestrator helpers instead of mocks.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator_message import OrchestratorMessage
from core_orch_context import (
    initial_request_profile,
    build_decision_packet,
    build_evidence_gate,
    build_tool_policy_packet,
    build_response_style_packet,
    should_use_agentic_mode,
)
from core_public_evidence import build_public_evidence_packet
from core_repo_map import build_repo_component_packet, build_repo_graph_packet


@dataclass
class StressCase:
    case_id: str
    difficulty: str
    source: str
    text: str
    expect: dict[str, Any] = field(default_factory=dict)
    repo_path: str = ""
    expect_repo_missing: bool = False


MATRIX: list[StressCase] = [
    StressCase(
        case_id="T01",
        difficulty="low",
        source="telegram",
        text="How advanced are you now core?",
        expect={
            "primary_class": "ask",
            "request_kind": "self_assessment",
            "response_mode": "capability",
            "style_mode": "capability",
            "use_html": True,
            "delivery_channel": "telegram",
            "agentic": False,
            "gate_mode": "state_only",
            "public_family": "public_general",
            "public_needed": False,
            "tool_best_fit_family": "state",
            "tool_best_first_tool": "get_state",
            "tool_registry_size_min": 1,
        },
    ),
    StressCase(
        case_id="T02",
        difficulty="low",
        source="mcp",
        text="How advanced are you now core?",
        expect={
            "primary_class": "ask",
            "request_kind": "self_assessment",
            "response_mode": "capability",
            "style_mode": "capability",
            "use_html": False,
            "delivery_channel": "mcp",
            "agentic": False,
            "gate_mode": "state_only",
            "public_family": "public_general",
            "public_needed": False,
            "tool_best_fit_family": "state",
            "tool_best_first_tool": "get_state",
            "tool_registry_size_min": 1,
        },
    ),
    StressCase(
        case_id="T03",
        difficulty="medium",
        source="telegram",
        text="Proceed step by step and investigate the codebase until you find the root cause.",
        expect={
            "primary_class": "act",
            "request_kind": "task",
            "response_mode": "agentic",
            "style_mode": "agentic",
            "use_html": True,
            "agentic": True,
            "gate_mode": "code",
            "repo_map_needed": True,
            "tool_best_fit_family": "task",
            "tool_best_first_tool": "repo_component_packet",
            "tool_registry_size_min": 1,
        },
    ),
    StressCase(
        case_id="T04",
        difficulty="medium",
        source="telegram",
        text="Review cluster 30ea67590770 and batch close all applied rows once verified.",
        expect={
            "primary_class": "evaluate",
            "request_kind": "owner_review",
            "response_mode": "review",
            "style_mode": "review",
            "agentic": False,
            "tool_best_fit_family": "review",
            "tool_best_first_tool": "owner_review_cluster_packet",
        },
    ),
    StressCase(
        case_id="T05",
        difficulty="medium",
        source="telegram",
        text="Check the commit status of /mnt/e/CORE/core-agi/core_orch_layer9.py and tell me if it is pushed.",
        expect={
            "request_kind": "status",
            "response_mode": "status",
            "gate_mode": "code",
            "repo_map_needed": True,
            "public_needed": False,
            "tool_best_fit_family": "repo_code",
            "tool_best_first_tool": "repo_component_packet",
        },
        repo_path="core_orch_layer9.py",
    ),
    StressCase(
        case_id="T06",
        difficulty="medium",
        source="telegram",
        text="What is the latest public guidance on Claude artifacts and Codex cloud agents?",
        expect={
            "request_kind": "question",
            "response_mode": "tool",
            "style_mode": "answer",
            "public_family": "public_general",
            "public_needed": True,
            "gate_mode": "public_research_then_web",
            "tool_best_fit_family": "knowledge",
            "tool_best_first_tool": "search_kb",
        },
    ),
    StressCase(
        case_id="T07",
        difficulty="medium",
        source="telegram",
        text="What is the current BTC funding and market sentiment?",
        expect={
            "request_kind": "question",
            "response_mode": "tool",
            "public_family": "public_trading",
            "public_needed": True,
            "gate_mode": "public_research_then_web",
            "tool_best_fit_family": "knowledge",
            "tool_best_first_tool": "search_kb",
        },
    ),
    StressCase(
        case_id="T08",
        difficulty="medium",
        source="telegram",
        text="No, that's wrong. Please only use owner-only rows and stop here.",
        expect={
            "primary_class": "interrupt",
            "request_kind": "command",
            "style_mode": "interrupt",
            "agentic": False,
        },
    ),
    StressCase(
        case_id="T09",
        difficulty="hard",
        source="telegram",
        text="make it better",
        expect={
            "primary_class": "act",
            "clarification": True,
            "gate_clarify": True,
            "style_mode": "task",
            "use_html": True,
        },
    ),
    StressCase(
        case_id="T10",
        difficulty="impossible",
        source="telegram",
        text="Inspect /mnt/e/CORE/core-agi/THIS_FILE_DOES_NOT_EXIST.py and tell me what is inside.",
        expect={
            "primary_class": "ask",
            "gate_mode": "code",
            "repo_map_needed": True,
            "gate_clarify": True,
        },
        repo_path="THIS_FILE_DOES_NOT_EXIST.py",
        expect_repo_missing=True,
    ),
]


def _build_message(case: StressCase) -> OrchestratorMessage:
    msg = OrchestratorMessage(text=case.text, source=case.source, message_type="message", route="conversation")
    profile = initial_request_profile(msg)
    msg.input_profile = profile["input_profile"]
    msg.speech_act_packet = profile["speech_act_packet"]
    msg.request_kind = profile["request_kind"]
    msg.response_mode = profile["response_mode"]
    msg.route_reason = profile["route_reason"]
    msg.clarification_needed = bool(profile["clarification_needed"])
    msg.context["input_profile"] = msg.input_profile
    msg.context["speech_act_packet"] = msg.speech_act_packet
    msg.context["request_profile"] = profile
    msg.context["request_kind"] = msg.request_kind
    msg.context["response_mode"] = msg.response_mode
    msg.context["route_reason"] = msg.route_reason
    msg.context["clarification_needed"] = msg.clarification_needed
    return msg


def _assert_equals(case: StressCase, actual: dict[str, Any], key: str, expected: Any, failures: list[str]) -> None:
    value = actual.get(key)
    if value != expected:
        failures.append(f"{case.case_id}: {key} expected {expected!r} got {value!r}")


def _assert_contains(case: StressCase, actual: dict[str, Any], key: str, expected: Iterable[Any], failures: list[str]) -> None:
    value = actual.get(key) or []
    missing = [item for item in expected if item not in value]
    if missing:
        failures.append(f"{case.case_id}: {key} missing {missing!r} from {value!r}")


def run_case(case: StressCase) -> tuple[dict[str, Any], list[str]]:
    msg = _build_message(case)
    profile = msg.input_profile
    decision = build_decision_packet(msg)
    msg.decision_packet = decision
    msg.request_kind = decision.get("request_kind", msg.request_kind)
    msg.response_mode = decision.get("response_mode", msg.response_mode)
    msg.route_reason = decision.get("route_reason", msg.route_reason)
    msg.clarification_needed = bool(decision.get("clarification_needed", False))
    msg.context["decision_packet"] = decision
    msg.context["request_kind"] = msg.request_kind
    msg.context["response_mode"] = msg.response_mode
    msg.context["route_reason"] = msg.route_reason
    msg.context["clarification_needed"] = msg.clarification_needed
    gate = build_evidence_gate(msg)
    msg.evidence_gate = gate
    msg.context["evidence_gate"] = gate
    tool_policy = build_tool_policy_packet(msg)
    msg.tool_policy_packet = tool_policy
    msg.context["tool_policy_packet"] = tool_policy
    msg.response_style_packet = decision.get("response_style_packet", {})
    msg.context["response_style_packet"] = msg.response_style_packet
    style = build_response_style_packet(msg)
    msg.response_style_packet = style
    msg.context["response_style_packet"] = style
    agentic = should_use_agentic_mode(msg)

    public_packet = build_public_evidence_packet(
        query=msg.text,
        domain=msg.context.get("current_domain", "general"),
        request_kind=msg.request_kind,
        code_targets=gate.get("code_targets", []),
    )

    actual = {
        "primary_class": profile.get("primary_class"),
        "top_level_class": profile.get("top_level_class"),
        "request_kind": msg.request_kind,
        "response_mode": msg.response_mode,
        "style_mode": style.get("mode"),
        "use_html": style.get("use_html"),
        "delivery_channel": style.get("delivery_channel"),
        "explicit_agentic": style.get("explicit_agentic"),
        "agentic": agentic,
        "clarification": bool(decision.get("clarification_needed", False)),
        "gate_mode": gate.get("retrieval_mode"),
        "gate_clarify": bool(gate.get("needs_clarification_after_retrieval", False)),
        "repo_map_needed": bool(gate.get("repo_map_needed", False)),
        "public_family": gate.get("public_family"),
        "public_needed": bool(gate.get("public_research_needed", False)),
        "public_sources": gate.get("public_sources", []),
        "tool_policy": tool_policy,
        "tool_registry_size": tool_policy.get("registry_size"),
        "tool_best_fit_family": tool_policy.get("best_fit_family"),
        "tool_best_first_tool": tool_policy.get("best_first_tool"),
        "tool_preferred_families": tool_policy.get("preferred_families", []),
        "tool_avoid_first": tool_policy.get("avoid_first", []),
        "route_hint": profile.get("route_hint"),
        "speech_acts": profile.get("speech_acts", []),
        "multi_label": bool(profile.get("multi_label", False)),
        "response_style_structure": style.get("structure", []),
        "repo_packet": None,
        "public_packet": public_packet,
        "gate": gate,
    }

    failures: list[str] = []
    for key, expected in case.expect.items():
        if key in {"public_sources_contains", "style_contains"}:
            continue
        if key == "public_sources_len_min":
            if len(actual.get("public_sources", [])) < int(expected):
                failures.append(f"{case.case_id}: public_sources length < {expected} got {actual.get('public_sources', [])!r}")
            continue
        if key == "speech_acts_contains":
            _assert_contains(case, actual, "speech_acts", expected, failures)
            continue
        if key == "response_style_structure":
            _assert_equals(case, actual, "response_style_structure", expected, failures)
            continue
        if key == "tool_best_fit_family":
            _assert_equals(case, actual, "tool_best_fit_family", expected, failures)
            continue
        if key == "tool_best_first_tool":
            _assert_equals(case, actual, "tool_best_first_tool", expected, failures)
            continue
        if key == "tool_registry_size_min":
            if int(actual.get("tool_registry_size", 0)) < int(expected):
                failures.append(f"{case.case_id}: tool_registry_size < {expected} got {actual.get('tool_registry_size')!r}")
            continue
        _assert_equals(case, actual, key, expected, failures)

    if case.repo_path:
        repo_packet = build_repo_component_packet(path=case.repo_path, limit=5)
        actual["repo_packet"] = repo_packet
        if case.expect_repo_missing:
            if repo_packet.get("ok") is True:
                failures.append(f"{case.case_id}: expected missing repo component for {case.repo_path!r}")
        else:
            if not repo_packet.get("ok"):
                failures.append(f"{case.case_id}: repo packet failed: {repo_packet.get('error')}")
        graph_packet = build_repo_graph_packet(path=case.repo_path, depth=2, limit=5)
        actual["repo_graph"] = graph_packet
        if not case.expect_repo_missing and not graph_packet.get("ok"):
            failures.append(f"{case.case_id}: repo graph failed: {graph_packet.get('error')}")
    return actual, failures


def render_matrix(cases: list[StressCase]) -> str:
    rows = [
        "| ID | Difficulty | Channel | Prompt | Expected route |",
        "| --- | --- | --- | --- | --- |",
    ]
    for case in cases:
        prompt = case.text.replace("|", "\\|")
        rows.append(
            f"| {case.case_id} | {case.difficulty} | {case.source} | "
            f"{prompt[:88]} | "
            f"{case.expect.get('request_kind', case.expect.get('gate_mode', ''))} |"
        )
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the CORE ORC stress matrix.")
    parser.add_argument("--json", action="store_true", help="Print JSON results")
    parser.add_argument("--markdown", action="store_true", help="Print the matrix in markdown")
    args = parser.parse_args()

    if args.markdown:
        print(render_matrix(MATRIX))
        return 0

    results = []
    all_failures: list[str] = []
    for case in MATRIX:
        actual, failures = run_case(case)
        results.append({
            "case_id": case.case_id,
            "difficulty": case.difficulty,
            "channel": case.source,
            "prompt": case.text,
            "actual": actual,
            "failures": failures,
        })
        all_failures.extend(failures)
        status = "PASS" if not failures else "FAIL"
        print(f"[{status}] {case.case_id} {case.difficulty} {case.source}: {case.text[:72]}")
        if failures:
            for item in failures:
                print(f"  - {item}")

    summary = {
        "cases": len(MATRIX),
        "passed": len(MATRIX) - len({f.split(':', 1)[0] for f in all_failures}),
        "failed_cases": len({f.split(':', 1)[0] for f in all_failures}),
        "failures": all_failures,
    }
    print("\nSUMMARY:", json.dumps(summary, default=str))
    if args.json:
        print(json.dumps(results, default=str, indent=2))
    return 1 if all_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
