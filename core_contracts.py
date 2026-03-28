import uuid
import time
from dataclasses import dataclass, field

@dataclass
class Message:
    """The Universal Data Contract for CORE AGI."""
    cid: str
    msg_id: int
    text: str
    intent_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    username: str = "unknown"
    ts: float = field(default_factory=time.time)
