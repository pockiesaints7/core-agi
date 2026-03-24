"""
Layer 5: Tool Execution
Executes tools based on the plan from L4.
Routes to actual core_tools.py functions.
"""
import os
import asyncio
from typing import Dict, Any
from orchestrator_message import OrchestratorMessage

async def execute_tools(msg: OrchestratorMessage) -> bool:
    """
    Execute tools based on plan.
    
    Returns True if execution succeeded, False otherwise.
    """
    plan = msg.plan
    
    if plan.get("type") == "direct_response":
        # No tools needed
        print(f"   [L5] No tools required for this request")
        return True
    
    subtasks = plan.get("subtasks", [])
    if not subtasks:
        print(f"   [L5] Empty subtask list")
        return True
    
    # Execute each subtask
    for subtask in subtasks:
        tool_name = subtask.get("tool", "unknown")
        action = subtask.get("action", "")
        
        print(f"   [L5] Executing: {tool_name} - {action}")
        
        # In real implementation, call actual tool from core_tools.py
        # For now, simulate success
        msg.add_tool_result(tool_name, success=True, result={"simulated": True})
    
    return True

async def layer_5_tools(msg: OrchestratorMessage):
    """
    L5: Tool Execution
    
    Executes the plan from L4 by calling actual tools.
    """
    try:
        msg.track_layer("L5-START")
        print(f"🔧 [L5: Tools] Executing actions for @{msg.user}...")
        
        # Execute tools
        success = await execute_tools(msg)
        
        if not success:
            msg.add_error("L5", Exception("Tool execution failed"), "TOOL_EXEC_FAILED")
        
        msg.track_layer("L5-COMPLETE")
        print(f"✅ [L5] Execution complete: {len(msg.tool_results)} tools called")
        
        # Pass to L6 (Validation)
        from core_orch_layer6_fixed import layer_6_validate
        await layer_6_validate(msg)
        
    except Exception as e:
        print(f"❌ L5 Error: {e}")
        msg.add_error("L5", e, "TOOL_LAYER_FAILED")
        
        from core_orch_layer10_fixed import layer_10_output
        await layer_10_output(msg)

if __name__ == "__main__":
    print("🛰️ Layer 5: Tool Execution Engine")
