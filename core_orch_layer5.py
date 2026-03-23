"""
core_orch_layer5.py — L5: OUTPUT
==================================
Formats, chunks, and delivers the final reply to Telegram.
Owns the confirmation gate for destructive operations.
Enforces silence policy for background loop noise.

Confirmation gate:
  - Destructive ops → send CONFIRM/REJECT to owner → wait CONFIRM_TIMEOUT_S
  - Timeout → auto-abort (NEVER auto-confirm — C2)
  - Reply CONFIRM/yes/lanjut → execute
  - Reply REJECT/no/batal  → abort silently

Silence policy (background loops):
  Notify owner ONLY when:
    - error
    - task milestone completed
    - evolution ready for review
    - deploy status change
    - heartbeat miss ×3
    - explicitly requested summary
  Minimum interval between proactive messages: PROACTIVE_MIN_INTERVAL_S
"""

import asyncio
import threading
import time
from datetime import datetime

CONFIRM_TIMEOUT_S        = 120
TG_MAX_CHARS             = 4000
PROACTIVE_MIN_INTERVAL_S = 600   # 10 min between unprompted notifications

# ── Pending confirmation state ────────────────────────────────────────────────
_pending: dict  = {}   # cid → {"event": Event, "confirmed": bool|None}
_pending_lock   = threading.Lock()

# ── Proactive message rate limiting ──────────────────────────────────────────
_last_proactive: dict = {}   # cid → last send ts
_proactive_lock       = threading.Lock()


# ── Telegram delivery ─────────────────────────────────────────────────────────

def _tg_send(cid: str, text: str):
    """Send message to Telegram. Chunks semantically if > TG_MAX_CHARS."""
    from core_config import notify

    if not text:
        return

    if len(text) <= TG_MAX_CHARS:
        notify(text, cid)
        return

    # Semantic chunking: split at double-newlines (paragraphs) first
    parts = text.split("\n\n")
    chunk = ""
    part_num = 1

    for para in parts:
        if len(chunk) + len(para) + 2 <= TG_MAX_CHARS:
            chunk = (chunk + "\n\n" + para).lstrip("\n")
        else:
            if chunk:
                notify(chunk, cid)
                time.sleep(0.3)
                part_num += 1
            chunk = para

    if chunk:
        notify(chunk, cid)


def _tg_typing(cid: str):
    try:
        import os, httpx
        token = os.environ.get("TELEGRAM_TOKEN", "")
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendChatAction",
            data={"chat_id": cid, "action": "typing"},
            timeout=5,
        )
    except Exception:
        pass


# ── Main output ───────────────────────────────────────────────────────────────

async def layer_5_output(intent: dict, reply: str, tool_results: list):
    """
    Format and deliver reply. Also saves to conversation history.
    Called from L4 (after tool loop) and L3 (direct answers).
    """
    cid = intent["sender_id"]

    if not reply:
        reply = "✅ Done."

    # Append to conversation history
    from core_orch_layer2 import append_history
    append_history(cid, "assistant", reply)

    # Deliver
    _tg_send(cid, reply)
    print(f"[L5] Delivered to {cid}: {len(reply)}c")


# ── Confirmation gate ─────────────────────────────────────────────────────────

async def layer_5_request_confirm(intent: dict, execution_plan: dict) -> bool:
    """
    Send CONFIRM/REJECT prompt to owner. Block until response or timeout.
    Returns True if confirmed, False if rejected or timed out.
    Per Constitution C2: timeout = abort, NEVER auto-confirm.
    """
    cid        = intent["sender_id"]
    plan_text  = "\n".join(f"  {i+1}. {s}" for i, s in
                           enumerate(execution_plan.get("plan", [])[:5]))
    risk       = execution_plan.get("risk", "unknown")
    intent_str = execution_plan.get("intent_parsed", intent["text"])[:200]

    msg = (
        f"⚠️ <b>CONFIRMATION REQUIRED</b>\n\n"
        f"Intent: {intent_str}\n"
        f"Risk: <b>{risk.upper()}</b>\n\n"
        f"Plan:\n{plan_text}\n\n"
        f"Reply <b>CONFIRM</b> to proceed or <b>REJECT</b> to cancel.\n"
        f"<i>Timeout {CONFIRM_TIMEOUT_S}s → auto-abort.</i>"
    )

    event = threading.Event()
    with _pending_lock:
        _pending[cid] = {"event": event, "confirmed": None}

    _tg_send(cid, msg)

    # Wait for response
    loop = asyncio.get_event_loop()
    fired = await loop.run_in_executor(None, event.wait, CONFIRM_TIMEOUT_S)

    with _pending_lock:
        gate      = _pending.pop(cid, {})
        confirmed = gate.get("confirmed", False)

    if not fired:
        _tg_send(cid, "⏱ Confirmation timed out — action aborted.")
        return False

    if not confirmed:
        _tg_send(cid, "🚫 Action cancelled.")
        return False

    return True


async def receive_confirmation(cid: str, text: str) -> bool:
    """
    Called from L1 when a CONFIRM/REJECT message comes in.
    Returns True if the message was handled as a confirmation reply.
    """
    _CONFIRM_YES = {"confirm", "yes", "y", "ok", "go", "do it", "proceed", "lanjut"}
    _CONFIRM_NO  = {"reject", "cancel", "no", "n", "stop", "abort", "skip", "batal"}

    lower = text.strip().lower()

    with _pending_lock:
        gate = _pending.get(cid)
        if not gate:
            return False   # no pending gate for this user

        if lower in _CONFIRM_YES:
            gate["confirmed"] = True
            gate["event"].set()
            return True
        elif lower in _CONFIRM_NO:
            gate["confirmed"] = False
            gate["event"].set()
            return True

    return False


# ── Proactive outreach (background loops → L6) ───────────────────────────────

def send_proactive(cid: str, message: str, force: bool = False) -> bool:
    """
    Send a proactive notification respecting silence policy.
    force=True bypasses rate limit (for critical errors only).
    Returns True if message was sent.
    """
    if not force:
        with _proactive_lock:
            last = _last_proactive.get(cid, 0)
            if time.time() - last < PROACTIVE_MIN_INTERVAL_S:
                print(f"[L5] Proactive silenced (rate limit): {message[:60]!r}")
                return False
            _last_proactive[cid] = time.time()

    _tg_send(cid, message)
    return True


if __name__ == "__main__":
    print("🛰️ Layer 5: Output — Online.")
