import os
import asyncio
from core_config import notify, GITHUB_REPO, KB_MINE_BATCH_SIZE

async def layer_4_reason(text: str, chat_id: int, user: str, context: str, intent: str):
    try:
        print(f"🧠 [L4: Reasoning] Analyzing for @{user} (Batch Size: {KB_MINE_BATCH_SIZE})...")
        
        # Pass to Layer 5 (Tools/Action)
        from core_orch_layer5 import layer_5_tools
        await layer_5_tools(text, chat_id, user, context, intent, "Strategy: Direct Execution")
        
    except Exception as e:
        print(f"❌ L4 Error: {e}")
        from core_orch_layer10 import layer_10_output
        await layer_10_output(text, chat_id, user, f"Error at Layer 4: {e}")
