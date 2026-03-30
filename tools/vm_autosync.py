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
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


DEFAULT_REPOS = [
    Path("/home/ubuntu/core-agi"),
    Path("/home/ubuntu/trading-bot"),
    Path("/home/ubuntu/specter-alpha"),
]


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
            f"git {' '.join(args)} failed in {cwd}: {proc.returncode}
"
            f"stdout:
{proc.stdout}
"
            f"stderr:
{proc.stderr}"
        )
    return proc


def current_branch(repo: Path) -> str:
    proc = run_git(["branch", "--show-current"], cwd=repo)
    branch = proc.stdout.strip()
    return branch or "main"


def head_sha(repo: Path, ref: str = "HEAD") -> str:
    proc = run_git(["rev-parse", ref], cwd=repo)
    return proc.stdout.strip()


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
    run_git(["reset", "--hard", remote_ref], cwd=repo)
    run_git(["clean", "-fd"], cwd=repo)
    repo_state.last_synced_remote = remote_head
    repo_state.last_synced_at = time.time()
    return True


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
