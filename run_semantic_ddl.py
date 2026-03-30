#!/usr/bin/env python3
"""Bootstrap the CORE semantic-memory schema."""
from __future__ import annotations

import json

from core_embeddings import apply_semantic_schema, semantic_schema_ddl


def main() -> int:
    print(json.dumps({
        "ok": True,
        "ddl_preview": semantic_schema_ddl()[:3],
        "ddl_count": len(semantic_schema_ddl()),
    }, indent=2, default=str))
    result = apply_semantic_schema()
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())