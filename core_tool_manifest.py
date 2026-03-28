"""Canonical tool-family map for CORE AGI.

This module is documentation-first and intentionally light on logic.
It tells future work where a new tool should live before it gets added to the
facade registry in core_tools.py.
"""

TOOL_FAMILY_MODULES = {
    "reasoning": "core_tools_memory.py",
    "graph": "core_tools_graph.py",
    "temporal_world_model": "core_tools_world_model.py",
    "reasoning_world_model": "core_tools_world_model.py",
    "web": "core_web.py",
    "task_autonomy": "core_task_autonomy.py",
    "code_autonomy": "core_code_autonomy.py",
    "integration_autonomy": "core_integration_autonomy.py",
    "research_autonomy": "core_research_autonomy.py",
    "proposal_router": "core_proposal_router.py",
    "semantic_projection": "core_semantic_projection.py",
    "evolution_autonomy": "core_evolution_autonomy.py",
    "governance": "core_tools_governance.py",
}


def tool_module_for_family(family: str) -> str:
    """Return the recommended module filename for a tool family."""
    return TOOL_FAMILY_MODULES.get((family or "").strip(), "core_tools.py")
