import os
import asyncio
from core_config import notify

async def layer_1_triage(text: str, chat_id: int, user: str):
    """
    Reception: Receives the pulse and passes it to Layer 2.
    """
    try:
        print(f"📥 [L1: Triage] Incoming pulse from @{user}...")
        
        # Pass to Layer 2 (Context & Memory)
        from core_orch_layer2 import layer_2_process
        await layer_2_process(text, chat_id, user)
        
    except Exception as e:
        print(f"❌ L1 Error: {e}")
        # Fallback to output if the chain breaks early
        from core_orch_layer10 import layer_1_output
        await layer_1_output(text, chat_id, user, f"Error at Layer 1: {e}")

if __name__ == "__main__":
    print("🛰️ Layer 1: Triage Online.")
