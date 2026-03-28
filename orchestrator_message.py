"""
orchestrator_message.py — CORE AGI Orchestrator
Single message object passed through all 11 layers (L0→L10).
No mocks. Production ready.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime
import uuid


@dataclass
class OrchestratorMessage:
    """Single message object passed through all 11 layers."""

    # Core identity (set at L1)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    text: str = ""
    chat_id: int = 0
    user: str = ""
    timestamp: float = field(default_factory=lambda: datetime.utcnow().timestamp())

    # Source & routing (set at L1)
    source: str = "telegram"          # telegram | mcp | api | system
    message_type: str = "message"     # command | message | file | event | voice
    route: str = "conversation"       # command | conversation | background

    # Permission tier (set at L0)
    tier: str = "anonymous"           # owner | trusted | anonymous

    # Layer state accumulators
    context: Dict[str, Any] = field(default_factory=dict)          # L2 Memory
    intent: Optional[str] = None                                    # L3 Intent
    plan: Dict[str, Any] = field(default_factory=dict)              # L4 Plan
    tool_results: List[Dict[str, Any]] = field(default_factory=list)# L5 Tools
    validation_status: Dict[str, Any] = field(default_factory=dict) # L6 Validation
    evolutions_proposed: List[Dict[str, Any]] = field(default_factory=list)  # L7
    safety_redacted: List[str] = field(default_factory=list)        # L8 Safety
    styled_response: Optional[str] = None                           # L9 Tone
    final_output: Optional[str] = None                              # L10 Output

    # Error tracking
    errors: List[Dict[str, Any]] = field(default_factory=list)
    layer_stack: List[str] = field(default_factory=list)

    # Attachments (L1)
    attachments: List[Dict[str, Any]] = field(default_factory=list)

    def add_error(self, layer: str, error: Exception, error_code: str = "UNKNOWN"):
        self.errors.append({
            "layer": layer,
            "error_type": type(error).__name__,
            "message": str(error),
            "error_code": error_code,
            "timestamp": datetime.utcnow().timestamp(),
        })

    def add_tool_result(self, tool_name: str, success: bool, result: Any):
        self.tool_results.append({
            "tool": tool_name,
            "success": success,
            "result": result,
            "timestamp": datetime.utcnow().timestamp(),
        })

    def track_layer(self, layer: str):
        self.layer_stack.append(f"{layer}@{datetime.utcnow().timestamp():.3f}")

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0
