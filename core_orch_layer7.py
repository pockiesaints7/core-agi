import os
import asyncio
import json
from core_config import groq_chat, notify

# --- LAYER 7: SELF-REFINEMENT & EVOLUTION ---

async def layer_7_refine(text: str, chat_id: int, user: str, tool_result: any):
    """
    Analyzes the interaction to see if a tool or system logic should evolve.
    Integrates with core_train.py (Evolution Queue).
    """
    try:
        print(f"🔧 [L7: Refinement] Analyzing system performance for @{user}...")

        # 1. Use Groq to look for "Evolution Opportunities"
        # We check if the tool result was clunky or if the code could be cleaner.
        refine_prompt = f"""
        USER REQUEST: {text}
        TOOL RESULT: {str(tool_result)[:1000]}
        
        TASK: Is there a way to optimize the tool used or the system's logic?
        If yes, propose a technical improvement.
        Return JSON: {"propose_evolution": true, "reason": "...", "suggestion": "..."}
        If no, return {"propose_evolution": false}.
        """

        raw_refine = groq_chat(refine_prompt)
        try:
            refine_json = json.loads(raw_refine.strip().replace('```json', '').replace('```', ''))
        except:
            refine_json = {"propose_evolution": False}

        # 2. If an improvement is found, send it to the Evolution Queue (Supabase)
        if refine_json.get("propose_evolution"):
            from core_tools import handle_jsonrpc
            print(f"🧬 [L7] Proposing Evolution: {refine_json.get('reason')}")
            
            # Using your existing tool_improve logic from the big core_tools file
            await handle_jsonrpc("tool_improve", {
                "tool_name": "system_logic",
                "new_code": refine_json.get("suggestion")
            })
            notify(f"🧬 <b>Evolution Proposed:</b> {refine_json.get('reason')}", chat_id)

        # 3. Hand-off to Layer 8 (Safety & Policy)
        # Layer 8 ensures the final response doesn't leak keys or break rules.
        try:
            from core_orch_layer8 import layer_8_safety
            await layer_8_safety(text, chat_id, user, tool_result)
        except ImportError:
            # If L8 is not ready, jump to Layer 10
            from core_orch_layer10 import layer_10_output
            await layer_10_output(text, chat_id, user, tool_result)

    except Exception as e:
        print(f"❌ L7 Error: {e}")
        from core_orch_layer10 import layer_10_output
        await layer_10_output(text, chat_id, user, tool_result)

if __name__ == "__main__":
    print("🛰️ Layer 7: Refinement Engine Online.")
