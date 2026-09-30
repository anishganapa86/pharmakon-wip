"""Shared fetching, extraction, and normalization helpers for the skincare
product importer and verifier (scripts/import_skincare_product.py,
scripts/verify_skincare_products.py). Not a standalone script.

Fetch etiquette: robots.txt is checked per host, requests are paced at least
MIN_SECONDS_BETWEEN_HOST_REQUESTS apart per host, a clear User-Agent is sent,
and a failed request is retried at most once.

Extraction is a heuristic over arbitrary manufacturer HTML: it looks for a
text node matching "Ingredients" / "Full Ingredients" / "INCI" and takes the
nearest comma-separated block of text near it. It will not be right for
every site's markup -- when it isn't confident (no match, more than one
differing match, or too short a match to be a real list) it reports why and
extracts nothing, rather than guessing.
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

USER_AGENT = "PharmakonSkincareImporter/1.0 (non-commercial research tool)"
REQUEST_TIMEOUT = 15.0
MIN_SECONDS_BETWEEN_HOST_REQUESTS = 2.0
MIN_INGREDIENT_COUNT = 5

_INGREDIENT_HEADING_RE = re.compile(
    r"\b(full\s+ingredients|ingredients|inci)\b", re.IGNORECASE
)


def normalize_line_endings(text: str) -> str:
    """CRLF/CR -> LF, so stored strings never carry \\r regardless of the
    platform a page or a pasted file came from."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_ingredients(raw_text: str) -> list[str]:
    """Verbatim split on commas: preserves source spelling, case, and order.
    Only whitespace (including embedded newlines from HTML text extraction)
    is collapsed -- nothing is reworded."""
    text = normalize_line_endings(raw_text)
    parts = [re.sub(r"\s+", " ", p).strip() for p in text.split(",")]
    return [p for p in parts if p]


class HostRateLimiter:
    """Serializes requests per host with a minimum delay between them --
    "one request per host at a time, a 2-second delay"."""

    def __init__(self, min_interval: float = MIN_SECONDS_BETWEEN_HOST_REQUESTS):
        self.min_interval = min_interval
        self._last_request_at: dict[str, float] = {}

    def wait(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self._last_request_at[host] = time.monotonic()


@dataclass
class FetchResult:
    text: str | None
    reason: str | None  # set whenever text is None


_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}


def _robots_allowed(url: str, user_agent: str) -> tuple[bool, str | None]:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    robots_url = origin + "/robots.txt"
    parser = _robots_cache.get(origin)
    if parser is None:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            resp = httpx.get(
                robots_url,
                headers={"User-Agent": user_agent},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code >= 400:
                parser.parse([])  # no robots.txt: conventional allow-all
            else:
                parser.parse(resp.text.splitlines())
        except httpx.HTTPError:
            # Can't reach robots.txt itself -- don't block every fetch on a
            # transient network blip; the real page fetch below will surface
            # any actual connectivity problem.
            parser.parse([])
        _robots_cache[origin] = parser
    allowed = parser.can_fetch(user_agent, url)
    return allowed, None if allowed else f"disallowed by {robots_url}"


def fetch_page(
    url: str,
    limiter: HostRateLimiter,
    user_agent: str = USER_AGENT,
) -> FetchResult:
    """Fetch `url` politely: robots check, per-host pacing, one retry."""
    host = urlparse(url).netloc
    allowed, why_blocked = _robots_allowed(url, user_agent)
    if not allowed:
        return FetchResult(None, f"blocked by robots.txt ({why_blocked})")

    last_error: str | None = None
    for _attempt in range(2):  # one request, one retry
        limiter.wait(host)
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": user_agent},
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
            )
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}"
                continue
            return FetchResult(resp.text, None)
        except httpx.HTTPError as exc:
            last_error = str(exc)
    return FetchResult(None, last_error or "unknown fetch error")


@dataclass
class ExtractionResult:
    raw_text: str | None
    ingredients: list[str]
    reason: str | None  # set whenever raw_text is None


def _sibling_text(node) -> str | None:
    if isinstance(node, Tag):
        text = node.get_text(" ", strip=True)
        return text or None
    if isinstance(node, NavigableString):
        text = str(node).strip()
        return text or None
    return None


def _following_siblings_text(node, limit: int = 3) -> list[str]:
    out: list[str] = []
    sib = node.next_sibling if node is not None else None
    while sib is not None and len(out) < limit:
        text = _sibling_text(sib)
        if text:
            out.append(text)
        sib = sib.next_sibling
    return out


def _looks_like_ingredient_list(text: str) -> bool:
    """Loose gate for "is this text block a list at all" (candidate
    detection) -- deliberately looser than MIN_INGREDIENT_COUNT so a short
    list near an Ingredients label is picked up as a candidate and reported
    as a partial capture, rather than missed entirely as "no section found"."""
    return text.count(",") >= 1


def _candidate_texts(soup: BeautifulSoup) -> list[str]:
    """Distinct text blocks (by parsed ingredient list) that look like an
    ingredient list near an Ingredients/Full Ingredients/INCI label."""
    raw_candidates: list[str] = []

    for el in soup.find_all(string=_INGREDIENT_HEADING_RE):
        parent = el.parent
        if parent is None:
            continue
        parent_text = parent.get_text(" ", strip=True)
        remainder = _INGREDIENT_HEADING_RE.sub("", parent_text, count=1)
        remainder = remainder.strip(" :–-—")
        texts_to_check = [remainder, *_following_siblings_text(parent)]
        for text in texts_to_check:
            if _looks_like_ingredient_list(text):
                raw_candidates.append(normalize_line_endings(text).strip())
                break

    distinct: list[str] = []
    distinct_parsed: list[list[str]] = []
    for candidate in raw_candidates:
        parsed = split_ingredients(candidate)
        if parsed not in distinct_parsed:
            distinct.append(candidate)
            distinct_parsed.append(parsed)
    return distinct


def extract_ingredients(html: str) -> ExtractionResult:
    soup = BeautifulSoup(html, "html.parser")
    candidates = _candidate_texts(soup)

    if not candidates:
        return ExtractionResult(None, [], "no ingredient section found")

    if len(candidates) > 1:
        return ExtractionResult(
            None,
            [],
            f"ambiguous: {len(candidates)} differing ingredient sections found",
        )

    raw_text = candidates[0]
    ingredients = split_ingredients(raw_text)
    if len(ingredients) < MIN_INGREDIENT_COUNT:
        return ExtractionResult(
            None,
            [],
            f"partial capture: only {len(ingredients)} ingredient(s) found",
        )
    return ExtractionResult(raw_text, ingredients, None)
