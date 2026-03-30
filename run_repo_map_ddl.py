#!/usr/bin/env python3
"""Bootstrap the CORE repo-map schema."""
from __future__ import annotations

import json

from core_repo_map import apply_repo_map_schema, repo_map_ddl


def main() -> int:
    print(json.dumps({
        "ok": True,
        "ddl_preview": repo_map_ddl()[:3],
    }, indent=2, default=str))
    result = apply_repo_map_schema()
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())