"""Google Drive service for uploading research results."""

import os
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_PATH = Path(__file__).resolve().parent.parent / "credentials" / "drive_token.json"
CREDENTIALS_PATH = Path(__file__).resolve().parent.parent / "credentials" / "credentials.json"


def get_drive_service():
    """Get authenticated Google Drive service."""
    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_PATH}. "
                    "Download OAuth credentials from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_PATH, "w") as token:
            token.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


MIME_TYPES = {
    ".md": "text/markdown",
    ".json": "application/json",
}


def upload_to_drive(file_path: Path, folder_id: str | None = None) -> str:
    """
    Upload a file to Google Drive.

    Args:
        file_path: Path to the file to upload
        folder_id: Optional Google Drive folder ID. If None, uses GOOGLE_DRIVE_FOLDER_ID env var.

    Returns:
        The file ID of the uploaded file.
    """
    folder_id = folder_id or os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if not folder_id:
        raise ValueError("No folder_id provided and GOOGLE_DRIVE_FOLDER_ID env var not set.")

    service = get_drive_service()

    file_metadata = {
        "name": file_path.name,
        "parents": [folder_id],
    }

    mimetype = MIME_TYPES.get(file_path.suffix, "application/octet-stream")
    media = MediaFileUpload(str(file_path), mimetype=mimetype)

    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()

    return file.get("id")