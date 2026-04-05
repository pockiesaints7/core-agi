"""core_repo_map.py -- CORE-native repository semantic map.

This module scans the CORE repository, writes a semantic component graph to
Supabase, and exposes read packets for the orchestrator. It is intentionally
CORE-owned: the trading-bot scripts inspired the design, but they are not
runtime dependencies here.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from core_config import SUPABASE_PAT, SUPABASE_REF, SUPABASE_URL, _env_int, _sbh_count_svc, sb_get, sb_patch, sb_post, sb_upsert

try:
    from core_semantic import embed_on_insert, search_many
except Exception:  # pragma: no cover - module still imports while semantic layer is patched
    embed_on_insert = None
    search_many = None

REPO_ROOT = Path(os.getenv("CORE_REPO_ROOT", Path(__file__).resolve().parent)).resolve()
REPO_NAME = os.getenv("CORE_REPO_MAP_NAME", "core-agi")
REPO_MAP_ENABLED = os.getenv("CORE_REPO_MAP_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
REPO_MAP_INTERVAL_S = max(300, _env_int("CORE_REPO_MAP_INTERVAL_S", 1800))
REPO_MAP_BATCH_LIMIT = max(1, _env_int("CORE_REPO_MAP_BATCH_LIMIT", 400))
REPO_MAP_DEEP_RECONCILE_S = max(REPO_MAP_INTERVAL_S, _env_int("CORE_REPO_MAP_DEEP_RECONCILE_S", REPO_MAP_INTERVAL_S * 4))
REPO_MAP_PROJECT_TO_KB = os.getenv("CORE_REPO_MAP_PROJECT_TO_KB", "false").strip().lower() in {"1", "true", "yes", "on"}
_MANAGED_TABLES = ("repo_components", "repo_component_chunks", "repo_component_edges", "repo_scan_runs")
_EXCLUDED_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".tox", ".venv", "venv",
    "node_modules", "dist", "build", ".idea", ".vscode",
}
_EXCLUDED_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".tgz", ".bz2", ".7z", ".exe", ".dll", ".so", ".pyd",
}
_INCLUDE_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".sql", ".sh",
    ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".csv",
}
_lock = threading.Lock()
_state = {
    "running": False,
    "last_run_at": "",
    "last_error": "",
    "last_summary": {},
    "last_run_id": None,
    "last_deep_reconcile_at": "",
    "root_path": str(REPO_ROOT),
    "bootstrapped": False,
}


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _safe_text(value: Any, limit: int = 800) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        return str(value)


def _rel_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def _is_text_file(path: Path) -> bool:
    name = path.name.lower()
    if name in {".env", ".gitignore", ".dockerignore"}:
        return True
    if path.suffix.lower() in _EXCLUDED_SUFFIXES:
        return False
    if path.suffix.lower() in _INCLUDE_SUFFIXES:
        return True
    return name in {"dockerfile", "makefile", "procfile", "readme", "license"} or name.endswith(".md")


def _is_ignored(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    return any(part in _EXCLUDED_DIRS for part in parts)


def _language_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix == ".md":
        return "markdown"
    if suffix == ".json":
        return "json"
    if suffix == ".toml":
        return "toml"
    if suffix in {".yml", ".yaml"}:
        return "yaml"
    if suffix in {".js", ".jsx"}:
        return "javascript"
    if suffix in {".ts", ".tsx"}:
        return "typescript"
    if suffix == ".sh":
        return "shell"
    if suffix == ".sql":
        return "sql"
    if suffix == ".html":
        return "html"
    if suffix == ".css":
        return "css"
    return "text"


def _file_role(path: Path) -> str:
    rel = _rel_path(path)
    name = path.name.lower()
    if rel in {"core_main.py", "core_orch_main.py"}:
        return "entrypoint"
    if rel.startswith("core_orch_layer") and path.suffix == ".py":
        return "orchestrator_layer"
    if "orchestrator_message" in name:
        return "message_contract"
    if "core_orch_agent" in name:
        return "agentic_loop"
    if name.startswith("core_tools"):
        return "tool_family"
    if name.startswith("core_") and path.suffix == ".py":
        return "core_module"
    if path.suffix.lower() in {".md", ".txt"}:
        return "doc"
    if path.suffix.lower() in {".json", ".toml", ".yaml", ".yml"}:
        return "config"
    if path.suffix.lower() == ".sh":
        return "script"
    return "asset"


def _component_type(path: Path) -> str:
    role = _file_role(path)
    if role == "entrypoint":
        return "entrypoint"
    if role in {"orchestrator_layer", "message_contract", "agentic_loop", "tool_family", "core_module"}:
        return "module"
    if role == "doc":
        return "doc"
    if role == "config":
        return "config"
    if role == "script":
        return "script"
    return "file"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        try:
            return path.read_text(errors="ignore")
        except Exception:
            return ""


def _file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _parse_python(text: str) -> dict[str, Any]:
    result = {
        "functions": [],
        "classes": [],
        "imports": [],
        "from_imports": [],
        "docstring": "",
    }
    try:
        tree = ast.parse(text)
    except Exception:
        return result

    result["docstring"] = ast.get_docstring(tree) or ""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            result["functions"].append(node.name)
        elif isinstance(node, ast.AsyncFunctionDef):
            result["functions"].append(node.name)
        elif isinstance(node, ast.ClassDef):
            result["classes"].append(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                result["imports"].append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                result["from_imports"].append({"module": module, "name": alias.name, "level": node.level})
    return result


def _parse_markdown(text: str) -> dict[str, Any]:
    headings = []
    links = []
    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if m:
            headings.append(m.group(2).strip()[:140])
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
        target = target.strip()
        if target:
            links.append(target)
    return {"headings": headings, "links": links}


def _parse_structured(text: str, suffix: str) -> dict[str, Any]:
    suffix = suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return {"keys": list(data.keys())[:40], "shape": "dict"}
            if isinstance(data, list):
                return {"keys": [], "shape": "list", "count": len(data)}
        except Exception:
            return {"keys": []}
    if suffix == ".toml":
        try:
            import tomllib
            data = tomllib.loads(text)
            if isinstance(data, dict):
                return {"keys": list(data.keys())[:40], "shape": "dict"}
        except Exception:
            return {"keys": []}
    if suffix in {".yml", ".yaml"}:
        keys = []
        for line in text.splitlines():
            if re.match(r"^\s*[A-Za-z0-9_.-]+\s*:\s*", line):
                key = line.split(":", 1)[0].strip()
                if key and key not in keys:
                    keys.append(key)
        return {"keys": keys[:40], "shape": "yaml"}
    return {"keys": []}


def _extract_path_refs(text: str) -> list[str]:
    refs = []
    patterns = [
        r"[\w./-]+\.(?:py|md|json|toml|ya?ml|txt|sh|sql|js|jsx|ts|tsx|html|css)",
        r"(?:/[\w.-]+)+",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text or ""):
            candidate = str(match).strip(" ,;:()[]{}<>\"'")
            if not candidate:
                continue
            if candidate not in refs:
                refs.append(candidate)
    return refs[:40]


def _resolve_module_target(module_name: str) -> list[str]:
    targets = []
    if not module_name:
        return targets
    module_name = module_name.lstrip(".").strip()
    if not module_name:
        return targets
    candidate = REPO_ROOT.joinpath(*module_name.split("."))
    for suffix in (".py", ".md", ".json", ".toml", ".yaml", ".yml"):
        path = candidate.with_suffix(suffix)
        if path.exists():
            targets.append(_rel_path(path))
            break
    init_path = candidate / "__init__.py"
    if init_path.exists():
        targets.append(_rel_path(init_path))
    return targets


def _python_metadata(path: Path, text: str) -> dict[str, Any]:
    meta = _parse_python(text)
    imports = list(meta.get("imports") or [])
    from_imports = list(meta.get("from_imports") or [])
    refs = []
    for module in imports:
        refs.extend(_resolve_module_target(str(module)))
    for entry in from_imports:
        module = str(entry.get("module") or "").strip()
        refs.extend(_resolve_module_target(module))
    for ref in _extract_path_refs(text):
        if ref not in refs:
            refs.append(ref)
    return {
        "language": "python",
        "symbols": {
            "functions": meta.get("functions", []),
            "classes": meta.get("classes", []),
        },
        "imports": imports,
        "from_imports": from_imports,
        "links": refs[:40],
        "docstring": meta.get("docstring", ""),
    }


def _markdown_metadata(path: Path, text: str) -> dict[str, Any]:
    meta = _parse_markdown(text)
    return {
        "language": "markdown",
        "symbols": {"headings": meta.get("headings", [])},
        "imports": [],
        "from_imports": [],
        "links": meta.get("links", []),
        "docstring": "",
    }


def _structured_metadata(path: Path, text: str) -> dict[str, Any]:
    parsed = _parse_structured(text, path.suffix)
    return {
        "language": _language_for_path(path),
        "symbols": parsed,
        "imports": [],
        "from_imports": [],
        "links": _extract_path_refs(text),
        "docstring": "",
    }


def _file_metadata(path: Path, text: str) -> dict[str, Any]:
    lang = _language_for_path(path)
    if lang == "python":
        return _python_metadata(path, text)
    if lang == "markdown":
        return _markdown_metadata(path, text)
    if lang in {"json", "toml", "yaml"}:
        return _structured_metadata(path, text)
    return {
        "language": lang,
        "symbols": {},
        "imports": [],
        "from_imports": [],
        "links": _extract_path_refs(text),
        "docstring": "",
    }


def _summary_for_component(path: Path, text: str, meta: dict[str, Any]) -> str:
    parts = [
        f"path={_rel_path(path)}",
        f"role={_file_role(path)}",
        f"language={meta.get('language')}",
    ]
    doc = _safe_text(meta.get("docstring"), 220)
    if doc:
        parts.append(f"doc={doc}")
    symbols = meta.get("symbols") or {}
    if isinstance(symbols, dict):
        funcs = symbols.get("functions") or []
        classes = symbols.get("classes") or []
        headings = symbols.get("headings") or []
        keys = symbols.get("keys") or []
        if funcs:
            parts.append(f"functions={', '.join(str(f) for f in funcs[:8])}")
        if classes:
            parts.append(f"classes={', '.join(str(c) for c in classes[:8])}")
        if headings:
            parts.append(f"headings={', '.join(str(h) for h in headings[:6])}")
        if keys:
            parts.append(f"keys={', '.join(str(k) for k in keys[:10])}")
    imports = meta.get("imports") or []
    if imports:
        parts.append(f"imports={', '.join(str(i) for i in imports[:8])}")
    links = meta.get("links") or []
    if links:
        parts.append(f"links={', '.join(str(l) for l in links[:8])}")
    for line in text.splitlines():
        if line.strip():
            parts.append(f"preview={line.strip()[:200]}")
            break
    return " | ".join(parts)[:4000]


def _chunk_text(text: str, max_lines: int = 90, max_chars: int = 2500) -> list[dict[str, Any]]:
    lines = text.splitlines()
    if not lines:
        return []
    chunks = []
    start = 0
    chunk_index = 0
    total = len(lines)
    while start < total:
        end = min(total, start + max_lines)
        block = "\n".join(lines[start:end]).strip()
        if len(block) > max_chars:
            block = block[:max_chars]
        if block:
            chunks.append({
                "chunk_index": chunk_index,
                "start_line": start + 1,
                "end_line": end,
                "content": block,
            })
            chunk_index += 1
        start = end
    return chunks


def _scan_files(root: Path | None = None) -> list[Path]:
    root = (root or REPO_ROOT).resolve()
    files: list[Path] = []
    for current_root, dirs, filenames in os.walk(root):
        current = Path(current_root)
        dirs[:] = [d for d in dirs if d not in _EXCLUDED_DIRS and not d.startswith(".git")]
        for name in filenames:
            path = current / name
            if _is_ignored(path):
                continue
            if not _is_text_file(path):
                continue
            files.append(path)
    files.sort(key=lambda p: _rel_path(p))
    return files





def _is_transient_supabase_error(text: str) -> bool:
    lowered = (text or '').lower()
    return any(token in lowered for token in (
        'recovery mode',
        'not accepting connections',
        'hot standby mode is disabled',
        'econnreset',
        'client network socket disconnected',
        'could not connect',
        'timed out',
    ))


def _mgmt_query(sql: str) -> dict:
    if not SUPABASE_PAT or not SUPABASE_REF:
        return {"ok": False, "error": "SUPABASE_PAT or SUPABASE_REF missing"}
    last_error = None
    for attempt in range(1, 13):
        try:
            resp = httpx.post(
                f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query",
                headers={"Authorization": f"Bearer {SUPABASE_PAT}", "Content-Type": "application/json"},
                json={"query": sql},
                timeout=30,
            )
            if resp.is_success:
                try:
                    return {"ok": True, "rows": resp.json()}
                except Exception:
                    return {"ok": True, "rows": []}
            last_error = resp.text[:500]
            if _is_transient_supabase_error(last_error) and attempt < 12:
                time.sleep(min(30, 2 ** attempt))
                continue
            return {"ok": False, "status_code": resp.status_code, "error": last_error}
        except Exception as exc:
            last_error = str(exc)
            if _is_transient_supabase_error(last_error) and attempt < 12:
                time.sleep(min(30, 2 ** attempt))
                continue
            return {"ok": False, "error": last_error}
    return {"ok": False, "error": last_error or 'unknown error'}


def _count_table(table: str, where: str = "") -> int:
    """Lightweight table counter used by repo-map status and evidence summaries."""
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?select=id&limit=1"
        if where:
            url += f"&{where}"
        resp = httpx.get(url, headers=_sbh_count_svc(), timeout=10)
        if not resp.is_success:
            return -1
        cr = resp.headers.get("content-range", "*/0")
        return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return -1


def repo_map_ddl() -> list[str]:
    return [
        "CREATE EXTENSION IF NOT EXISTS vector;",
        """
        CREATE TABLE IF NOT EXISTS repo_components (
            id BIGSERIAL PRIMARY KEY,
            repo TEXT NOT NULL DEFAULT 'core-agi',
            path TEXT NOT NULL UNIQUE,
            file_name TEXT NOT NULL DEFAULT '',
            file_ext TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT '',
            item_type TEXT NOT NULL DEFAULT 'file',
            runtime_role TEXT NOT NULL DEFAULT 'module',
            summary TEXT NOT NULL DEFAULT '',
            purpose_summary TEXT NOT NULL DEFAULT '',
            symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
            imports JSONB NOT NULL DEFAULT '[]'::jsonb,
            links JSONB NOT NULL DEFAULT '[]'::jsonb,
            file_hash TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            line_count INTEGER NOT NULL DEFAULT 0,
            char_count INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            edge_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            active BOOLEAN NOT NULL DEFAULT TRUE,
            embedding VECTOR(1024),
            last_scanned_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS repo_components_path_idx ON repo_components(path);",
        "CREATE INDEX IF NOT EXISTS repo_components_role_idx ON repo_components(runtime_role);",
        """
        CREATE TABLE IF NOT EXISTS repo_component_chunks (
            id BIGSERIAL PRIMARY KEY,
            repo TEXT NOT NULL DEFAULT 'core-agi',
            component_path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL DEFAULT 0,
            chunk_type TEXT NOT NULL DEFAULT 'text',
            start_line INTEGER NOT NULL DEFAULT 1,
            end_line INTEGER NOT NULL DEFAULT 1,
            summary TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            chunk_hash TEXT NOT NULL DEFAULT '',
            token_estimate INTEGER NOT NULL DEFAULT 0,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            embedding VECTOR(1024),
            last_scanned_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS repo_component_chunks_idx ON repo_component_chunks(component_path, chunk_index);",
        "CREATE INDEX IF NOT EXISTS repo_component_chunks_path_idx ON repo_component_chunks(component_path);",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS repo TEXT NOT NULL DEFAULT 'core-agi';",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS component_path TEXT NOT NULL DEFAULT '';",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS chunk_index INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS chunk_type TEXT NOT NULL DEFAULT 'text';",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS start_line INTEGER NOT NULL DEFAULT 1;",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS end_line INTEGER NOT NULL DEFAULT 1;",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT '';",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS content TEXT NOT NULL DEFAULT '';",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS chunk_hash TEXT NOT NULL DEFAULT '';",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS token_estimate INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS embedding VECTOR(1024);",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS last_scanned_at TIMESTAMPTZ;",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();",
        "ALTER TABLE IF EXISTS repo_component_chunks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();",
        """
        CREATE TABLE IF NOT EXISTS repo_component_edges (
            id BIGSERIAL PRIMARY KEY,
            repo TEXT NOT NULL DEFAULT 'core-agi',
            source_path TEXT NOT NULL,
            target_path TEXT NOT NULL,
            relation TEXT NOT NULL DEFAULT 'references',
            source_symbol TEXT NOT NULL DEFAULT '',
            target_symbol TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL DEFAULT '',
            weight FLOAT8 NOT NULL DEFAULT 0.5,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            embedding VECTOR(1024),
            last_scanned_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS repo_component_edges_idx ON repo_component_edges(source_path, target_path, relation, source_symbol, target_symbol);",
        "CREATE INDEX IF NOT EXISTS repo_component_edges_source_idx ON repo_component_edges(source_path);",
        "CREATE INDEX IF NOT EXISTS repo_component_edges_target_idx ON repo_component_edges(target_path);",
        "ALTER TABLE IF EXISTS repo_component_edges ADD COLUMN IF NOT EXISTS repo TEXT NOT NULL DEFAULT 'core-agi';",
        "ALTER TABLE IF EXISTS repo_component_edges ADD COLUMN IF NOT EXISTS source_path TEXT NOT NULL DEFAULT '';",
        "ALTER TABLE IF EXISTS repo_component_edges ADD COLUMN IF NOT EXISTS target_path TEXT NOT NULL DEFAULT '';",
        "ALTER TABLE IF EXISTS repo_component_edges ADD COLUMN IF NOT EXISTS relation TEXT NOT NULL DEFAULT 'references';",
        "ALTER TABLE IF EXISTS repo_component_edges ADD COLUMN IF NOT EXISTS source_symbol TEXT NOT NULL DEFAULT '';",
        "ALTER TABLE IF EXISTS repo_component_edges ADD COLUMN IF NOT EXISTS target_symbol TEXT NOT NULL DEFAULT '';",
        "ALTER TABLE IF EXISTS repo_component_edges ADD COLUMN IF NOT EXISTS evidence TEXT NOT NULL DEFAULT '';",
        "ALTER TABLE IF EXISTS repo_component_edges ADD COLUMN IF NOT EXISTS weight FLOAT8 NOT NULL DEFAULT 0.5;",
        "ALTER TABLE IF EXISTS repo_component_edges ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;",
        "ALTER TABLE IF EXISTS repo_component_edges ADD COLUMN IF NOT EXISTS embedding VECTOR(1024);",
        "ALTER TABLE IF EXISTS repo_component_edges ADD COLUMN IF NOT EXISTS last_scanned_at TIMESTAMPTZ;",
        "ALTER TABLE IF EXISTS repo_component_edges ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();",
        "ALTER TABLE IF EXISTS repo_component_edges ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();",
        """
        CREATE TABLE IF NOT EXISTS repo_scan_runs (
            id BIGSERIAL PRIMARY KEY,
            repo TEXT NOT NULL DEFAULT 'core-agi',
            root_path TEXT NOT NULL DEFAULT '',
            trigger TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'ok',
            files_total INTEGER NOT NULL DEFAULT 0,
            files_changed INTEGER NOT NULL DEFAULT 0,
            components_upserted INTEGER NOT NULL DEFAULT 0,
            chunks_upserted INTEGER NOT NULL DEFAULT 0,
            edges_upserted INTEGER NOT NULL DEFAULT 0,
            duration_sec FLOAT8 NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
        "CREATE INDEX IF NOT EXISTS repo_scan_runs_created_idx ON repo_scan_runs(created_at DESC);",
    ]


def apply_repo_map_schema() -> dict:
    if _state.get("bootstrapped"):
        return {"ok": True, "already_bootstrapped": True}
    results = []
    errors = []
    for stmt in repo_map_ddl():
        stmt = stmt.strip()
        if not stmt:
            continue
        res = _mgmt_query(stmt)
        results.append(res)
        if not res.get("ok"):
            errors.append(res.get("error") or stmt[:120])
    reload_res = _mgmt_query("NOTIFY pgrst, 'reload schema';")
    results.append(reload_res)
    if not reload_res.get("ok"):
        errors.append(reload_res.get("error") or "schema reload failed")
    _state["bootstrapped"] = not errors
    return {
        "ok": not errors,
        "bootstrapped": not errors,
        "results": results[:8],
        "errors": errors[:8],
    }


def _ensure_repo_map_schema() -> dict:
    """Best-effort bootstrap guard used before repo-map reads/writes."""
    if _state.get("bootstrapped"):
        return {"ok": True, "already_bootstrapped": True}
    try:
        return apply_repo_map_schema()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _existing_components_map() -> dict[str, dict]:
    rows = []
    try:
        limit = 500
        offset = 0
        while True:
            batch = sb_get(
                "repo_components",
                (
                    "select=id,path,file_hash,content_hash,active"
                    "&active=eq.true"
                    "&order=path.asc"
                    f"&limit={limit}&offset={offset}"
                ),
                svc=True,
            ) or []
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
    except Exception:
        rows = []
    return {str(r.get("path") or ""): r for r in rows if r.get("path")}


def _existing_children_map(table: str, field: str, value: str) -> list[dict]:
    rows: list[dict] = []
    try:
        if table == "repo_component_chunks":
            select = "select=id,component_path,chunk_index,active,chunk_hash"
        elif table == "repo_component_edges":
            select = "select=id,source_path,target_path,relation,source_symbol,target_symbol,active"
        else:
            select = "select=id"
        limit = 500
        offset = 0
        while True:
            batch = sb_get(
                table,
                f"{select}&{field}=eq.{value}&active=eq.true&order=id.asc&limit={limit}&offset={offset}",
                svc=True,
            ) or []
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
    except Exception:
        return []
    return rows


def _deep_reconcile_due(trigger: str) -> bool:
    if trigger != "loop":
        return True
    with _lock:
        last = str(_state.get("last_deep_reconcile_at") or "").strip()
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        return True
    return (datetime.utcnow() - last_dt).total_seconds() >= REPO_MAP_DEEP_RECONCILE_S


def _upsert_component(row: dict, text: str) -> dict:
    ok = sb_upsert("repo_components", row, on_conflict="path")
    if not ok:
        return {"ok": False, "error": f"failed_upsert:{row.get('path')}"}
    comp_rows = sb_get("repo_components", f"select=id,path,file_hash,content_hash&path=eq.{row['path']}&limit=1", svc=True) or []
    comp = comp_rows[0] if comp_rows else {}
    comp_id = comp.get("id")
    if comp_id and text and embed_on_insert:
        try:
            embed_on_insert("repo_components", comp_id, text)
        except Exception:
            pass
    return {"ok": True, "id": comp_id, "row": comp}


def _upsert_chunk(row: dict, text: str) -> dict:
    ok = sb_upsert("repo_component_chunks", row, on_conflict="component_path,chunk_index")
    if not ok:
        return {"ok": False, "error": f"failed_upsert:{row.get('component_path')}#{row.get('chunk_index')}"}
    chunk_id = row.get("id")
    if not chunk_id:
        try:
            matches = sb_get(
                "repo_component_chunks",
                f"select=id&component_path=eq.{row.get('component_path')}&chunk_index=eq.{row.get('chunk_index')}&limit=1",
                svc=True,
            ) or []
            chunk_id = matches[0].get("id") if matches else None
        except Exception:
            chunk_id = None
    if chunk_id and text and embed_on_insert:
        try:
            embed_on_insert("repo_component_chunks", chunk_id, text)
        except Exception:
            pass
    return {"ok": True, "id": chunk_id}


def _upsert_edge(row: dict, text: str) -> dict:
    ok = sb_upsert("repo_component_edges", row, on_conflict="source_path,target_path,relation,source_symbol,target_symbol")
    if not ok:
        return {"ok": False, "error": f"failed_upsert:{row.get('source_path')}->{row.get('target_path')}"}
    edge_id = row.get("id")
    if not edge_id:
        try:
            matches = sb_get(
                "repo_component_edges",
                (
                    "select=id"
                    f"&source_path=eq.{row.get('source_path')}"
                    f"&target_path=eq.{row.get('target_path')}"
                    f"&relation=eq.{row.get('relation')}"
                    f"&source_symbol=eq.{row.get('source_symbol', '')}"
                    f"&target_symbol=eq.{row.get('target_symbol', '')}"
                    "&limit=1"
                ),
                svc=True,
            ) or []
            edge_id = matches[0].get("id") if matches else None
        except Exception:
            edge_id = None
    if edge_id and text and embed_on_insert:
        try:
            embed_on_insert("repo_component_edges", edge_id, text)
        except Exception:
            pass
    return {"ok": True, "id": edge_id}


def _component_row(path: Path, text: str, meta: dict[str, Any], root: Path | None = None) -> dict:
    rel = _rel_path(path)
    file_ext = path.suffix.lower().lstrip(".")
    file_name = path.name
    symbols = meta.get("symbols") or {}
    imports = meta.get("imports") or []
    links = meta.get("links") or []
    chunks = _chunk_text(text)
    summary = _summary_for_component(path, text, meta)
    content_hash = _file_hash(summary + "\n" + text[:12000])
    return {
        "repo": REPO_NAME,
        "path": rel,
        "file_name": file_name,
        "file_ext": file_ext,
        "language": meta.get("language") or _language_for_path(path),
        "item_type": _component_type(path),
        "runtime_role": _file_role(path),
        "summary": summary,
        "purpose_summary": summary[:1000],
        "symbols": _jsonable(symbols),
        "imports": _jsonable(imports),
        "links": _jsonable(links),
        "file_hash": _file_hash(text),
        "content_hash": content_hash,
        "line_count": len(text.splitlines()) or 0,
        "char_count": len(text),
        "chunk_count": len(chunks),
        "edge_count": 0,
        "status": "active",
        "active": True,
        "last_scanned_at": _utcnow(),
        "updated_at": _utcnow(),
    }


def _build_edges(path: Path, text: str, meta: dict[str, Any]) -> list[dict]:
    rel = _rel_path(path)
    edges: list[dict] = []
    imports = meta.get("imports") or []
    for module in imports:
        for target in _resolve_module_target(str(module)):
            edges.append({
                "repo": REPO_NAME,
                "source_path": rel,
                "target_path": target,
                "relation": "imports",
                "source_symbol": "",
                "target_symbol": "",
                "evidence": str(module),
                "weight": 0.92,
                "active": True,
                "last_scanned_at": _utcnow(),
                "updated_at": _utcnow(),
            })
    for entry in meta.get("from_imports") or []:
        module = str(entry.get("module") or "").strip()
        name = str(entry.get("name") or "").strip()
        for target in _resolve_module_target(module):
            edges.append({
                "repo": REPO_NAME,
                "source_path": rel,
                "target_path": target,
                "relation": "from_imports",
                "source_symbol": name,
                "target_symbol": "",
                "evidence": f"{module}:{name}",
                "weight": 0.9,
                "active": True,
                "last_scanned_at": _utcnow(),
                "updated_at": _utcnow(),
            })
    for link in meta.get("links") or []:
        link = str(link).strip()
        if not link or link.startswith("http"):
            continue
        candidate = (REPO_ROOT / link).resolve() if not os.path.isabs(link) else Path(link).resolve()
        try:
            if candidate.exists() and (candidate == REPO_ROOT or REPO_ROOT in candidate.parents):
                edges.append({
                    "repo": REPO_NAME,
                    "source_path": rel,
                    "target_path": _rel_path(candidate),
                    "relation": "links",
                    "source_symbol": "",
                    "target_symbol": "",
                    "evidence": link,
                    "weight": 0.8,
                    "active": True,
                    "last_scanned_at": _utcnow(),
                    "updated_at": _utcnow(),
                })
        except Exception:
            continue
    for ref in _extract_path_refs(text):
        if ref == rel:
            continue
        candidate = (REPO_ROOT / ref).resolve() if not os.path.isabs(ref) else Path(ref).resolve()
        try:
            if candidate.exists() and (candidate == REPO_ROOT or REPO_ROOT in candidate.parents):
                edges.append({
                    "repo": REPO_NAME,
                    "source_path": rel,
                    "target_path": _rel_path(candidate),
                    "relation": "mentions",
                    "source_symbol": "",
                    "target_symbol": "",
                    "evidence": ref,
                    "weight": 0.55,
                    "active": True,
                    "last_scanned_at": _utcnow(),
                    "updated_at": _utcnow(),
                })
        except Exception:
            pass
    deduped = []
    seen = set()
    for edge in edges:
        key = (
            edge["source_path"],
            edge["target_path"],
            edge["relation"],
            edge.get("source_symbol", ""),
            edge.get("target_symbol", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
    return deduped


def sync_repo_map(trigger: str = "manual", root_path: str = "", project_to_kb: str = "false") -> dict:
    root = Path(root_path).resolve() if root_path else REPO_ROOT
    if not root.exists():
        return {"ok": False, "error": f"root_path not found: {root}"}
    bootstrap = apply_repo_map_schema()
    if not bootstrap.get("ok"):
        return {"ok": False, "error": "schema bootstrap failed", "bootstrap": bootstrap}

    started = time.monotonic()
    with _lock:
        _state["running"] = True
        _state["last_error"] = ""
    try:
        files = _scan_files(root)
        all_local_paths = {_rel_path(path) for path in files}
        existing = _existing_components_map()
        components_upserted = 0
        chunks_upserted = 0
        edges_upserted = 0
        changed_files = 0
        errors: list[str] = []
        deep_reconcile = _deep_reconcile_due(trigger)

        for idx, path in enumerate(files):
            if idx >= REPO_MAP_BATCH_LIMIT:
                break
            rel = _rel_path(path)
            text = _read_text(path)
            if not text:
                continue
            meta = _file_metadata(path, text)
            row = _component_row(path, text, meta, root=root)
            prev = existing.get(rel) or {}
            changed = (
                row["file_hash"] != (prev.get("file_hash") or "")
                or row["content_hash"] != (prev.get("content_hash") or "")
                or not prev
                or prev.get("active") is False
            )
            if changed:
                changed_files += 1
            comp_res = _upsert_component(row, row["summary"])
            if not comp_res.get("ok"):
                errors.append(comp_res.get("error") or rel)
                continue
            components_upserted += 1 if changed or not prev else 0
            comp_id = comp_res.get("id")
            if changed or deep_reconcile:
                chunk_rows = _chunk_text(text)
                existing_chunks = _existing_children_map("repo_component_chunks", "component_path", rel)
                current_chunk_indexes = set()
                for chunk in chunk_rows:
                    current_chunk_indexes.add(chunk["chunk_index"])
                    chunk_text = chunk["content"]
                    chunk_row = {
                        "repo": REPO_NAME,
                        "component_path": rel,
                        "chunk_index": chunk["chunk_index"],
                        "chunk_type": _component_type(path),
                        "start_line": chunk["start_line"],
                        "end_line": chunk["end_line"],
                        "summary": chunk_text.splitlines()[0][:220] if chunk_text.splitlines() else rel,
                        "content": chunk_text,
                        "chunk_hash": _file_hash(chunk_text),
                        "token_estimate": max(1, len(chunk_text) // 4),
                        "active": True,
                        "last_scanned_at": _utcnow(),
                        "updated_at": _utcnow(),
                    }
                    if changed or not existing_chunks:
                        chunk_res = _upsert_chunk(chunk_row, chunk_text)
                        if chunk_res.get("ok"):
                            chunks_upserted += 1
                        else:
                            errors.append(chunk_res.get("error") or f"{rel}#{chunk_row['chunk_index']}")

                for existing_chunk in existing_chunks:
                    try:
                        chunk_index = int(existing_chunk.get("chunk_index", -1))
                    except Exception:
                        chunk_index = -1
                    if chunk_index not in current_chunk_indexes and existing_chunk.get("active", True):
                        sb_patch(
                            "repo_component_chunks",
                            f"id=eq.{existing_chunk['id']}",
                            {"active": False, "updated_at": _utcnow(), "last_scanned_at": _utcnow()},
                        )

                edge_rows = _build_edges(path, text, meta)
                existing_edges = _existing_children_map("repo_component_edges", "source_path", rel)
                current_edge_keys = set()
                for edge in edge_rows:
                    current_edge_keys.add((
                        edge["source_path"],
                        edge["target_path"],
                        edge["relation"],
                        edge.get("source_symbol", ""),
                        edge.get("target_symbol", ""),
                    ))
                    edge_res = _upsert_edge(edge, f"{edge['source_path']}->{edge['target_path']}|{edge['relation']}|{edge.get('evidence','')}")
                    if edge_res.get("ok"):
                        edges_upserted += 1
                    else:
                        errors.append(edge_res.get("error") or f"{edge['source_path']}->{edge['target_path']}")

                for existing_edge in existing_edges:
                    key = (
                        existing_edge.get("source_path", ""),
                        existing_edge.get("target_path", ""),
                        existing_edge.get("relation", ""),
                        existing_edge.get("source_symbol", ""),
                        existing_edge.get("target_symbol", ""),
                    )
                    if key not in current_edge_keys and existing_edge.get("active", True):
                        sb_patch(
                            "repo_component_edges",
                            f"id=eq.{existing_edge['id']}",
                            {"active": False, "updated_at": _utcnow(), "last_scanned_at": _utcnow()},
                        )

            if REPO_MAP_PROJECT_TO_KB and comp_id and embed_on_insert:
                try:
                    embed_on_insert("repo_components", comp_id, row["summary"])
                except Exception:
                    pass

        removed = [p for p in existing.keys() if p not in all_local_paths]
        for rel in removed:
            row = existing.get(rel) or {}
            if row and row.get("active", True):
                sb_patch(
                    "repo_components",
                    f"id=eq.{row['id']}",
                    {"active": False, "status": "tombstone", "updated_at": _utcnow(), "last_scanned_at": _utcnow()},
                )

        duration = round(time.monotonic() - started, 3)
        summary = {
            "files_total": len(files),
            "files_changed": changed_files,
            "components_upserted": components_upserted,
            "chunks_upserted": chunks_upserted,
            "edges_upserted": edges_upserted,
            "removed": len(removed),
            "repo_root": str(root),
        }
        run_row = {
            "repo": REPO_NAME,
            "root_path": str(root),
            "trigger": trigger,
            "status": "ok" if not errors else "partial",
            "files_total": len(files),
            "files_changed": changed_files,
            "components_upserted": components_upserted,
            "chunks_upserted": chunks_upserted,
            "edges_upserted": edges_upserted,
            "duration_sec": duration,
            "summary": json.dumps(summary, default=str),
            "error": "; ".join(errors[:8]),
            "payload": _jsonable(summary),
        }
        sb_post("repo_scan_runs", run_row)
        with _lock:
            _state.update({
                "running": False,
                "last_run_at": _utcnow(),
                "last_deep_reconcile_at": _utcnow() if deep_reconcile else _state.get("last_deep_reconcile_at", ""),
                "last_error": "; ".join(errors[:8]),
                "last_summary": summary,
                "last_run_id": None,
                "root_path": str(root),
            })
        return {"ok": True, "summary": summary, "errors": errors[:10], "duration_sec": duration}
    except Exception as exc:
        with _lock:
            _state.update({"running": False, "last_error": str(exc)[:500], "last_run_at": _utcnow()})
        return {"ok": False, "error": str(exc), "root_path": str(root)}


def run_repo_map_cycle(trigger: str = "manual", root_path: str = "") -> dict:
    return sync_repo_map(trigger=trigger, root_path=root_path)


def repo_map_status(scope: str = "summary", limit: int = 10) -> dict:
    try:
        _ensure_repo_map_schema()
        counts = {
            "repo_components": _count_table("repo_components", "active=eq.true"),
            "repo_component_chunks": _count_table("repo_component_chunks", "active=eq.true"),
            "repo_component_edges": _count_table("repo_component_edges", "active=eq.true"),
            "repo_scan_runs": _count_table("repo_scan_runs"),
        }
        latest = sb_get(
            "repo_scan_runs",
            "select=id,trigger,status,root_path,files_total,files_changed,components_upserted,chunks_upserted,edges_upserted,duration_sec,summary,error,created_at&order=created_at.desc&limit=1",
            svc=True,
        ) or []
        latest_row = latest[0] if latest else {}
        return {
            "ok": True,
            "enabled": REPO_MAP_ENABLED,
            "running": _state.get("running", False),
            "root_path": _state.get("root_path", str(REPO_ROOT)),
            "last_run_at": _state.get("last_run_at", ""),
            "last_error": _state.get("last_error", ""),
            "last_summary": _state.get("last_summary", {}),
            "counts": counts,
            "latest_run": latest_row,
            "scope": scope,
            "limit": max(1, min(int(limit or 10), 50)),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "enabled": REPO_MAP_ENABLED, "running": False}


def render_repo_map_status_report(status: dict) -> str:
    lines = [
        f"Status: {'enabled' if status.get('enabled') else 'disabled'} | running={status.get('running', False)}",
        f"Root: {status.get('root_path', str(REPO_ROOT))}",
        "Counts: "
        + " | ".join(
            f"{k}={status.get('counts', {}).get(k, 0)}"
            for k in ("repo_components", "repo_component_chunks", "repo_component_edges", "repo_scan_runs")
        ),
        f"Last run: {status.get('last_run_at') or 'n/a'}",
    ]
    if status.get("last_summary"):
        lines.append(f"Last summary: {json.dumps(status.get('last_summary'), default=str)[:700]}")
    if status.get("last_error"):
        lines.append(f"Last error: {status.get('last_error')}")
    latest = status.get("latest_run") or {}
    if latest:
        lines.append(
            "Latest run: "
            + " | ".join(
                f"{k}={latest.get(k)}"
                for k in ("trigger", "status", "files_total", "files_changed", "components_upserted", "chunks_upserted", "edges_upserted")
                if latest.get(k) is not None
            )
        )
    return "\n".join(lines)


def _component_lookup(path: str) -> dict:
    rel = path.strip().replace("\\", "/")
    if not rel:
        return {}
    _ensure_repo_map_schema()
    candidates = [
        rel,
        rel.lstrip("./"),
        rel.replace(str(REPO_ROOT).replace("\\", "/") + "/", ""),
    ]
    for candidate in candidates:
        try:
            rows = sb_get(
                "repo_components",
                f"select=* &path=eq.{candidate}&limit=1".replace(" ", ""),
                svc=True,
            ) or []
            if rows:
                return rows[0]
        except Exception:
            continue
    return {}


def _normalize_component_path(path: str) -> str:
    raw = str(path or "").strip().strip('"').strip("'").replace("\\", "/")
    if not raw:
        return ""
    repo_root_norm = str(REPO_ROOT).replace("\\", "/").rstrip("/")
    if raw.startswith(repo_root_norm + "/"):
        return raw[len(repo_root_norm) + 1:]
    repo_marker = f"/{REPO_NAME}/"
    if repo_marker in raw:
        return raw.split(repo_marker, 1)[1]
    drive_marker = "/mnt/"
    if raw.startswith(drive_marker) and len(raw) > 6 and raw[6] == "/":
        raw = f"{raw[5].upper()}:/{raw[7:]}"
        if raw.replace("\\", "/").startswith(repo_root_norm + "/"):
            return raw[len(repo_root_norm) + 1:]
    return raw.lstrip("./")


def _local_component_packet(path: str, limit: int = 10) -> dict:
    normalized = _normalize_component_path(path)
    if not normalized:
        return {"ok": False, "error": "path required"}
    candidates: list[Path] = []
    raw_path = Path(normalized)
    if raw_path.is_absolute():
        candidates.append(raw_path)
    candidates.append(REPO_ROOT / normalized)
    candidates.append(REPO_ROOT / Path(normalized).name)
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if not resolved.exists() or not resolved.is_file():
                continue
            if resolved != REPO_ROOT and REPO_ROOT not in resolved.parents:
                continue
            if not _is_text_file(resolved):
                continue
            text = _read_text(resolved)
            if not text:
                continue
            meta = _file_metadata(resolved, text)
            component = _component_row(resolved, text, meta)
            chunks = []
            for chunk in _chunk_text(text)[:max(1, min(limit, 50))]:
                chunks.append({
                    "component_path": component["path"],
                    "chunk_index": chunk["chunk_index"],
                    "chunk_type": _component_type(resolved),
                    "start_line": chunk["start_line"],
                    "end_line": chunk["end_line"],
                    "summary": chunk["content"].splitlines()[0][:220] if chunk["content"].splitlines() else component["path"],
                    "content": chunk["content"],
                    "chunk_hash": _file_hash(chunk["content"]),
                    "active": True,
                })
            edges = _build_edges(resolved, text, meta)[: max(1, min(limit * 3, 150))]
            return {
                "ok": True,
                "query": path,
                "focus_path": component["path"],
                "components": [component],
                "chunks": chunks,
                "edges": edges,
                "summary": component.get("summary", ""),
                "source": "local_fallback",
            }
        except Exception:
            continue
    return {"ok": False, "error": f"component not found: {path}"}


def build_repo_component_packet(path: str = "", query: str = "", limit: int = 10) -> dict:
    try:
        lim = max(1, min(int(limit or 10), 50))
    except Exception:
        lim = 10
    _ensure_repo_map_schema()

    try:
        if path:
            component = _component_lookup(path)
            if not component:
                fallback = _local_component_packet(path, limit=lim)
                if fallback.get("ok"):
                    return fallback
                return {"ok": False, "error": f"component not found: {path}"}
            chunks = sb_get(
                "repo_component_chunks",
                f"select=id,component_path,chunk_index,chunk_type,start_line,end_line,summary,content,chunk_hash,active&component_path=eq.{component['path']}&order=chunk_index.asc&limit={lim}",
                svc=True,
            ) or []
            edges = sb_get(
                "repo_component_edges",
                f"select=id,source_path,target_path,relation,source_symbol,target_symbol,evidence,weight,active&or=(source_path.eq.{component['path']},target_path.eq.{component['path']})&order=id.asc&limit={lim}",
                svc=True,
            ) or []
            return {
                "ok": True,
                "query": query or path,
                "focus_path": component["path"],
                "components": [component],
                "chunks": chunks,
                "edges": edges,
                "summary": component.get("summary", ""),
            }
        if not query:
            return {"ok": False, "error": "path or query required"}
        if search_many is not None:
            rows = search_many(query=query, tables=["repo_components", "repo_component_chunks", "repo_component_edges"], limit=lim, domain="repo_map") or []
            components = [r for r in rows if r.get("semantic_table") == "repo_components"]
            chunks = [r for r in rows if r.get("semantic_table") == "repo_component_chunks"]
            edges = [r for r in rows if r.get("semantic_table") == "repo_component_edges"]
            return {
                "ok": True,
                "query": query,
                "components": components,
                "chunks": chunks,
                "edges": edges,
                "count": len(rows),
                "memory_by_table": {
                    "repo_components": len(components),
                    "repo_component_chunks": len(chunks),
                    "repo_component_edges": len(edges),
                },
            }
        rows = sb_get(
            "repo_components",
            f"select=id,path,file_name,file_ext,language,item_type,runtime_role,summary,purpose_summary,symbols,imports,links&or=(path.ilike.*{query}*,summary.ilike.*{query}*,purpose_summary.ilike.*{query}*)&order=updated_at.desc&limit={lim}",
            svc=True,
        ) or []
        return {"ok": True, "query": query, "components": rows, "chunks": [], "edges": [], "count": len(rows)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "components": [], "chunks": [], "edges": []}


def build_repo_graph_packet(path: str = "", query: str = "", depth: int = 2, limit: int = 10) -> dict:
    try:
        lim = max(1, min(int(limit or 10), 50))
    except Exception:
        lim = 10
    try:
        depth = max(1, min(int(depth or 2), 4))
    except Exception:
        depth = 2

    roots: list[dict] = []
    if path:
        comp = _component_lookup(path)
        if comp:
            roots.append(comp)
        else:
            fallback = _local_component_packet(path, limit=lim)
            if fallback.get("ok"):
                roots.extend(fallback.get("components") or [])
    if not roots and query:
        packet = build_repo_component_packet(query=query, limit=lim)
        if packet.get("ok"):
            roots.extend(packet.get("components") or [])
            roots.extend(packet.get("chunks") or [])

    if not roots:
        return {"ok": False, "error": "path or query required"}

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    frontier = []

    def _add_component_node(row: dict) -> str:
        rel = row.get("path") or row.get("component_path") or row.get("source_path") or row.get("target_path") or ""
        key = rel or f"{row.get('semantic_table','repo')}:{row.get('id','')}"
        if key not in nodes:
            nodes[key] = {
                "id": key,
                "path": rel,
                "type": row.get("runtime_role") or row.get("semantic_table") or "repo",
                "table": row.get("semantic_table") or "repo_components",
                "label": row.get("summary") or row.get("title") or rel,
                "raw": row,
            }
        return key

    for root in roots[:lim]:
        if root.get("path"):
            frontier.append(root["path"])
        _add_component_node(root)

    seen_paths = set(frontier)
    for _ in range(depth):
        next_frontier = []
        for current_path in list(frontier):
            rel_rows = sb_get(
                "repo_component_edges",
                f"select=source_path,target_path,relation,source_symbol,target_symbol,evidence,weight,active&active=eq.true&or=(source_path.eq.{current_path},target_path.eq.{current_path})&order=id.asc&limit=100",
                svc=True,
            ) or []
            for edge in rel_rows:
                src = edge.get("source_path", "")
                tgt = edge.get("target_path", "")
                edges.append(edge)
                for ref in (src, tgt):
                    if ref and ref not in seen_paths:
                        seen_paths.add(ref)
                        next_frontier.append(ref)
                        comp = _component_lookup(ref)
                        if comp:
                            _add_component_node(comp)
            if len(edges) >= lim * 20:
                break
        frontier = next_frontier[:lim]
        if not frontier:
            break

    return {
        "ok": True,
        "roots": roots[:lim],
        "nodes": list(nodes.values())[: lim * 3],
        "edges": edges[: lim * 25],
        "depth": depth,
        "count_nodes": len(nodes),
        "count_edges": len(edges),
    }


def repo_map_loop() -> None:
    while True:
        try:
            if not REPO_MAP_ENABLED:
                time.sleep(REPO_MAP_INTERVAL_S)
                continue
            result = sync_repo_map(trigger="loop")
            if not result.get("ok"):
                with _lock:
                    _state["last_error"] = result.get("error", "repo_map sync failed")
            time.sleep(REPO_MAP_INTERVAL_S)
        except Exception as exc:
            with _lock:
                _state["last_error"] = str(exc)[:500]
                _state["running"] = False
            time.sleep(min(60, REPO_MAP_INTERVAL_S))


def t_repo_map_sync(trigger: str = "manual", root_path: str = "") -> dict:
    return sync_repo_map(trigger=trigger, root_path=root_path)


def t_repo_map_status(scope: str = "summary", limit: str = "10") -> dict:
    try:
        lim = max(1, min(int(limit or 10), 50))
    except Exception:
        lim = 10
    return repo_map_status(scope=scope, limit=lim)


def t_repo_component_packet(path: str = "", query: str = "", limit: str = "10") -> dict:
    try:
        lim = max(1, min(int(limit or 10), 50))
    except Exception:
        lim = 10
    return build_repo_component_packet(path=path, query=query, limit=lim)


def t_repo_graph_packet(path: str = "", query: str = "", depth: str = "2", limit: str = "10") -> dict:
    try:
        lim = max(1, min(int(limit or 10), 50))
    except Exception:
        lim = 10
    try:
        dep = max(1, min(int(depth or 2), 4))
    except Exception:
        dep = 2
    return build_repo_graph_packet(path=path, query=query, depth=dep, limit=lim)
