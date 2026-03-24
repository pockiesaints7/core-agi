"""
Layer 4: Reasoning & Planning
Pre-flight cognitive checks, task decomposition, and execution planning.
"""
import os
import asyncio
import json
from typing import Dict, Any, List
from orchestrator_message import OrchestratorMessage

def mock_groq_chat(prompt: str, model: str = "llama-3.3-70b-versatile") -> str:
    """Mock Groq chat"""
    print(f"   [MOCK] groq_chat for planning (prompt_len={len(prompt)})")
    return json.dumps({
        "needs_planning": False,
        "can_answer_directly": True,
        "requires_tools": False,
        "plan": []
    })

async def cognitive_preflight(msg: OrchestratorMessage) -> Dict[str, Any]:
    """
    Run cognitive pre-flight checks before execution.
    
    Checks:
        - Am I about to assume something I should query?
        - Is this action reversible?
        - Is this the right layer for this task?
        - Do I have enough context?
        - Am I solving the right problem?
    
    Returns dict with check results.
    """
    checks = {
        "passed": True,
        "warnings": [],
        "blockers": []
    }
    
    # Check 1: Do we have necessary context?
    if not msg.context.get("session"):
        checks["warnings"].append("No session context loaded")
    
    # Check 2: Is this a destructive action?
    destructive_keywords = ["delete", "remove", "drop", "force", "destroy"]
    if any(kw in msg.text.lower() for kw in destructive_keywords):
        if msg.tier != "owner":
            checks["blockers"].append("Destructive action requires owner tier")
            checks["passed"] = False
        else:
            checks["warnings"].append("Destructive action detected - will require confirmation")
    
    # Check 3: Intent confidence check
    intent_data = msg.context.get("intent_classification", {})
    if intent_data.get("confidence", 0) < 0.7:
        checks["warnings"].append(f"Low intent confidence: {intent_data.get('confidence')}")
    
    print(f"   [L4] Pre-flight: {'PASS' if checks['passed'] else 'BLOCKED'}")
    if checks["warnings"]:
        print(f"   [L4] Warnings: {checks['warnings']}")
    if checks["blockers"]:
        print(f"   [L4] Blockers: {checks['blockers']}")
    
    return checks

async def create_execution_plan(msg: OrchestratorMessage) -> Dict[str, Any]:
    """
    Create execution plan based on intent and context.
    
    For simple queries: no plan needed, direct response
    For complex tasks: decompose into subtasks with tool selection
    """
    intent_data = msg.context.get("intent_classification", {})
    
    # Simple conversational messages don't need planning
    if intent_data.get("category") == "conversation" and not intent_data.get("requires_tools"):
        return {
            "type": "direct_response",
            "subtasks": [],
            "estimated_complexity": "low"
        }
    
    # For tool-requiring tasks, build a plan
    if intent_data.get("requires_tools"):
        # Use Groq to decompose
        plan_prompt = f"""
You are a task planner for CORE AGI.

USER REQUEST: {msg.text}
INTENT: {msg.intent}
CONTEXT: {json.dumps(msg.context.get("session", {}))}

TASK: Create an execution plan.

Return JSON only:
{{
    "type": "tool_execution",
    "subtasks": [
        {{"step": 1, "action": "...", "tool": "...", "expected_output": "..."}},
        ...
    ],
    "estimated_complexity": "low|medium|high",
    "requires_confirmation": true|false
}}
"""
        
        try:
            plan_response = mock_groq_chat(plan_prompt)
            plan = json.loads(plan_response.strip())
            print(f"   [L4] Created plan with {len(plan.get('subtasks', []))} subtasks")
            return plan
        except Exception as e:
            print(f"   [L4] Planning failed: {e}")
            # Fallback plan
            return {
                "type": "direct_response",
                "subtasks": [],
                "estimated_complexity": "unknown",
                "error": str(e)
            }
    
    # Default: simple response
    return {
        "type": "direct_response",
        "subtasks": [],
        "estimated_complexity": "low"
    }

async def layer_4_reason(msg: OrchestratorMessage):
    """
    L4: Reasoning & Planning
    
    Runs cognitive pre-flight checks and creates execution plan.
    
    Mutates msg.plan with execution strategy.
    """
    try:
        msg.track_layer("L4-START")
        print(f"🧠 [L4: Reasoning] Planning execution for @{msg.user}...")
        
        # 1. Cognitive pre-flight checks
        preflight = await cognitive_preflight(msg)
        msg.context["preflight_checks"] = preflight
        
        # 2. If preflight blocked, skip to output with error
        if not preflight["passed"]:
            msg.add_error("L4", Exception("Pre-flight checks failed"), "PREFLIGHT_BLOCKED")
            from core_orch_layer10_fixed import layer_10_output
            await layer_10_output(msg)
            return
        
        # 3. Create execution plan
        plan = await create_execution_plan(msg)
        msg.plan = plan
        msg.context["execution_plan"] = plan
        
        msg.track_layer("L4-COMPLETE")
        print(f"✅ [L4] Plan created: {plan.get('type')}, complexity={plan.get('estimated_complexity')}")
        
        # Pass to L5 (Tool Execution)
        from core_orch_layer5_fixed import layer_5_tools
        await layer_5_tools(msg)
        
    except Exception as e:
        print(f"❌ L4 Error: {e}")
        msg.add_error("L4", e, "REASONING_FAILED")
        
        from core_orch_layer10_fixed import layer_10_output
        await layer_10_output(msg)

if __name__ == "__main__":
    print("🛰️ Layer 4: Reasoning & Planning Engine")
    
    async def test():
        test_msg = OrchestratorMessage(
            text="Show me system health",
            chat_id=838737537,
            user="test_user",
            tier="owner"
        )
        test_msg.intent = "system_command"
        test_msg.context["intent_classification"] = {
            "requires_tools": True,
            "category": "command",
            "confidence": 0.9
        }
        
        await layer_4_reason(test_msg)
        print(f"   Plan: {test_msg.plan}")
    
    asyncio.run(test())
