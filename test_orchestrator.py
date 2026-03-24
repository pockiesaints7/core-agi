"""
Full orchestrator integration test.
Tests the complete L0 → L10 pipeline.
"""
import asyncio
import json
from orchestrator_message import OrchestratorMessage
from core_orch_layer1_fixed import layer_1_triage

async def test_simple_message():
    """Test 1: Simple conversational message"""
    print("\n" + "="*60)
    print("TEST 1: Simple Conversational Message")
    print("="*60)
    
    telegram_update = {
        "message": {
            "text": "Hello, how are you?",
            "chat": {"id": 838737537},
            "from": {"username": "reinvagnar"}
        }
    }
    
    msg = await layer_1_triage(telegram_update, "telegram")
    
    print(f"\n📊 RESULT:")
    print(f"   Message ID: {msg.message_id}")
    print(f"   Tier: {msg.tier}")
    print(f"   Intent: {msg.intent}")
    print(f"   Errors: {len(msg.errors)}")
    print(f"   Tool Results: {len(msg.tool_results)}")
    print(f"   Layer Stack: {' → '.join(msg.layer_stack)}")
    
    if msg.final_output:
        print(f"\n💬 FINAL OUTPUT:")
        print(f"   {msg.final_output[:200]}...")
    
    return msg

async def test_command_message():
    """Test 2: Command with tool execution"""
    print("\n" + "="*60)
    print("TEST 2: System Command")
    print("="*60)
    
    telegram_update = {
        "message": {
            "text": "/health",
            "chat": {"id": 838737537},
            "from": {"username": "reinvagnar"}
        }
    }
    
    msg = await layer_1_triage(telegram_update, "telegram")
    
    print(f"\n📊 RESULT:")
    print(f"   Tier: {msg.tier}")
    print(f"   Intent: {msg.intent}")
    print(f"   Plan Type: {msg.plan.get('type')}")
    print(f"   Tools Called: {len(msg.tool_results)}")
    print(f"   Errors: {len(msg.errors)}")
    print(f"   Layer Stack: {' → '.join(msg.layer_stack)}")
    
    return msg

async def test_anonymous_user():
    """Test 3: Anonymous user attempting command"""
    print("\n" + "="*60)
    print("TEST 3: Anonymous User (Should Reject)")
    print("="*60)
    
    telegram_update = {
        "message": {
            "text": "/admin",
            "chat": {"id": 999999},  # Not owner
            "from": {"username": "anonymous"}
        }
    }
    
    msg = await layer_1_triage(telegram_update, "telegram")
    
    print(f"\n📊 RESULT:")
    print(f"   Tier: {msg.tier}")
    print(f"   Errors: {len(msg.errors)}")
    if msg.errors:
        print(f"   Error: {msg.errors[0]['message']}")
    print(f"   Layer Stack: {' → '.join(msg.layer_stack)}")
    
    return msg

async def main():
    """Run all tests"""
    print("🚀 CORE AGI ORCHESTRATOR - INTEGRATION TEST")
    print("Testing full L0→L10 pipeline\n")
    
    # Test 1: Simple message
    msg1 = await test_simple_message()
    
    # Test 2: Command
    msg2 = await test_command_message()
    
    # Test 3: Anonymous
    msg3 = await test_anonymous_user()
    
    print("\n" + "="*60)
    print("✅ ALL TESTS COMPLETE")
    print("="*60)
    print(f"Test 1: {len(msg1.errors)} errors")
    print(f"Test 2: {len(msg2.errors)} errors")
    print(f"Test 3: {len(msg3.errors)} errors (expected)")

if __name__ == "__main__":
    asyncio.run(main())
