"""
Layer 1: Input
Transforms raw signals into structured OrchestratorMessage objects.
Routes to appropriate handler (command/conversation/background).
"""
import os
import asyncio
from typing import Dict, Any
from orchestrator_message import OrchestratorMessage

async def parse_telegram_message(update: Dict[str, Any]) -> OrchestratorMessage:
    """Parse Telegram update into OrchestratorMessage."""
    message = update.get("message", {})
    
    msg = OrchestratorMessage(
        text=message.get("text", ""),
        chat_id=message.get("chat", {}).get("id", 0),
        user=message.get("from", {}).get("username", "unknown"),
        source="telegram",
        message_type="message"
    )
    
    # Detect message type
    if msg.text.startswith("/"):
        msg.message_type = "command"
        msg.route = "command"
    else:
        msg.message_type = "message"
        msg.route = "conversation"
    
    # Handle attachments
    if "photo" in message:
        msg.attachments.append({"type": "photo", "data": message["photo"]})
    if "document" in message:
        msg.attachments.append({"type": "document", "data": message["document"]})
    if "voice" in message:
        msg.attachments.append({"type": "voice", "data": message["voice"]})
        msg.message_type = "voice"
    
    return msg

async def parse_mcp_request(request: Dict[str, Any]) -> OrchestratorMessage:
    """Parse MCP tool call into OrchestratorMessage."""
    msg = OrchestratorMessage(
        text=request.get("params", {}).get("query", ""),
        chat_id=int(os.getenv("TELEGRAM_CHAT", "838737537")),  # MCP = owner
        user="claude_desktop",
        source="mcp",
        message_type="command",
        route="command"
    )
    
    # Store full MCP context
    msg.context["mcp_method"] = request.get("method", "")
    msg.context["mcp_params"] = request.get("params", {})
    
    return msg

async def parse_system_event(event: Dict[str, Any]) -> OrchestratorMessage:
    """Parse system event (cron, heartbeat) into OrchestratorMessage."""
    msg = OrchestratorMessage(
        text=event.get("event_type", ""),
        chat_id=int(os.getenv("TELEGRAM_CHAT", "838737537")),
        user="system",
        source="system",
        message_type="event",
        route="background"
    )
    
    msg.context["event_data"] = event.get("data", {})
    
    return msg

async def layer_1_triage(raw_input: Dict[str, Any], input_type: str = "telegram") -> OrchestratorMessage:
    """
    L1: Input Reception & Parsing
    
    Transforms raw input into OrchestratorMessage and routes to L2.
    
    Args:
        raw_input: Raw input dict (Telegram update, MCP request, system event)
        input_type: "telegram" | "mcp" | "system"
    
    Returns:
        OrchestratorMessage ready for L2
    """
    try:
        print(f"📥 [L1: Triage] Processing {input_type} input...")
        
        # Parse based on input type
        if input_type == "telegram":
            msg = await parse_telegram_message(raw_input)
        elif input_type == "mcp":
            msg = await parse_mcp_request(raw_input)
        elif input_type == "system":
            msg = await parse_system_event(raw_input)
        else:
            raise ValueError(f"Unknown input_type: {input_type}")
        
        msg.track_layer("L1-PARSE")
        
        print(f"📋 [L1] Parsed: type={msg.message_type}, route={msg.route}, user=@{msg.user}")
        
        # Pass to L0 for security gate
        from core_orch_layer0_fixed import gate_check
        if not gate_check(msg):
            print(f"🚫 [L1] Security gate rejected message")
            # Still return msg so error can be surfaced
            return msg
        
        # Pass to L2 (Context & Memory)
        from core_orch_layer2_fixed import layer_2_process
        await layer_2_process(msg)
        
        return msg
        
    except Exception as e:
        print(f"❌ L1 Error: {e}")
        msg = OrchestratorMessage(text=str(raw_input), chat_id=0, user="error")
        msg.add_error("L1", e, "PARSE_ERROR")
        
        # Jump to L10 for error output
        from core_orch_layer10_fixed import layer_10_output
        await layer_10_output(msg)
        
        return msg

if __name__ == "__main__":
    print("🛰️ Layer 1: Input Triage & Parsing Engine")
    
    # Self-test
    async def test():
        test_telegram = {
            "message": {
                "text": "Hello CORE",
                "chat": {"id": 838737537},
                "from": {"username": "reinvagnar"}
            }
        }
        
        msg = await layer_1_triage(test_telegram, "telegram")
        print(f"   Test result: {msg.message_type}, tier={msg.tier}")
    
    asyncio.run(test())
