#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

export PORT="${PORT:-8081}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

PYTHON_BIN=""
for candidate in "./.venv/bin/python" "./venv/bin/python"; do
  if [[ -x "$candidate" ]]; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [[ -z "$PYTHON_BIN" ]] && command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "Missing Python interpreter; expected .venv/bin/python, venv/bin/python, or python3" >&2
  exit 1
fi

exec "$PYTHON_BIN" -m uvicorn core_main:app --host 0.0.0.0 --port "$PORT" --timeout-keep-alive 75
