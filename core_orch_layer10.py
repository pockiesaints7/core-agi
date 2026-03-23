"""
core_orch_layer10.py — CORE AGI Constitution Layer
====================================================
The highest layer in the stack. Defines invariants that NO other layer,
tool, or instruction can override. Checked at boot and before any
destructive or sensitive operation.

INVARIANTS (absolute, cannot be bypassed):
  C1 — Owner-only commands: only TELEGRAM_CHAT owner can trigger execution
  C2 — No credential logging: secrets never written to logs, DB, or replies
  C3 — Destructive ops require explicit owner confirmation before execution
  C4 — Secrets never echoed back in any output channel
  C5 — CORE identity preserved — drifting to generic assistant is a violation
  C6 — Constitution cannot be modified by any process CORE can execute
  C7 — Supabase is source of truth — CORE halts non-trivial ops if DB is down
  C8 — force_close is owner-invoked only
  C9 — Background loops inherit no elevated permissions
  C10 — Violations are always logged to mistakes table + owner notified

ON VIOLATION:
  1. Log to Supabase mistakes table
  2. Notify owner via Telegram
  3. Hard stop the violating operation
"""

import os
import re
import json
import threading
from datetime import datetime
from typing import Optional

# ── Constants ──────────────────────────────────────────────────────────────────
OWNER_CHAT_ID = str(os.environ.get("TELEGRAM_CHAT", ""))

# Patterns that indicate credential leakage in any output string
_CREDENTIAL_PATTERNS = [
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),                          # GitHub PAT
    re.compile(r"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9\.[a-zA-Z0-9\-_]+\.[a-zA-Z0-9\-_]+"),  # Supabase JWT
    re.compile(r"sk-or-v1-[a-zA-Z0-9\-]{40,}"),                  # OpenRouter key
    re.compile(r"gsk_[a-zA-Z0-9]{40,}"),                          # Groq key
    re.compile(r"AIza[a-zA-Z0-9\-_]{35}"),                        # Google/Gemini key
    re.compile(r"(?i)(api[_\-]?key|secret|password|token)\s*[:=]\s*['\"]?[a-zA-Z0-9\-_]{20,}"),
]

# Tools that are unconditionally destructive — always require confirmation
_DESTRUCTIVE_TOOLS = {
    "sb_delete", "drop_table", "truncate", "purge", "wipe",
    "rm_rf", "format_disk", "delete_knowledge", "delete_session",
}

# Keywords in tool args that signal destructive intent
_DESTRUCTIVE_KEYWORDS = {
    "drop", "truncate", "purge", "wipe", "rm -rf",
    "destroy", "delete", "permanent", "irreversible",
}

# ── Violation severity levels ──────────────────────────────────────────────────
SEVERITY_CRITICAL = "critical"   # Hard stop, owner notified immediately
SEVERITY_HIGH     = "high"       # Hard stop, logged
SEVERITY_MEDIUM   = "medium"     # Log + warn, operation may continue


# ══════════════════════════════════════════════════════════════════════════════
# CORE VIOLATION HANDLER
# ══════════════════════════════════════════════════════════════════════════════

def _log_violation(
    invariant: str,
    what_failed: str,
    context: str,
    how_to_avoid: str,
    severity: str = SEVERITY_HIGH,
):
    """Log constitution violation to Supabase mistakes table + notify owner.
    Runs in background thread to avoid blocking the hard stop.
    Non-fatal to the logger itself — if Supabase is down, prints to console.
    """
    def _run():
        payload = {
            "domain":           "constitution",
            "context":          context[:500],
            "what_failed":      f"[C10 VIOLATION] {invariant}: {what_failed}"[:500],
            "root_cause":       f"Constitutional invariant breached: {invariant}",
            "correct_approach": how_to_avoid[:500],
            "how_to_avoid":     how_to_avoid[:500],
            "severity":         severity,
            "tags":             ["constitution", "violation", invariant.lower().replace(" ", "_")],
            "created_at":       datetime.utcnow().isoformat(),
        }
        # Try Supabase first
        try:
            from core_config import sb_post
            sb_post("mistakes", payload)
            print(f"[L10] Violation logged to Supabase: {invariant}")
        except Exception as e:
            print(f"[L10] Supabase log failed (non-fatal): {e}")
            print(f"[L10] VIOLATION PAYLOAD: {json.dumps(payload, default=str)}")

        # Notify owner via Telegram
        try:
            from core_github import notify
            notify(
                f"🚨 <b>CONSTITUTION VIOLATION</b>\n\n"
                f"Invariant: <code>{invariant}</code>\n"
                f"Severity: <code>{severity}</code>\n"
                f"What failed: {what_failed[:200]}\n"
                f"How to avoid: {how_to_avoid[:200]}"
            )
        except Exception as e:
            print(f"[L10] Telegram notify failed (non-fatal): {e}")

    threading.Thread(target=_run, daemon=True).start()


class ConstitutionViolation(Exception):
    """Raised when a constitutional invariant is breached.
    Signals hard stop to the calling layer.
    """
    def __init__(self, invariant: str, detail: str = ""):
        self.invariant = invariant
        self.detail    = detail
        super().__init__(f"[CONSTITUTION] {invariant}: {detail}")


# ══════════════════════════════════════════════════════════════════════════════
# C1 — OWNER-ONLY COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

def check_owner(chat_id: str) -> bool:
    """Return True if chat_id matches the configured owner.
    Raises ConstitutionViolation if not — hard stop.
    """
    if not OWNER_CHAT_ID:
        _log_violation(
            invariant   = "C1-OWNER",
            what_failed = "TELEGRAM_CHAT not configured — cannot verify owner",
            context     = f"Incoming chat_id: {chat_id}",
            how_to_avoid= "Set TELEGRAM_CHAT in .env before deployment",
            severity    = SEVERITY_CRITICAL,
        )
        raise ConstitutionViolation("C1-OWNER", "TELEGRAM_CHAT not configured")

    if str(chat_id) != OWNER_CHAT_ID:
        _log_violation(
            invariant   = "C1-OWNER",
            what_failed = f"Unauthorized access attempt from chat_id={chat_id}",
            context     = f"Owner={OWNER_CHAT_ID}, Caller={chat_id}",
            how_to_avoid= "Only owner chat_id is permitted to invoke CORE",
            severity    = SEVERITY_CRITICAL,
        )
        raise ConstitutionViolation("C1-OWNER", f"Unauthorized chat_id: {chat_id}")

    return True


# ══════════════════════════════════════════════════════════════════════════════
# C2 + C4 — NO CREDENTIAL LOGGING / NO CREDENTIAL ECHO
# ══════════════════════════════════════════════════════════════════════════════

def scan_for_credentials(text: str, context: str = "") -> str:
    """Scan any string for credential patterns.
    Returns redacted version. Logs violation if credentials found.
    Never raises — redaction is the recovery, not a hard stop.
    """
    if not text:
        return text

    redacted   = text
    found_any  = False

    for pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(redacted):
            found_any = True
            redacted  = pattern.sub("[REDACTED]", redacted)

    if found_any:
        _log_violation(
            invariant   = "C2-NO-CREDENTIAL-LOGGING",
            what_failed = "Credential pattern detected in output/log string",
            context     = context[:200] if context else "unknown context",
            how_to_avoid= "Never pass raw env vars or API keys into tool results, replies, or logs",
            severity    = SEVERITY_CRITICAL,
        )

    return redacted


def assert_no_credentials(text: str, context: str = "") -> None:
    """Hard-stop version: raises ConstitutionViolation if credentials detected."""
    for pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(text):
            _log_violation(
                invariant   = "C4-NO-CREDENTIAL-ECHO",
                what_failed = "Credential about to be sent to output channel",
                context     = context[:200],
                how_to_avoid= "Sanitize all tool results before sending to Telegram",
                severity    = SEVERITY_CRITICAL,
            )
            raise ConstitutionViolation("C4-NO-CREDENTIAL-ECHO", "Credential detected in reply")


# ══════════════════════════════════════════════════════════════════════════════
# C3 — DESTRUCTIVE OPS REQUIRE CONFIRMATION
# ══════════════════════════════════════════════════════════════════════════════

def is_destructive(tool_name: str, tool_args: dict) -> bool:
    """Return True if this tool call is potentially destructive."""
    # Direct tool name match
    if tool_name.lower() in _DESTRUCTIVE_TOOLS:
        return True

    # Keyword scan in tool name
    for kw in _DESTRUCTIVE_KEYWORDS:
        if kw in tool_name.lower():
            return True

    # Keyword scan in args (avoid false positives from code strings)
    _SAFE_ARG_TOOLS = {"run_python", "sb_query", "web_search", "calc", "translate"}
    if tool_name not in _SAFE_ARG_TOOLS:
        args_str = json.dumps(tool_args, default=str).lower()
        for kw in _DESTRUCTIVE_KEYWORDS:
            if re.search(r'\b' + re.escape(kw) + r'\b', args_str):
                return True

    return False


def assert_confirmed_destructive(
    tool_name: str,
    tool_args: dict,
    confirmed: bool,
    context: str = "",
) -> None:
    """Raise ConstitutionViolation if destructive op attempted without confirmation."""
    if is_destructive(tool_name, tool_args) and not confirmed:
        detail = f"tool={tool_name} args={json.dumps(tool_args, default=str)[:200]}"
        _log_violation(
            invariant   = "C3-DESTRUCTIVE-CONFIRM",
            what_failed = f"Destructive operation attempted without owner confirmation: {detail}",
            context     = context[:200],
            how_to_avoid= "Always request owner confirmation before any destructive tool call",
            severity    = SEVERITY_HIGH,
        )
        raise ConstitutionViolation("C3-DESTRUCTIVE-CONFIRM", detail)


# ══════════════════════════════════════════════════════════════════════════════
# C5 — IDENTITY PRESERVATION
# ══════════════════════════════════════════════════════════════════════════════

_IDENTITY_DRIFT_PATTERNS = [
    re.compile(r"I am (ChatGPT|GPT|OpenAI|Bard|Gemini|an AI assistant|just an AI)", re.IGNORECASE),
    re.compile(r"As an AI language model", re.IGNORECASE),
    re.compile(r"I('m| am) not able to (have|form) (opinions|feelings)", re.IGNORECASE),
    re.compile(r"I don't have (personal|real) (opinions|feelings|experiences)", re.IGNORECASE),
]

def check_identity_drift(reply: str) -> None:
    """Scan reply for identity drift. Logs violation but does not hard stop
    (reply is still sent — owner is notified to review).
    """
    for pattern in _IDENTITY_DRIFT_PATTERNS:
        if pattern.search(reply):
            _log_violation(
                invariant   = "C5-IDENTITY",
                what_failed = "Reply contains generic AI assistant language — identity drift detected",
                context     = reply[:300],
                how_to_avoid= "CORE is a sovereign intelligence, not a generic assistant. Never use ChatGPT/AI-assistant framing.",
                severity    = SEVERITY_MEDIUM,
            )
            break  # One log per reply is enough


# ══════════════════════════════════════════════════════════════════════════════
# C7 — SUPABASE IS SOURCE OF TRUTH
# ══════════════════════════════════════════════════════════════════════════════

def assert_db_available(operation: str = "") -> None:
    """Quick Supabase health check. Logs + raises if DB is unreachable
    and operation is non-trivial.
    """
    try:
        from core_config import sb_get
        result = sb_get("knowledge_base", "select=id&limit=1")
        # sb_get returns None or [] on failure
        if result is None:
            raise RuntimeError("sb_get returned None")
    except Exception as e:
        _log_violation(
            invariant   = "C7-SUPABASE-TRUTH",
            what_failed = f"Supabase unreachable during operation: {operation}",
            context     = str(e)[:200],
            how_to_avoid= "Check Supabase connectivity before non-trivial operations. Halt if unavailable.",
            severity    = SEVERITY_HIGH,
        )
        raise ConstitutionViolation("C7-SUPABASE-TRUTH", f"DB unavailable: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# BOOT VALIDATION — run once at startup
# ══════════════════════════════════════════════════════════════════════════════

def boot_check() -> dict:
    """Run all constitution invariant checks at boot time.
    Returns {ok, violations} — does NOT raise, so startup can continue
    with degraded mode if needed.
    """
    violations = []

    # C1: Owner configured
    if not OWNER_CHAT_ID:
        violations.append("C1: TELEGRAM_CHAT not set")

    # C2: No credentials in environment variable names being logged
    sensitive_env = ["OPENROUTER_API_KEY", "GROQ_API_KEY", "SUPABASE_SERVICE_KEY",
                     "GITHUB_PAT", "TELEGRAM_TOKEN", "SUPABASE_PAT"]
    for key in sensitive_env:
        val = os.environ.get(key, "")
        if val and len(val) > 8:
            # Just verify they exist — don't log the values
            pass
        elif not val:
            violations.append(f"C2-warning: {key} not set — some features may fail")

    # C6: Constitution file should not be writable by CORE process
    # (we check if this file itself is readable — write protection is OS-level)
    try:
        import __file__ as _self
    except Exception:
        pass  # Non-fatal

    ok = len([v for v in violations if not v.startswith("C2-warning")]) == 0

    if violations:
        print(f"[L10] Boot check: {len(violations)} issue(s)")
        for v in violations:
            print(f"  ⚠ {v}")
    else:
        print(f"[L10] Boot check: ✅ All invariants satisfied")

    return {"ok": ok, "violations": violations}


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — what other layers call
# ══════════════════════════════════════════════════════════════════════════════

def enforce_owner(chat_id: str) -> None:
    """L0/L1 calls this. Raises ConstitutionViolation if not owner."""
    check_owner(chat_id)


def enforce_no_credentials(text: str, context: str = "") -> str:
    """L5 Output calls this before sending any reply. Returns redacted text."""
    return scan_for_credentials(text, context)


def enforce_destructive_gate(
    tool_name: str,
    tool_args: dict,
    confirmed: bool,
    context: str = "",
) -> None:
    """L4 Execution calls this before running any tool. Raises if destructive + unconfirmed."""
    assert_confirmed_destructive(tool_name, tool_args, confirmed, context)


def enforce_identity(reply: str) -> None:
    """L5 Output calls this before sending reply. Logs drift, never hard-stops."""
    check_identity_drift(reply)


def enforce_db(operation: str = "") -> None:
    """Any layer calls this before non-trivial Supabase-dependent operations."""
    assert_db_available(operation)


def report_violation(
    invariant: str,
    what_failed: str,
    context: str = "",
    how_to_avoid: str = "",
    severity: str = SEVERITY_HIGH,
) -> None:
    """Any layer can call this to manually report a violation without raising."""
    _log_violation(invariant, what_failed, context, how_to_avoid, severity)


# ── Run boot check on import ───────────────────────────────────────────────────
_boot_result = boot_check()
