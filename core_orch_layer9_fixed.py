"""
Layer 9: Tone & Personality
Transforms raw data into styled response with CORE personality.
"""
import os
import asyncio
import json
from orchestrator_message import OrchestratorMessage

def mock_groq_chat(prompt: str) -> str:
    """Mock Groq"""
    return "Here's a styled response based on the data provided."

async def apply_personality(msg: OrchestratorMessage) -> str:
    """
    Apply CORE personality to the response.
    
    Takes raw tool results and context, generates natural response.
    """
    
    # Gather all data to present
    results_summary = []
    for result in msg.tool_results:
        results_summary.append(f"- {result.get('tool')}: {result.get('result')}")
    
    # Build styling prompt
    style_prompt = f"""
USER MESSAGE: {msg.text}
TOOL RESULTS:
{chr(10).join(results_summary)}

You are CORE - an autonomous AGI orchestration system.
Your tone is: direct, technical, competent. No fluff.
Transform the tool results into a natural response.

RESPONSE:
"""
    
    try:
        styled = mock_groq_chat(style_prompt)
        print(f"   [L9] Styled response ({len(styled)} chars)")
        return styled
    except Exception as e:
        print(f"   [L9] Styling failed: {e}")
        # Fallback to raw summary
        return "\n".join(results_summary)

async def layer_9_tone(msg: OrchestratorMessage):
    """
    L9: Tone & Personality
    
    Styles the response with CORE's personality.
    """
    try:
        msg.track_layer("L9-START")
        print(f"🎭 [L9: Tone] Styling response for @{msg.user}...")
        
        # Apply personality
        styled = await apply_personality(msg)
        msg.styled_response = styled
        
        msg.track_layer("L9-COMPLETE")
        
        # Pass to L10 (Output)
        from core_orch_layer10_fixed import layer_10_output
        await layer_10_output(msg)
        
    except Exception as e:
        print(f"❌ L9 Error: {e}")
        msg.add_error("L9", e, "STYLING_FAILED")
        
        from core_orch_layer10_fixed import layer_10_output
        await layer_10_output(msg)

if __name__ == "__main__":
    print("🛰️ Layer 9: Persona Engine")
