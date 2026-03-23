from dotenv import load_dotenv
load_dotenv()  # This pulls the variables from your .env file into the OS
import os
import sys
import time
import threading
from typing import Final

# --- THE CONSTITUTION ---
TPS_LIMIT: Final[float] = 2.0  
OWNER_ID: Final[str] = os.getenv("TELEGRAM_CHAT", "")

class GlobalRateLimiter:
    def __init__(self, tps: float):
        self.tps = tps
        self.tokens = tps
        self.updated = time.time()
        self.lock = threading.Lock()

    def consume(self) -> bool:
        with self.lock:
            now = time.time()
            self.tokens = min(self.tps, self.tokens + (now - self.updated) * self.tps)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False

LIMITER = GlobalRateLimiter(TPS_LIMIT)

def validate_environment():
    """Ensure Ubuntu env has all secrets before L1 starts."""
    required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT", "SUPABASE_URL", "SUPABASE_KEY"]
    missing = [r for r in required if not os.getenv(r)]
    if missing:
        print(f"❌ L0 CRITICAL: Missing: {missing}")
        sys.exit(1)
    print(f"✅ L0: Secured. Owner: {OWNER_ID}")
