import asyncio
from typing import List, Dict, Any
import os

from .tools import get_token_budget_info

# Content size limit for fetched pages (in characters) - ~50KB of text
MAX_CONTENT_SIZE = 50000

# User-Agent rotation pool for avoiding bot detection
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


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


def _get_random_user_agent() -> str:
    """Return a random User-Agent from the pool."""
    import random
    return random.choice(USER_AGENTS)


def _fetch_with_requests(url: str, timeout: int = 15) -> Dict[str, Any]:
    """
    Lightweight fallback fetcher using requests + basic HTML parsing.
    Used when the full browser-based crawl fails (403, blocked, etc.).
    """
    import requests
    from html.parser import HTMLParser
    import re

    class SimpleHTMLTextExtractor(HTMLParser):
        """Simple HTML parser that extracts text content."""
        def __init__(self):
            super().__init__()
            self.text_parts = []
            self.skip_tags = {'script', 'style', 'noscript', 'header', 'footer', 'nav', 'aside'}
            self.current_skip = 0
            self.title = None
            self.in_title = False

        def handle_starttag(self, tag, attrs):
            if tag in self.skip_tags:
                self.current_skip += 1
            if tag == 'title':
                self.in_title = True

        def handle_endtag(self, tag):
            if tag in self.skip_tags and self.current_skip > 0:
                self.current_skip -= 1
            if tag == 'title':
                self.in_title = False

        def handle_data(self, data):
            if self.in_title and not self.title:
                self.title = data.strip()
            elif self.current_skip == 0:
                text = data.strip()
                if text:
                    self.text_parts.append(text)

        def get_text(self) -> str:
            return '\n'.join(self.text_parts)

    headers = {
        "User-Agent": _get_random_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )

        if response.status_code >= 400:
            return {
                "url": url,
                "status": "failure",
                "status_code": response.status_code,
                "error": f"HTTP {response.status_code}",
            }

        content_type = response.headers.get('content-type', '')
        if 'text/html' not in content_type and 'application/xhtml' not in content_type:
            return {
                "url": url,
                "status": "failure",
                "error": f"Non-HTML content type: {content_type}",
            }

        # Parse HTML and extract text
        parser = SimpleHTMLTextExtractor()
        try:
            parser.feed(response.text)
        except Exception:
            # If parsing fails, try to extract text with regex fallback
            text = re.sub(r'<[^>]+>', ' ', response.text)
            text = re.sub(r'\s+', ' ', text).strip()
            return {
                "url": url,
                "redirected_url": response.url if response.url != url else None,
                "title": None,
                "status": "success",
                "status_code": response.status_code,
                "content": _truncate_content(text),
                "fetcher": "requests_regex_fallback",
            }

        content = parser.get_text()

        return {
            "url": url,
            "redirected_url": response.url if response.url != url else None,
            "title": parser.title,
            "status": "success",
            "status_code": response.status_code,
            "content": _truncate_content(content),
            "fetcher": "requests",
        }

    except requests.exceptions.Timeout:
        return {"url": url, "status": "failure", "error": "timeout"}
    except requests.exceptions.RequestException as e:
        return {"url": url, "status": "failure", "error": str(e)}


def _normalize_url(url: str) -> str:
    """Ensure URL has a scheme (https:// by default)."""
    url = url.strip()
    if not url:
        return url
    # Check if URL already has a scheme
    if not url.startswith(('http://', 'https://', '//')):
        return f'https://{url}'
    # Handle protocol-relative URLs
    if url.startswith('//'):
        return f'https:{url}'
    return url


def fetch_page_content(urls: list[str], max_parallel: int = 5) -> dict:
    """
    A tool for the agent to fetch page content from a list of URLs, and organize it in an LLM-friendly way.
    Uses parallel fetching with retry logic and fallback mechanisms for robustness.

    Args:
        urls (list[str]): A list of URLs to fetch content from.
        max_parallel (int): Maximum number of URLs to fetch in parallel (default: 5).

    Returns:
        A dict with 'web_page_content' (list of results) and 'token_usage_info'.
    """
    import random
    import time as time_module

    # Basic validation
    if not urls:
        return {"web_page_content": [], "token_usage_info": get_token_budget_info()}

    # Normalize URLs - add https:// if missing
    urls = [_normalize_url(u) for u in urls]

    # Ensure Crawl4AI writes inside the workspace (sandbox safe)
    crawl_base_dir = os.environ.get("CRAWL4_AI_BASE_DIRECTORY")
    if not crawl_base_dir:
        crawl_base_dir = os.path.join(os.getcwd(), ".crawl4ai_cache")
        os.environ["CRAWL4_AI_BASE_DIRECTORY"] = crawl_base_dir
    os.makedirs(crawl_base_dir, exist_ok=True)

    # Retry configuration
    MAX_RETRIES = 2
    RETRY_DELAY_BASE = 1.0  # seconds

    async def _crawl_single_url_with_retry(url: str, semaphore: asyncio.Semaphore) -> Dict[str, Any]:
        """
        Crawl a single URL with retry logic and fallback to requests-based fetcher.
        Uses semaphore to limit concurrent browser instances.
        """
        from crawl4ai import (
            AsyncWebCrawler,
            BrowserConfig,
            CrawlerRunConfig,
            CacheMode,
            DefaultMarkdownGenerator,
            PruningContentFilter,
        )

        # Browser configuration - optimized for compatibility, not memory
        browser_config = BrowserConfig(
            headless=True,
            verbose=False,
            extra_args=[
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-plugins",
                "--no-first-run",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
            ],
        )

        # Content pruning - balanced for quality and size
        md_generator = DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(
                threshold=0.48,  # Slightly less aggressive for better content retention
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

        # JavaScript to handle cookie consent popups and other overlays
        # This runs before we wait for content, clicking common "Accept" buttons
        dismiss_overlays_js = """
        (async () => {
            await new Promise(r => setTimeout(r, 1000));
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                const text = (btn.innerText || '').toLowerCase();
                if ((text.includes('accept') || text.includes('agree') || text.includes('allow all'))
                    && btn.offsetParent !== null) {
                    btn.click();
                    await new Promise(r => setTimeout(r, 500));
                    break;
                }
            }
        })();
        """

        # JavaScript wait condition - waits for meaningful content to appear
        # This handles SPA/React sites that render content dynamically
        wait_for_content_js = """() => {
            const body = document.body;
            if (!body) return false;
            const text = body.innerText || '';
            return text.length > 1000;
        }"""

        run_config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            screenshot=False,
            pdf=False,
            stream=False,
            verbose=False,
            markdown_generator=md_generator,
            excluded_tags=["script", "style", "noscript", "iframe", "svg", "canvas", "video", "audio", "img", "picture", "figure"],
            remove_overlay_elements=True,
            # Execute JS to dismiss cookie banners
            js_code=dismiss_overlays_js,
            # Wait for JS content to render
            wait_for=f"js:{wait_for_content_js}",
            page_timeout=45000,  # 45 second timeout for slow JS sites
            delay_before_return_html=2.0,  # Extra wait for final renders
        )

        # Rotate User-Agent for each request
        headers = {
            "User-Agent": _get_random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
        }

        last_error = None

        # Try browser-based crawl with retries
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with semaphore:
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
                                content_text = getattr(
                                    crawl_result.markdown,
                                    "fit_markdown",
                                    None,
                                ) or getattr(
                                    crawl_result.markdown,
                                    "raw_markdown",
                                    None,
                                ) or ""

                            if not content_text and hasattr(crawl_result, "cleaned_html"):
                                content_text = crawl_result.cleaned_html or ""

                            content_text = _truncate_content(content_text.strip()) if content_text else ""

                            # Check if we got meaningful content
                            if content_text and len(content_text) > 100:
                                return {
                                    "url": url,
                                    "redirected_url": getattr(crawl_result, "redirected_url", None) or url,
                                    "title": (crawl_result.metadata or {}).get("title") if crawl_result.metadata else None,
                                    "status": "success",
                                    "status_code": getattr(crawl_result, "status_code", None),
                                    "content": content_text,
                                    "fetcher": "crawl4ai",
                                }

                        # Crawl didn't return meaningful content
                        status_code = getattr(crawl_result, "status_code", None) if crawl_result else None
                        last_error = getattr(crawl_result, "error_message", "No content extracted") if crawl_result else "Empty result"

                        # For 403/404/5xx, try fallback immediately instead of retrying
                        if status_code and (status_code == 403 or status_code == 404 or status_code >= 500):
                            break

            except Exception as e:
                last_error = str(e)

            # Exponential backoff before retry
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY_BASE * (2 ** attempt) + random.uniform(0, 0.5)
                await asyncio.sleep(delay)
                # Rotate User-Agent for retry
                headers["User-Agent"] = _get_random_user_agent()

        # Browser-based crawl failed - try lightweight requests-based fallback
        fallback_result = _fetch_with_requests(url)
        if fallback_result.get("status") == "success":
            return fallback_result

        # Both methods failed
        return {
            "url": url,
            "status": "failure",
            "error": f"Browser crawl failed: {last_error}; Requests fallback: {fallback_result.get('error', 'unknown')}",
        }

    async def _wait_for_tasks_to_clear(loop: asyncio.AbstractEventLoop, timeout: float = 30.0) -> None:
        """Wait for all pending tasks (except current) to complete, with timeout."""
        current_task = asyncio.current_task(loop)
        start = time_module.monotonic()
        while True:
            pending = [t for t in asyncio.all_tasks(loop) if t is not current_task and not t.done()]
            if not pending:
                break
            if time_module.monotonic() - start > timeout:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                break
            await asyncio.sleep(0.05)

    async def _crawl_all_parallel(target_urls: List[str], max_concurrent: int) -> List[Dict[str, Any]]:
        """Process URLs in parallel with controlled concurrency."""
        semaphore = asyncio.Semaphore(max_concurrent)
        tasks = [_crawl_single_url_with_retry(url, semaphore) for url in target_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Convert exceptions to error dicts
        processed_results = []
        for url, result in zip(target_urls, results):
            if isinstance(result, Exception):
                processed_results.append({
                    "url": url,
                    "status": "failure",
                    "error": str(result),
                })
            else:
                processed_results.append(result)

        return processed_results

    def _cleanup_loop(loop: asyncio.AbstractEventLoop) -> None:
        """
        Comprehensive event loop cleanup that handles subprocess transports.
        This prevents 'Event loop is closed' errors from Playwright on Linux.
        """
        import gc

        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass

        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass

        try:
            loop.run_until_complete(loop.shutdown_default_executor())
        except Exception:
            pass

        gc.collect()

        try:
            loop.run_until_complete(asyncio.sleep(0))
        except Exception:
            pass

        gc.collect()
        loop.close()

        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass

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
                result = new_loop.run_until_complete(_crawl_all_parallel(urls, max_parallel))
                new_loop.run_until_complete(_wait_for_tasks_to_clear(new_loop, timeout=30.0))
                return result
            finally:
                _cleanup_loop(new_loop)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_run_in_thread)
            crawl_results = future.result()
    else:
        # Not in async context - can use asyncio.run directly
        try:
            crawl_results = asyncio.run(_crawl_all_parallel(urls, max_parallel))
        except Exception as e:
            return {"web_page_content": [{"url": None, "status": "failure", "error": f"Error running crawler: {str(e)}"}], "token_usage_info": get_token_budget_info()}

    return {
        "web_page_content": crawl_results,
        "token_usage_info": get_token_budget_info()
    }