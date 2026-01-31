from googleapiclient.discovery import build
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import os
from pathlib import Path

# GPT 5.2 has hard 272000 max input token limit
MAX_INPUT_TOKENS = 240000

def get_date() -> str:
    """Returns the current date in YYYY-MM-DD format."""
    return datetime.now().strftime("%Y-%m-%d")


def get_token_budget_info() -> dict:
    """
    Returns token budget information including max limit and current consumption.
    Call this periodically to monitor your token usage and avoid exceeding the limit.

    Returns:
        dict: Contains max_input_tokens, current_prompt_tokens, tokens_remaining, usage_percent, potential warning message.
    """
    import json
    from pathlib import Path

    # Token usage file written by research_runner during execution
    token_usage_file = Path(__file__).resolve().parent.parent / "research_history" / "current_token_usage.json"

    current_prompt_tokens = 0
    if token_usage_file.exists():
        try:
            data = json.loads(token_usage_file.read_text(encoding="utf-8"))
            current_prompt_tokens = data.get("prompt_token_count", 0)
        except (json.JSONDecodeError, IOError):
            pass

    tokens_remaining = MAX_INPUT_TOKENS - current_prompt_tokens
    usage_percent = round((current_prompt_tokens / MAX_INPUT_TOKENS) * 100, 2) if MAX_INPUT_TOKENS > 0 else 0

    warning_msg = "SYSTEM WARNING: Token usage has exceeded 90% of max token usage limit, you MUST wrap up the research and start presenting the findings!!!" if usage_percent > 90 else "None"

    return {
        "max_input_tokens": MAX_INPUT_TOKENS,
        "current_prompt_tokens": current_prompt_tokens,
        "tokens_remaining": tokens_remaining,
        "usage_percent": f"{usage_percent}%",
        "usage_warning": warning_msg,
    }

def _extract_news_from_trace(trace_content: str) -> list | None:
    """
    Extract the 'news' array from trace JSON content.
    Uses the same logic as extract_final_text_from_dicts in research_runner.py.

    Args:
        trace_content: The raw JSON string of the trace file (a JSON array of events).

    Returns:
        list: The 'news' array from the final output, or None if not found.
    """
    import json
    import re

    try:
        trace_events = json.loads(trace_content)
    except json.JSONDecodeError:
        return None

    # Extract final text from trace events (same logic as extract_final_text_from_dicts)
    final_text = None
    for event in reversed(trace_events):
        if "content" in event and event["content"]:
            c = event["content"]
            if "parts" in c:
                for p in c["parts"]:
                    if p and "text" in p and p["text"]:
                        final_text = p["text"]
                        break
                if final_text:
                    break

    if not final_text:
        return None

    # Extract JSON from the final text (may be in code blocks)
    # Try to find JSON in code blocks first
    json_block_pattern = r"```(?:json)?\s*(\{[\s\S]*?\})\s*```"
    match = re.search(json_block_pattern, final_text)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if "news" in parsed:
                return parsed["news"]
        except json.JSONDecodeError:
            pass

    # Try to find raw JSON object
    json_pattern = r"\{[\s\S]*\"news\"[\s\S]*\}"
    match = re.search(json_pattern, final_text)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if "news" in parsed:
                return parsed["news"]
        except json.JSONDecodeError:
            pass

    return None


def _get_previous_research_from_drive() -> list | None:
    """
    Attempt to fetch previous day's research news from Google Drive trace file.
    Uses the same credentials and folder as the upload service.

    Returns:
        list: The 'news' array from the previous day's trace, or None if not found.
    """
    from pathlib import Path

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.http import MediaIoBaseDownload
        import io

        SCOPES = ["https://www.googleapis.com/auth/drive.file"]
        TOKEN_PATH = Path(__file__).resolve().parent.parent / "credentials" / "drive_token.json"

        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        if not folder_id:
            return None

        if not TOKEN_PATH.exists():
            return None

        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                return None

        service = build("drive", "v3", credentials=creds)

        # Calculate yesterday's date in the format used by filenames (YYYYMMDD)
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

        # Search for trace files from yesterday in the target folder
        query = f"'{folder_id}' in parents and name contains 'trace_{yesterday}' and name contains '.json' and trashed = false"

        results = service.files().list(
            q=query,
            fields="files(id, name, createdTime)",
            orderBy="createdTime desc",
            pageSize=1
        ).execute()

        files = results.get("files", [])

        if not files:
            return None

        # Download the most recent trace file from yesterday
        file_id = files[0]["id"]

        request = service.files().get_media(fileId=file_id)
        file_content = io.BytesIO()
        downloader = MediaIoBaseDownload(file_content, request)

        done = False
        while not done:
            _, done = downloader.next_chunk()

        content = file_content.getvalue().decode("utf-8")
        return _extract_news_from_trace(content)

    except Exception:
        # Silently fail and return None - caller will handle the fallback message
        return None


def get_previous_research_result() -> str:
    """
    Look for yesterday's research news from storage.
    First checks local research_history/ directory for trace JSON files from yesterday,
    then falls back to Google Drive to fetch yesterday's trace.

    Returns:
        str: JSON string of the 'news' array from yesterday's research, or an error message if not found.
    """
    import json

    research_dir = os.path.join(os.getcwd(), "research_history")

    # Ensure the directory exists
    if not os.path.exists(research_dir):
        os.makedirs(research_dir, exist_ok=True)

    # Calculate yesterday's date in the format used by filenames (YYYYMMDD)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    # Find trace JSON files from yesterday only
    trace_files = [
        f for f in os.listdir(research_dir)
        if f.startswith(f'trace_{yesterday}') and f.endswith('.json')
    ]

    # If local trace files from yesterday exist, use them
    if trace_files:
        if len(trace_files) > 1:
            # Sort by modification time to get the most recent one from yesterday
            trace_files_with_time = [(f, os.path.getmtime(os.path.join(research_dir, f))) for f in trace_files]
            trace_files_with_time.sort(key=lambda x: x[1], reverse=True)
            latest_file = trace_files_with_time[0][0]
        else:
            latest_file = trace_files[0]

        # Read the trace file and extract news
        file_path = os.path.join(research_dir, latest_file)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            news = _extract_news_from_trace(content)
            if news:
                return json.dumps(news, indent=2)
            return "No news found in yesterday's trace file."
        except Exception as e:
            return f"Error reading previous trace from {latest_file}: {str(e)}"

    # No local files from yesterday found - try to fetch from Google Drive
    drive_news = _get_previous_research_from_drive()
    if drive_news:
        return json.dumps(drive_news, indent=2)

    return "No previous research results found from yesterday. Start without previous reference."


def verify_urls(urls: list[str]) -> list[dict]:
    """
    Verify that URLs are alive and accessible by performing HEAD requests.
    Use this to validate source URLs before including them in your final output.
    Uses curl_cffi with TLS fingerprint spoofing for robust verification that
    matches what fetch_page_content will experience.

    Args:
        urls (list[str]): A list of URLs to verify.

    Returns:
        A list of dicts, each with 'url', 'valid' (bool), and 'status_code' or 'error'.
    """
    # Lazy imports to avoid circular dependency (fetch_tool imports from tools)
    from curl_cffi import requests as curl_requests
    from . import fetch_tool

    results = []

    for url in urls:
        # Check for known blocked domains first
        is_blocked, block_reason = fetch_tool._is_known_blocked_domain(url)
        if is_blocked:
            results.append({
                "url": url,
                "valid": False,
                "error": f"known_blocked_domain: {block_reason}",
            })
            continue

        # Get matched User-Agent and TLS fingerprint profile
        user_agent, impersonate = fetch_tool._get_matched_profile()

        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
        }

        # Add realistic referer header
        referer = fetch_tool._get_random_referer()
        if referer:
            headers["Referer"] = referer
            headers["Sec-Fetch-Site"] = "cross-site"
        else:
            headers["Sec-Fetch-Site"] = "none"

        try:
            # Use HEAD request for lightweight check
            resp = curl_requests.head(
                url,
                headers=headers,
                timeout=10,
                allow_redirects=True,
                impersonate=impersonate,
            )

            # Fall back to GET if HEAD not allowed
            if resp.status_code == 405:
                resp = curl_requests.get(
                    url,
                    headers=headers,
                    timeout=10,
                    allow_redirects=True,
                    impersonate=impersonate,
                )

            # Check for soft-block indicators in response headers/content
            is_soft_blocked = False
            if resp.status_code == 200:
                # For GET responses, check content for soft-block
                if resp.request.method == "GET" and resp.text:
                    is_soft_blocked = fetch_tool._is_soft_block(resp.text[:5000], resp.status_code)
                # For HEAD, check headers for Cloudflare challenge indicators
                else:
                    cf_headers = ["cf-mitigated", "cf-chl-bypass", "cf-ray"]
                    if any(h in resp.headers for h in cf_headers):
                        # Has CF headers, do a quick GET to check for challenge page
                        get_resp = curl_requests.get(
                            url,
                            headers=headers,
                            timeout=10,
                            allow_redirects=True,
                            impersonate=impersonate,
                        )
                        is_soft_blocked = fetch_tool._is_soft_block(get_resp.text[:5000], get_resp.status_code)

            if is_soft_blocked:
                results.append({
                    "url": url,
                    "valid": False,
                    "status_code": resp.status_code,
                    "error": "soft_block_detected",
                })
            else:
                valid = resp.status_code < 400
                results.append({
                    "url": url,
                    "valid": valid,
                    "status_code": resp.status_code,
                })

        except Exception as e:
            error_str = str(e).lower()
            if "timeout" in error_str:
                results.append({
                    "url": url,
                    "valid": False,
                    "error": "timeout",
                })
            else:
                results.append({
                    "url": url,
                    "valid": False,
                    "error": str(e),
                })

    return results


def youtube_search_tool(
        query: str,
        max_results: int = 10,
        published_after: str = (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        video_duration: str = "any",
        order: str = "relevance",
        language: str = "en"
    ) -> dict:
    """
    A tool for the agent to search YouTube videos.

    Args:
        query (str): The search query, specified by the agent.
        max_results (int): Limited to 10.
        published_after (str): Specified by the agent, by default it is set to 1 days prior to the current date. Make sure to search newest videos. Format: YYYY-MM-DDT00:00:00Z
        video_duration (str): Specified by the agent. Options: "any", "short", "medium", "long". By default it is "any".
        order (str): Specified by the agent. Options: "date", "rating", "relevance", "title", "videoCount", "viewCount". By default it is "relevance".
        language (str): Specified by the agent. The language code for the search results. By default it is "en".

    Returns:
        A dictionary with 'status' and 'report' keys. 
        'status' is 'success' or 'failure'. 
        'report' contains the data for each video, including title, truncated description, channel name, published date, and URL.
    """

    try:
        load_dotenv()
        API_KEY = os.getenv('GCP_SERVICES_API_KEY')

        if not API_KEY:
            return {
                "status": "failure",
                "report": "YouTube Search Failed"
            }

        yt = build('youtube', 'v3', developerKey=API_KEY)

        search_params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": max_results,
            "order": order,
            "publishedAfter": published_after,
            "videoDuration": video_duration,
            "relevanceLanguage": language,
        }

        request = yt.search().list(**search_params)
        response = request.execute()

        videos = []
        for item in response.get("items", []):
            video_id = item["id"]["videoId"]
            snippet = item["snippet"]
            videos.append({
                "title": snippet["title"],
                "description": snippet["description"], # truncated description
                "channel": snippet["channelTitle"],
                "published_at": snippet["publishedAt"],
                "url": f"https://www.youtube.com/watch?v={video_id}",
            })

        return {
            "status": "success",
            "report": videos
        }

    except Exception as e:
        return {
            "status": "failure",
            "report": f"Error searching YouTube: {str(e)}"
        }


# Agent notes directory
AGENT_NOTES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agent_notes")


def take_notes(notes: list[dict]) -> dict:
    """
    Save research notes as separate markdown files in agent_notes/ directory.
    Use this to jot down important findings, thoughts, or reminders during research.

    Args:
        notes: A list of note objects, each with:
            - title (str): Used as the filename (will be sanitized)
            - content (str): The note content

    Returns:
        dict: Status and list of saved note filenames

    Example:
        take_notes([
            {"title": "new_model_release", "content": "GPT-5 announced on Jan 15..."},
            {"title": "todo_follow_up", "content": "Check Anthropic blog for details"}
        ])
    """
    if not notes:
        return {"status": "failure", "error": "No notes provided"}

    os.makedirs(AGENT_NOTES_DIR, exist_ok=True)

    saved_files = []
    errors = []

    for note in notes:
        title = note.get("title", "").strip()
        content = note.get("content", "").strip()

        if not title:
            errors.append("Note missing title, skipped")
            continue
        if not content:
            errors.append(f"Note '{title}' has no content, skipped")
            continue

        # Sanitize filename: replace spaces/special chars, ensure .md extension
        safe_title = "".join(c if c.isalnum() or c in "-_" else "_" for c in title)
        safe_title = safe_title[:100]  # Limit filename length
        filename = f"{safe_title}.md"
        filepath = os.path.join(AGENT_NOTES_DIR, filename)

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"# {title}\n\n{content}")
            saved_files.append(filename)
        except Exception as e:
            errors.append(f"Failed to save '{title}': {str(e)}")

    result = {
        "status": "success" if saved_files else "failure",
        "saved_notes": saved_files,
    }
    if errors:
        result["errors"] = errors

    return result


def read_notes(mode: str = "list", filenames: list[str] = None) -> dict:
    """
    Read research notes from agent_notes/ directory.

    Args:
        mode: Either "list" (returns all note filenames) or "content" (returns note contents)
        filenames: Required when mode="content". List of note filenames to read.

    Returns:
        dict: In "list" mode, returns {"notes": [list of filenames]}
              In "content" mode, returns {"notes": {filename: content, ...}}

    Example:
        # First list available notes
        read_notes(mode="list")
        # Then read specific ones
        read_notes(mode="content", filenames=["new_model_release.md", "todo_follow_up.md"])
    """
    if not os.path.exists(AGENT_NOTES_DIR):
        return {"status": "success", "notes": [] if mode == "list" else {}, "message": "No notes directory exists yet"}

    if mode == "list":
        # List all .md files in the notes directory
        try:
            md_files = sorted([f for f in os.listdir(AGENT_NOTES_DIR) if f.endswith(".md")])
            return {"status": "success", "notes": md_files}
        except Exception as e:
            return {"status": "failure", "error": str(e)}

    elif mode == "content":
        if not filenames:
            return {"status": "failure", "error": "filenames required for content mode"}

        notes_content = {}
        errors = []

        for filename in filenames:
            filepath = os.path.join(AGENT_NOTES_DIR, filename)
            if not os.path.exists(filepath):
                errors.append(f"Note '{filename}' not found")
                continue
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    notes_content[filename] = f.read()
            except Exception as e:
                errors.append(f"Failed to read '{filename}': {str(e)}")

        result = {"status": "success", "notes": notes_content}
        if errors:
            result["errors"] = errors
        return result

    else:
        return {"status": "failure", "error": f"Invalid mode '{mode}'. Use 'list' or 'content'"}
    
# only xAI official API offers native X search, not available on openrouter endpoint
# use separate openai sdk
def grok_x_search(objectives: str) -> dict:
    """
    Invoke the X search sub-agent to search native X content about AI developments on X

    Args:
        objectives: the search objectives for the X search agent

    Returns:
        dict: a JSON containing the sug-agent's findings
    """
    from openai import OpenAI
    PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

    def _load_prompt(name: str) -> str:
        path = PROMPTS_DIR / name
        return path.read_text(encoding="utf-8")
    
    load_dotenv()

    xai = OpenAI(
        api_key=os.environ["XAI_API_KEY"],
        base_url="https://api.x.ai/v1",
    )

    resp = xai.responses.create(
        model="grok-4-1-fast-reasoning",
        input=[
            {"role": "system", "content": _load_prompt("x_grok_research_agent_instructions.md")},
            {"role": "user", "content": objectives},
        ],
        tools=[{"type": "x_search"}],  # enable only; agent decides args/calls
    )

    text_parts = []
    for item in resp.output:
        if getattr(item, "type", None) == "message":
            for part in item.content:
                if getattr(part, "type", None) == "output_text":
                    text_parts.append(part.text)

    return {
        "text": "".join(text_parts),
        "token_usage_info": get_token_budget_info(),
    }