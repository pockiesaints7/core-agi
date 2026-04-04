#!/usr/bin/env python3
"""
CORE orchestrator progress notifier smoke.

Validates the Telegram progress formatter used by the freeform agentic loop.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core_orch_agent


def main() -> int:
    sent: list[str] = []

    def _capture(message, cid=None):
        sent.append(str(message))
        return True

    with patch("core_github.notify", side_effect=_capture):
        asyncio.run(
            core_orch_agent._send_progress(
                SimpleNamespace(chat_id="owner"),
                step=9,
                elapsed=41.2,
                progress="Checking repo map status to see if it is running.",
            )
        )

    if not sent:
        print("orch_agent_progress_smoke: no progress message captured")
        return 1

    msg = sent[-1]
    checks = [
        ("header", "<b>CORE working</b>" in msg),
        ("step", "step 9" in msg),
        ("elapsed", "elapsed 41s" in msg),
        ("progress", "Checking repo map status to see if it is running." in msg),
        ("clean_encoding", all(token not in msg for token in ("Ã", "â", "ð"))),
        ("throttle_default", core_orch_agent.AGENT_PROGRESS_MIN_SEC >= 30),
    ]
    failed = [name for name, ok in checks if not ok]
    total = len(checks)
    passed = total - len(failed)
    if failed:
        print(f"orch_agent_progress_smoke: {passed}/{total} checks passed")
        for name in failed:
            print(f"- failed: {name}")
        print(msg)
        return 1

    print(f"orch_agent_progress_smoke: {passed}/{total} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
