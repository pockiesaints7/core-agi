"""core_tools_code_reader.py — canonical code reading packet for CORE.

This module provides a structured read model over repo files, functions, and
search hits so code_autonomy can reason from one packet instead of scattered
read helpers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from core_github import gh_read


def _safe_text(value: Any, limit: int = 600) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _safe_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _safe_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _read_file(path: str, repo: str = "", start_line: str = "", end_line: str = "") -> dict:
    raw = gh_read(path, repo or "")
    lines = raw.splitlines(keepends=True)
    total = len(lines)
    if start_line or end_line:
        s = max(0, int(start_line) - 1) if start_line else 0
        e = int(end_line) if end_line else total
        lines = lines[s:e]
        raw = "".join(lines)
    truncated = len(raw) > 8000
    result = {"ok": True, "content": raw[:8000], "total_line_count": total, "truncated": truncated}
    if truncated:
        result["truncation_warning"] = (
            "TRUNCATED at 8000 chars. Use line ranges for exact patch context."
        )
    return result


def _search_in_file(path: str, pattern: str, repo: str = "", regex: str = "false", case_sensitive: str = "false") -> dict:
    import re as _re

    content = gh_read(path, repo or "")
    lines = content.splitlines()
    matches = []
    use_regex = str(regex).lower() == "true"
    use_case = str(case_sensitive).lower() == "true"
    flags = 0 if use_case else _re.IGNORECASE
    for i, line in enumerate(lines, 1):
        if use_regex:
            if _re.search(pattern, line, flags):
                matches.append({"line": i, "content": line})
        else:
            hay = line if use_case else line.lower()
            ndl = pattern if use_case else pattern.lower()
            if ndl in hay:
                matches.append({"line": i, "content": line})
    return {
        "ok": True,
        "path": path,
        "pattern": pattern,
        "regex": use_regex,
        "case_sensitive": use_case,
        "total_lines": len(lines),
        "matches": matches,
        "count": len(matches),
    }


def _read_functions(path: str, fn_names: list[str]) -> dict:
    content = gh_read(path, "")
    lines = content.splitlines()
    results = []
    for fn_name in fn_names:
        start = None
        indent = None
        for i, line in enumerate(lines):
            if line.strip().startswith(f"def {fn_name}(") or line.strip() == f"def {fn_name}()":
                start = i
                indent = len(line) - len(line.lstrip())
                break
        if start is None:
            results.append({"fn_name": fn_name, "found": False})
            continue
        end = start + 1
        while end < len(lines):
            line = lines[end]
            if line.strip() == "":
                end += 1
                continue
            cur_indent = len(line) - len(line.lstrip())
            if cur_indent <= indent and line.strip().startswith("def "):
                break
            end += 1
        results.append({
            "fn_name": fn_name,
            "found": True,
            "start_line": start + 1,
            "end_line": end,
            "line_count": end - start,
            "source": "\n".join(lines[start:end]),
        })
    return {"ok": True, "path": path, "functions": results}


@dataclass
class CodeReadingPacket:
    query: str
    files: list[dict]
    functions: list[dict]
    search_hits: list[dict]

    def to_dict(self) -> dict:
        search_by_file = {}
        for hit in self.search_hits:
            search_by_file.setdefault(hit.get("path") or "", []).append(hit)
        return {
            "ok": True,
            "query": self.query,
            "files": self.files,
            "functions": self.functions,
            "search_hits": self.search_hits,
            "search_hits_by_file": search_by_file,
            "file_count": len(self.files),
            "function_count": len(self.functions),
            "search_hit_count": len(self.search_hits),
        }


def build_code_reading_packet(
    query: str = "",
    files: list[str] | None = None,
    functions: list[dict] | None = None,
    search_terms: list[dict] | None = None,
) -> dict:
    query = _safe_text(query, 500)
    if not query:
        return {"ok": False, "error": "query required"}

    file_packets = []
    fn_packets = []
    hit_packets = []

    for spec in files or []:
        if isinstance(spec, dict):
            path = spec.get("path") or ""
            repo = spec.get("repo") or ""
            start_line = spec.get("start_line") or ""
            end_line = spec.get("end_line") or ""
        else:
            path = str(spec)
            repo = ""
            start_line = ""
            end_line = ""
        if not path:
            continue
        packet = _read_file(path, repo=repo, start_line=start_line, end_line=end_line)
        packet.update({"path": path, "repo": repo})
        file_packets.append(packet)

    for spec in functions or []:
        if isinstance(spec, dict):
            path = spec.get("path") or ""
            fns = _safe_list(spec.get("functions") or spec.get("fn_names") or [])
        else:
            path = ""
            fns = []
        if path and fns:
            fn_packets.extend(_read_functions(path, [str(fn) for fn in fns if str(fn).strip()]).get("functions", []))

    for spec in search_terms or []:
        if isinstance(spec, dict):
            path = spec.get("path") or ""
            pattern = spec.get("pattern") or query
            repo = spec.get("repo") or ""
            regex = spec.get("regex") or "false"
            case_sensitive = spec.get("case_sensitive") or "false"
        else:
            path = ""
            pattern = query
            repo = ""
            regex = "false"
            case_sensitive = "false"
        if not path:
            continue
        hit_packets.append(
            _search_in_file(
                path=path,
                pattern=pattern,
                repo=repo,
                regex=regex,
                case_sensitive=case_sensitive,
            )
        )

    packet = CodeReadingPacket(
        query=query,
        files=file_packets,
        functions=fn_packets,
        search_hits=hit_packets,
    ).to_dict()
    packet["verification"] = {
        "verified": bool(file_packets or fn_packets or hit_packets),
        "warnings": [],
        "summary": f"files={len(file_packets)} | functions={len(fn_packets)} | searches={len(hit_packets)}",
    }
    return packet


def t_code_read_packet(
    query: str = "",
    files: str = "",
    functions: str = "",
    search_terms: str = "",
) -> dict:
    try:
        file_specs = json.loads(files) if files else []
        fn_specs = json.loads(functions) if functions else []
        search_specs = json.loads(search_terms) if search_terms else []
        return build_code_reading_packet(
            query=query,
            files=file_specs,
            functions=fn_specs,
            search_terms=search_specs,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}

