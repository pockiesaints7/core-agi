"""
core_orch_layer6.py — CORE AGI GitHub Layer
============================================
Wrapper around core_github.py functions. Adds:
  - L10 Constitution checks
  - Error normalization
  - Telegram notification integration

DOES NOT:
  - Make raw GitHub API calls (delegates to core_github)
  - Bypass L10 checks
  - Store credentials

All other layers call L6 for GitHub access, never core_github directly.
"""

from typing import Optional, Dict, Any

# Import L10 Constitution
try:
    from core_orch_layer10 import report_violation, SEVERITY_HIGH
except ImportError:
    print("[L6] WARNING: L10 Constitution Layer not available")
    def report_violation(*args, **kwargs): pass
    SEVERITY_HIGH = "high"

# Import actual GitHub functions from core_github
try:
    from core_github import gh_read, gh_write, notify, gh_patch
except ImportError:
    print("[L6] CRITICAL: Cannot import core_github functions")
    def gh_read(*args, **kwargs): return None
    def gh_write(*args, **kwargs): return None
    def notify(*args, **kwargs): pass
    def gh_patch(*args, **kwargs): return None


# ══════════════════════════════════════════════════════════════════════════════
# L6 PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def read_file(path: str, repo: str = "pockiesaints7/core-agi") -> Optional[str]:
    """
    Read file from GitHub repository.
    
    Args:
        path: File path in repo (e.g. "core_config.py")
        repo: Repository in format "owner/repo"
    
    Returns:
        File content as string, or None on failure
    """
    try:
        content = gh_read(path, repo=repo)
        
        if content is None:
            raise RuntimeError(f"gh_read returned None for {path}")
        
        return content
    
    except Exception as e:
        print(f"[L6] read_file {path} failed: {e}")
        report_violation(
            invariant="L6-GITHUB",
            what_failed=f"Failed to read file from GitHub: {path}",
            context=f"repo={repo}, error={str(e)[:200]}",
            how_to_avoid="Check GitHub connectivity and file path",
            severity=SEVERITY_HIGH,
        )
        return None


def write_file(path: str, content: str, message: str,
               repo: str = "pockiesaints7/core-agi") -> bool:
    """
    Write file to GitHub repository.
    
    Args:
        path: File path in repo
        content: File content
        message: Commit message
        repo: Repository in format "owner/repo"
    
    Returns:
        True on success, False on failure
    """
    try:
        result = gh_write(path, content, message=message, repo=repo)
        
        if not result or not result.get("ok"):
            raise RuntimeError(f"gh_write failed for {path}")
        
        return True
    
    except Exception as e:
        print(f"[L6] write_file {path} failed: {e}")
        report_violation(
            invariant="L6-GITHUB",
            what_failed=f"Failed to write file to GitHub: {path}",
            context=f"repo={repo}, message={message[:100]}, error={str(e)[:200]}",
            how_to_avoid="Check GitHub connectivity and write permissions",
            severity=SEVERITY_HIGH,
        )
        return False


def patch_file(path: str, old_str: str, new_str: str, message: str,
               repo: str = "pockiesaints7/core-agi") -> bool:
    """
    Patch file in GitHub repository (search-and-replace).
    
    Args:
        path: File path in repo
        old_str: String to find (must be unique)
        new_str: Replacement string
        message: Commit message
        repo: Repository in format "owner/repo"
    
    Returns:
        True on success, False on failure
    """
    try:
        result = gh_patch(path, old_str, new_str, message=message, repo=repo)
        
        if not result or not result.get("ok"):
            raise RuntimeError(f"gh_patch failed for {path}")
        
        return True
    
    except Exception as e:
        print(f"[L6] patch_file {path} failed: {e}")
        report_violation(
            invariant="L6-GITHUB",
            what_failed=f"Failed to patch file in GitHub: {path}",
            context=f"repo={repo}, message={message[:100]}, error={str(e)[:200]}",
            how_to_avoid="Check that old_str exists exactly once in file",
            severity=SEVERITY_HIGH,
        )
        return False


def send_notification(message: str, chat_id: str = None) -> None:
    """
    Send notification via Telegram.
    
    Args:
        message: Notification text (supports HTML)
        chat_id: Optional chat ID (defaults to owner)
    """
    try:
        notify(message, cid=chat_id)
    except Exception as e:
        print(f"[L6] send_notification failed: {e}")
        # Don't report violation for notifications - they're non-critical


# ══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def health_check() -> bool:
    """
    Quick GitHub connectivity check.
    
    Returns:
        True if GitHub is reachable, False otherwise
    """
    try:
        result = read_file("README.md")
        return result is not None
    except Exception as e:
        print(f"[L6] health_check failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# INITIALIZATION
# ══════════════════════════════════════════════════════════════════════════════

print("[L6] GitHub Layer loaded")
