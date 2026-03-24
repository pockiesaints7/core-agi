"""
Layer 0: Security & Policy
Foundation layer - validates identity, enforces rate limits, manages permissions.
Runs before everything else. Cannot be bypassed.
"""
from dotenv import load_dotenv
load_dotenv()

import os
import sys
import time
import threading
from typing import Final, Optional
from orchestrator_message import OrchestratorMessage

# --- CONSTITUTION ---
TPS_LIMIT: Final[float] = 2.0
OWNER_ID: Final[str] = os.getenv("TELEGRAM_CHAT", "838737537")

class GlobalRateLimiter:
    """Token bucket rate limiter - prevents runaway loops."""
    def __init__(self, tps: float):
        self.tps = tps
        self.tokens = tps
        self.updated = time.time()
        self.lock = threading.Lock()

    def consume(self) -> bool:
        with self.lock:
            now = time.time()
            # Refill tokens based on time elapsed
            self.tokens = min(self.tps, self.tokens + (now - self.updated) * self.tps)
            self.updated = now
            
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False
    
    def wait_time(self) -> float:
        """Return seconds to wait for next token."""
        with self.lock:
            if self.tokens >= 1:
                return 0.0
            return (1.0 - self.tokens) / self.tps

LIMITER = GlobalRateLimiter(TPS_LIMIT)

def validate_environment() -> bool:
    """Ensure all required secrets are present."""
    required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT", "SUPABASE_URL", "SUPABASE_KEY"]
    missing = [r for r in required if not os.getenv(r)]
    
    if missing:
        print(f"❌ L0 CRITICAL: Missing environment variables: {missing}")
        return False
    
    print(f"✅ L0: Environment validated. Owner: {OWNER_ID}")
    return True

def determine_permission_tier(chat_id: int, source: str) -> str:
    """
    Determine permission tier based on identity.
    
    Returns:
        - "owner": All actions allowed
        - "trusted": Read + non-destructive write
        - "anonymous": Read-only, no tool execution
    """
    # Owner check
    if str(chat_id) == OWNER_ID:
        return "owner"
    
    # MCP from owner's Claude Desktop = owner tier
    if source == "mcp" and chat_id == int(OWNER_ID):
        return "owner"
    
    # System events = owner tier (they come from Railway)
    if source == "system":
        return "owner"
    
    # Everyone else is anonymous
    return "anonymous"

def check_rate_limit() -> bool:
    """Check if action is within rate limit."""
    return LIMITER.consume()

def gate_check(msg: OrchestratorMessage) -> bool:
    """
    L0 Security Gate - runs before any layer processes message.
    
    Returns:
        True if message should proceed
        False if message should be rejected
    """
    msg.track_layer("L0-START")
    
    # 1. Environment validation (one-time check)
    if not validate_environment():
        msg.add_error("L0", Exception("Environment validation failed"), "ENV_MISSING")
        return False
    
    # 2. Permission tier assignment
    tier = determine_permission_tier(msg.chat_id, msg.source)
    msg.tier = tier
    
    # 3. Rate limit check
    if not check_rate_limit():
        wait_time = LIMITER.wait_time()
        print(f"⏱️ L0: Rate limit hit. Wait {wait_time:.1f}s")
        msg.add_error("L0", Exception(f"Rate limit: wait {wait_time:.1f}s"), "RATE_LIMIT")
        return False
    
    # 4. Permission validation based on message type
    if msg.route == "command" and tier == "anonymous":
        print(f"🚫 L0: Anonymous user tried command execution")
        msg.add_error("L0", Exception("Permission denied: anonymous cannot execute commands"), "PERM_DENIED")
        return False
    
    print(f"✅ L0: Gate passed. Tier={tier}, Source={msg.source}, Route={msg.route}")
    msg.track_layer("L0-PASS")
    return True

if __name__ == "__main__":
    print("🛡️ Layer 0: Security & Policy Engine")
    print(f"   TPS Limit: {TPS_LIMIT}")
    print(f"   Owner ID: {OWNER_ID}")
    
    # Self-test
    test_msg = OrchestratorMessage(
        text="test",
        chat_id=int(OWNER_ID),
        user="test_user",
        source="telegram"
    )
    
    result = gate_check(test_msg)
    print(f"   Self-test: {'PASS' if result else 'FAIL'}")
    print(f"   Tier assigned: {test_msg.tier}")
