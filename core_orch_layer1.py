"""
core_orch_layer1.py — CORE AGI Orchestration Layer
===================================================
The brain of the orchestrator. Receives validated requests from L0, makes
high-level decisions, coordinates L2-L9, and returns results.

RESPONSIBILITIES:
  - Route commands to appropriate handlers (/status, /train, /deploy, etc.)
  - Decide which reasoning mode to use (hot/cold/autonomous)
  - Coordinate layer interactions (L2 Context → L3 Reasoning → L4 Execution → L5 Output)
  - Manage conversation state and multi-turn flows
  - Handle errors gracefully with user-facing messages
  - Log orchestration decisions to Supabase

DOES NOT:
  - Execute tools directly (delegates to L4)
  - Generate LLM responses (delegates to L3)
  - Access raw Telegram API (L0 and L5 handle I/O)
  - Bypass L10 Constitution checks

LAYER COORDINATION:
  L0 → L1 → L2 (gather context) → L3 (reason) → L4 (execute) → L5 (output) → L0
           ↓
          L10 (enforce constitution at each step)
"""

import os
import json
import traceback
from typing import Dict, Any, Optional
from datetime import datetime

# Import other layers
try:
    from core_orch_layer10 import enforce_db, report_violation, SEVERITY_HIGH
except ImportError:
    print("[L1] WARNING: L10 Constitution Layer not available")
    def enforce_db(*args, **kwargs): pass
    def report_violation(*args, **kwargs): pass
    SEVERITY_HIGH = "high"


# ══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATION RESULT
# ══════════════════════════════════════════════════════════════════════════════

class OrchResult:
    """Standard result format for orchestration."""
    def __init__(
        self,
        status: str = "ok",
        message: str = "",
        data: Optional[Dict] = None,
        error: Optional[str] = None,
    ):
        self.status  = status  # "ok", "error", "pending", "processing"
        self.message = message  # User-facing message
        self.data    = data or {}
        self.error   = error
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "data": self.data,
            "error": self.error,
        }


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

def handle_status(chat_id: str, args: str) -> OrchResult:
    """Handle /status command — report CORE health across all layers."""
    print("[L1] /status requested")
    
    try:
        # Check L10 Constitution
        from core_orch_layer10 import boot_check
        boot = boot_check()
        
        # Check Supabase connectivity (L7)
        enforce_db("status_check")
        
        # TODO: Check other layers when implemented
        # - L2 Context availability
        # - L3 Reasoning (Groq/OpenRouter health)
        # - L4 Execution (tool registry)
        # - L9 Training (cold processor status)
        
        status_msg = (
            "✅ <b>CORE Status</b>\n\n"
            f"L10 Constitution: {'✅ OK' if boot['ok'] else '⚠️ Issues'}\n"
            f"L7 Supabase: ✅ Connected\n"
            f"Timestamp: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        
        return OrchResult(status="ok", message=status_msg, data=boot)
    
    except Exception as e:
        print(f"[L1] /status failed: {e}")
        return OrchResult(
            status="error",
            message=f"⚠️ Status check failed: {str(e)[:200]}",
            error=str(e)
        )


def handle_train(chat_id: str, args: str) -> OrchResult:
    """Handle /train command — trigger training pipeline (L9)."""
    print(f"[L1] /train requested | args={args}")
    
    try:
        # Import L9 Training Layer
        from core_orch_layer9 import run_training_cycle
        
        # Trigger training
        result = run_training_cycle(mode=args or "hot")
        
        return OrchResult(
            status="ok",
            message=f"✅ Training cycle started: {args or 'hot'} mode",
            data=result
        )
    
    except ImportError:
        return OrchResult(
            status="error",
            message="⚠️ L9 Training Layer not available",
            error="L9 missing"
        )
    except Exception as e:
        print(f"[L1] /train failed: {e}")
        return OrchResult(
            status="error",
            message=f"⚠️ Training failed: {str(e)[:200]}",
            error=str(e)
        )


def handle_deploy(chat_id: str, args: str) -> OrchResult:
    """Handle /deploy command — trigger Railway deployment (L8)."""
    print(f"[L1] /deploy requested | args={args}")
    
    try:
        # Import L8 Deployment Layer
        from core_orch_layer8 import deploy_to_railway
        
        result = deploy_to_railway(
            commit_message=args or "Manual deploy via /deploy",
            auto_confirm=False  # Requires owner confirmation
        )
        
        return OrchResult(
            status="ok",
            message="✅ Deploy initiated",
            data=result
        )
    
    except ImportError:
        return OrchResult(
            status="error",
            message="⚠️ L8 Deployment Layer not available",
            error="L8 missing"
        )
    except Exception as e:
        print(f"[L1] /deploy failed: {e}")
        return OrchResult(
            status="error",
            message=f"⚠️ Deploy failed: {str(e)[:200]}",
            error=str(e)
        )


def handle_task(chat_id: str, args: str) -> OrchResult:
    """Handle /task command — query or manage task queue."""
    print(f"[L1] /task requested | args={args}")
    
    try:
        from core_config import sb_query
        
        if not args or args == "list":
            # List active tasks
            tasks = sb_query(
                "task_queue",
                filters={"status": "eq.pending"},
                select="id,title,priority,domain,created_at",
                order="priority.desc,created_at.asc",
                limit=10
            )
            
            if not tasks:
                return OrchResult(status="ok", message="✅ Task queue empty")
            
            msg = "📋 <b>Active Tasks</b>\n\n"
            for t in tasks[:5]:
                msg += f"• {t['title'][:60]}\n"
            
            return OrchResult(status="ok", message=msg, data={"tasks": tasks})
        
        else:
            # TODO: Support /task <id> for details
            return OrchResult(
                status="ok",
                message="⚠️ Task details not yet implemented"
            )
    
    except Exception as e:
        print(f"[L1] /task failed: {e}")
        return OrchResult(
            status="error",
            message=f"⚠️ Task query failed: {str(e)[:200]}",
            error=str(e)
        )


def handle_raw_text(chat_id: str, text: str) -> OrchResult:
    """Handle non-command text — route to L3 Reasoning for LLM response."""
    print(f"[L1] Raw text routing to L3 | text={text[:50]}")
    
    try:
        # Import L2 Context and L3 Reasoning
        from core_orch_layer2 import gather_context
        from core_orch_layer3 import generate_response
        
        # Step 1: Gather context (L2)
        context = gather_context(text, chat_id)
        
        # Step 2: Generate response (L3)
        response = generate_response(text, context)
        
        # Step 3: Send via L5 Output
        from core_orch_layer5 import send_reply
        send_reply(chat_id, response)
        
        return OrchResult(
            status="ok",
            message=response,
            data={"context_items": len(context.get("items", []))}
        )
    
    except ImportError as ie:
        missing_layer = str(ie).split("'")[-2] if "'" in str(ie) else "unknown"
        return OrchResult(
            status="error",
            message=f"⚠️ Layer not available: {missing_layer}",
            error=str(ie)
        )
    except Exception as e:
        print(f"[L1] Raw text handling failed: {e}")
        print(traceback.format_exc())
        return OrchResult(
            status="error",
            message=f"⚠️ Processing failed: {str(e)[:200]}",
            error=str(e)
        )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATION FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def orchestrate(
    text: str,
    chat_id: str,
    is_command: bool = False,
    command: str = "",
    args: str = "",
) -> Dict[str, Any]:
    """Main orchestration entry point called by L0.
    
    Args:
        text: Full message text from Telegram
        chat_id: Telegram chat ID (already validated by L0)
        is_command: Whether text starts with /
        command: Parsed command name (without /)
        args: Command arguments
    
    Returns:
        Dict with status, message, data, error
    """
    start_ts = datetime.utcnow()
    print(f"[L1] Orchestrating | command={command or 'text'} | args={args[:30] if args else 'none'}")
    
    try:
        # ═══════════════════════════════════════════════════════════════════════
        # COMMAND ROUTING
        # ═══════════════════════════════════════════════════════════════════════
        if is_command:
            # Map commands to handlers
            handlers = {
                "status": handle_status,
                "train": handle_train,
                "deploy": handle_deploy,
                "task": handle_task,
                "tasks": handle_task,
            }
            
            handler = handlers.get(command)
            
            if handler:
                result = handler(chat_id, args)
            else:
                result = OrchResult(
                    status="error",
                    message=f"⚠️ Unknown command: /{command}"
                )
        
        # ═══════════════════════════════════════════════════════════════════════
        # RAW TEXT ROUTING
        # ═══════════════════════════════════════════════════════════════════════
        else:
            result = handle_raw_text(chat_id, text)
        
        # ═══════════════════════════════════════════════════════════════════════
        # LOG ORCHESTRATION DECISION
        # ═══════════════════════════════════════════════════════════════════════
        elapsed_ms = int((datetime.utcnow() - start_ts).total_seconds() * 1000)
        print(f"[L1] Orchestration complete | status={result.status} | {elapsed_ms}ms")
        
        # TODO: Log to Supabase orchestration_log table when L7 is ready
        
        return result.to_dict()
    
    except Exception as e:
        print(f"[L1] ORCHESTRATION FAILED: {e}")
        print(traceback.format_exc())
        
        # Report violation to L10 if this is unexpected
        report_violation(
            invariant="L1-ORCHESTRATION",
            what_failed=f"Unhandled exception in orchestrate(): {str(e)[:200]}",
            context=f"text={text[:100]}, command={command}",
            how_to_avoid="Add proper error handling for this code path",
            severity=SEVERITY_HIGH
        )
        
        return OrchResult(
            status="error",
            message="⚠️ Internal orchestration error",
            error=str(e)
        ).to_dict()


# ══════════════════════════════════════════════════════════════════════════════
# INITIALIZATION
# ══════════════════════════════════════════════════════════════════════════════

print("[L1] Orchestration Layer loaded")
