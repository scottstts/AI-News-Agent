from googleapiclient.discovery import build
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import os
import asyncio
from typing import List, Dict, Any


def get_date() -> str:
    """Returns the current date in YYYY-MM-DD format."""
    return datetime.now().strftime("%Y-%m-%d")

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

def fetch_page_content(urls: list[str]) -> dict:
    """
    A tool for the agent to fetch page content from a list of URLs, and organize it in an LLM-friendly way.

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

    async def _crawl(target_urls: List[str]) -> List[Dict[str, Any]]:
        from crawl4ai import (
            AsyncWebCrawler,
            CrawlerRunConfig,
            CacheMode,
            DefaultMarkdownGenerator,
            PruningContentFilter,
        )

        results: List[Dict[str, Any]] = []

        # Use BYPASS to avoid stale cache; caller cares about fresh content
        # Prefer fit_html-based markdown (pruned) and suppress links/media noise
        md_generator = DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(
                threshold=0.48,  # More aggressive pruning for cleaner content
                threshold_type="fixed",
            ),
            options={
                "ignore_links": True,
                "ignore_images": True,
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
        )

        # Browser-like headers to avoid bot detection
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        async with AsyncWebCrawler(
            base_directory=crawl_base_dir,
            headers=headers,
            page_timeout=30000,
        ) as crawler:
            for url in target_urls:
                try:
                    crawl_container = await crawler.arun(url=url, config=run_config)
                    # arun returns CrawlResultContainer with single CrawlResult
                    crawl_result = crawl_container[0] if len(crawl_container) else None

                    if crawl_result and crawl_result.success:
                        content_text = ""
                        if crawl_result.markdown:
                            # Try fit_markdown first (best quality), then raw_markdown, then fallback to cleaned_html
                            content_text = getattr(
                                crawl_result.markdown,
                                "fit_markdown",
                                None,
                            ) or getattr(
                                crawl_result.markdown,
                                "raw_markdown",
                                None,
                            ) or getattr(
                                crawl_result,
                                "cleaned_html",
                                str(crawl_result.markdown),
                            )

                        # Fallback to cleaned_html if markdown is empty
                        if not content_text and hasattr(crawl_result, "cleaned_html"):
                            content_text = crawl_result.cleaned_html

                        results.append({
                            "url": url,
                            "redirected_url": getattr(crawl_result, "redirected_url", None) or url,
                            "title": (crawl_result.metadata or {}).get("title") if crawl_result.metadata else None,
                            "status": "success",
                            "status_code": getattr(crawl_result, "status_code", None),
                            "content": content_text.strip() if content_text else "",
                        })
                    else:
                        error_message = getattr(crawl_result, "error_message", "Unknown error") if crawl_result else "Empty crawl result"
                        results.append({
                            "url": url,
                            "status": "failure",
                            "error": error_message,
                        })
                except Exception as e:
                    results.append({
                        "url": url,
                        "status": "failure",
                        "error": str(e),
                    })

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
                return new_loop.run_until_complete(_crawl(urls))
            finally:
                new_loop.close()

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(_run_in_thread)
            crawl_results = future.result()
    else:
        # Not in async context - can use asyncio.run directly
        try:
            crawl_results = asyncio.run(_crawl(urls))
        except Exception as e:
            return [{"url": None, "status": "failure", "error": f"Error running crawler: {str(e)}"}]

    return crawl_results

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