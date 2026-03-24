"""
Layer 6: Validation
Validates tool outputs and plan execution results.
"""
import os
import asyncio
from orchestrator_message import OrchestratorMessage

async def validate_results(msg: OrchestratorMessage) -> bool:
    """Validate that tool results match expected outputs."""
    
    # Check if any tools failed
    failed_tools = [r for r in msg.tool_results if not r.get("success")]
    
    if failed_tools:
        print(f"   [L6] {len(failed_tools)} tools failed validation")
        return False
    
    print(f"   [L6] All {len(msg.tool_results)} tools passed validation")
    return True

async def layer_6_validate(msg: OrchestratorMessage):
    """
    L6: Validation & Verification
    
    Checks if tool outputs make sense before passing forward.
    """
    try:
        msg.track_layer("L6-START")
        print(f"⚖️ [L6: Validation] Verifying results for @{msg.user}...")
        
        # Validate results
        valid = await validate_results(msg)
        
        msg.validation_status = {
            "passed": valid,
            "checked_at": msg.timestamp
        }
        
        msg.track_layer("L6-COMPLETE")
        
        # Pass to L7 (Refinement)
        from core_orch_layer7_fixed import layer_7_refine
        await layer_7_refine(msg)
        
    except Exception as e:
        print(f"❌ L6 Error: {e}")
        msg.add_error("L6", e, "VALIDATION_FAILED")
        
        from core_orch_layer10_fixed import layer_10_output
        await layer_10_output(msg)

if __name__ == "__main__":
    print("🛰️ Layer 6: Validation Engine")
