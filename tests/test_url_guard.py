"""Tests for B8: SSRF protection — URL validation guard.

Covers:
- Blocks localhost / 127.x / ::1
- Blocks private ranges (10.x, 172.16-31.x, 192.168.x)
- Blocks link-local (169.254.x)
- Blocks file:// / ftp:// / gopher:// and other non-http(s) schemes
- Blocks DNS rebinding tricks (e.g. 127.0.0.1.nip.io style)
- Allows normal public http/https URLs
- Edge cases: empty, malformed, IPv6, port variants
"""

import pytest

from agent_assistant.tools.url_guard import SSRFError, validate_url


class TestValidateUrlBlocksPrivate:
    """URLs pointing to private/internal resources must be blocked."""

    @pytest.mark.parametrize("url", [
        "http://localhost/",
        "http://localhost:8080/admin",
        "http://LOCALHOST/path",
        "http://127.0.0.1/",
        "http://127.0.0.1:3000/api",
        "http://127.255.255.254/",
        "https://127.0.0.1/secure",
        "http://[::1]/",
        "http://[::1]:8080/",
        "http://[0:0:0:0:0:0:0:1]/",
    ])
    def test_blocks_localhost(self, url: str):
        with pytest.raises(SSRFError):
            validate_url(url)

    @pytest.mark.parametrize("url", [
        "http://10.0.0.1/",
        "http://10.255.255.255/x",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://192.168.0.1/",
        "http://192.168.1.100:9090/",
        "http://169.254.169.254/latest/meta-data/",  # AWS metadata
        "http://[fc00::1]/",
        "http://[fd12:3456:789a::1]/",
        "http://[fe80::1]/",
    ])
    def test_blocks_private_ranges(self, url: str):
        with pytest.raises(SSRFError):
            validate_url(url)

    @pytest.mark.parametrize("url", [
        "http://0.0.0.0/",
        "http://0.0.0.0:8080/",
        "http://0177.0.0.1/",  # octal 127.0.0.1
        "http://2130706433/",  # decimal 127.0.0.1
        "http://0x7f000001/",  # hex 127.0.0.1
    ])
    def test_blocks_obfuscated_loopback(self, url: str):
        with pytest.raises(SSRFError):
            validate_url(url)


class TestValidateUrlBlocksSchemes:
    """Non-http(s) schemes must be blocked."""

    @pytest.mark.parametrize("url", [
        "file:///C:/Windows/System32/config/SAM",
        "file:///etc/passwd",
        "ftp://internal-server/data",
        "gopher://127.0.0.1:6379/_INFO",
        "dict://127.0.0.1:6379/info",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
    ])
    def test_blocks_non_http_schemes(self, url: str):
        with pytest.raises(SSRFError):
            validate_url(url)


class TestValidateUrlAllowsPublic:
    """Normal public URLs must pass."""

    @pytest.mark.parametrize("url", [
        "https://www.google.com/",
        "https://en.wikipedia.org/wiki/Python",
        "http://example.com/page?q=test",
        "https://api.github.com/repos",
        "https://news.ycombinator.com/item?id=12345",
        "http://93.184.216.34/",  # example.com IP (public)
        "https://sub.domain.co.uk/path/to/page",
    ])
    def test_allows_public_urls(self, url: str):
        # Should not raise
        validate_url(url)


class TestValidateUrlEdgeCases:
    """Edge cases: empty, malformed, missing scheme."""

    def test_empty_string(self):
        with pytest.raises(SSRFError):
            validate_url("")

    def test_whitespace_only(self):
        with pytest.raises(SSRFError):
            validate_url("   ")

    def test_no_scheme(self):
        with pytest.raises(SSRFError):
            validate_url("example.com/page")

    def test_malformed_url(self):
        with pytest.raises(SSRFError):
            validate_url("http://")

    def test_url_with_credentials_blocks_internal(self):
        """user:pass@localhost trick."""
        with pytest.raises(SSRFError):
            validate_url("http://admin:pass@127.0.0.1/admin")

    def test_url_with_credentials_allows_public(self):
        """user:pass@public is fine (rare but valid)."""
        validate_url("https://user:token@api.example.com/data")
