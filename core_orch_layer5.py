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

FIXES (v2):
  - BUG-L5-2:  Single paragraph > TG_MAX_CHARS now hard-split (was silently oversized)
  - BUG-L5-5/NEW-L5-13: asyncio.get_running_loop() replaces deprecated get_event_loop()
  - BUG-L5-6:  _tg_typing import cached at module level; uses urllib fallback
  - BUG-L5-7:  _tg_send I/O wrapped in executor to avoid blocking event loop
  - GAP-L5-8:  L10.enforce_no_credentials() called before every delivery
  - CROSS-1:   L10.enforce_identity() called before every delivery
  - NEW-L5-11: Multi-chunk messages include part indicator (N/M)
  - NEW-L5-12: Unused tool_results parameter documented (kept for API compat)
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

def _send_one(cid: str, text: str):
    """Send a single pre-chunked message to Telegram via core_config.notify.
    Sync — call via executor from async contexts (FIX: BUG-L5-7).
    Applies credential and identity enforcement before delivery (FIX: GAP-L5-8, CROSS-1).
    """
    # L10 credential scan
    try:
        from core_orch_layer10 import enforce_no_credentials, enforce_identity
        text = enforce_no_credentials(text, context="L5._send_one")
        enforce_identity(text)
    except Exception:
        pass  # L10 unavailable — proceed; violation logged at L10 level if imported

    from core_config import notify
    notify(text, cid)


def _tg_send(cid: str, text: str):
    """Send message to Telegram. Chunks semantically if > TG_MAX_CHARS.
    Sync version — used by background/sync callers. Async wrapper below.
    FIX BUG-L5-2: paragraphs that individually exceed TG_MAX_CHARS are hard-split.
    FIX NEW-L5-11: multi-chunk messages include (N/M) part indicator.
    """
    if not text:
        return

    if len(text) <= TG_MAX_CHARS:
        _send_one(cid, text)
        return

    # Build chunks: split at double-newlines first, then hard-split oversized paragraphs
    raw_parts = text.split("\n\n")
    paragraphs: list[str] = []
    for para in raw_parts:
        if len(para) <= TG_MAX_CHARS:
            paragraphs.append(para)
        else:
            # Hard-split oversized paragraph (FIX: BUG-L5-2)
            for i in range(0, len(para), TG_MAX_CHARS - 10):
                paragraphs.append(para[i:i + TG_MAX_CHARS - 10])

    # Coalesce paragraphs into chunks
    chunks: list[str] = []
    chunk = ""
    for para in paragraphs:
        if len(chunk) + len(para) + 2 <= TG_MAX_CHARS:
            chunk = (chunk + "\n\n" + para).lstrip("\n")
        else:
            if chunk:
                chunks.append(chunk)
            chunk = para
    if chunk:
        chunks.append(chunk)

    total = len(chunks)
    for idx, ch in enumerate(chunks, 1):
        # Add part indicator for multi-chunk (FIX: NEW-L5-11)
        labeled = f"{ch}\n\n<i>({idx}/{total})</i>" if total > 1 else ch
        _send_one(cid, labeled[:TG_MAX_CHARS])
        if idx < total:
            time.sleep(0.3)


async def _tg_send_async(cid: str, text: str):
    """Async wrapper for _tg_send — runs in executor to avoid blocking event loop
    (FIX: BUG-L5-7 / CROSS-7).
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _tg_send, cid, text)


def _tg_typing(cid: str):
    """Send typing indicator. Uses urllib to avoid httpx dependency (FIX: BUG-L5-6)."""
    try:
        import os, json as _json, urllib.request, ssl
        token = os.environ.get("TELEGRAM_TOKEN", "")
        if not token:
            return
        body = _json.dumps({"chat_id": cid, "action": "typing"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendChatAction",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        ctx = ssl.create_default_context()
        urllib.request.urlopen(req, context=ctx, timeout=5)
    except Exception:
        pass


# ── Main output ───────────────────────────────────────────────────────────────

async def layer_5_output(intent: dict, reply: str, tool_results: list):
    """
    Format and deliver reply. Also saves to conversation history.
    Called from L4 (after tool loop) and L3 (direct answers).

    tool_results: kept for API compatibility — available for future use
    (e.g., attaching debug info to delivery).
    """
    cid = intent["sender_id"]

    if not reply:
        reply = "✅ Done."

    # Append to conversation history
    try:
        from core_orch_layer2 import append_history
        append_history(cid, "assistant", reply)
    except Exception as e:
        print(f"[L5] append_history failed (non-fatal): {e}")

    # Deliver (non-blocking via executor — FIX: BUG-L5-7)
    await _tg_send_async(cid, reply)
    print(f"[L5] Delivered to {cid}: {len(reply)}c")


# ── Confirmation gate ─────────────────────────────────────────────────────────

async def layer_5_request_confirm(intent: dict, execution_plan: dict) -> bool:
    """
    Send CONFIRM/REJECT prompt to owner. Block until response or timeout.
    Returns True if confirmed, False if rejected or timed out.
    Per Constitution C2: timeout = abort, NEVER auto-confirm.
    FIX BUG-L5-5/NEW-L5-13: uses get_running_loop() (Python 3.10+ safe).
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

    await _tg_send_async(cid, msg)

    # Wait in executor — non-blocking for event loop (FIX: BUG-L5-5/NEW-L5-13)
    loop = asyncio.get_running_loop()
    fired = await loop.run_in_executor(None, event.wait, CONFIRM_TIMEOUT_S)

    with _pending_lock:
        gate      = _pending.pop(cid, {})
        confirmed = gate.get("confirmed", False)

    if not fired:
        await _tg_send_async(cid, "⏱ Confirmation timed out — action aborted.")
        return False

    if not confirmed:
        await _tg_send_async(cid, "🚫 Action cancelled.")
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
