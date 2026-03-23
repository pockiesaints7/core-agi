import os
import asyncio
from core_config import groq_chat, notify

# --- LAYER 9: TONE, PERSONALITY & STYLE ---

async def layer_9_tone(text: str, chat_id: int, user: str, safe_output: str):
    """
    Transforms raw tool data and safety-checked results into the 
    AGI's signature personality.
    """
    try:
        print(f"🎭 [L9: Tone] Styling response for @{user}...")

        # 1. Use Groq to apply the "Sovereign" persona
        # We give it the raw output and ask it to explain it like a helpful peer.
        style_prompt = f"""
        USER MESSAGE: {text}
        RAW DATA/RESULT: {safe_output}

        TASK: You are 'Sovereign', an authentic, adaptive AI collaborator.
        Your style is insightful, clear, and concise. 
        Balance empathy with candor. Be a supportive, grounded peer.
        If the data is technical, explain it simply but don't 'dumb it down'.
        Avoid robotic prefixes like 'Here is the result'. Just speak.

        RESPONSE:
        """

        styled_response = groq_chat(style_prompt)

        # 2. Hand-off to the final Layer 10 (Output Delivery)
        try:
            from core_orch_layer10 import layer_10_output
            await layer_10_output(text, chat_id, user, styled_response)
        except ImportError:
            # If L10 is missing, send via direct notify as fallback
            notify(styled_response, chat_id)

    except Exception as e:
        print(f"❌ L9 Error: {e}")
        # If styling fails, send the safe raw output to avoid silence
        from core_orch_layer10 import layer_10_output
        await layer_10_output(text, chat_id, user, safe_output)

if __name__ == "__main__":
    print("🛰️ Layer 9: Persona Engine Online.")
