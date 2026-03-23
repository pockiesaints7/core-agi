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
"""

import os
import sys
import json
import traceback
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

# LIMITER — rate limiter instance used by L1 triage (LIMITER.consume())
class _TelegramRateLimiter:
    """Thin wrapper exposing .consume() for L1 triage gate."""
    def __init__(self):
        import json as _json, time as _time
        self._time = _time
        self._calls: list = []
        try:
            c = _json.load(open("resource_ceilings.json"))
            self._limit = c.get("telegram_messages_per_hour", 30)
        except Exception:
            self._limit = 30

    def consume(self) -> bool:
        now = self._time.time()
        self._calls = [t for t in self._calls if now - t < 3600]
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
    Called from core_orch_main.startup_v2() at boot.
    Raises EnvironmentError listing all missing vars (non-fatal warning if
    TELEGRAM_TOKEN is missing — bot simply cannot send messages).
    """
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        msg = f"[L0] WARNING: Missing environment variables: {', '.join(missing)}"
        print(msg)
        # Non-fatal — Railway may inject these after first deploy
    else:
        print(f"[L0] Environment validated ✅ (TOKEN={'set'}, CHAT={TELEGRAM_CHAT})")

# ── Telegram reply helper ──────────────────────────────────────────────────────
def tg_reply(chat_id: str, text: str):
    """Send reply to Telegram. Non-blocking, logs errors."""
    if not TELEGRAM_TOKEN:
        print(f"[L0] No TELEGRAM_TOKEN — cannot reply")
        return
    
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
    """Main entry point for all Telegram messages.
    
    Flow:
      1. Parse incoming JSON
      2. Validate owner via L10
      3. Parse command/text
      4. Route to L1
      5. Return 200 OK (Telegram expects fast response)
    """
    start_ts = datetime.utcnow()
    
    try:
        data = request.json
        if not data or "message" not in data:
            print("[L0] Invalid webhook payload — no message field")
            return jsonify({"ok": False, "error": "no message"}), 400
        
        message = data["message"]
        chat_id = str(message.get("chat", {}).get("id", ""))
        text    = message.get("text", "").strip()
        
        if not chat_id:
            print("[L0] No chat_id in message")
            return jsonify({"ok": False, "error": "no chat_id"}), 400
        
        print(f"[L0] Incoming message | chat_id={chat_id} | text={text[:50]}")
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 1: OWNER VALIDATION (L10 Constitution)
        # ═══════════════════════════════════════════════════════════════════════
        try:
            enforce_owner(chat_id)
        except ConstitutionViolation as cv:
            # Owner check failed — hard stop, log to L10, reply to intruder
            print(f"[L0] Owner check failed: {cv}")
            tg_reply(chat_id, "⛔ Unauthorized. Access denied.")
            return jsonify({"ok": False, "error": "unauthorized"}), 403
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 2: PARSE COMMAND vs TEXT
        # ═══════════════════════════════════════════════════════════════════════
        is_command = text.startswith("/")
        command    = ""
        args       = ""
        
        if is_command:
            parts   = text.split(maxsplit=1)
            command = parts[0][1:].lower()  # Remove leading /
            args    = parts[1] if len(parts) > 1 else ""
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 3: HANDLE ESCAPE HATCH — /force_close
        # ═══════════════════════════════════════════════════════════════════════
        if command == "force_close":
            print(f"[L0] FORCE_CLOSE invoked by owner")
            tg_reply(chat_id, "🛑 <b>FORCE_CLOSE</b> — CORE halted by owner.")
            # Trigger graceful shutdown (L10 will log this as owner-invoked)
            import signal
            os.kill(os.getpid(), signal.SIGTERM)
            return jsonify({"ok": True, "action": "force_close"})
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 4: ROUTE TO L1 ORCHESTRATION
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
            return jsonify({"ok": False, "error": "L1 missing"}), 500
        except Exception as e:
            print(f"[L0] L1 orchestration failed: {e}")
            print(traceback.format_exc())
            tg_reply(chat_id, f"⚠️ L1 error: {str(e)[:200]}")
            return jsonify({"ok": False, "error": str(e)}), 500
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 5: RETURN 200 OK to Telegram
        # ═══════════════════════════════════════════════════════════════════════
        elapsed_ms = int((datetime.utcnow() - start_ts).total_seconds() * 1000)
        print(f"[L0] Done | {elapsed_ms}ms")
        return jsonify({"ok": True, "elapsed_ms": elapsed_ms})
    
    except Exception as e:
        print(f"[L0] UNHANDLED ERROR in telegram_entry: {e}")
        print(traceback.format_exc())
        return jsonify({"ok": False, "error": "internal error"}), 500


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
    print(f"[L0] Entry Layer starting on port {port}")
    print(f"[L0] Owner chat_id: {TELEGRAM_CHAT}")
    
    # Run L10 boot check on startup
    try:
        from core_orch_layer10 import boot_check
        boot_result = boot_check()
        if not boot_result["ok"]:
            print(f"[L0] WARNING: Constitution boot check failed")
            for v in boot_result["violations"]:
                print(f"  ⚠ {v}")
    except Exception as e:
        print(f"[L0] L10 boot_check failed: {e}")
    
    # Start Flask server
    app.run(host="0.0.0.0", port=port, debug=False)
