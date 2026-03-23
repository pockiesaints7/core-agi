"""
core_orch_layer6.py — L6: AUTONOMY (Background Loops + Validation)
====================================================================
NOTE: This file serves dual purpose:
  1. VALIDATION gate — called by L4 to validate tool output before passing to L7
  2. AUTONOMY helpers — background loop utilities with L0 permission boundary

Per Blueprint L6 + Constitution C9:
  Background loops inherit L0 permissions ONLY.
  Destructive ops from background loops → must still route through L5 confirm gate.
  Loops never self-authorize force_close or evolution approval.

Validation responsibilities:
  - Schema check: does result match expected output structure?
  - Sanity check: does result actually answer the intent?
  - Hallucination guard: fast rule-based (no LLM call)
  - Syntax validate: for code-change tool results

FIXES (v2):
  - BUG-L6-2:  Narration check patterns tightened — removed overly broad patterns
               that matched valid replies ("will confirm", "will retrieve")
  - BUG-L6-5:  Task sweeper Supabase query now includes id=gt.1 filter
               (HARD RULE: bigserial PK tables always require id=gt.1)
  - GAP-L6-7:  layer_6_validate return value extended with correction_needed
               sentinel so L4/L8 can detect hallucination and trigger re-gen
  - NEW-L6-11: Consistent API: narration issues flagged separately; ok=True
               for narration (not a hard block), ok=False only for
               hallucination and prompt_leak
"""

import json
import re
import time
import asyncio
import threading
import os
from datetime import datetime

# ── Hallucination guard ───────────────────────────────────────────────────────

_UUID_RE      = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
_SCORE_RE     = re.compile(r'quality[^.\n]{0,50}(0\.[0-9]{2})', re.I)
_CONFIRMED_RE = re.compile(r'(?:CONFIRMED|VERIFIED)[^.\n]{0,200}', re.I)


def _hallucination_guard(reply: str, tool_results: list) -> dict:
    """
    Fast rule-based guard — no LLM call.
    Checks if reply contains specific values (UUIDs, scores, IDs)
    not present in any tool result.
    """
    if not reply or not tool_results:
        return {"ok": True}

    # Normalise all tool results to string for substring matching
    all_results_text = " ".join(
        str(r.get("result", "")) for r in tool_results
    ).lower()

    suspicious = []

    # Check UUIDs in reply vs tool results
    for uid in _UUID_RE.findall(reply):
        if uid.lower() not in all_results_text:
            suspicious.append(f"UUID not in tool results: {uid[:18]}")

    # Check quality scores claimed as CONFIRMED but not in results
    for claim in _CONFIRMED_RE.findall(reply):
        nums = re.findall(r'\d+\.\d+|\d{4,}', claim)
        for num in nums:
            if num not in all_results_text:
                suspicious.append(f"Confirmed value not in results: {claim[:60]}")
                break

    # Check quality scores
    for score in _SCORE_RE.findall(reply):
        if score not in all_results_text:
            suspicious.append(f"Quality score {score} not in tool results")

    if suspicious:
        print(f"[L6] Hallucination guard: {suspicious[:2]}")
        return {
            "ok":               False,
            "suspicious":       suspicious[:3],
            "correction_hint":  (
                "Reply contains data not found in any tool result. "
                "Do NOT invent values. Report only what the tools returned. "
                "If a tool returned no data, say so explicitly."
            ),
        }

    return {"ok": True}


def _prompt_leak_check(reply: str) -> bool:
    """Return True if system prompt content leaked into reply."""
    _LEAK_KW = [
        "CORE OPERATING MANDATE", "PRIME DIRECTIVE", "EXECUTION PHILOSOPHY",
        "You are CORE", "BEHAVIORAL RULES:", "ACTIVE GOALS (cross-session",
        "CONSTITUTION:", "C1 ", "C2 ", "C10 ",
    ]
    return any(kw in reply for kw in _LEAK_KW)


def _narration_check(reply: str) -> bool:
    """Return True if reply narrates instead of executing.
    FIX BUG-L6-2: patterns tightened to avoid false positives on valid replies.
    Removed: 'will retrieve', 'will confirm' — too common in legitimate responses.
    Kept only patterns that are unambiguously narration/planning, not execution.
    """
    _NARR = [
        r"^I will now\b",        # Only at start of reply
        r"^I am going to\b",
        r"\bI'll call\b",
        r"\bI would call\b",
        r"\bLet me call\b",
        r"\bI should call\b",
        r"\bI'll use the\b",
        r"\bI will use the\b",
        r"\bTo do this, I\b",
        r"\bI'll execute\b",
        r"\bI will execute\b",
        r"\bI am executing\b",
    ]
    for p in _NARR:
        if re.search(p, reply, re.IGNORECASE | re.MULTILINE):
            return True
    return False


# ── Validation entry point ────────────────────────────────────────────────────

async def layer_6_validate(
    intent: dict,
    reply: str,
    tool_results: list,
) -> dict:
    """
    Validates the reply before it reaches L7/L5 output.

    Returns:
      {
        "ok":               bool,       # False = hard block (hallucination, prompt_leak)
        "reply":            str,        # Possibly prefixed with correction sentinel
        "issues":           list,       # All detected issues
        "correction_needed": bool,      # True if L4/L8 should trigger re-generation
      }

    FIX GAP-L6-7: 'correction_needed' sentinel added so L4/L8 can detect
    hallucination and trigger a re-generation pass rather than passing corrupted
    reply downstream.

    FIX NEW-L6-11: ok=True for narration-only issues; ok=False only for
    hallucination and prompt_leak (hard blocks).
    """
    issues = []
    corrected_reply     = reply
    correction_needed   = False

    # 1. Hallucination guard
    hg = _hallucination_guard(reply, tool_results)
    if not hg["ok"]:
        issues.append({"type": "hallucination", "detail": hg.get("suspicious", [])})
        correction_needed = True
        corrected_reply = (
            f"[CORRECTION_REQUIRED]\n{hg.get('correction_hint', '')}\n\n"
            f"Original (possibly hallucinated):\n{reply[:500]}"
        )

    # 2. Prompt leak
    if _prompt_leak_check(reply):
        issues.append({"type": "prompt_leak"})
        correction_needed = True
        corrected_reply = "[System prompt leaked into reply — response blocked]"

    # 3. Narration instead of execution
    # Flag only, don't block (ok stays True for narration alone).
    # L4/L8 handles retry on narration.
    if _narration_check(reply) and not tool_results:
        issues.append({"type": "narration", "detail": "response describes action instead of executing"})
        # narration does NOT set correction_needed — L4/L8 has its own retry for this

    hard_block_types = {"hallucination", "prompt_leak"}
    is_hard_blocked  = any(i["type"] in hard_block_types for i in issues)

    if issues:
        print(f"[L6] Validation issues: {[i['type'] for i in issues]}")

    return {
        "ok":               not is_hard_blocked,
        "reply":            corrected_reply,
        "issues":           issues,
        "correction_needed": correction_needed,
    }


# ── AUTONOMY: Background loop helpers ────────────────────────────────────────
# Per C9: background loops CANNOT elevate permissions.
# All destructive actions from loops must route through L5 confirm gate.

OWNER_ID = os.environ.get("TELEGRAM_CHAT", "")


def _bg_notify(message: str, force: bool = False):
    """Background-safe notification. Uses L5 silence policy."""
    try:
        from core_orch_layer5 import send_proactive
        send_proactive(OWNER_ID, message, force=force)
    except Exception as e:
        print(f"[L6/bg] notify error: {e}")


def start_heartbeat(interval_s: int = 300):
    """Heartbeat: polls /health every interval_s. Alerts owner on 3 consecutive failures."""
    fail_count = [0]

    def _run():
        while True:
            time.sleep(interval_s)
            try:
                from core_tools import t_health
                h = t_health()
                overall = h.get("overall", "degraded")
                if overall == "ok":
                    fail_count[0] = 0
                else:
                    fail_count[0] += 1
                    print(f"[L6/heartbeat] Degraded ({fail_count[0]}): {h.get('components')}")
                    if fail_count[0] >= 3:
                        _bg_notify(
                            f"⚠️ <b>CORE Heartbeat Alert</b>\n"
                            f"Health degraded for {fail_count[0]} consecutive checks.\n"
                            f"Components: {h.get('components')}",
                            force=True,
                        )
                        fail_count[0] = 0
            except Exception as e:
                fail_count[0] += 1
                print(f"[L6/heartbeat] Exception ({fail_count[0]}): {e}")
                if fail_count[0] >= 3:
                    _bg_notify(f"🚨 CORE Heartbeat EXCEPTION: {e}", force=True)
                    fail_count[0] = 0

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print("[L6] Heartbeat started")
    return t


def start_task_sweeper(interval_s: int = 900):
    """Checks task_queue for stale in_progress tasks. Flags and alerts.
    FIX BUG-L6-5: Added id=gt.1 filter — required HARD RULE for bigserial PK tables.
    """
    STALE_THRESHOLD_H = 4

    def _run():
        while True:
            time.sleep(interval_s)
            try:
                from core_config import sb_get
                from datetime import timedelta
                cutoff = (datetime.utcnow() - timedelta(hours=STALE_THRESHOLD_H)).isoformat()
                stale = sb_get(
                    "task_queue",
                    # FIX: id=gt.1 required for bigserial PK tables (HARD RULE)
                    f"select=id,task,priority,updated_at"
                    f"&id=gt.1"
                    f"&status=eq.in_progress"
                    f"&updated_at=lt.{cutoff}"
                    f"&order=priority.desc&limit=5",
                ) or []
                if stale:
                    lines = []
                    for task_row in stale:
                        raw = task_row.get("task", "")
                        try:
                            title = (
                                json.loads(raw).get("title", raw[:60])
                                if isinstance(raw, str) else str(raw)[:60]
                            )
                        except Exception:
                            title = str(raw)[:60]
                        lines.append(f"  P{task_row.get('priority', '?')} — {title}")
                    _bg_notify(
                        f"⚠️ <b>Stale Tasks Detected</b>\n"
                        f"{len(stale)} task(s) stuck in_progress >{STALE_THRESHOLD_H}h:\n"
                        + "\n".join(lines),
                    )
            except Exception as e:
                print(f"[L6/sweeper] Error: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print("[L6] Task sweeper started")
    return t


def start_all_background_loops():
    """Start all L6 autonomous background processes. Called from startup."""
    start_heartbeat()
    start_task_sweeper()
    print("[L6] All background loops started")


if __name__ == "__main__":
    print("🛰️ Layer 6: Autonomy / Validation — Online.")
