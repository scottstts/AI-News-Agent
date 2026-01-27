import asyncio
from typing import List, Dict, Any, Tuple
from collections import defaultdict
from urllib.parse import urlparse
import os
import time
import random
import re
import hashlib
import json

from .tools import get_token_budget_info

# Content size limit for fetched pages (in characters) - ~50KB of text
MAX_CONTENT_SIZE = 50000

# Domain rate limiting configuration
DOMAIN_MIN_DELAY = 2.0  # Minimum seconds between requests to the same domain
DOMAIN_MAX_DELAY = 4.0  # Maximum seconds (for jitter) between requests to the same domain
DOMAIN_MAX_CONCURRENT = 2  # Max concurrent requests per domain

# Wayback Machine rate limiting (archive.org can block aggressive scraping)
ARCHIVE_MIN_DELAY = 3.0  # Minimum seconds between archive.org requests
_archive_last_request_time = 0.0  # Module-level tracker for archive.org rate limiting

# Archive cache configuration (avoids re-hitting archive.org for same URLs)
ARCHIVE_CACHE_DIR = ".archive_cache"
ARCHIVE_CACHE_TTL = 86400  # Cache TTL in seconds (24 hours)

# User-Agent + TLS fingerprint pairs (matched by OS for consistency)
# Format: (User-Agent, curl_cffi impersonate profile)
# Updated to 2025-era browser versions for better compatibility
# NOTE: curl_cffi impersonate profiles lag behind UA versions. When curl_cffi releases
# newer profiles (e.g., chrome131, chrome132), update the second element of each tuple.
# Check available profiles: https://github.com/yifeikong/curl_cffi#supported-impersonate-targets
USER_AGENT_PROFILES = [
    # Desktop - Chrome on Mac (2025 versions)
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "chrome120"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36", "chrome120"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36", "chrome120"),
    # Desktop - Chrome on Windows (2025 versions)
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "chrome120"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36", "chrome120"),
    # Desktop - Safari on Mac (2025 versions)
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15", "safari15_5"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15", "safari15_5"),
    # Desktop - Chrome on Linux
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "chrome120"),
    # Mobile - Chrome on Android (2025 versions)
    ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36", "chrome120"),
    ("Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36", "chrome120"),
    # Mobile - Safari on iPhone (2025 versions)
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1", "safari15_5"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1", "safari15_5"),
]

# Realistic referer headers to mimic traffic from search/social
REFERER_SOURCES = [
    "https://www.google.com/",
    "https://www.google.com/search?q=",
    "https://www.bing.com/search?q=",
    "https://t.co/",  # Twitter/X short links
    "https://www.reddit.com/",
    "https://news.ycombinator.com/",
    "https://duckduckgo.com/",
    None,  # Sometimes no referer is more natural
]

# Common viewport sizes for fingerprint randomization
VIEWPORT_SIZES = [
    (1920, 1080),
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1280, 720),
    (1600, 900),
    (2560, 1440),
]

# Phrases that indicate a soft block (bot detection page, not actual 404)
SOFT_BLOCK_INDICATORS = [
    "security check",
    "cloudflare",
    "captcha",
    "please verify",
    "access denied",
    "pardon our interruption",
    "unusual traffic",
    "bot detected",
    "please enable javascript",
    "checking your browser",
    "ddos protection",
    "are you a robot",
    "human verification",
    "just a moment",
    "attention required",
    "ray id",  # Cloudflare Ray ID
    # Newer Cloudflare/PerimeterX/hCaptcha/Turnstile phrases
    "cf-browser-verification",
    "cf-challenge",
    "cf-please-wait",
    "cf-chl-bypass",
    "one moment please",
    "you are being redirected",
    "checking if the site connection is secure",
    "verify you are human",
    "please complete the security check",
    "press & hold",
    "perimeterx",
    "powered by perimeterx",
    "px-captcha",
    "hcaptcha",
    "turnstile",
    # Paywall/login/soft-404 indicators
    "subscribe to read",
    "sign in to read",
    "sign in to continue",
    "create an account to continue",
    "article not found",
    "page has been removed",
    "this content is no longer available",
    "members only",
    "premium content",
]


class DomainRateLimiter:
    """Rate limiter that ensures we don't overwhelm individual domains."""

    def __init__(self, min_delay: float = DOMAIN_MIN_DELAY, max_delay: float = DOMAIN_MAX_DELAY, max_concurrent: int = DOMAIN_MAX_CONCURRENT):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_concurrent = max_concurrent
        self._last_request_time: Dict[str, float] = defaultdict(float)
        self._domain_semaphores: Dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    def _get_domain(self, url: str) -> str:
        """Extract domain from URL."""
        try:
            parsed = urlparse(url)
            return parsed.netloc.lower()
        except Exception:
            return url

    async def _get_semaphore(self, domain: str) -> asyncio.Semaphore:
        """Get or create a semaphore for a domain."""
        async with self._lock:
            if domain not in self._domain_semaphores:
                self._domain_semaphores[domain] = asyncio.Semaphore(self.max_concurrent)
            return self._domain_semaphores[domain]

    async def acquire(self, url: str) -> None:
        """Acquire permission to make a request to the given URL's domain."""
        domain = self._get_domain(url)
        semaphore = await self._get_semaphore(domain)

        await semaphore.acquire()

        # Enforce randomized delay between requests to the same domain (more human-like)
        async with self._lock:
            last_time = self._last_request_time[domain]
            now = time.monotonic()
            # Use random jitter between min and max delay for more human-like behavior
            target_delay = random.uniform(self.min_delay, self.max_delay)
            wait_time = max(0, target_delay - (now - last_time))
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            self._last_request_time[domain] = time.monotonic()

    async def release(self, url: str) -> None:
        """Release the domain semaphore after request completes."""
        domain = self._get_domain(url)
        semaphore = await self._get_semaphore(domain)
        semaphore.release()


class SimpleHTMLTextExtractor:
    """
    Simple HTML parser that extracts text content.
    Shared by multiple fetcher functions to avoid code duplication.
    """
    from html.parser import HTMLParser

    class _Parser(HTMLParser):
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

    @classmethod
    def extract(cls, html: str) -> Tuple[str, str | None]:
        """
        Extract text and title from HTML.
        Returns (text_content, title).
        """
        parser = cls._Parser()
        parser.feed(html)
        return parser.get_text(), parser.title


def _get_matched_profile() -> Tuple[str, str]:
    """Return a matched User-Agent and TLS fingerprint profile pair."""
    return random.choice(USER_AGENT_PROFILES)


def _get_random_referer() -> str | None:
    """Return a random referer header to mimic search/social traffic."""
    return random.choice(REFERER_SOURCES)


def _get_random_viewport() -> Tuple[int, int]:
    """Return a random viewport size for fingerprint variation."""
    return random.choice(VIEWPORT_SIZES)


def _is_soft_block(content: str, status_code: int | None) -> bool:
    """
    Detect if a page is a soft block (bot detection) rather than actual content.
    Many sites return 200/404 with a challenge page instead of the real content.
    """
    if not content:
        return False

    content_lower = content.lower()

    # Check for soft block indicators
    indicator_count = sum(1 for indicator in SOFT_BLOCK_INDICATORS if indicator in content_lower)

    # If multiple indicators found, likely a block page
    if indicator_count >= 2:
        return True

    # Very short content with any indicator is suspicious
    if len(content) < 1000 and indicator_count >= 1:
        return True

    # 403/404 with block indicators
    if status_code in (403, 404) and indicator_count >= 1:
        return True

    return False


def _get_archive_cache_path(url: str) -> str:
    """Generate cache file path for a URL."""
    cache_key = hashlib.md5(url.encode()).hexdigest()
    return os.path.join(ARCHIVE_CACHE_DIR, cache_key + ".json")


def _get_cached_archive_result(url: str) -> Dict[str, Any] | None:
    """
    Check if we have a valid cached archive result for this URL.
    Returns the cached result dict if valid, None otherwise.
    """
    cache_path = _get_archive_cache_path(url)
    
    if not os.path.exists(cache_path):
        return None
    
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cached_data = json.load(f)
        
        # Check TTL
        cached_time = cached_data.get("cached_at", 0)
        if time.time() - cached_time > ARCHIVE_CACHE_TTL:
            # Cache expired, remove it
            os.remove(cache_path)
            return None
        
        # Return cached result
        return {
            "url": url,
            "archive_url": cached_data.get("archive_url"),
            "title": cached_data.get("title"),
            "status": "success",
            "content": cached_data.get("content"),
            "fetcher": "archive_cached",
        }
    except (json.JSONDecodeError, OSError, KeyError):
        # Invalid cache file, remove it
        try:
            os.remove(cache_path)
        except OSError:
            pass
        return None


def _cache_archive_result(url: str, archive_url: str, title: str | None, content: str) -> None:
    """Cache a successful archive fetch result."""
    os.makedirs(ARCHIVE_CACHE_DIR, exist_ok=True)
    cache_path = _get_archive_cache_path(url)
    
    cache_data = {
        "url": url,
        "archive_url": archive_url,
        "title": title,
        "content": content,
        "cached_at": time.time(),
    }
    
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False)
    except OSError:
        # Failed to write cache, not critical
        pass


def _fetch_from_archive(url: str, timeout: int = 20) -> Dict[str, Any]:
    """
    Fetch content from Internet Archive (Wayback Machine) as a fallback.
    Bypasses target site's WAF completely by fetching cached version.
    Includes rate limiting to avoid being blocked by archive.org.
    Uses local file cache to avoid repeated requests for the same URLs.
    Uses CDX API to find the best snapshot with HTTP 200 status.
    """
    global _archive_last_request_time
    from curl_cffi import requests as curl_requests
    from urllib.parse import quote

    # Check cache first (use original URL for cache key)
    cached_result = _get_cached_archive_result(url)
    if cached_result:
        return cached_result

    # Strip URL fragments - Wayback often has better snapshots without them
    clean_url = url.split('#')[0]

    # Rate limit archive.org requests to avoid being blocked
    now = time.monotonic()
    time_since_last = now - _archive_last_request_time
    if time_since_last < ARCHIVE_MIN_DELAY:
        time.sleep(ARCHIVE_MIN_DELAY - time_since_last)
    _archive_last_request_time = time.monotonic()

    user_agent, impersonate = _get_matched_profile()

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # Try CDX API to find the best snapshot (most recent with 200 status)
    archive_url = None
    best_timestamp = None
    
    try:
        cdx_url = f"https://web.archive.org/cdx/search/cdx?url={quote(clean_url, safe='')}&output=json&fl=timestamp,statuscode&limit=20"
        cdx_response = curl_requests.get(
            cdx_url,
            headers=headers,
            timeout=10,
            impersonate=impersonate,
        )
        
        if cdx_response.status_code == 200:
            cdx_data = cdx_response.json()
            # First row is header: ["timestamp", "statuscode"]
            if len(cdx_data) > 1:
                # Find most recent 200-status snapshot (iterate backwards for most recent first)
                for row in reversed(cdx_data[1:]):
                    if len(row) >= 2 and row[1] == "200":
                        best_timestamp = row[0]
                        break
                
                if best_timestamp:
                    archive_url = f"https://web.archive.org/web/{best_timestamp}/{clean_url}"
    except Exception:
        # CDX lookup failed, fall back to /web/2/
        pass
    
    # Fall back to /web/2/ if CDX didn't find a good snapshot
    if not archive_url:
        archive_url = f"https://web.archive.org/web/2/{clean_url}"

    # Update headers for HTML fetch
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    headers["Accept-Encoding"] = "gzip, deflate, br"

    try:
        response = curl_requests.get(
            archive_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
            impersonate=impersonate,
        )

        if response.status_code >= 400:
            return {
                "url": url,
                "status": "failure",
                "error": f"Archive returned HTTP {response.status_code}",
            }

        # Parse HTML using shared extractor
        try:
            content, title = SimpleHTMLTextExtractor.extract(response.text)
        except Exception:
            text = re.sub(r'<[^>]+>', ' ', response.text)
            text = re.sub(r'\s+', ' ', text).strip()
            if text and len(text) > 200:
                truncated_text = _truncate_content(text)
                _cache_archive_result(url, archive_url, None, truncated_text)
                return {
                    "url": url,
                    "archive_url": archive_url,
                    "title": None,
                    "status": "success",
                    "content": truncated_text,
                    "fetcher": "archive_regex_fallback",
                }
            return {"url": url, "status": "failure", "error": "Archive content parsing failed"}

        if content and len(content) > 200:
            truncated_content = _truncate_content(content)
            _cache_archive_result(url, archive_url, title, truncated_content)
            return {
                "url": url,
                "archive_url": archive_url,
                "title": title,
                "status": "success",
                "content": truncated_content,
                "fetcher": "archive",
            }

        return {"url": url, "status": "failure", "error": "Archive returned empty content"}

    except Exception as e:
        return {"url": url, "status": "failure", "error": f"Archive fetch failed: {str(e)}"}


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
    user_agent, _ = _get_matched_profile()
    return user_agent


def _fetch_with_curl_cffi(url: str, timeout: int = 15) -> Dict[str, Any]:
    """
    Lightweight fallback fetcher using curl_cffi + basic HTML parsing.
    curl_cffi spoofs TLS fingerprints of real browsers, bypassing most Cloudflare/403 blocks.
    Uses matched User-Agent/TLS profiles and realistic referer headers.
    """
    from curl_cffi import requests as curl_requests

    # Get matched User-Agent and TLS fingerprint profile
    user_agent, impersonate = _get_matched_profile()

    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",  # Changed to cross-site when using referer
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }

    # Add realistic referer header
    referer = _get_random_referer()
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "cross-site"
    else:
        headers["Sec-Fetch-Site"] = "none"

    try:
        response = curl_requests.get(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
            impersonate=impersonate,  # Matched TLS fingerprint
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

        # Parse HTML and extract text using shared extractor
        try:
            content, title = SimpleHTMLTextExtractor.extract(response.text)
        except Exception:
            # If parsing fails, try to extract text with regex fallback
            text = re.sub(r'<[^>]+>', ' ', response.text)
            text = re.sub(r'\s+', ' ', text).strip()

            # Check for soft block
            if _is_soft_block(text, response.status_code):
                return {
                    "url": url,
                    "status": "failure",
                    "status_code": response.status_code,
                    "error": "soft_block_detected",
                    "is_soft_block": True,
                }

            return {
                "url": url,
                "redirected_url": str(response.url) if str(response.url) != url else None,
                "title": None,
                "status": "success",
                "status_code": response.status_code,
                "content": _truncate_content(text),
                "fetcher": "curl_cffi_regex_fallback",
            }

        # Check for soft block (bot detection page)
        if _is_soft_block(content, response.status_code):
            return {
                "url": url,
                "status": "failure",
                "status_code": response.status_code,
                "error": "soft_block_detected",
                "is_soft_block": True,
            }

        return {
            "url": url,
            "redirected_url": str(response.url) if str(response.url) != url else None,
            "title": title,
            "status": "success",
            "status_code": response.status_code,
            "content": _truncate_content(content),
            "fetcher": "curl_cffi",
        }

    except Exception as e:
        error_str = str(e).lower()
        if "timeout" in error_str:
            return {"url": url, "status": "failure", "error": "timeout"}
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

    # Domain rate limiter to prevent 429 errors from hitting same domain too fast
    domain_limiter = DomainRateLimiter(
        min_delay=DOMAIN_MIN_DELAY,
        max_concurrent=DOMAIN_MAX_CONCURRENT
    )

    async def _crawl_single_url_with_retry(url: str, semaphore: asyncio.Semaphore) -> Dict[str, Any]:
        """
        Crawl a single URL with retry logic and fallback to curl_cffi-based fetcher.
        Uses semaphore to limit concurrent browser instances and domain rate limiter
        to prevent overwhelming individual domains.
        """
        from crawl4ai import (
            AsyncWebCrawler,
            BrowserConfig,
            CrawlerRunConfig,
            CacheMode,
            DefaultMarkdownGenerator,
            PruningContentFilter,
        )

        # Randomize viewport for fingerprint evasion
        viewport_width, viewport_height = _get_random_viewport()

        # Browser configuration - optimized for stealth and compatibility
        # Try to enable stealth mode which patches navigator, WebGL fingerprints, etc.
        browser_config_kwargs = {
            "headless": True,
            "verbose": False,
            "text_mode": True,  # Optimized for text extraction
            "extra_args": [
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-plugins",
                "--no-first-run",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--disable-blink-features=AutomationControlled",  # Hide automation
                "--disable-infobars",
                f"--window-size={viewport_width},{viewport_height}",  # Randomized viewport
            ],
        }
        # Add stealth mode if available (patches navigator.webdriver, plugins, etc.)
        # Some versions may have import issues, so we try it and fall back gracefully
        try:
            browser_config = BrowserConfig(**browser_config_kwargs, stealth=True)
        except (TypeError, ImportError):
            # Stealth mode not available in this version, use config without it
            browser_config = BrowserConfig(**browser_config_kwargs)

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

        # JavaScript to handle cookie consent popups, scroll for lazy loading, and dismiss overlays
        # This runs before we wait for content
        page_interaction_js = """
        (async () => {
            // 0. Initial warm-up pause - some sites only load content after detecting human-like timing
            await new Promise(r => setTimeout(r, 1500));

            // 1. Dismiss cookie consent popups
            await new Promise(r => setTimeout(r, 1000));
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                const text = (btn.innerText || '').toLowerCase();
                if ((text.includes('accept') || text.includes('agree') || text.includes('allow all')
                    || text.includes('got it') || text.includes('i understand'))
                    && btn.offsetParent !== null) {
                    btn.click();
                    await new Promise(r => setTimeout(r, 500));
                    break;
                }
            }

            // 2. Smooth scroll to trigger lazy loading (simulates real user behavior)
            // Many sites like TechCrunch only load content as user scrolls
            const scrollStep = async (targetY, duration) => {
                const startY = window.scrollY;
                const distance = targetY - startY;
                const startTime = performance.now();

                return new Promise(resolve => {
                    const step = (currentTime) => {
                        const elapsed = currentTime - startTime;
                        const progress = Math.min(elapsed / duration, 1);
                        // Ease out cubic for natural feel
                        const easeProgress = 1 - Math.pow(1 - progress, 3);
                        window.scrollTo(0, startY + distance * easeProgress);

                        if (progress < 1) {
                            requestAnimationFrame(step);
                        } else {
                            resolve();
                        }
                    };
                    requestAnimationFrame(step);
                });
            };

            // Scroll down in chunks to trigger lazy loading
            const docHeight = Math.max(
                document.body.scrollHeight,
                document.documentElement.scrollHeight,
                5000  // minimum scroll distance
            );

            // Scroll to 25%, 50%, 75%, then 100% of page
            for (let i = 1; i <= 4; i++) {
                await scrollStep(docHeight * (i / 4), 300);
                await new Promise(r => setTimeout(r, 200));  // Wait for lazy content
            }

            // Scroll back to top
            await scrollStep(0, 200);

            // 3. Small random mouse movements to appear human-like
            const event = new MouseEvent('mousemove', {
                clientX: Math.random() * 500 + 100,
                clientY: Math.random() * 300 + 100,
                bubbles: true
            });
            document.dispatchEvent(event);
        })();
        """

        # JavaScript wait condition - waits for meaningful content to appear
        # This handles SPA/React sites that render content dynamically
        # Using 600 char threshold to filter out paywall/login interstitials while still accepting minimal valid pages
        wait_for_content_js = """() => {
            const body = document.body;
            if (!body) return false;
            const text = body.innerText || '';
            return text.length > 600;
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
            # Execute JS to dismiss cookie banners and scroll for lazy loading
            js_code=page_interaction_js,
            # Wait for JS content to render
            wait_for=f"js:{wait_for_content_js}",
            page_timeout=45000,  # 45 second timeout for slow JS sites
            delay_before_return_html=2.5,  # Extra wait for lazy-loaded content after scroll
        )

        # Get matched User-Agent for browser
        user_agent = _get_random_user_agent()

        # Add realistic referer header to mimic search/social traffic
        referer = _get_random_referer()

        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
        }

        if referer:
            headers["Referer"] = referer

        last_error = None

        # Try browser-based crawl with retries
        for attempt in range(MAX_RETRIES + 1):
            try:
                # Acquire domain rate limit before making request
                await domain_limiter.acquire(url)
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

                                # Check for soft blocks (bot detection pages that return 200)
                                status_code = getattr(crawl_result, "status_code", None)
                                if _is_soft_block(content_text, status_code):
                                    last_error = "soft_block_detected"
                                    break  # Try fallbacks

                                # Check if we got meaningful content
                                if content_text and len(content_text) > 100:
                                    return {
                                        "url": url,
                                        "redirected_url": getattr(crawl_result, "redirected_url", None) or url,
                                        "title": (crawl_result.metadata or {}).get("title") if crawl_result.metadata else None,
                                        "status": "success",
                                        "status_code": status_code,
                                        "content": content_text,
                                        "fetcher": "crawl4ai",
                                    }

                            # Crawl didn't return meaningful content
                            status_code = getattr(crawl_result, "status_code", None) if crawl_result else None
                            last_error = getattr(crawl_result, "error_message", "No content extracted") if crawl_result else "Empty result"

                            # For 403/404/5xx, try fallback immediately instead of retrying
                            if status_code and (status_code == 403 or status_code == 404 or status_code >= 500):
                                break
                finally:
                    await domain_limiter.release(url)

            except Exception as e:
                last_error = str(e)

            # Exponential backoff before retry with jittered base delay
            if attempt < MAX_RETRIES:
                # Randomize base delay for more human-like timing
                jittered_base = random.uniform(RETRY_DELAY_BASE * 0.5, RETRY_DELAY_BASE * 1.5)
                delay = jittered_base * (2 ** attempt) + random.uniform(0, 0.5)
                await asyncio.sleep(delay)
                # Rotate User-Agent for retry
                headers["User-Agent"] = _get_random_user_agent()

        # Browser-based crawl failed - try lightweight curl_cffi-based fallback
        # curl_cffi spoofs TLS fingerprints, bypassing most Cloudflare/bot detection
        # Run in thread to avoid blocking the event loop with sync HTTP
        fallback_result = await asyncio.to_thread(_fetch_with_curl_cffi, url)
        if fallback_result.get("status") == "success":
            return fallback_result

        # Check if this is a soft block (bot detection page) or hard block (403/404/429)
        is_blocked = (
            fallback_result.get("is_soft_block")
            or fallback_result.get("status_code") in (403, 404, 429)
            or "403" in str(last_error)
            or "404" in str(last_error)
        )

        # If blocked, try Internet Archive as final fallback
        # This bypasses the target site's WAF completely
        if is_blocked:
            # Run in thread to avoid blocking the event loop with sync HTTP + sleep
            archive_result = await asyncio.to_thread(_fetch_from_archive, url)
            if archive_result.get("status") == "success":
                return archive_result

        # All methods failed
        return {
            "url": url,
            "status": "failure",
            "error": f"Browser crawl: {last_error}; curl_cffi: {fallback_result.get('error', 'unknown')}; Archive: attempted" if is_blocked else f"Browser crawl: {last_error}; curl_cffi: {fallback_result.get('error', 'unknown')}",
        }

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

    # Check if we're already in an async context
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        # We're inside an async context - run in a separate thread with its own loop
        import concurrent.futures

        def _run_in_thread():
            return asyncio.run(_crawl_all_parallel(urls, max_parallel))

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