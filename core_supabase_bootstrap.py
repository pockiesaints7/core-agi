#!/usr/bin/env python3
"""Bootstrap the Supabase schema required by core-agi."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
ENV_CANDIDATES = [ROOT / '.env', ROOT.parent / 'core-trading-bot' / '.env', ROOT.parent / 'specter-alpha' / '.env']
FULL_SCHEMA_MANIFEST = ROOT.parent / 'supabase_sync' / 'migrations' / '20260329_235818_schema.json'

CORE_EXTRA_DDL = [
    """
    CREATE TABLE IF NOT EXISTS memory (
        key text PRIMARY KEY,
        category text NOT NULL DEFAULT 'general',
        value text NOT NULL DEFAULT '',
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX IF NOT EXISTS memory_category_idx ON memory(category);",
    """
    CREATE TABLE IF NOT EXISTS playbook (
        id bigserial PRIMARY KEY,
        topic text NOT NULL,
        method text NOT NULL DEFAULT '',
        why_best text,
        supersedes text,
        previous_method text,
        version integer NOT NULL DEFAULT 1,
        tags text[] DEFAULT '{}'::text[],
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    );
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS playbook_topic_uidx ON playbook(topic);",
    """
    CREATE TABLE IF NOT EXISTS output_reflections (
        id bigserial PRIMARY KEY,
        session_id bigint,
        source text,
        critique_score double precision,
        verdict text,
        gap text,
        gap_domain text,
        new_behavior text,
        evo_worthy boolean DEFAULT false,
        prompt_patch text,
        created_at timestamptz NOT NULL DEFAULT now()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS agentic_sessions (
        id bigserial PRIMARY KEY,
        session_id text NOT NULL,
        state jsonb DEFAULT '{}'::jsonb,
        step_index integer DEFAULT 0,
        current_step text,
        completed_steps jsonb DEFAULT '[]'::jsonb,
        action_log jsonb DEFAULT '[]'::jsonb,
        last_updated timestamptz NOT NULL DEFAULT now(),
        goal text,
        status text,
        chat_id text,
        created_at timestamptz NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX IF NOT EXISTS agentic_sessions_session_id_idx ON agentic_sessions(session_id);",
    """
    CREATE TABLE IF NOT EXISTS project_context (
        id bigserial PRIMARY KEY,
        project_id text NOT NULL,
        prepared_by text,
        context_md text NOT NULL DEFAULT '',
        consumed boolean NOT NULL DEFAULT false,
        prepared_at timestamptz NOT NULL DEFAULT now(),
        consumed_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX IF NOT EXISTS project_context_project_consumed_idx ON project_context(project_id, consumed, prepared_at DESC);",
    """
    CREATE TABLE IF NOT EXISTS backlog (
        id bigserial PRIMARY KEY,
        title text NOT NULL,
        type text,
        priority integer NOT NULL DEFAULT 1,
        description text,
        domain text,
        effort text,
        impact text,
        status text NOT NULL DEFAULT 'pending',
        discovered_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    );
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS backlog_title_uidx ON backlog(title);",
    """
    CREATE TABLE IF NOT EXISTS reasoning_log (
        id bigserial PRIMARY KEY,
        session_id text,
        domain text,
        action_planned text,
        preflight_result text,
        assumptions_caught integer NOT NULL DEFAULT 0,
        queries_triggered integer NOT NULL DEFAULT 0,
        owner_confirm_needed boolean NOT NULL DEFAULT false,
        behavioral_rule_proposed boolean NOT NULL DEFAULT false,
        outcome text,
        reasoning text,
        created_at timestamptz NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX IF NOT EXISTS reasoning_log_session_id_idx ON reasoning_log(session_id, created_at DESC);",
    """
    CREATE TABLE IF NOT EXISTS projects (
        project_id text PRIMARY KEY,
        name text NOT NULL DEFAULT '',
        folder_path text,
        index_path text,
        status text NOT NULL DEFAULT 'active',
        last_indexed timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS changelog (
        id bigserial PRIMARY KEY,
        action text,
        detail text,
        domain text,
        version text,
        change_type text,
        component text,
        title text,
        description text,
        triggered_by text,
        growth_flag_type text,
        before_state text,
        after_state text,
        files_changed text[] DEFAULT '{}'::text[],
        session_id bigint,
        created_at timestamptz NOT NULL DEFAULT now()
    );
    """,
    "ALTER TABLE changelog ADD COLUMN IF NOT EXISTS change_type text;",
    "ALTER TABLE changelog ADD COLUMN IF NOT EXISTS component text;",
    "ALTER TABLE changelog ADD COLUMN IF NOT EXISTS title text;",
    "ALTER TABLE changelog ADD COLUMN IF NOT EXISTS description text;",
    "ALTER TABLE changelog ADD COLUMN IF NOT EXISTS triggered_by text;",
    "ALTER TABLE changelog ADD COLUMN IF NOT EXISTS growth_flag_type text;",
    "ALTER TABLE changelog ADD COLUMN IF NOT EXISTS before_state text;",
    "ALTER TABLE changelog ADD COLUMN IF NOT EXISTS after_state text;",
    "ALTER TABLE changelog ADD COLUMN IF NOT EXISTS files_changed text[] DEFAULT '{}'::text[];",
    "ALTER TABLE changelog ADD COLUMN IF NOT EXISTS session_id bigint;",
]


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding='utf-8', errors='ignore').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        values[key] = value
    return values


def _load_env() -> dict[str, str]:
    env = dict(os.environ)
    for candidate in ENV_CANDIDATES:
        for key, value in _read_env_file(candidate).items():
            env.setdefault(key, value)
    return env


def _project_ref(supabase_url: str) -> str:
    url = (supabase_url or '').strip().rstrip('/')
    if not url:
        raise RuntimeError('SUPABASE_URL is missing')
    if '//' not in url or '.supabase.co' not in url:
        raise RuntimeError(f'Could not infer project ref from {supabase_url!r}')
    return url.split('//', 1)[1].split('.', 1)[0]


def _management_pat(env: dict[str, str]) -> str:
    for key in ('SUPABASE_MANAGEMENT_PAT', 'SUPABASE_PAT', 'SUPABASE_SECRET_KEY', 'SUPABASE_SVC_KEY'):
        value = (env.get(key) or '').strip()
        if value:
            return value
    raise RuntimeError('Missing Supabase management PAT in .env')


def _query(ref: str, pat: str, sql: str, timeout: int = 120) -> list[dict]:
    resp = httpx.post(
        f'https://api.supabase.com/v1/projects/{ref}/database/query',
        headers={
            'Authorization': f'Bearer {pat}',
            'Content-Type': 'application/json',
        },
        json={'query': sql if sql.rstrip().endswith(';') else sql + ';'},
        timeout=timeout,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f'{resp.status_code}: {resp.text[:500]}')
    try:
        return resp.json()
    except Exception:
        return []


def _apply_statements(ref: str, pat: str, statements: list[str], label: str) -> dict:
    results = []
    errors = []
    for stmt in statements:
        sql = stmt.strip()
        if not sql:
            continue
        try:
            _query(ref, pat, sql)
            results.append({'ok': True, 'label': label})
        except Exception as exc:
            errors.append(f'{label}: {str(exc)[:240]}')
            print(f'[BOOTSTRAP] {label} failed: {exc}')
            results.append({'ok': False, 'label': label, 'error': str(exc)})
    return {'ok': not errors, 'results': results, 'errors': errors}


def _run_script(script: Path) -> None:
    print(f'[bootstrap] running {script}')
    proc = subprocess.run([sys.executable, str(script)], cwd=str(ROOT), capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout, end='')
    if proc.stderr:
        print(proc.stderr, end='')
    if proc.returncode != 0:
        raise RuntimeError(f'{script.name} failed with exit code {proc.returncode}')


def _table_count(ref: str, pat: str) -> int:
    rows = _query(ref, pat, "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';")
    if not rows:
        return 0
    try:
        return int(rows[0].get('n') or 0)
    except Exception:
        return 0


def bootstrap_supabase() -> dict:
    env = _load_env()
    supabase_url = (env.get('SUPABASE_URL') or '').strip()
    if not supabase_url:
        return {'ok': False, 'error': 'SUPABASE_URL is missing'}
    ref = _project_ref(supabase_url)
    pat = _management_pat(env)

    table_count = _table_count(ref, pat)
    print(f'[bootstrap] {ref} public base tables: {table_count}')

    results = []
    errors = []

    if table_count == 0:
        manifest = json.loads(FULL_SCHEMA_MANIFEST.read_text(encoding='utf-8'))
        chunks = manifest.get('chunks') or []
        labels = manifest.get('chunk_labels') or ['chunk'] * len(chunks)
        print(f'[bootstrap] applying {FULL_SCHEMA_MANIFEST.name} with {len(chunks)} chunks')
        for idx, chunk in enumerate(chunks, start=1):
            label = labels[idx - 1] if idx - 1 < len(labels) else 'chunk'
            print(f'[bootstrap] {label} {idx}/{len(chunks)}')
            try:
                _query(ref, pat, chunk, timeout=120)
            except Exception as exc:
                if label == 'indexes':
                    print(f'[bootstrap] skipping index chunk {idx}: {exc}')
                    continue
                errors.append(f'{label}: {exc}')
                raise
            time.sleep(1.5)
        _query(ref, pat, "SELECT pg_notify('pgrst', 'reload schema');", timeout=30)
        time.sleep(3)
        _query(ref, pat, "SELECT pg_notify('pgrst', 'reload schema');", timeout=30)

    for script in (
        ROOT / 'run_reflection_audit_ddl.py',
        ROOT / 'run_repo_map_ddl.py',
        ROOT / 'run_semantic_ddl.py',
    ):
        _run_script(script)

    results.append(_apply_statements(ref, pat, CORE_EXTRA_DDL, 'core-extra-ddl'))
    try:
        _query(ref, pat, "SELECT pg_notify('pgrst', 'reload schema');", timeout=30)
    except Exception as exc:
        errors.append(f'reload_schema: {exc}')
        print(f'[BOOTSTRAP] schema reload failed: {exc}')

    return {
        'ok': not errors,
        'ref': ref,
        'results': results[:20],
        'errors': errors[:20],
    }


def main() -> int:
    result = bootstrap_supabase()
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
