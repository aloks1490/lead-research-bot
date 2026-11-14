"""LinkedIn URL handling. The normalised URL is the cache key: every spelling of one profile must give one string."""
from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit

# Slack wraps links as <https://example.com> or <https://example.com|label>.
_SLACK_LINK = re.compile(r"<(https?://[^>|\s]+)(?:\|[^>]*)?>")
_BARE_LINK = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/[^\s<>|]+", re.IGNORECASE)
_LINKEDIN_HOST = re.compile(r"^(?:[a-z]{2,3}\.)?linkedin\.com$")  # www., de., uk. ... all point at the same profile


def extract_url(text: str) -> str | None:
    """The first link in a Slack message, or None."""
    match = _SLACK_LINK.search(text) or _BARE_LINK.search(text)
    if not match:
        return None
    return match.group(1) if match.re is _SLACK_LINK else match.group(0)


def normalise_linkedin_url(url: str) -> str | None:
    """'https://www.LinkedIn.com/in/Jane-Doe/?utm=x' -> 'linkedin.com/in/jane-doe'.
    Returns None for anything that is not a personal profile (company pages, posts, other sites)."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not _LINKEDIN_HOST.match(host):
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or segments[0].lower() != "in":
        return None
    slug = quote(unquote(segments[1]).strip().lower(), safe="-_.~")
    return f"linkedin.com/in/{slug}" if slug else None
