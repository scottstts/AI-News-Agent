"""Gmail service for sending research result emails."""

import base64
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import markdown
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
TOKEN_PATH = Path(__file__).resolve().parent.parent / "credentials" / "gmail_token.json"
CREDENTIALS_PATH = Path(__file__).resolve().parent.parent / "credentials" / "credentials.json"


def get_gmail_service():
    """Get authenticated Gmail service."""
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

    return build("gmail", "v1", credentials=creds)


def markdown_to_html(md_content: str) -> str:
    """Convert markdown content to styled HTML for email."""
    html_body = markdown.markdown(
        md_content,
        extensions=["extra", "nl2br", "sane_lists"]
    )

    styled_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
            }}
            h1 {{
                color: #1a73e8;
                border-bottom: 2px solid #1a73e8;
                padding-bottom: 10px;
            }}
            h2 {{
                color: #34a853;
                margin-top: 30px;
            }}
            h3 {{
                color: #444;
                margin-top: 20px;
            }}
            a {{
                color: #1a73e8;
                text-decoration: none;
            }}
            a:hover {{
                text-decoration: underline;
            }}
            hr {{
                border: none;
                border-top: 1px solid #e0e0e0;
                margin: 20px 0;
            }}
            ul {{
                padding-left: 20px;
            }}
            li {{
                margin-bottom: 5px;
            }}
            code {{
                background-color: #f5f5f5;
                padding: 2px 6px;
                border-radius: 3px;
                font-family: 'Consolas', monospace;
            }}
            blockquote {{
                border-left: 4px solid #1a73e8;
                margin-left: 0;
                padding-left: 20px;
                color: #666;
            }}
            table {{
                border-collapse: collapse;
                margin: 15px 0;
                font-size: 14px;
            }}
            th, td {{
                border: 1px solid #e0e0e0;
                padding: 8px 12px;
                text-align: left;
            }}
            th {{
                background-color: #f5f5f5;
                font-weight: 600;
            }}
            tr:nth-child(even) {{
                background-color: #fafafa;
            }}
        </style>
    </head>
    <body>
        {html_body}
    </body>
    </html>
    """
    return styled_html


def send_research_email(
    md_file_path: Path,
    to_email: str | None = None,
    subject: str | None = None
) -> str:
    """
    Send research results as a formatted HTML email.

    Args:
        md_file_path: Path to the markdown file with research results
        to_email: Recipient email. If None, uses RECIPIENT_EMAIL env var.
        subject: Email subject. If None, generates from date.

    Returns:
        The message ID of the sent email.
    """
    to_email = to_email or os.getenv("RECIPIENT_EMAIL")
    if not to_email:
        raise ValueError("No to_email provided and RECIPIENT_EMAIL env var not set.")

    md_content = md_file_path.read_text(encoding="utf-8")
    html_content = markdown_to_html(md_content)

    if not subject:
        subject = f"AI News Research Results - {md_file_path.stem.replace('research_', '')}"

    service = get_gmail_service()

    message = MIMEMultipart("alternative")
    message["to"] = to_email
    message["subject"] = subject

    # Attach both plain text and HTML versions
    part1 = MIMEText(md_content, "plain")
    part2 = MIMEText(html_content, "html")
    message.attach(part1)
    message.attach(part2)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    body = {"raw": raw}

    sent_message = service.users().messages().send(userId="me", body=body).execute()
    return sent_message["id"]
