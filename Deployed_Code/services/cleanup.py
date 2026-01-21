"""Cleanup service for managing research history files."""

from pathlib import Path

RESEARCH_HISTORY_DIR = Path(__file__).resolve().parent.parent / "research_history"


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
