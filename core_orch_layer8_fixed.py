"""
Layer 8: Safety & Policy
Scans output for sensitive data and redacts before forwarding.
"""
import os
import re
import asyncio
from orchestrator_message import OrchestratorMessage

REDACTION_PATTERNS = {
    "GITHUB_PAT": r"ghp_[a-zA-Z0-9]{36}",
    "SUPABASE_KEY": r"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9\.[a-zA-Z0-9\-_]+\.[a-zA-Z0-9\-_]+",
    "LOCAL_PATH": r"/home/ubuntu/[\w\-/]+",
    "EMAIL": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
}

async def scan_and_redact(text: str) -> tuple[str, list[str]]:
    """
    Scan text for sensitive patterns and redact them.
    
    Returns:
        (redacted_text, list_of_redacted_patterns)
    """
    redacted = text
    found_patterns = []
    
    for label, pattern in REDACTION_PATTERNS.items():
        matches = re.findall(pattern, redacted)
        if matches:
            found_patterns.append(label)
            redacted = re.sub(pattern, f"[REDACTED_{label}]", redacted)
    
    return redacted, found_patterns

async def layer_8_safety(msg: OrchestratorMessage):
    """
    L8: Safety & Policy
    
    Scans all outputs for sensitive data and redacts.
    """
    try:
        msg.track_layer("L8-START")
        print(f"🛡️ [L8: Safety] Scanning output for @{msg.user}...")
        
        # Scan tool results
        for result in msg.tool_results:
            result_str = str(result.get("result", ""))
            clean_result, redacted = await scan_and_redact(result_str)
            
            if redacted:
                msg.safety_redacted.extend(redacted)
                result["result"] = clean_result
                print(f"   [L8] Redacted {redacted} from tool result")
        
        msg.track_layer("L8-COMPLETE")
        
        # Pass to L9 (Tone)
        from core_orch_layer9_fixed import layer_9_tone
        await layer_9_tone(msg)
        
    except Exception as e:
        print(f"❌ L8 Error: {e}")
        msg.add_error("L8", e, "SAFETY_CHECK_FAILED")
        
        from core_orch_layer10_fixed import layer_10_output
        await layer_10_output(msg)

if __name__ == "__main__":
    print("🛰️ Layer 8: Safety Guardian")
