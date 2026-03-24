"""
Layer 2: Memory & Context
Loads session context, behavioral rules, and memory from Supabase.
Hydrates the context dict for downstream layers.
"""
import os
import asyncio
from typing import Dict, Any, List
from orchestrator_message import OrchestratorMessage

# Mock Supabase functions (replace with actual core_config imports)
def mock_sb_query(table: str, filters: str = "", select: str = "*") -> List[Dict[str, Any]]:
    """Mock Supabase query - replace with actual core_config.sb_query"""
    print(f"   [MOCK] sb_query(table={table}, filters={filters[:50]}...)")
    return []

def mock_search_kb(domain: str = "", topic: str = "") -> List[Dict[str, Any]]:
    """Mock KB search - replace with actual search_kb tool"""
    print(f"   [MOCK] search_kb(domain={domain}, topic={topic})")
    return []

async def load_session_context(msg: OrchestratorMessage) -> Dict[str, Any]:
    """
    Load session context from Supabase.
    
    Returns dict with:
        - session_id: current session
        - last_session: what happened last time
        - in_progress_tasks: tasks that need resuming
        - health: system health status
    """
    context = {}
    
    try:
        # In real implementation, call actual session_start tool
        # For now, simulate structure
        context["session_id"] = "session_" + str(int(msg.timestamp))
        context["last_session"] = {
            "session_id": "previous_session",
            "summary": "Mock previous session",
            "tasks_completed": 0
        }
        context["in_progress_tasks"] = []
        context["health"] = {
            "railway": "unknown",
            "supabase": "unknown",
            "groq": "unknown"
        }
        
        print(f"   [L2] Session context loaded: {context['session_id']}")
        
    except Exception as e:
        print(f"   [L2] Failed to load session context: {e}")
        context["error"] = str(e)
    
    return context

async def load_behavioral_rules(msg: OrchestratorMessage) -> List[Dict[str, Any]]:
    """
    Load behavioral rules from knowledge_base.
    
    These are domain-specific prompt rules that modify behavior.
    """
    rules = []
    
    try:
        # In real implementation:
        # rules = search_kb(domain="core_agi.behavior", topic="")
        
        # Mock for now
        rules = mock_search_kb(domain="core_agi.behavior")
        
        print(f"   [L2] Loaded {len(rules)} behavioral rules")
        
    except Exception as e:
        print(f"   [L2] Failed to load behavioral rules: {e}")
    
    return rules

async def load_domain_mistakes(msg: OrchestratorMessage, domain: str = "general") -> List[Dict[str, Any]]:
    """
    Load recent mistakes for the current domain.
    Critical for avoiding repeated failures.
    """
    mistakes = []
    
    try:
        # In real implementation:
        # mistakes = mock_sb_query(
        #     table="domain_mistakes",
        #     filters=f"domain=eq.{domain}&order=created_at.desc",
        #     select="id,domain,mistake_text,created_at"
        # )[:5]  # Top 5 most recent
        
        print(f"   [L2] Loaded {len(mistakes)} domain mistakes for '{domain}'")
        
    except Exception as e:
        print(f"   [L2] Failed to load domain mistakes: {e}")
    
    return mistakes

async def load_working_memory(msg: OrchestratorMessage) -> Dict[str, Any]:
    """
    Load working memory (task variables, scratchpad).
    This is session-scoped key-value storage.
    """
    working_mem = {}
    
    try:
        # In real implementation, might query a working_memory table
        # or use Redis/in-memory cache
        
        working_mem["scratchpad"] = {}
        working_mem["task_vars"] = {}
        
        print(f"   [L2] Working memory initialized")
        
    except Exception as e:
        print(f"   [L2] Failed to load working memory: {e}")
    
    return working_mem

async def layer_2_process(msg: OrchestratorMessage):
    """
    L2: Memory & Context Loading
    
    Hydrates msg.context with all necessary memory:
        - Session context
        - Behavioral rules
        - Domain mistakes
        - Working memory
        - Short-term conversation buffer
    
    Mutates msg.context in place.
    """
    try:
        msg.track_layer("L2-START")
        print(f"🧠 [L2: Memory] Building context for @{msg.user}...")
        
        # 1. Load session context (who am I, what project, what's my state)
        session_ctx = await load_session_context(msg)
        msg.context["session"] = session_ctx
        
        # 2. Load behavioral rules for this session
        rules = await load_behavioral_rules(msg)
        msg.context["behavioral_rules"] = rules
        
        # 3. Load domain mistakes (if route is command, try to infer domain)
        domain = "general"
        if msg.route == "command":
            # In real implementation, classify domain from command
            domain = "general"
        
        mistakes = await load_domain_mistakes(msg, domain)
        msg.context["domain_mistakes"] = mistakes
        msg.context["current_domain"] = domain
        
        # 4. Initialize working memory
        working_mem = await load_working_memory(msg)
        msg.context["working_memory"] = working_mem
        
        # 5. Short-term conversation buffer (last N messages)
        # In real implementation, load from conversation history
        msg.context["conversation_history"] = []
        
        msg.track_layer("L2-COMPLETE")
        print(f"✅ [L2] Context loaded: {len(rules)} rules, {len(mistakes)} mistakes")
        
        # Pass to L3 (Intent Classification)
        from core_orch_layer3_fixed import layer_3_classify
        await layer_3_classify(msg)
        
    except Exception as e:
        print(f"❌ L2 Error: {e}")
        msg.add_error("L2", e, "CONTEXT_LOAD_FAILED")
        
        # Jump to L10 for error output
        from core_orch_layer10_fixed import layer_10_output
        await layer_10_output(msg)

if __name__ == "__main__":
    print("🛰️ Layer 2: Memory & Context Engine")
    
    # Self-test
    async def test():
        test_msg = OrchestratorMessage(
            text="test message",
            chat_id=838737537,
            user="test_user",
            source="telegram"
        )
        
        await layer_2_process(test_msg)
        print(f"   Context keys: {list(test_msg.context.keys())}")
    
    asyncio.run(test())
