"""
core_orch_layer0.py — L0: Security & Policy
Foundation layer. Validates identity, enforces rate limits, manages permission tiers.
Runs before everything else. Cannot be bypassed.
Deployed on Oracle Ubuntu VM — no Railway dependency.
"""
try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False
import os
import time
import threading
from typing import Final

from orchestrator_message import OrchestratorMessage

# ── Constitution ──────────────────────────────────────────────────────────────
TPS_LIMIT: Final[float] = 2.0
OWNER_ID: Final[str] = os.getenv("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT", "838737537"))

# Optional additional trusted chat IDs (pipe-separated in env)
_TRUSTED_RAW = os.getenv("TRUSTED_CHAT_IDS", "")
TRUSTED_IDS: Final[set] = {
    s.strip() for s in _TRUSTED_RAW.split("|") if s.strip()
} if _TRUSTED_RAW else set()

REQUIRED_ENV = [
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "SUPABASE_URL", "SUPABASE_SERVICE_KEY",
    "GROQ_API_KEY", "GITHUB_PAT", "MCP_SECRET",
    "OPENROUTER_API", "GEMINI_KEYS",
]


# ── Rate limiter ───────────────────────────────────────────────────────────────
class _TokenBucket:
    """Thread-safe token bucket rate limiter."""
    def __init__(self, tps: float):
        self.tps = tps
        self.tokens = tps
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def consume(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.tps, self.tokens + (now - self.updated) * self.tps)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False

    def wait_time(self) -> float:
        with self._lock:
            if self.tokens >= 1:
                return 0.0
            return (1.0 - self.tokens) / self.tps


LIMITER = _TokenBucket(TPS_LIMIT)
_env_validated: bool = False


# ── Helpers ───────────────────────────────────────────────────────────────────
def validate_environment() -> bool:
    global _env_validated
    if _env_validated:
        return True
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        print(f"[L0] CRITICAL — missing env vars: {missing}")
        return False
    _env_validated = True
    print(f"[L0] Environment OK. Owner={OWNER_ID}")
    return True


def determine_permission_tier(chat_id: int, source: str) -> str:
    """
    owner   → all actions allowed
    trusted → read + non-destructive write
    anonymous → read-only, no tool execution
    """
    sid = str(chat_id)
    if sid == OWNER_ID:
        return "owner"
    if source in ("mcp", "system"):
        # MCP and system events always originate from owner context
        return "owner"
    if sid in TRUSTED_IDS:
        return "trusted"
    return "anonymous"


# ── Gate ──────────────────────────────────────────────────────────────────────
def gate_check(msg: OrchestratorMessage) -> bool:
    """
    L0 security gate. Returns True if message should proceed.
    Mutates msg.tier in place.
    """
    msg.track_layer("L0-START")

    # 1. Environment check (cached after first pass)
    if not validate_environment():
        msg.add_error("L0", Exception("Missing required environment variables"), "ENV_MISSING")
        return False

    # 2. Assign permission tier
    msg.tier = determine_permission_tier(msg.chat_id, msg.source)

    # 3. Rate limit
    if not LIMITER.consume():
        wait = LIMITER.wait_time()
        msg.add_error("L0", Exception(f"Rate limit — wait {wait:.1f}s"), "RATE_LIMIT")
        print(f"[L0] Rate limit hit. Wait {wait:.1f}s")
        return False

    # 4. Permission × route enforcement
    if msg.route == "command" and msg.tier == "anonymous":
        msg.add_error("L0", Exception("Anonymous users cannot execute commands"), "PERM_DENIED")
        print(f"[L0] DENIED anonymous command from chat_id={msg.chat_id}")
        return False

    # Trusted tier: read-only commands only (no destructive tools)
    if msg.tier == "trusted" and msg.route == "command":
        msg.context["trusted_restrictions"] = True

    print(f"[L0] PASS  tier={msg.tier}  source={msg.source}  route={msg.route}")
    msg.track_layer("L0-PASS")
    return True



