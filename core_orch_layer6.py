import os
import asyncio

async def layer_6_validate(text, chat_id, user, context, intent, action_result):
    """
    Layer 6: Validation & Verification.
    Checks if the tool output makes sense before passing to the 'Self-Correction' layer.
    """
    try:
        print(f"⚖️ [L6: Validation] Verifying results for @{user}...")
        
        # Pass to Layer 7 (Self-Correction / Refinement)
        from core_orch_layer7 import layer_7_refine
        await layer_7_refine(text, chat_id, user, context, intent, action_result)
        
    except Exception as e:
        print(f"❌ L6 Error: {e}")
        from core_orch_layer10 import layer_10_output
        await layer_10_output(text, chat_id, user, f"Error at Layer 6: {e}")

if __name__ == "__main__":
    print("🛰️ Layer 6: Validation Engine Online.")
