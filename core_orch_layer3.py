import os
import asyncio
from core_config import notify

async def layer_3_classify(text: str, chat_id: int, user: str, context: str):
    """
    Layer 3: Intent Classification.
    """
    try:
        print(f"🚦 [L3: Intent] Classifying pulse from @{user}...")
        
        # Pass to Layer 4 (Reasoning)
        from core_orch_layer4 import layer_4_reason
        await layer_4_reason(text, chat_id, user, context, "Intent: General Query")
        
    except Exception as e:
        print(f"❌ L3 Error: {e}")
        from core_orch_layer10 import layer_10_output
        await layer_10_output(text, chat_id, user, f"Error at Layer 3: {e}")

if __name__ == "__main__":
    print("🛰️ Layer 3: Intent Engine Online.")
