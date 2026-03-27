#!/usr/bin/env python3
"""Apply the CORE reflection audit schema to the live Supabase project."""
from __future__ import annotations

import json

from core_reflection_audit import apply_reflection_audit_schema, reflection_audit_ddl


def main() -> int:
    print(json.dumps({
        "ok": True,
        "ddl_preview": reflection_audit_ddl()[:400],
    }, indent=2))
    ok = apply_reflection_audit_schema()
    print(json.dumps({"ok": ok}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
