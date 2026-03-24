"""
Layer 7: Self-Refinement
Analyzes the interaction for evolution opportunities.
"""
import os
import asyncio
import json
from orchestrator_message import OrchestratorMessage

def mock_groq_chat(prompt: str) -> str:
    """Mock Groq"""
    return json.dumps({"propose_evolution": False})

async def check_evolution_opportunity(msg: OrchestratorMessage) -> bool:
    """Check if this interaction reveals an improvement opportunity."""
    
    # Only check for owner-tier interactions
    if msg.tier != "owner":
        return False
    
    # Don't evolve on errors
    if msg.errors:
        print(f"   [L7] Skipping evolution check - errors present")
        return False
    
    # Use Groq to analyze
    refine_prompt = f"""
USER REQUEST: {msg.text}
TOOL RESULTS: {json.dumps(msg.tool_results)}

Is there an optimization or improvement opportunity?
Return JSON: {{"propose_evolution": true|false, "reason": "...", "suggestion": "..."}}
"""
    
    try:
        response = mock_groq_chat(refine_prompt)
        analysis = json.loads(response.strip())
        
        if analysis.get("propose_evolution"):
            msg.evolutions_proposed.append(analysis)
            print(f"   [L7] Evolution proposed: {analysis.get('reason')}")
            return True
        
    except Exception as e:
        print(f"   [L7] Evolution check failed: {e}")
    
    return False

async def layer_7_refine(msg: OrchestratorMessage):
    """
    L7: Self-Refinement & Evolution
    
    Looks for improvement opportunities.
    """
    try:
        msg.track_layer("L7-START")
        print(f"🔧 [L7: Refinement] Analyzing for improvements...")
        
        # Check for evolution opportunity
        await check_evolution_opportunity(msg)
        
        msg.track_layer("L7-COMPLETE")
        
        # Pass to L8 (Safety)
        from core_orch_layer8_fixed import layer_8_safety
        await layer_8_safety(msg)
        
    except Exception as e:
        print(f"❌ L7 Error: {e}")
        msg.add_error("L7", e, "REFINEMENT_FAILED")
        
        from core_orch_layer10_fixed import layer_10_output
        await layer_10_output(msg)

if __name__ == "__main__":
    print("🛰️ Layer 7: Refinement Engine")
