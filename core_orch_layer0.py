"""
core_orch_layer0.py — CORE AGI Entry Layer
============================================
The lowest layer in the orchestrator stack. Receives incoming Telegram messages,
validates owner, parses commands, and routes to L1 Orchestration Layer.

RESPONSIBILITIES:
  - Telegram webhook handler (Flask POST /telegram)
  - Owner validation via L10.enforce_owner() — FIRST action on every message
  - Parse /commands vs raw text
  - Route validated requests to L1
  - Handle /force_close escape hatch (owner-only emergency stop)
  - Zero reasoning — just entry, validation, routing

DOES NOT:
  - Make decisions (that's L1's job)
  - Execute tools (that's L4's job)
  - Access Supabase directly (delegates to upper layers)
  - Process or interpret message content (L1 does that)

FLOW:
  Telegram → L0 → validate owner → parse → L1 → ...

FIXES (v2):
  - BUG-L0-3:  /force_close sends reply BEFORE SIGTERM to avoid race condition
  - BUG-L0-4:  validate_environment() called in standalone __main__ block
  - BUG-L0-6:  Bot command @suffix stripped (handles /cmd@botname in groups)
  - BUG-L0-12: Critical L10 boot violations cause startup abort
  - GAP-L0-8:  edited_message / my_chat_member / non-message updates → 200 OK silently
  - GAP-L0-10: update_id deduplication prevents double-processing Telegram retries
  - GAP-L0-11: Empty text message guarded; still routed to L1 for handling
  - NEW-L0-13: Unauthorized/bad requests return 200 to Telegram (prevent retry floods)
  - NEW-L0-14: LIMITER._calls uses bounded deque to prevent unbounded growth
  - CROSS:     tg_reply runs credential scan before sending (L10.enforce_no_credentials)
"""

import os
import sys
import json
import traceback
from collections import deque
from flask import Flask, request, jsonify
from datetime import datetime

# Import L10 Constitution enforcement
try:
    from core_orch_layer10 import enforce_owner, ConstitutionViolation
except ImportError:
    print("[L0] CRITICAL: Cannot import L10 Constitution Layer")
    sys.exit(1)

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)

# Telegram bot token for sending replies
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT", "")

# OWNER_ID — alias for TELEGRAM_CHAT, used by core_orch_main.py startup notify
OWNER_ID = TELEGRAM_CHAT

# ── Seen update deduplication ──────────────────────────────────────────────────
# Bounded deque: keeps last 500 processed update_ids to handle Telegram retries.
_SEEN_UPDATES: deque = deque(maxlen=500)


# ── Rate limiter ───────────────────────────────────────────────────────────────

class _TelegramRateLimiter:
    """Thin wrapper exposing .consume() for L1 triage gate.
    Uses bounded deque to prevent unbounded memory growth (FIX: NEW-L0-14).
    """
    def __init__(self):
        import time as _time
        self._time = _time
        # maxlen=500 caps memory at ~500 timestamps regardless of message volume
        self._calls: deque = deque(maxlen=500)
        try:
            c = json.load(open("resource_ceilings.json"))
            self._limit = c.get("telegram_messages_per_hour", 30)
        except Exception:
            self._limit = 30

    def consume(self) -> bool:
        now = self._time.time()
        # Evict entries older than 1 hour
        while self._calls and now - self._calls[0] >= 3600:
            self._calls.popleft()
        if len(self._calls) >= self._limit:
            return False
        self._calls.append(now)
        return True


LIMITER = _TelegramRateLimiter()

# Required environment variables
_REQUIRED_ENV_VARS = [
    "TELEGRAM_TOKEN",
    "TELEGRAM_CHAT",
]


def validate_environment() -> None:
    """
    Validate that all required environment variables are present.
    Called at startup. Prints warnings for missing vars.
    """
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        msg = f"[L0] WARNING: Missing environment variables: {', '.join(missing)}"
        print(msg)
    else:
        print(f"[L0] Environment validated ✅ (TOKEN=set, CHAT={TELEGRAM_CHAT})")


# ── Telegram reply helper ──────────────────────────────────────────────────────

def tg_reply(chat_id: str, text: str):
    """Send reply to Telegram. Non-blocking, logs errors.
    Runs credential scan before sending (L10 C2/C4 compliance).
    """
    if not TELEGRAM_TOKEN:
        print(f"[L0] No TELEGRAM_TOKEN — cannot reply")
        return

    # CROSS FIX: credential scan before any outbound message
    try:
        from core_orch_layer10 import enforce_no_credentials
        text = enforce_no_credentials(text, context="L0.tg_reply")
    except Exception:
        pass  # L10 unavailable — proceed; violation logged by L10 if imported

    try:
        import urllib.request
        import ssl
        body = json.dumps({
            "chat_id": chat_id,
            "text": text[:4000],  # Telegram limit
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        ctx = ssl.create_default_context()
        urllib.request.urlopen(req, context=ctx, timeout=10)
    except Exception as e:
        print(f"[L0] tg_reply failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM WEBHOOK HANDLER
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/telegram", methods=["POST"])
def telegram_entry():
    """Main entry point for all Telegram updates.

    Flow:
      1. Parse incoming JSON
      2. Deduplicate update_id
      3. Silently ACK non-message updates (edited_message, my_chat_member, etc.)
      4. Validate owner via L10
      5. Parse command/text
      6. Route to L1
      7. Return 200 OK to Telegram — ALWAYS (prevents Telegram retry floods)
    """
    start_ts = datetime.utcnow()

    try:
        data = request.json
        if not data:
            # Malformed payload — ack silently to stop retries (FIX: NEW-L0-13)
            print("[L0] Empty or non-JSON webhook payload")
            return jsonify({"ok": True, "action": "ignored_empty"}), 200

        # ═══════════════════════════════════════════════════════════════════════
        # DEDUPLICATION: update_id (FIX: GAP-L0-10)
        # ═══════════════════════════════════════════════════════════════════════
        update_id = data.get("update_id")
        if update_id is not None:
            if update_id in _SEEN_UPDATES:
                print(f"[L0] Duplicate update_id={update_id} — ignored")
                return jsonify({"ok": True, "action": "duplicate"}), 200
            _SEEN_UPDATES.append(update_id)

        # ═══════════════════════════════════════════════════════════════════════
        # SILENT ACK: non-message update types (FIX: GAP-L0-8, NEW-L0-15)
        # edited_message, channel_post, my_chat_member, chat_member, etc.
        # Telegram will retry on 4xx — return 200 for anything we don't handle.
        # ═══════════════════════════════════════════════════════════════════════
        _HANDLED_UPDATE_KEYS = {"message"}
        if not any(k in data for k in _HANDLED_UPDATE_KEYS):
            update_type = next(
                (k for k in data if k not in ("update_id",)), "unknown"
            )
            print(f"[L0] Non-message update type '{update_type}' — ACK silently")
            return jsonify({"ok": True, "action": f"ignored_{update_type}"}), 200

        message = data["message"]
        chat_id = str(message.get("chat", {}).get("id", ""))
        text    = message.get("text", "").strip()

        if not chat_id:
            print("[L0] No chat_id in message")
            return jsonify({"ok": True, "action": "no_chat_id"}), 200

        print(f"[L0] Incoming message | chat_id={chat_id} | text={text[:50]!r}")

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 1: OWNER VALIDATION (L10 Constitution)
        # Return 200 even on rejection — Telegram must not retry unauthorized msgs
        # (FIX: NEW-L0-13 — never return 4xx to Telegram)
        # ═══════════════════════════════════════════════════════════════════════
        try:
            enforce_owner(chat_id)
        except ConstitutionViolation as cv:
            print(f"[L0] Owner check failed: {cv}")
            tg_reply(chat_id, "⛔ Unauthorized. Access denied.")
            return jsonify({"ok": True, "action": "unauthorized"}), 200

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 2: PARSE COMMAND vs TEXT
        # Strip @botname suffix from commands (FIX: BUG-L0-6)
        # ═══════════════════════════════════════════════════════════════════════
        is_command = text.startswith("/")
        command    = ""
        args       = ""

        if is_command:
            parts   = text.split(maxsplit=1)
            # Strip @botname suffix: "/force_close@my_bot" → "force_close"
            raw_cmd = parts[0][1:].lower()
            command = raw_cmd.split("@")[0]
            args    = parts[1] if len(parts) > 1 else ""

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 3: HANDLE ESCAPE HATCH — /force_close
        # Send reply BEFORE kill to avoid response-never-sent race (FIX: BUG-L0-3)
        # ═══════════════════════════════════════════════════════════════════════
        if command == "force_close":
            print(f"[L0] FORCE_CLOSE invoked by owner")
            # Reply FIRST, then schedule termination
            tg_reply(chat_id, "🛑 <b>FORCE_CLOSE</b> — CORE halted by owner.")
            # Schedule SIGTERM via thread to allow this response to complete
            import signal, threading
            def _deferred_kill():
                import time
                time.sleep(0.5)  # Give Flask time to send the response
                os.kill(os.getpid(), signal.SIGTERM)
            threading.Thread(target=_deferred_kill, daemon=True).start()
            return jsonify({"ok": True, "action": "force_close"}), 200

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 4: ROUTE TO L1 ORCHESTRATION
        # Empty text is passed through — L1 decides how to handle (FIX: GAP-L0-11)
        # ═══════════════════════════════════════════════════════════════════════
        try:
            from core_orch_layer1 import orchestrate
            result = orchestrate(
                text=text,
                chat_id=chat_id,
                is_command=is_command,
                command=command,
                args=args,
            )
            print(f"[L0] L1 returned: {result.get('status', 'unknown')}")
        except ImportError:
            print("[L0] ERROR: L1 (orchestration layer) not found")
            tg_reply(chat_id, "⚠️ L1 Orchestration Layer missing — cannot process request.")
            return jsonify({"ok": True, "error": "L1 missing"}), 200
        except Exception as e:
            print(f"[L0] L1 orchestration failed: {e}")
            print(traceback.format_exc())
            tg_reply(chat_id, f"⚠️ L1 error: {str(e)[:200]}")
            return jsonify({"ok": True, "error": str(e)}), 200

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 5: RETURN 200 OK to Telegram
        # ═══════════════════════════════════════════════════════════════════════
        elapsed_ms = int((datetime.utcnow() - start_ts).total_seconds() * 1000)
        print(f"[L0] Done | {elapsed_ms}ms")
        return jsonify({"ok": True, "elapsed_ms": elapsed_ms}), 200

    except Exception as e:
        print(f"[L0] UNHANDLED ERROR in telegram_entry: {e}")
        print(traceback.format_exc())
        # Still return 200 to prevent Telegram from retrying a broken update
        return jsonify({"ok": True, "error": "internal error"}), 200


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    """Railway health check endpoint."""
    return jsonify({
        "ok": True,
        "layer": "L0-Entry",
        "timestamp": datetime.utcnow().isoformat(),
    })


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))

    # Validate environment first (FIX: BUG-L0-4 — was not called in standalone mode)
    validate_environment()

    print(f"[L0] Entry Layer starting on port {port}")
    print(f"[L0] Owner chat_id: {TELEGRAM_CHAT}")

    # Run L10 boot check — abort on critical violations (FIX: BUG-L0-12)
    try:
        from core_orch_layer10 import boot_check
        boot_result = boot_check()
        if not boot_result["ok"]:
            print(f"[L0] WARNING: Constitution boot check failed")
            for v in boot_result["violations"]:
                print(f"  ⚠ {v}")
            # Abort startup if critical violations exist (not just C2 warnings)
            critical = [v for v in boot_result["violations"] if not v.startswith("C2-warning")]
            if critical:
                print(f"[L0] CRITICAL boot violations detected — aborting startup")
                sys.exit(1)
    except Exception as e:
        print(f"[L0] L10 boot_check failed: {e}")

    # Start Flask server
    app.run(host="0.0.0.0", port=port, debug=False)
