import os
import re
import asyncio
from core_config import notify

# --- LAYER 8: SAFETY & POLICY ---

async def layer_8_safety(text: str, chat_id: int, user: str, tool_result: any):
    """
    Scans the tool output for sensitive data (API Keys, PATs, local paths) 
    before it moves to the final synthesis layer.
    """
    try:
        print(f"🛡️ [L8: Safety] Scanning output for @{user}...")

        # Convert result to string for scanning
        output_str = str(tool_result)

        # 1. Regex Patterns for sensitive data
        # Protecting your GitHub PATs and Supabase keys
        patterns = {
            "GITHUB_PAT": r"ghp_[a-zA-Z0-9]{36}",
            "GENERIC_KEY": r"key-[a-zA-Z0-9]{20,}",
            "SUPABASE_KEY": r"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9\.[a-zA-Z0-9\-_]+\.[a-zA-Z0-9\-_]+",
            "LOCAL_PATH": r"/home/ubuntu/[\w\-/]+"
        }

        safe_output = output_str
        leaks_found = []

        for label, pattern in patterns.items():
            if re.search(pattern, safe_output):
                leaks_found.append(label)
                safe_output = re.sub(pattern, f"[REDACTED_{label}]", safe_output)

        if leaks_found:
            print(f"🚫 [L8] Redacted sensitive info: {leaks_found}")
            # Alert you privately that the AI tried to leak a key
            notify(f"🛡️ <b>Safety Alert:</b> Redacted {leaks_found} from output to @{user}", chat_id)

        # 2. Hand-off to Layer 9 (Personality & Tone)
        # Now that it's safe, we make it sound like "Sovereign."
        try:
            from core_orch_layer9 import layer_9_tone
            await layer_9_tone(text, chat_id, user, safe_output)
        except ImportError:
            # If L9 is not ready, go to final output
            from core_orch_layer10 import layer_10_output
            await layer_10_output(text, chat_id, user, safe_output)

    except Exception as e:
        print(f"❌ L8 Error: {e}")
        from core_orch_layer10 import layer_10_output
        await layer_10_output(text, chat_id, user, "Safety check failed, providing sanitized fallback.")

if __name__ == "__main__":
    print("🛰️ Layer 8: Safety Guardian Online.")
