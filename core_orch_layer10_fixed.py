"""
Layer 10: Output Delivery
Final layer - formats and delivers response to user.
"""
import os
import asyncio
from orchestrator_message import OrchestratorMessage

def mock_notify(text: str, chat_id: int):
    """Mock Telegram notify - replace with actual core_config.notify"""
    print(f"   [TELEGRAM] → {chat_id}: {text[:100]}...")

async def format_for_telegram(msg: OrchestratorMessage) -> str:
    """
    Format response for Telegram delivery.
    
    Handles:
        - MarkdownV2 escaping
        - 4096 char limit
        - Error formatting
    """
    
    # If errors present, format error message
    if msg.errors:
        error_text = "❌ Errors occurred:\n\n"
        for err in msg.errors:
            error_text += f"• {err['layer']}: {err['message']}\n"
        return error_text[:4000]
    
    # Use styled response if available
    if msg.styled_response:
        response = msg.styled_response
    else:
        # Fallback: summarize tool results
        response = "✅ Task completed\n\n"
        for result in msg.tool_results:
            response += f"• {result.get('tool')}: {result.get('success')}\n"
    
    # Truncate if needed
    if len(response) > 4000:
        response = response[:3900] + "\n\n[Truncated - response too long]"
    
    return response

async def layer_10_output(msg: OrchestratorMessage):
    """
    L10: Output Delivery
    
    Formats and delivers the final response.
    This is the last layer - no forwarding from here.
    """
    try:
        msg.track_layer("L10-START")
        print(f"📡 [L10: Output] Delivering to @{msg.user}...")
        
        # Format based on source
        if msg.source == "telegram":
            output = await format_for_telegram(msg)
            mock_notify(output, msg.chat_id)
        elif msg.source == "mcp":
            # For MCP, return JSON
            output = {
                "success": len(msg.errors) == 0,
                "response": msg.styled_response or "Completed",
                "tool_results": msg.tool_results
            }
            print(f"   [MCP] Returning: {output}")
        else:
            # System events - maybe log only
            print(f"   [SYSTEM] Event processed: {msg.text}")
        
        msg.final_output = str(output)
        msg.track_layer("L10-COMPLETE")
        
        # Log completion
        print(f"🏁 [Complete] Layers: {' → '.join(msg.layer_stack)}")
        print(f"   Total time: {len(msg.layer_stack)} layers processed")
        
    except Exception as e:
        print(f"❌ L10 CRITICAL Error: {e}")
        # Last resort - try to notify owner
        try:
            mock_notify(f"⚠️ L10 failure: {str(e)[:100]}", msg.chat_id)
        except:
            print("   [L10] Even fallback notify failed")

if __name__ == "__main__":
    print("🛰️ Layer 10: Output Dispatcher")
