from googleapiclient.discovery import build
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import os
import asyncio
from typing import List, Dict, Any

# GPT 5.2 has hard 272000 max input token limit
MAX_INPUT_TOKENS = 200000

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

def get_previous_research_result() -> str:
    """
    Look for the previous research result from storage.
    Reads the single MD file in research_history/ directory.

    Returns:
        str: The content of the previous research result, or an error message if not found.
    """
    research_dir = os.path.join(os.getcwd(), "research_history")

    # Ensure the directory exists
    if not os.path.exists(research_dir):
        return "No previous research results available."

    # Find all markdown files in the directory
    md_files = [f for f in os.listdir(research_dir) if f.endswith('.md')]

    if len(md_files) == 0:
        return "No previous research results found. Start without previous reference."

    if len(md_files) > 1:
        # Sort by modification time to get the most recent one
        md_files_with_time = [(f, os.path.getmtime(os.path.join(research_dir, f))) for f in md_files]
        md_files_with_time.sort(key=lambda x: x[1], reverse=True)
        latest_file = md_files_with_time[0][0]
    else:
        latest_file = md_files[0]

    # Read the file content
    file_path = os.path.join(research_dir, latest_file)
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return content
    except Exception as e:
        return f"Error reading previous research result from {latest_file}: {str(e)}"

# Content size limit for fetched pages (in characters) - ~50KB of text
MAX_CONTENT_SIZE = 50000


def _truncate_content(content: str, max_size: int = MAX_CONTENT_SIZE) -> str:
    """Truncate content to max size, preserving complete sentences where possible."""
    if not content or len(content) <= max_size:
        return content

    # Try to cut at a sentence boundary
    truncated = content[:max_size]
    last_period = truncated.rfind('. ')
    last_newline = truncated.rfind('\n')
    cut_point = max(last_period, last_newline)

    if cut_point > max_size * 0.8:  # Only use boundary if it's not too far back
        return truncated[:cut_point + 1] + "\n\n[Content truncated...]"
    return truncated + "\n\n[Content truncated...]"


def fetch_page_content(urls: list[str]) -> dict:
    """
    A tool for the agent to fetch page content from a list of URLs, and organize it in an LLM-friendly way.
    Optimized for low-memory environments by processing URLs one at a time with browser restart.

    Args:
        urls (list[str]): A list of URLs to fetch content from.

    Returns:
        A list containing all the fetched and organized content.
    """

    # Basic validation
    if not urls:
        return []

    # Ensure Crawl4AI writes inside the workspace (sandbox safe)
    crawl_base_dir = os.environ.get("CRAWL4_AI_BASE_DIRECTORY")
    if not crawl_base_dir:
        crawl_base_dir = os.path.join(os.getcwd(), ".crawl4ai_cache")
        os.environ["CRAWL4_AI_BASE_DIRECTORY"] = crawl_base_dir
    os.makedirs(crawl_base_dir, exist_ok=True)

    async def _crawl_single_url(url: str) -> Dict[str, Any]:
        """Crawl a single URL with its own browser instance for memory efficiency."""
        from crawl4ai import (
            AsyncWebCrawler,
            BrowserConfig,
            CrawlerRunConfig,
            CacheMode,
            DefaultMarkdownGenerator,
            PruningContentFilter,
        )

        # Memory-optimized browser configuration for low-RAM environments
        browser_config = BrowserConfig(
            headless=True,
            verbose=False,
            extra_args=[
                "--disable-dev-shm-usage",  # Use /tmp instead of /dev/shm (critical for low RAM)
                "--single-process",          # Run in single process to reduce memory
                "--no-zygote",               # Disable zygote process
                "--disable-gpu",             # Disable GPU acceleration
                "--disable-extensions",      # No extensions
                "--disable-plugins",         # No plugins
                "--disable-software-rasterizer",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--no-first-run",
                "--safebrowsing-disable-auto-update",
                "--js-flags=--max-old-space-size=128",  # Limit JS heap to 128MB
            ],
        )

        # Aggressive content pruning - prioritize text, omit noise
        md_generator = DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(
                threshold=0.55,  # More aggressive pruning for cleaner/smaller content
                threshold_type="fixed",
            ),
            options={
                "ignore_links": True,
                "ignore_images": True,
                "ignore_videos": True,
                "ignore_audio": True,
                "ignore_forms": True,
                "body_width": 0,
            },
            content_source="fit_html",
        )

        run_config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            screenshot=False,
            pdf=False,
            stream=False,
            verbose=False,
            markdown_generator=md_generator,
            # Skip non-text content to save memory
            excluded_tags=["script", "style", "noscript", "iframe", "svg", "canvas", "video", "audio", "img", "picture", "figure"],
            remove_overlay_elements=True,
        )

        # Browser-like headers to avoid bot detection
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        try:
            # Each URL gets its own browser instance that is fully closed after use
            async with AsyncWebCrawler(
                config=browser_config,
                base_directory=crawl_base_dir,
                headers=headers,
            ) as crawler:
                crawl_container = await crawler.arun(url=url, config=run_config)
                crawl_result = crawl_container[0] if len(crawl_container) else None

                if crawl_result and crawl_result.success:
                    content_text = ""
                    if crawl_result.markdown:
                        # Try fit_markdown first (best quality), then raw_markdown
                        content_text = getattr(
                            crawl_result.markdown,
                            "fit_markdown",
                            None,
                        ) or getattr(
                            crawl_result.markdown,
                            "raw_markdown",
                            None,
                        ) or ""

                    # Fallback to cleaned_html if markdown is empty
                    if not content_text and hasattr(crawl_result, "cleaned_html"):
                        content_text = crawl_result.cleaned_html or ""

                    # Apply content size limit
                    content_text = _truncate_content(content_text.strip()) if content_text else ""

                    return {
                        "url": url,
                        "redirected_url": getattr(crawl_result, "redirected_url", None) or url,
                        "title": (crawl_result.metadata or {}).get("title") if crawl_result.metadata else None,
                        "status": "success",
                        "status_code": getattr(crawl_result, "status_code", None),
                        "content": content_text,
                    }
                else:
                    error_message = getattr(crawl_result, "error_message", "Unknown error") if crawl_result else "Empty crawl result"
                    return {
                        "url": url,
                        "status": "failure",
                        "error": error_message,
                    }
        except Exception as e:
            return {
                "url": url,
                "status": "failure",
                "error": str(e),
            }

    async def _crawl_all(target_urls: List[str]) -> List[Dict[str, Any]]:
        """Process URLs one at a time to minimize peak memory usage."""
        results: List[Dict[str, Any]] = []
        for url in target_urls:
            result = await _crawl_single_url(url)
            results.append(result)
            # Force garbage collection between URLs to free browser memory
            import gc
            gc.collect()
        return results

    # Check if we're already in an async context
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        # We're inside an async context - run in a separate thread with its own loop
        import concurrent.futures

        def _run_in_thread():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                return new_loop.run_until_complete(_crawl_all(urls))
            finally:
                new_loop.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_run_in_thread)
            crawl_results = future.result()
    else:
        # Not in async context - can use asyncio.run directly
        try:
            crawl_results = asyncio.run(_crawl_all(urls))
        except Exception as e:
            return [{"url": None, "status": "failure", "error": f"Error running crawler: {str(e)}"}]

    return {
        "web_page_content": crawl_results,
        "token_usage_info": get_token_budget_info()
    }

def verify_urls(urls: list[str]) -> list[dict]:
    """
    Verify that URLs are alive and accessible by performing HEAD requests.
    Use this to validate source URLs before including them in your final output.

    Args:
        urls (list[str]): A list of URLs to verify.

    Returns:
        A list of dicts, each with 'url', 'valid' (bool), and 'status_code' or 'error'.
    """
    import requests

    results = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    for url in urls:
        try:
            # Use HEAD request for lightweight check; fall back to GET if HEAD fails
            resp = requests.head(url, headers=headers, timeout=10, allow_redirects=True)
            if resp.status_code == 405:  # Method not allowed, try GET
                resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True, stream=True)
                resp.close()

            valid = resp.status_code < 400
            results.append({
                "url": url,
                "valid": valid,
                "status_code": resp.status_code,
            })
        except requests.exceptions.Timeout:
            results.append({
                "url": url,
                "valid": False,
                "error": "timeout",
            })
        except requests.exceptions.RequestException as e:
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