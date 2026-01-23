"""Cleanup service for managing research history files."""

import json
import shutil
from datetime import datetime
from pathlib import Path

RESEARCH_HISTORY_DIR = Path(__file__).resolve().parent.parent / "research_history"
RESEARCH_HISTORY_DIR.mkdir(exist_ok=True)

AGENT_NOTES_DIR = Path(__file__).resolve().parent.parent / "agent_notes"

# File for real-time token usage tracking (read by get_token_budget_info tool)
TOKEN_USAGE_FILE = RESEARCH_HISTORY_DIR / "current_token_usage.json"


def update_token_usage(prompt_tokens: int, total_tokens: int) -> None:
    """Write current token usage to file for the agent tool to read."""
    data = {
        "prompt_token_count": prompt_tokens,
        "total_token_count": total_tokens,
        "updated_at": datetime.now().isoformat(),
    }
    TOKEN_USAGE_FILE.write_text(json.dumps(data), encoding="utf-8")


def clear_token_usage() -> None:
    """Clear token usage file at start of new run."""
    if TOKEN_USAGE_FILE.exists():
        TOKEN_USAGE_FILE.unlink()


def clear_agent_notes() -> None:
    """Clear all agent notes from previous run."""
    if AGENT_NOTES_DIR.exists():
        shutil.rmtree(AGENT_NOTES_DIR)
    AGENT_NOTES_DIR.mkdir(exist_ok=True)


def cleanup_failed_run(trace_file: Path, md_file: Path | None = None) -> None:
    """
    Clean up partial outputs from a failed run.
    Removes the current run's trace and md files while preserving previous run's results.
    """
    # Remove partial trace file from this failed run
    if trace_file.exists():
        try:
            trace_file.unlink()
        except OSError:
            pass

    # Remove partial md file if it was created
    if md_file and md_file.exists():
        try:
            md_file.unlink()
        except OSError:
            pass

    # Clear agent notes (they're session-specific)
    clear_agent_notes()

    # Clear token usage file
    clear_token_usage()


def cleanup_previous_run() -> None:
    """
    Clean up outputs from the previous successful run after current run succeeds.
    Keeps only the most recent md and trace files.
    """
    cleanup_old_files(keep_latest=True)


def cleanup_old_files(keep_latest: bool = True) -> list[Path]:
    """
    Clean up old research files, keeping only the latest ones.

    Args:
        keep_latest: If True, keeps the most recent md and trace files.

    Returns:
        List of deleted file paths.
    """
    deleted = []

    md_files = sorted(
        RESEARCH_HISTORY_DIR.glob("research_*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )
    trace_files = sorted(
        RESEARCH_HISTORY_DIR.glob("trace_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

    # Keep the latest, delete the rest
    files_to_delete = []
    if keep_latest:
        files_to_delete.extend(md_files[1:])
        files_to_delete.extend(trace_files[1:])
    else:
        files_to_delete.extend(md_files)
        files_to_delete.extend(trace_files)

    for file in files_to_delete:
        try:
            file.unlink()
            deleted.append(file)
        except OSError:
            pass

    return deleted


def get_file_counts() -> dict[str, int]:
    """Get counts of files in research history."""
    return {
        "md_files": len(list(RESEARCH_HISTORY_DIR.glob("research_*.md"))),
        "trace_files": len(list(RESEARCH_HISTORY_DIR.glob("trace_*.json"))),
    }
