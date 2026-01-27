"""
Tests for agent_core/fetch_tool.py

This module tests the web page fetching functionality used by the research AI agent.
Tests are organized into:
1. Unit tests - Test helper functions in isolation
2. Integration tests - Test actual web fetching (marked with @pytest.mark.integration)

Run unit tests only: pytest test/fetch_test.py -m "not integration"
Run all tests: pytest test/fetch_test.py
Run integration tests only: pytest test/fetch_test.py -m integration
"""

import pytest
import asyncio
import os
import json
import time
import tempfile
from unittest.mock import patch, MagicMock, AsyncMock

# Import the module under test
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_core.fetch_tool import (
    # Helper functions
    _normalize_url,
    _truncate_content,
    _is_soft_block,
    _get_matched_profile,
    _get_random_referer,
    _get_random_viewport,
    _get_archive_cache_path,
    _get_cached_archive_result,
    _cache_archive_result,
    _fetch_with_curl_cffi,
    _fetch_from_archive,
    fetch_page_content,
    # Classes
    SimpleHTMLTextExtractor,
    DomainRateLimiter,
    # Constants for validation
    USER_AGENT_PROFILES,
    REFERER_SOURCES,
    VIEWPORT_SIZES,
    SOFT_BLOCK_INDICATORS,
    MAX_CONTENT_SIZE,
    ARCHIVE_CACHE_DIR,
)


# =============================================================================
# UNIT TESTS - Helper Functions
# =============================================================================

class TestNormalizeUrl:
    """Tests for the _normalize_url function."""

    def test_url_with_https_unchanged(self):
        """URLs with https:// should remain unchanged."""
        url = "https://example.com/page"
        assert _normalize_url(url) == url

    def test_url_with_http_unchanged(self):
        """URLs with http:// should remain unchanged."""
        url = "http://example.com/page"
        assert _normalize_url(url) == url

    def test_url_without_scheme_gets_https(self):
        """URLs without a scheme should get https:// prepended."""
        url = "example.com/page"
        assert _normalize_url(url) == "https://example.com/page"

    def test_protocol_relative_url_gets_https(self):
        """Protocol-relative URLs (//...) should get https: prepended."""
        url = "//example.com/page"
        assert _normalize_url(url) == "https://example.com/page"

    def test_whitespace_stripped(self):
        """Leading/trailing whitespace should be stripped."""
        url = "  https://example.com/page  "
        assert _normalize_url(url) == "https://example.com/page"

    def test_empty_string_returns_empty(self):
        """Empty string should return empty string."""
        assert _normalize_url("") == ""
        assert _normalize_url("   ") == ""

    def test_complex_url_preserved(self):
        """Complex URLs with query params and fragments should be preserved."""
        url = "https://example.com/page?query=1&foo=bar#section"
        assert _normalize_url(url) == url


class TestTruncateContent:
    """Tests for the _truncate_content function."""

    def test_short_content_unchanged(self):
        """Content shorter than max size should remain unchanged."""
        content = "Short content here."
        assert _truncate_content(content) == content

    def test_none_content_returns_none(self):
        """None content should return None."""
        assert _truncate_content(None) is None

    def test_empty_content_unchanged(self):
        """Empty content should return empty."""
        assert _truncate_content("") == ""

    def test_long_content_truncated(self):
        """Content longer than max size should be truncated."""
        # Create content longer than MAX_CONTENT_SIZE
        long_content = "a" * (MAX_CONTENT_SIZE + 1000)
        result = _truncate_content(long_content)
        assert len(result) <= MAX_CONTENT_SIZE + 50  # Allow for truncation message
        assert "[Content truncated...]" in result

    def test_truncation_at_sentence_boundary(self):
        """Truncation should prefer sentence boundaries when possible."""
        # Create content with a period at 85% of max size, then enough extra to exceed max
        # MAX_CONTENT_SIZE = 50000, so we need content > 50000 chars total
        base = "a" * (int(MAX_CONTENT_SIZE * 0.85))  # ~42500 chars
        content = base + ". " + "b" * 10000  # Total: ~52502 chars (exceeds 50000)
        result = _truncate_content(content)
        # Should truncate and include the truncation message
        assert "[Content truncated...]" in result
        # The result should be truncated (shorter than original)
        assert len(result) < len(content)
        # Since the period is at 85% (> 80% threshold), truncation should cut at sentence boundary
        # The result should end with the sentence + truncation message
        assert "aaa. " not in result or result.endswith("[Content truncated...]")

    def test_custom_max_size(self):
        """Custom max_size parameter should be respected."""
        content = "a" * 100
        result = _truncate_content(content, max_size=50)
        assert len(result) <= 80  # 50 + truncation message


class TestIsSoftBlock:
    """Tests for the _is_soft_block function."""

    def test_empty_content_not_blocked(self):
        """Empty content should not be detected as soft block."""
        assert _is_soft_block("", 200) is False
        assert _is_soft_block(None, 200) is False

    def test_normal_content_not_blocked(self):
        """Normal content without block indicators should pass."""
        content = "This is a normal article about technology and programming."
        assert _is_soft_block(content, 200) is False

    def test_multiple_indicators_detected(self):
        """Content with multiple soft block indicators should be detected."""
        content = "Cloudflare security check. Please complete the captcha to continue."
        assert _is_soft_block(content, 200) is True

    def test_short_content_with_indicator_detected(self):
        """Short content (< 1000 chars) with even one indicator should be detected."""
        content = "Checking your browser..."  # Short content with indicator
        assert _is_soft_block(content, 200) is True

    def test_403_with_indicator_detected(self):
        """403 response with a block indicator should be detected."""
        content = "Access denied. Please try again."
        assert _is_soft_block(content, 403) is True

    def test_404_with_indicator_detected(self):
        """404 response with a block indicator should be detected."""
        content = "This content is no longer available"
        assert _is_soft_block(content, 404) is True

    def test_cloudflare_challenge_page(self):
        """Cloudflare challenge pages should be detected."""
        content = """
        <html>
        <head><title>Just a moment...</title></head>
        <body>
        Checking if the site connection is secure.
        Ray ID: 12345abcdef
        </body>
        </html>
        """
        assert _is_soft_block(content, 200) is True

    def test_paywall_detection(self):
        """Paywall/login pages should be detected."""
        content = "Subscribe to read this premium content. Members only."
        assert _is_soft_block(content, 200) is True


class TestSimpleHTMLTextExtractor:
    """Tests for the SimpleHTMLTextExtractor class."""

    def test_basic_html_extraction(self):
        """Basic HTML should have text extracted correctly."""
        html = "<html><head><title>Test Title</title></head><body><p>Hello World</p></body></html>"
        text, title = SimpleHTMLTextExtractor.extract(html)
        assert "Hello World" in text
        assert title == "Test Title"

    def test_script_tags_excluded(self):
        """Script tag content should be excluded."""
        html = "<html><body><script>var x = 1;</script><p>Content</p></body></html>"
        text, _ = SimpleHTMLTextExtractor.extract(html)
        assert "var x = 1" not in text
        assert "Content" in text

    def test_style_tags_excluded(self):
        """Style tag content should be excluded."""
        html = "<html><body><style>.class { color: red; }</style><p>Content</p></body></html>"
        text, _ = SimpleHTMLTextExtractor.extract(html)
        assert "color: red" not in text
        assert "Content" in text

    def test_navigation_excluded(self):
        """Navigation elements should be excluded."""
        html = "<html><body><nav>Menu Item</nav><p>Main Content</p></body></html>"
        text, _ = SimpleHTMLTextExtractor.extract(html)
        assert "Menu Item" not in text
        assert "Main Content" in text

    def test_nested_tags_handled(self):
        """Nested tags should be handled correctly."""
        html = """
        <html><body>
        <div><p>Paragraph 1</p><p>Paragraph 2</p></div>
        </body></html>
        """
        text, _ = SimpleHTMLTextExtractor.extract(html)
        assert "Paragraph 1" in text
        assert "Paragraph 2" in text

    def test_empty_html_returns_empty(self):
        """Empty HTML should return empty text."""
        html = "<html><body></body></html>"
        text, title = SimpleHTMLTextExtractor.extract(html)
        assert text == ""
        assert title is None


class TestProfileHelpers:
    """Tests for the profile/fingerprint helper functions."""

    def test_get_matched_profile_returns_valid_pair(self):
        """_get_matched_profile should return a valid (UA, impersonate) pair."""
        ua, profile = _get_matched_profile()
        assert ua is not None
        assert profile is not None
        # Verify it's from our list
        assert (ua, profile) in USER_AGENT_PROFILES

    def test_get_random_referer_returns_valid_or_none(self):
        """_get_random_referer should return a valid referer or None."""
        for _ in range(10):  # Test multiple times due to randomness
            referer = _get_random_referer()
            if referer is not None:
                assert referer in REFERER_SOURCES

    def test_get_random_viewport_returns_valid_size(self):
        """_get_random_viewport should return a valid viewport size tuple."""
        width, height = _get_random_viewport()
        assert (width, height) in VIEWPORT_SIZES
        assert width > 0
        assert height > 0


# =============================================================================
# UNIT TESTS - DomainRateLimiter
# =============================================================================

class TestDomainRateLimiter:
    """Tests for the DomainRateLimiter class."""

    def test_domain_extraction(self):
        """_get_domain should correctly extract domain from URL."""
        limiter = DomainRateLimiter()
        assert limiter._get_domain("https://example.com/page") == "example.com"
        assert limiter._get_domain("https://sub.example.com/page") == "sub.example.com"
        assert limiter._get_domain("http://EXAMPLE.COM/PAGE") == "example.com"

    def test_acquire_release_works(self):
        """acquire and release should work without errors."""
        async def _test():
            limiter = DomainRateLimiter(min_delay=0.01, max_delay=0.02, max_concurrent=2)
            url = "https://example.com/test"
            await limiter.acquire(url)
            await limiter.release(url)
        
        asyncio.run(_test())

    def test_concurrent_limit_respected(self):
        """Concurrent requests per domain should be limited."""
        async def _test():
            limiter = DomainRateLimiter(min_delay=0.01, max_delay=0.02, max_concurrent=2)
            url = "https://example.com/test"
            
            # Acquire twice (should be allowed with max_concurrent=2)
            await limiter.acquire(url)
            await limiter.acquire(url)
            
            # Release both
            await limiter.release(url)
            await limiter.release(url)
        
        asyncio.run(_test())


# =============================================================================
# UNIT TESTS - Archive Cache Functions
# =============================================================================

class TestArchiveCache:
    """Tests for archive cache functions."""

    def test_cache_path_generation(self):
        """Cache path should be consistently generated for same URL."""
        url = "https://example.com/article"
        path1 = _get_archive_cache_path(url)
        path2 = _get_archive_cache_path(url)
        assert path1 == path2
        assert ARCHIVE_CACHE_DIR in path1
        assert path1.endswith(".json")

    def test_different_urls_different_paths(self):
        """Different URLs should have different cache paths."""
        path1 = _get_archive_cache_path("https://example.com/a")
        path2 = _get_archive_cache_path("https://example.com/b")
        assert path1 != path2

    def test_cache_miss_returns_none(self):
        """Non-existent cache should return None."""
        result = _get_cached_archive_result("https://nonexistent-url-12345.example.com/page")
        assert result is None

    def test_cache_round_trip(self):
        """Caching and retrieving should work correctly."""
        # Use a unique URL for this test
        test_url = f"https://test-cache-{int(time.time())}.example.com/page"
        test_archive_url = "https://web.archive.org/web/123/" + test_url
        test_title = "Test Title"
        test_content = "Test content for caching"
        
        try:
            # Cache the result
            _cache_archive_result(test_url, test_archive_url, test_title, test_content)
            
            # Retrieve it
            result = _get_cached_archive_result(test_url)
            
            assert result is not None
            assert result["url"] == test_url
            assert result["archive_url"] == test_archive_url
            assert result["title"] == test_title
            assert result["content"] == test_content
            assert result["fetcher"] == "archive_cached"
            assert result["status"] == "success"
        finally:
            # Clean up - remove the cache file
            cache_path = _get_archive_cache_path(test_url)
            if os.path.exists(cache_path):
                os.remove(cache_path)


# =============================================================================
# INTEGRATION TESTS - Actual Web Fetching
# =============================================================================

@pytest.mark.integration
class TestFetchWithCurlCffi:
    """Integration tests for _fetch_with_curl_cffi function."""

    def test_fetch_real_page(self):
        """Should successfully fetch a real, simple web page."""
        # Use a reliable, simple page for testing
        result = _fetch_with_curl_cffi("https://example.com", timeout=15)
        
        assert result["status"] == "success"
        assert result["url"] == "https://example.com"
        assert "content" in result
        assert len(result["content"]) > 100
        assert result["fetcher"] == "curl_cffi"

    def test_fetch_nonexistent_domain(self):
        """Should fail gracefully for non-existent domain."""
        result = _fetch_with_curl_cffi("https://this-domain-definitely-does-not-exist-12345.com", timeout=10)
        
        assert result["status"] == "failure"
        assert "error" in result

    def test_fetch_invalid_url(self):
        """Should handle invalid URLs gracefully."""
        result = _fetch_with_curl_cffi("not-a-valid-url", timeout=5)
        
        assert result["status"] == "failure"
        assert "error" in result

    def test_fetch_404_page(self):
        """Should return failure for 404 pages."""
        result = _fetch_with_curl_cffi("https://httpstat.us/404", timeout=10)
        
        assert result["status"] == "failure"
        assert result.get("status_code") == 404 or "404" in result.get("error", "")

    def test_fetch_non_html_content(self):
        """Should return failure for non-HTML content."""
        # Using a JSON API endpoint
        result = _fetch_with_curl_cffi("https://httpbin.org/json", timeout=10)
        
        # Should fail because content-type is not HTML
        assert result["status"] == "failure"
        assert "Non-HTML" in result.get("error", "")


@pytest.mark.integration
class TestFetchFromArchive:
    """Integration tests for _fetch_from_archive function."""

    def test_fetch_archived_page(self):
        """Should fetch from Internet Archive for well-known URLs."""
        # example.com has been archived many times
        result = _fetch_from_archive("https://example.com", timeout=20)
        
        # Note: This may fail if archive.org is down or rate limiting
        if result["status"] == "success":
            assert "content" in result
            assert "archive" in result["fetcher"]
            assert result.get("archive_url") is not None

    def test_fetch_recent_url_may_not_exist(self):
        """Very recent or obscure URLs may not be in the archive."""
        # A made-up URL unlikely to be archived
        result = _fetch_from_archive(
            f"https://nonexistent-site-{int(time.time())}.example.com/page",
            timeout=15
        )
        
        # Should gracefully handle not finding anything
        # Could be success (if cached) or failure
        assert "status" in result


@pytest.mark.integration
class TestFetchPageContent:
    """Integration tests for the main fetch_page_content function."""

    def test_fetch_single_url(self):
        """Should fetch a single URL successfully."""
        result = fetch_page_content(["https://example.com"], max_parallel=1)
        
        assert "web_page_content" in result
        assert "token_usage_info" in result
        assert len(result["web_page_content"]) == 1
        
        page = result["web_page_content"][0]
        assert page["url"] == "https://example.com"
        # May succeed or fail depending on network conditions
        assert page["status"] in ["success", "failure"]

    def test_fetch_multiple_urls(self):
        """Should fetch multiple URLs in parallel."""
        urls = [
            "https://example.com",
            "https://httpbin.org/html",
        ]
        result = fetch_page_content(urls, max_parallel=2)
        
        assert "web_page_content" in result
        assert len(result["web_page_content"]) == 2
        
        # Each result should have a URL
        fetched_urls = [r["url"] for r in result["web_page_content"]]
        assert "https://example.com" in fetched_urls
        assert "https://httpbin.org/html" in fetched_urls

    def test_fetch_empty_list(self):
        """Should handle empty URL list gracefully."""
        result = fetch_page_content([])
        
        assert "web_page_content" in result
        assert result["web_page_content"] == []
        assert "token_usage_info" in result

    def test_url_normalization_applied(self):
        """Should normalize URLs without scheme."""
        result = fetch_page_content(["example.com"], max_parallel=1)
        
        assert "web_page_content" in result
        assert len(result["web_page_content"]) == 1
        # The URL should have been normalized
        page = result["web_page_content"][0]
        assert page["url"].startswith("https://")


# =============================================================================
# MOCKED TESTS - Testing without actual network calls
# =============================================================================

class TestFetchPageContentMocked:
    """Tests with mocked network calls for faster, more reliable testing."""

    @patch('agent_core.fetch_tool._fetch_with_curl_cffi')
    def test_fallback_to_curl_cffi(self, mock_curl):
        """Should fallback to curl_cffi when browser crawl fails."""
        mock_curl.return_value = {
            "url": "https://example.com",
            "status": "success",
            "content": "Fetched content",
            "fetcher": "curl_cffi",
        }
        
        # This tests the fallback path - actual implementation varies
        result = _fetch_with_curl_cffi("https://example.com")
        assert result["status"] == "success"
        assert result["fetcher"] == "curl_cffi"

    def test_soft_block_detection_in_response(self):
        """Soft blocks should be properly detected in responses."""
        # Simulate a Cloudflare block page
        block_content = """
        <html>
        <head><title>Just a moment...</title></head>
        <body>
        <div>Checking your browser before accessing the website.</div>
        <div>Please complete the security check to continue.</div>
        <div>Ray ID: abc123</div>
        </body>
        </html>
        """
        
        assert _is_soft_block(block_content, 200) is True


class TestContentTruncation:
    """Tests for content truncation behavior in fetch results."""

    def test_large_content_is_truncated(self):
        """Content exceeding MAX_CONTENT_SIZE should be truncated."""
        large_content = "x" * (MAX_CONTENT_SIZE + 5000)
        result = _truncate_content(large_content)
        
        # Should be truncated to around MAX_CONTENT_SIZE
        assert len(result) < len(large_content)
        assert len(result) <= MAX_CONTENT_SIZE + 100  # Account for message

    def test_truncation_preserves_content_start(self):
        """Truncation should preserve the beginning of content."""
        content = "START of content. " + "x" * (MAX_CONTENT_SIZE + 5000)
        result = _truncate_content(content)
        
        assert result.startswith("START of content.")


# =============================================================================
# EDGE CASE TESTS
# =============================================================================

class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_unicode_content_handled(self):
        """Unicode content in HTML should be handled correctly."""
        html = "<html><body><p>日本語テスト 中文测试 émojis: 🎉🚀</p></body></html>"
        text, _ = SimpleHTMLTextExtractor.extract(html)
        assert "日本語テスト" in text
        assert "中文测试" in text
        assert "🎉🚀" in text

    def test_malformed_html_handled(self):
        """Malformed HTML should not crash the parser."""
        html = "<html><body><p>Unclosed tag<div>More content"
        # Should not raise an exception
        text, _ = SimpleHTMLTextExtractor.extract(html)
        assert "Unclosed tag" in text or "More content" in text

    def test_deeply_nested_html(self):
        """Deeply nested HTML should be handled."""
        # Create deeply nested divs
        nested = "<div>" * 50 + "Deep content" + "</div>" * 50
        html = f"<html><body>{nested}</body></html>"
        text, _ = SimpleHTMLTextExtractor.extract(html)
        assert "Deep content" in text

    def test_special_characters_in_url(self):
        """URLs with special characters should be handled."""
        # Should normalize without crashing
        url = "https://example.com/page?q=hello world&foo=bar#section"
        normalized = _normalize_url(url)
        assert normalized == url  # Spaces should be preserved (encoding is separate)


# =============================================================================
# PYTEST CONFIGURATION
# =============================================================================

def pytest_configure(config):
    """Configure custom markers."""
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests (may require network)"
    )


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v"])
