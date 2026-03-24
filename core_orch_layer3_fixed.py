"""
Layer 3: Intent Classification
Uses Groq to classify user intent and determine execution strategy.
"""
import os
import asyncio
import json
from typing import Dict, Any, Optional
from orchestrator_message import OrchestratorMessage

def mock_groq_chat(prompt: str, model: str = "llama-3.3-70b-versatile") -> str:
    """Mock Groq chat - replace with actual core_config.groq_chat"""
    print(f"   [MOCK] groq_chat(model={model}, prompt_len={len(prompt)})")
    
    # Return mock JSON response
    return json.dumps({
        "intent": "general_query",
        "confidence": 0.85,
        "category": "conversation",
        "requires_tools": False,
        "suggested_response_type": "conversational"
    })

async def classify_intent(msg: OrchestratorMessage) -> Dict[str, Any]:
    """
    Use Groq to classify the user's intent.
    
    Returns dict with:
        - intent: primary intent classification
        - confidence: 0-1 confidence score
        - category: broad category (task/question/command/conversation)
        - requires_tools: whether tool execution is needed
        - suggested_response_type: how to respond
    """
    
    # Build classification prompt
    prompt = f"""
You are an intent classifier for CORE AGI system.

USER MESSAGE: {msg.text}
SOURCE: {msg.source}
MESSAGE TYPE: {msg.message_type}
USER TIER: {msg.tier}

TASK: Classify the user's intent.

Return JSON only, no preamble:
{{
    "intent": "task_execution|general_query|system_command|conversation|greeting",
    "confidence": 0.0-1.0,
    "category": "task|question|command|conversation",
    "requires_tools": true|false,
    "suggested_response_type": "conversational|structured|confirmation"
}}
"""
    
    try:
        # In real implementation: groq_response = groq_chat(prompt)
        groq_response = mock_groq_chat(prompt)
        
        # Parse JSON response
        classification = json.loads(groq_response.strip())
        
        print(f"   [L3] Intent: {classification.get('intent')} (conf={classification.get('confidence')})")
        
        return classification
        
    except Exception as e:
        print(f"   [L3] Classification failed: {e}")
        # Fallback to safe defaults
        return {
            "intent": "general_query",
            "confidence": 0.5,
            "category": "conversation",
            "requires_tools": False,
            "suggested_response_type": "conversational"
        }

async def layer_3_classify(msg: OrchestratorMessage):
    """
    L3: Intent Classification
    
    Analyzes the message and context to determine:
        - What does the user want?
        - What category of response is needed?
        - Should we execute tools or just respond conversationally?
    
    Mutates msg.intent with classification result.
    """
    try:
        msg.track_layer("L3-START")
        print(f"🚦 [L3: Intent] Classifying intent from @{msg.user}...")
        
        # Classify the intent
        classification = await classify_intent(msg)
        
        # Store in message
        msg.intent = classification.get("intent", "general_query")
        msg.context["intent_classification"] = classification
        
        msg.track_layer("L3-COMPLETE")
        print(f"✅ [L3] Classified as: {msg.intent}")
        
        # Pass to L4 (Reasoning)
        from core_orch_layer4_fixed import layer_4_reason
        await layer_4_reason(msg)
        
    except Exception as e:
        print(f"❌ L3 Error: {e}")
        msg.add_error("L3", e, "INTENT_CLASSIFICATION_FAILED")
        
        # Jump to L10 for error output
        from core_orch_layer10_fixed import layer_10_output
        await layer_10_output(msg)

if __name__ == "__main__":
    print("🛰️ Layer 3: Intent Classification Engine")
    
    # Self-test
    async def test():
        test_msg = OrchestratorMessage(
            text="What's the current status of the system?",
            chat_id=838737537,
            user="test_user",
            source="telegram",
            tier="owner"
        )
        
        await layer_3_classify(test_msg)
        print(f"   Intent: {test_msg.intent}")
        print(f"   Classification: {test_msg.context.get('intent_classification')}")
    
    asyncio.run(test())
