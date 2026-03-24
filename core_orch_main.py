"""
core_orch_main.py — Orchestrator v2 Entry Point
================================================
Mounts to the existing FastAPI app in core_main.py.

HOW TO ACTIVATE (3 changes to core_main.py):

  # 1. Top imports:
  from core_orch_main import handle_telegram_message_v2, startup_v2

  # 2. In on_start():
  startup_v2()

  # 3. In handle_msg(), replace the final else branch:
  else:
      threading.Thread(
          target=lambda: asyncio.run(handle_telegram_message_v2(msg)),
          daemon=True
      ).start()

That's it. All other existing routes (/mcp/*, /webhook, etc.) stay unchanged.
The new orchestrator only replaces the freeform Telegram conversation path.

Layer wiring:
  Telegram msg → L0 (rate limit) → L1 (Intent) → L2 (Memory) →
  L3 (Reasoning) → L4 (Execution) → L5 (Output) → L9 (Learning)
                                                  ↕
                              L6 (Validation + Autonomy)
                              L7 (Observability)
                              L8 (Model coordination — called by L3/L4)
                              L10 (Constitution — enforced in L3)
"""

import asyncio
import threading
import os


# ── Startup ───────────────────────────────────────────────────────────────────

def startup_v2():
    """
    Called from core_main.py on_start().
    Validates environment + starts L6 background loops.
    """
    # L0: validate environment
    from core_orch_layer0 import validate_environment
    validate_environment()

    # (L6 background loops not used — orchestrator is request-driven, not polling)

    # Log active model OPENROUTER_MODEL="google/gemini-2.5-flash"
    model = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    print(f"[ORCH-V2] Started. Primary model: {model}")
    print("[ORCH-V2] Layer chain: L0→L1→L2→L3→L4→L5→L9 (L6/L7/L8 inlined)")

    # Notify owner
    try:
        from core_github import notify
        from core_orch_layer0 import OWNER_ID
        notify(
            f"🧠 <b>CORE Orchestrator v2 Online</b>\n"
            f"Model: {model}\n"
            f"Layers: L0–L9 active\n"
            f"Blueprint: 11-layer compliant",
            OWNER_ID,
        )
    except Exception as e:
        print(f"[ORCH-V2] Startup notify failed (non-fatal): {e}")


# ── Main entry point ──────────────────────────────────────────────────────────

async def handle_telegram_message_v2(msg: dict):
    """
    Async entry point. Called from core_main.py handle_msg() in a thread.
    Runs the full L1→L9 pipeline.
    """
    try:
        from core_orch_layer1 import layer_1_triage
        await layer_1_triage(msg, input_type="telegram")
    except Exception as e:
        print(f"[ORCH-V2] Unhandled top-level error: {e}")
        try:
            cid = str(msg.get("chat", {}).get("id", ""))
            from core_github import notify
            notify(f"⚠️ CORE Orchestrator v2 error: {e}", cid)
        except Exception:
            pass


# ── Helper: run async from sync thread ────────────────────────────────────────

def handle_telegram_message(msg: dict):
    """
    Sync wrapper — use this in threading.Thread(target=...).
    Handles event loop creation for thread contexts.
    """
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(handle_telegram_message_v2(msg))
    except Exception as e:
        print(f"[ORCH-V2] Thread error: {e}")
    finally:
        try:
            loop.close()
        except Exception:
            pass


if __name__ == "__main__":
    print("🛰️ Orchestrator v2 Entry Point — Online.")
    print("Mount via: from core_orch_main import handle_telegram_message, startup_v2")
