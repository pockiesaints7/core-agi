#!/usr/bin/env python3
"""Poll selected VM repos and auto-commit/push safe changes.

This is intentionally conservative:
- it ignores secrets, logs, build bundles, and local runtime artifacts
- it batches edits until the tree is stable for a short quiet window
- it only stages the safe paths it sees in `git status`

The script is designed to run under systemd on the VM.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Sequence


DEFAULT_REPOS = [
    Path("/home/ubuntu/core-agi"),
    Path("/home/ubuntu/trading-bot"),
    Path("/home/ubuntu/specter-alpha"),
]

DENY_DIRS = {
    ".git",
    "logs",
    "__pycache__",
    ".venv",
    "node_modules",
}

DENY_BASENAMES = {
    ".env",
    ".env.testnet",
    ".env.testnet.example",
}

DENY_PREFIXES = (
    ".env.",
)

DENY_SUFFIXES = (
    ".bundle",
    ".key",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".pyc",
)


@dataclass
class RepoState:
    last_signature: str | None = None
    first_seen_at: float | None = None
    last_commit_at: float | None = None


def run_git(args: Sequence[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    return proc


def path_is_safe(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]

    if not parts:
        return False

    for part in parts:
        if part in DENY_DIRS:
            return False

    basename = parts[-1]
    if basename in DENY_BASENAMES:
        return False

    if basename.startswith(DENY_PREFIXES):
        return False

    if any(basename.endswith(suffix) for suffix in DENY_SUFFIXES):
        return False

    if normalized.startswith("logs/") or "/logs/" in normalized:
        return False

    return True


def parse_porcelain_z(output: str) -> list[tuple[str, str]]:
    """Parse `git status --porcelain=v1 -z` into (status, path) tuples.

    Rename/copy records are normalized to the destination path.
    """

    records = [item for item in output.split("\0") if item]
    parsed: list[tuple[str, str]] = []
    i = 0
    while i < len(records):
        entry = records[i]
        status = entry[:2]
        if status and status[0] in {"R", "C"}:
            # Rename/copy: current record contains the old path, next record is the new path.
            path = records[i + 1] if i + 1 < len(records) else entry[3:]
            i += 2
        else:
            path = entry[3:]
            i += 1
        if path:
            parsed.append((status, path))
    return parsed


def safe_status_entries(repo: Path) -> list[tuple[str, str]]:
    proc = run_git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo,
        check=True,
    )
    entries = parse_porcelain_z(proc.stdout)
    return [(status, path) for status, path in entries if path_is_safe(path)]


def status_signature(entries: Sequence[tuple[str, str]]) -> str:
    return "\n".join(f"{status}:{path}" for status, path in sorted(entries, key=lambda item: item[1]))


def stage_entries(repo: Path, entries: Sequence[tuple[str, str]]) -> None:
    for _status, path in entries:
        run_git(["add", "-A", "--", path], cwd=repo, check=True)


def commit_and_push(repo: Path, repo_name: str) -> bool:
    staged = run_git(["diff", "--cached", "--name-only"], cwd=repo, check=True).stdout.strip()
    if not staged:
        return False

    ts = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    msg = f"chore(vm): autosync {repo_name} @ {ts}"
    run_git(["commit", "-m", msg], cwd=repo, check=True)

    push = run_git(["push"], cwd=repo, check=False)
    if push.returncode == 0:
        return True

    logging.warning("push failed for %s, retrying after rebase: %s", repo_name, push.stderr.strip())
    run_git(["fetch", "origin"], cwd=repo, check=True)
    branch = current_branch(repo)
    run_git(["rebase", "--autostash", f"origin/{branch}"], cwd=repo, check=True)
    run_git(["push"], cwd=repo, check=True)
    return True


def repo_name_from_path(repo: Path) -> str:
    return repo.name


def current_branch(repo: Path) -> str:
    proc = run_git(["branch", "--show-current"], cwd=repo, check=True)
    branch = proc.stdout.strip()
    return branch or "main"


@dataclass
class WatchState:
    repos: list[Path]
    quiet_seconds: int
    poll_seconds: int
    states: dict[Path, RepoState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for repo in self.repos:
            self.states.setdefault(repo, RepoState())


def maybe_sync_repo(repo: Path, repo_state: RepoState, quiet_seconds: int) -> bool:
    entries = safe_status_entries(repo)
    if not entries:
        repo_state.last_signature = None
        repo_state.first_seen_at = None
        return False

    signature = status_signature(entries)
    now = time.time()

    if signature != repo_state.last_signature:
        repo_state.last_signature = signature
        repo_state.first_seen_at = now
        return False

    if repo_state.first_seen_at is None:
        repo_state.first_seen_at = now
        return False

    if now - repo_state.first_seen_at < quiet_seconds:
        return False

    stage_entries(repo, entries)
    pushed = commit_and_push(repo, repo_name_from_path(repo))
    if pushed:
        repo_state.last_commit_at = now
        repo_state.last_signature = None
        repo_state.first_seen_at = None
    return pushed


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-commit and push safe VM repo changes.")
    parser.add_argument("--repo", action="append", type=Path, help="Repository path to watch (repeatable).")
    parser.add_argument("--quiet-seconds", type=int, default=30, help="How long changes must stay stable before commit.")
    parser.add_argument("--poll-seconds", type=int, default=10, help="Polling interval.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    repos = args.repo or DEFAULT_REPOS
    watch = WatchState(repos=[Path(repo) for repo in repos], quiet_seconds=args.quiet_seconds, poll_seconds=args.poll_seconds)

    logging.info("starting autosync for %s", ", ".join(str(repo) for repo in watch.repos))

    while True:
        for repo in watch.repos:
            state = watch.states[repo]
            try:
                changed = maybe_sync_repo(repo, state, watch.quiet_seconds)
                if changed:
                    logging.info("synced %s", repo)
            except Exception as exc:  # noqa: BLE001
                logging.exception("autosync error for %s: %s", repo, exc)
        time.sleep(watch.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
