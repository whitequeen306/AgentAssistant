"""B8: SSRF protection — URL validation guard.

Validates URLs before any outbound fetch (read_page, research sub-agent).
Blocks: localhost, private/link-local IPs, non-http(s) schemes, obfuscated IPs.

Design: raise SSRFError on violation; callers catch and return ToolResult.failure().
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


class SSRFError(Exception):
    """Raised when a URL fails SSRF validation."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"SSRF blocked: {reason}")


# Allowed schemes
_ALLOWED_SCHEMES = {"http", "https"}

# Hostnames that are always internal
_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}


def validate_url(url: str) -> None:
    """Validate a URL for SSRF safety. Raises SSRFError if blocked.

    Checks performed:
    1. Non-empty, parseable
    2. Scheme is http/https only
    3. Hostname is not localhost / internal alias
    4. IP (if literal) is not private / loopback / link-local / reserved
    5. Obfuscated IP encodings (octal, decimal, hex) are caught
    """
    if not url or not url.strip():
        raise SSRFError("empty URL")

    url = url.strip()

    # Parse
    try:
        parsed = urlparse(url)
    except Exception:
        raise SSRFError("malformed URL")

    # Scheme check
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(f"scheme '{scheme}' not allowed (http/https only)")

    # Hostname extraction
    hostname = parsed.hostname
    if not hostname:
        raise SSRFError("no hostname in URL")

    hostname_lower = hostname.lower()

    # Block known internal hostnames
    if hostname_lower in _BLOCKED_HOSTNAMES:
        raise SSRFError("localhost is blocked")

    # Also block *.localhost (e.g. foo.localhost)
    if hostname_lower.endswith(".localhost"):
        raise SSRFError("*.localhost is blocked")

    # Try to parse as IP address (handles IPv4, IPv6, and obfuscated forms)
    ip = _try_parse_ip(hostname)
    if ip is not None:
        _check_ip_blocked(ip)
    else:
        # Not a literal IP — it's a domain name.
        # Block obvious internal patterns (e.g. *.internal, *.local)
        if hostname_lower.endswith((".internal", ".local", ".lan")):
            raise SSRFError(f"internal domain '{hostname}' is blocked")


def _try_parse_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Attempt to parse hostname as an IP address.

    Handles:
    - Standard IPv4/IPv6
    - Octal (0177.0.0.1)
    - Decimal (2130706433)
    - Hex (0x7f000001)
    - IPv6 bracket notation already stripped by urlparse
    """
    # Direct parse (standard IPv4/IPv6)
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        pass

    # Try numeric forms (decimal / hex single-integer IPv4)
    try:
        # Pure decimal: 2130706433 → 127.0.0.1
        if hostname.isdigit():
            num = int(hostname)
            if 0 <= num <= 0xFFFFFFFF:
                return ipaddress.IPv4Address(num)
    except (ValueError, OverflowError):
        pass

    # Hex form: 0x7f000001
    if hostname.lower().startswith("0x"):
        try:
            num = int(hostname, 16)
            if 0 <= num <= 0xFFFFFFFF:
                return ipaddress.IPv4Address(num)
        except (ValueError, OverflowError):
            pass

    # Octal dotted form: 0177.0.0.1
    parts = hostname.split(".")
    if len(parts) == 4:
        try:
            octets = []
            for part in parts:
                if part.startswith("0") and len(part) > 1 and not part.startswith("0x"):
                    octets.append(int(part, 8))
                else:
                    octets.append(int(part))
            if all(0 <= o <= 255 for o in octets):
                return ipaddress.IPv4Address(".".join(str(o) for o in octets))
        except (ValueError, OverflowError):
            pass

    return None


def _check_ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    """Raise SSRFError if the IP is in a blocked range."""
    if ip.is_loopback:
        raise SSRFError(f"loopback address {ip} is blocked")
    if ip.is_private:
        raise SSRFError(f"private address {ip} is blocked")
    if ip.is_link_local:
        raise SSRFError(f"link-local address {ip} is blocked")
    if ip.is_reserved:
        raise SSRFError(f"reserved address {ip} is blocked")
    if ip.is_multicast:
        raise SSRFError(f"multicast address {ip} is blocked")
    # 0.0.0.0
    if ip == ipaddress.IPv4Address("0.0.0.0"):
        raise SSRFError("0.0.0.0 is blocked")
    # IPv6 unspecified
    if ip == ipaddress.IPv6Address("::"):
        raise SSRFError(":: is blocked")
