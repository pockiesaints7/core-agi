import os
import asyncio
from core_config import notify

# --- LAYER 10: OUTPUT DELIVERY ---

async def layer_10_output(text: str, chat_id: int, user: str, final_response: str):
    """
    The final step in the 10-layer process.
    Delivers the message to Telegram and logs the completion.
    """
    try:
        print(f"📡 [L10: Output] Delivering final response to @{user}...")

        # 1. Telegram Length Management
        # Telegram has a 4096 character limit per message.
        if len(final_response) > 4000:
            print("⚠️ Response too long, truncating for Telegram.")
            final_response = final_response[:3900] + "... [Truncated]"

        # 2. Final Delivery
        # We use the notify function from core_config
        notify(final_response, chat_id)

        # 3. Log Success to Console
        print(f"🏁 [Chain Complete] System successfully addressed: '{text[:30]}...'")

    except Exception as e:
        print(f"❌ L10 Error: {e}")
        # Absolute last resort fallback
        try:
            notify("⚠️ System encountered an output error, but the task was processed.", chat_id)
        except:
            pass

if __name__ == "__main__":
    print("🛰️ Layer 10: Output Dispatcher Online.")
