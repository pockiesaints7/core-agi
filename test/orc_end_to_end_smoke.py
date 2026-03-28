#!/usr/bin/env python3
"""
CORE ORC end-to-end smoke.

Runs the same matrix cases through the real orchestrator entry path:
- Telegram cases use the Telegram triage pipeline.
- MCP cases use the MCP triage path.

To keep this stable and safe in production-like smoke mode, the model and
outbound side effects are patched to deterministic no-ops while preserving the
actual routing, evidence gating, style shaping, and output formatting paths.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
TEST_DIR = REPO_ROOT / "test"
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from core_orch_layer1 import layer_1_triage
from core_orch_layer10 import _format_mcp
from core_orch_layer3 import _COMMAND_INTENT_MAP
from core_orch_context import build_decision_packet, build_evidence_gate, initial_request_profile, should_use_agentic_mode
from orc_stress_matrix import MATRIX
from orchestrator_message import OrchestratorMessage
from core_config import TELEGRAM_CHAT


def _fake_gemini_chat(*, system: str, user: str, max_tokens: int = 0, json_mode: bool = False, **_: object) -> str:
    text = user.lower()
    for line in user.splitlines():
        if line.startswith("MESSAGE:"):
            text = line.split(":", 1)[1].strip().lower()
            break
    intent = "general_query"
    category = "question"
    requires_tools = True
    tool_hints: list[str] = []
    suggested = "structured"
    domain = "general"

    if "how advanced" in text or "capability" in text or "what can you do" in text:
        intent = "self_assessment"
        requires_tools = False
        suggested = "structured"
    elif "review" in text or "owner only" in text or "batch close" in text or "cluster close" in text:
        intent = "owner_review"
        category = "command"
    elif "debug" in text or "broken" in text or "error" in text or "stack trace" in text:
        intent = "debug_request"
        category = "command"
    elif "time" in text or "weather" in text or "calc" in text or "search" in text:
        intent = "general_tool"
        category = "command"
    elif "commit status" in text or "pushed" in text or "repo" in text or ".py" in text:
        intent = "system_state"
        category = "command"
    elif "step by step" in text or "investigate" in text or "until" in text:
        intent = "task_execution"
        category = "task"
    elif "public guidance" in text or "latest" in text or "public" in text or "research" in text:
        intent = "general_query"
        requires_tools = True

    payload = {
        "intent": intent,
        "confidence": 0.91,
        "category": category,
        "requires_tools": requires_tools,
        "tool_hints": tool_hints,
        "suggested_response_type": suggested,
        "domain": domain,
    }
    return json.dumps(payload) if json_mode else str(payload)


def _fake_groq_chat(*, system: str, user: str, model: str = "", max_tokens: int = 0, **_: object) -> str:
    channel = "mcp" if "DELIVERY_CHANNEL: mcp" in user else "telegram"
    mode = "answer"
    for line in user.splitlines():
        if line.startswith("RESPONSE_MODE:"):
            mode = line.split(":", 1)[1].strip()
            break
        if line.startswith("REQUEST_KIND:") and mode == "answer":
            mode = line.split(":", 1)[1].strip()

    if "CAPABILITY PACKET:" in user or mode in {"status", "capability"}:
        if channel == "telegram":
            return (
                "<b>Capability</b>\n"
                f"- channel={channel}\n"
                "- answer=operational capability summary\n"
                "- style=chat-friendly\n"
                "- evidence=available\n"
            )
        return (
            "SUMMARY:\n"
            f"channel={channel}\n"
            "answer=operational capability summary\n"
            "style=structured\n"
            "evidence=available\n"
        )

    if mode in {"review"}:
        if channel == "telegram":
            return "<b>Verdict</b>\n- reviewed=ok\n- next_action=manual decision"
        return "VERDICT:\nreviewed=ok\nnext_action=manual decision"

    if mode in {"debug"}:
        if channel == "telegram":
            return "<b>Root cause</b>\n- evidence=available\n- fix_path=follow the matrix"
        return "ROOT_CAUSE:\nevidence=available\nfix_path=follow the matrix"

    if mode in {"task", "agentic"}:
        if channel == "telegram":
            return "<b>Action summary</b>\n- what_was_done=verified\n- what_next=continue\n- verification=ok"
        return "SUMMARY:\nwhat_was_done=verified\nwhat_next=continue\nverification=ok"

    if channel == "telegram":
        return "<b>Answer</b>\n- evidence=ok\n- channel=telegram"
    return "SUMMARY:\nevidence=ok\nchannel=mcp"


async def _no_op_async(*args, **kwargs):
    return None


def _build_telegram_update(case_id: str, text: str) -> dict:
    base = 100000 + int(case_id[1:])
    return {
        "message": {
            "message_id": base,
            "chat": {"id": int(str(TELEGRAM_CHAT)) if str(TELEGRAM_CHAT).strip() else 0},
            "from": {"username": "core_owner"},
            "text": text,
        }
    }


def _build_mcp_request(text: str) -> dict:
    return {"method": "tools/call", "params": {"query": text}, "id": 1}


async def _run_case(case) -> tuple[dict, list[str]]:
    # Build a dry message profile first so we can compare the live path with the
    # deterministic stress matrix.
    raw_source = "telegram" if case.source == "telegram" else "mcp"
    raw_input = _build_telegram_update(case.case_id, case.text) if raw_source == "telegram" else _build_mcp_request(case.text)

    with ExitStack() as stack:
        import core_orch_layer3 as l3
        import core_orch_layer9 as l9
        import core_orch_layer10 as l10
        import core_orch_layer1 as l1
        import core_orch_layer11 as l11

        stack.enter_context(patch.object(l3, "gemini_chat", side_effect=_fake_gemini_chat))
        stack.enter_context(patch.object(l9, "groq_chat", side_effect=_fake_groq_chat))
        stack.enter_context(patch.object(l10, "_notify_with_reply", return_value=True))
        stack.enter_context(patch.object(l10, "_send_followup", side_effect=_no_op_async))
        stack.enter_context(patch.object(l10, "_write_history_turn", side_effect=_no_op_async))
        stack.enter_context(patch.object(l10, "_log_conversation", side_effect=_no_op_async))
        stack.enter_context(patch.object(l10, "notify", return_value=True))
        stack.enter_context(patch.object(l1, "_send_typing", side_effect=_no_op_async))
        stack.enter_context(patch.object(l11, "fire_session", return_value=None))

        timeout_s = float(os.getenv("CORE_ORC_SMOKE_CASE_TIMEOUT", "90"))
        msg = await asyncio.wait_for(
            layer_1_triage(raw_input if raw_source == "mcp" else raw_input, input_type=raw_source),
            timeout=timeout_s,
        )

    decision = msg.context.get("decision_packet", {}) or {}
    gate = msg.context.get("evidence_gate", {}) or {}
    style = msg.context.get("response_style_packet", {}) or {}
    profile = msg.context.get("input_profile", {}) or {}
    actual = {
        "primary_class": profile.get("primary_class"),
        "request_kind": msg.request_kind,
        "response_mode": msg.response_mode,
        "style_mode": style.get("mode"),
        "use_html": style.get("use_html"),
        "delivery_channel": style.get("delivery_channel"),
        "agentic": should_use_agentic_mode(msg),
        "clarification": bool(decision.get("clarification_needed", False)),
        "gate_mode": gate.get("retrieval_mode"),
        "gate_clarify": bool(gate.get("needs_clarification_after_retrieval", False)),
        "repo_map_needed": bool(gate.get("repo_map_needed", False)),
        "public_family": gate.get("public_family"),
        "public_needed": bool(gate.get("public_research_needed", False)),
        "final_output": msg.final_output or "",
        "styled_response": msg.styled_response or "",
        "has_errors": msg.has_errors,
    }
    failures: list[str] = []
    for key, expected in case.expect.items():
        if actual.get(key) != expected:
            failures.append(f"{case.case_id}: {key} expected {expected!r} got {actual.get(key)!r}")

    if raw_source == "telegram":
        if not actual["final_output"] or actual["final_output"].strip().startswith("{"):
            failures.append(f"{case.case_id}: telegram output is not chat-formatted")
    else:
        try:
            payload = json.loads(actual["final_output"])
            if payload.get("response_style_packet", {}).get("delivery_channel") != "mcp":
                failures.append(f"{case.case_id}: MCP delivery channel missing from payload")
            if payload.get("success") is not True:
                failures.append(f"{case.case_id}: MCP payload not marked success")
        except Exception as exc:
            failures.append(f"{case.case_id}: MCP final output is not valid JSON: {exc}")

    if actual["has_errors"]:
        failures.append(f"{case.case_id}: pipeline produced errors {msg.errors!r}")
    return actual, failures


async def main() -> int:
    all_failures: list[str] = []
    for case in MATRIX:
        started = time.monotonic()
        print(f"[RUN ] {case.case_id} {case.source} {case.difficulty}: {case.text[:72]}", flush=True)
        try:
            actual, failures = await _run_case(case)
        except asyncio.TimeoutError:
            failures = [f"{case.case_id}: timed out while running ORC smoke path"]
            actual = {}
        elapsed = time.monotonic() - started
        status = "PASS" if not failures else "FAIL"
        print(f"[{status}] {case.case_id} {case.source} {case.difficulty} ({elapsed:.1f}s): {case.text[:72]}", flush=True)
        if failures:
            for item in failures:
                print(f"  - {item}", flush=True)
        all_failures.extend(failures)

    summary = {
        "cases": len(MATRIX),
        "failed_cases": len({f.split(':', 1)[0] for f in all_failures}),
        "failures": all_failures,
    }
    print("\nSUMMARY:", json.dumps(summary, default=str), flush=True)
    return 1 if all_failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
