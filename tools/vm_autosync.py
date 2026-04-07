#!/usr/bin/env python3
"""Mirror selected VM repos from GitHub by pulling safe updates only.

This service is intentionally one-way:
- GitHub is the source of truth
- the VM fetches and resets to origin branches
- no commit or push happens from the VM
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


DEFAULT_REPOS = [
    Path("/home/ubuntu/core-agi"),
    Path("/home/ubuntu/trading-bot"),
    Path("/home/ubuntu/specter-alpha"),
]

REPO_CLEAN_EXCLUDES: dict[Path, tuple[str, ...]] = {
    Path("/home/ubuntu/core-agi"): (
        ".env",
        ".runtime",
        ".state",
        "supabase_export.json",
    ),
    Path("/home/ubuntu/trading-bot"): (
        ".env",
        ".env.bak*",
        ".runtime",
        ".state",
        "data",
        ".cutover",
        ".cutover-backups",
    ),
    Path("/home/ubuntu/specter-alpha"): (
        ".env",
        ".runtime",
    ),
}

REPO_RESTORE_PATHS: dict[Path, tuple[str, ...]] = {
    Path("/home/ubuntu/trading-bot"): (
        ".runtime/trading_runtime_state.json",
    ),
}

REPO_RESTART_SERVICES: dict[Path, tuple[str, ...]] = {
    Path("/home/ubuntu/trading-bot"): ("core-trading-bot",),
    Path("/home/ubuntu/specter-alpha"): ("specter-alpha",),
}


@dataclass
class RepoState:
    last_synced_remote: str | None = None
    last_synced_at: float | None = None


@dataclass
class WatchState:
    repos: list[Path]
    poll_seconds: int
    states: dict[Path, RepoState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for repo in self.repos:
            self.states.setdefault(repo, RepoState())


def repo_key(repo: Path) -> Path:
    return Path(repo).resolve()


def run_git(args: Sequence[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("GIT_EDITOR", "true")
    env.setdefault("GIT_SEQUENCE_EDITOR", "true")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_PAGER", "cat")
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        env=env,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    return proc


def current_branch(repo: Path) -> str:
    proc = run_git(["branch", "--show-current"], cwd=repo)
    branch = proc.stdout.strip()
    return branch or "main"


def head_sha(repo: Path, ref: str = "HEAD") -> str:
    proc = run_git(["rev-parse", ref], cwd=repo)
    return proc.stdout.strip()


def backup_restore_paths(repo: Path) -> Path | None:
    restore_paths = REPO_RESTORE_PATHS.get(repo_key(repo), ())
    if not restore_paths:
        return None

    backup_root = Path(tempfile.mkdtemp(prefix="vm-autosync-"))
    copied_any = False
    for rel_path in restore_paths:
        source = repo / rel_path
        if not source.exists():
            continue
        target = backup_root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied_any = True

    if copied_any:
        return backup_root

    shutil.rmtree(backup_root, ignore_errors=True)
    return None


def restore_paths(repo: Path, backup_root: Path | None) -> None:
    if not backup_root or not backup_root.exists():
        return

    for source in backup_root.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(backup_root)
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    shutil.rmtree(backup_root, ignore_errors=True)


def clean_repo(repo: Path) -> None:
    clean_args = ["clean", "-fd"]
    for pattern in REPO_CLEAN_EXCLUDES.get(repo_key(repo), ()):
        clean_args.extend(["-e", pattern])
    run_git(clean_args, cwd=repo)


def restart_repo_services(repo: Path) -> None:
    services = REPO_RESTART_SERVICES.get(repo_key(repo), ())
    if not services:
        return

    logging.info("restarting services for %s: %s", repo, ", ".join(services))
    proc = subprocess.run(
        ["systemctl", "restart", *services],
        text=True,
        capture_output=True,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"systemctl restart {' '.join(services)} failed for {repo}: {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )


def mirror_repo(repo: Path, repo_state: RepoState) -> bool:
    run_git(["fetch", "origin", "--prune"], cwd=repo)
    branch = current_branch(repo)
    remote_ref = f"origin/{branch}"
    remote_head = head_sha(repo, remote_ref)
    local_head = head_sha(repo, "HEAD")

    if local_head == remote_head:
        repo_state.last_synced_remote = remote_head
        return False

    logging.info(
        "mirroring %s from %s to %s",
        repo,
        local_head[:7],
        remote_head[:7],
    )
    backup_root = backup_restore_paths(repo)
    try:
        run_git(["reset", "--hard", remote_ref], cwd=repo)
        clean_repo(repo)
        restore_paths(repo, backup_root)
        restart_repo_services(repo)
        repo_state.last_synced_remote = remote_head
        repo_state.last_synced_at = time.time()
        return True
    except Exception:
        if backup_root and backup_root.exists():
            restore_paths(repo, backup_root)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mirror VM repos from GitHub.")
    parser.add_argument("--repo", action="append", type=Path, help="Repository path to watch (repeatable).")
    parser.add_argument("--poll-seconds", type=int, default=10, help="Polling interval in seconds.")
    parser.add_argument("--once", action="store_true", help="Run one scan cycle and exit.")
    return parser.parse_args()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main() -> int:
    args = parse_args()
    setup_logging()

    repos = args.repo or DEFAULT_REPOS
    watch = WatchState(repos=[Path(repo) for repo in repos], poll_seconds=args.poll_seconds)

    logging.info("starting VM mirror for %s", ", ".join(str(repo) for repo in watch.repos))

    while True:
        for repo in watch.repos:
            state = watch.states[repo]
            try:
                changed = mirror_repo(repo, state)
                if changed:
                    logging.info("mirrored %s", repo)
            except Exception as exc:  # noqa: BLE001
                logging.exception("mirror error for %s: %s", repo, exc)
        if args.once:
            break
        time.sleep(watch.poll_seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
