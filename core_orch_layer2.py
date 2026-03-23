import os
import asyncio
from core_config import groq_chat, notify

async def layer_2_process(text: str, chat_id: int, user: str):
    """
    Layer 2: Context & Memory.
    """
    try:
        print(f"🧠 [L2: Context] Building memory for @{user}...")
        
        # Pass to Layer 3 (Intent)
        from core_orch_layer3 import layer_3_classify
        await layer_3_classify(text, chat_id, user, "Context Loaded")
        
    except Exception as e:
        print(f"❌ L2 Error: {e}")
        from core_orch_layer10 import layer_10_output
        await layer_10_output(text, chat_id, user, f"Error at Layer 2: {e}")

if __name__ == "__main__":
    print("🛰️ Layer 2: Context Engine Online.")
