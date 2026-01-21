"""Services module for the Post Content Agent."""

from .google_drive import upload_to_drive, get_drive_service
from .gmail import send_research_email, get_gmail_service, markdown_to_html
from .research_runner import run_research_agent, get_latest_research_file, RESEARCH_HISTORY_DIR
from .cleanup import cleanup_old_files, get_file_counts

__all__ = [
    "upload_to_drive",
    "get_drive_service",
    "send_research_email",
    "get_gmail_service",
    "markdown_to_html",
    "run_research_agent",
    "get_latest_research_file",
    "RESEARCH_HISTORY_DIR",
    "cleanup_old_files",
    "get_file_counts",
]
