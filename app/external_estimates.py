"""On-demand external estimates for a single property.

The way an agent gets these "each time": search the address to find the
property's page on a portal, fetch that page, read the estimate. No private API,
no 10k-a-week batch — one lookup when a user opens a property, then cached.

Currently covers homes.co.nz, whose estimate is embedded in the page JSON
(realestate.co.nz and Trade Me render theirs client-side and would need a
headless browser — deliberately left out rather than shipped flaky).
"""
from __future__ import annotations

import json
import re

import httpx

from .config import settings

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# "1.6M" / "1.69M" / "820K" / "3M" → dollars
_SHORT = re.compile(r"^\s*\$?([0-9]+(?:\.[0-9]+)?)\s*([MK])?\s*$", re.I)


def _parse_short(s: str | None) -> float | None:
    if not s:
        return None
    m = _SHORT.match(str(s))
    if not m:
        return None
    v = float(m.group(1))
    unit = (m.group(2) or "").upper()
    return v * 1_000_000 if unit == "M" else v * 1_000 if unit == "K" else v


_HOMES_HREF = re.compile(r'homes\.co\.nz/address/[a-z0-9\-/]+/[A-Za-z0-9]{4,}', re.I)


def _brave_search(query: str, client: httpx.Client) -> str | None:
    """Resolve the homes.co.nz page via the Brave Search API (reliable, keyed).
    Returns None if no key is set, so it degrades to the free engines below."""
    key = settings.brave_api_key
    if not key:
        return None
    try:
        r = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 5},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        for res in (r.json().get("web", {}) or {}).get("results", []):
            url = res.get("url", "")
            if _HOMES_HREF.search(url):
                return url
    except Exception:
        return None
    return None


def _resolve_homes_url(address: str, client: httpx.Client) -> str | None:
    """Find the property's homes.co.nz page. Brave Search API first (keyed,
    reliable), then free DuckDuckGo/Bing as a no-key fallback."""
    q = f"{address} site:homes.co.nz"
    brave = _brave_search(q, client)
    if brave:
        return brave
    for req in (
        lambda: client.post("https://html.duckduckgo.com/html/", data={"q": q}, timeout=15),
        lambda: client.get("https://www.bing.com/search", params={"q": q}, timeout=15),
    ):
        try:
            r = req()
            if r.status_code == 200:
                m = _HOMES_HREF.search(r.text)
                if m:
                    return "https://" + m.group(0)
        except Exception:
            continue
    return None


def _extract_homes(html: str) -> dict | None:
    m = re.search(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None

    result: dict = {}

    def walk(o):
        if isinstance(o, dict):
            pd = o.get("property_details")
            if isinstance(pd, dict) and pd.get("display_estimated_value_short"):
                result["value"] = _parse_short(pd.get("display_estimated_value_short"))
                result["low"] = _parse_short(pd.get("display_estimated_lower_value_short"))
                result["high"] = _parse_short(pd.get("display_estimated_upper_value_short"))
                cv = pd.get("capital_value")
                result["cv"] = float(cv) if isinstance(cv, (int, float)) else None
                result["revised"] = pd.get("estimated_value_revision_date")
            for v in o.values():
                if "value" not in result:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                if "value" not in result:
                    walk(v)

    walk(data)
    return result or None


def homes_estimate(address: str) -> dict | None:
    """{'value','low','high','cv','revised','url'} for the address, or None.

    Best-effort: any failure (no match, changed markup, blocked) returns None,
    never an exception — the caller shows the tile only when there's a real value.
    """
    if not address:
        return None
    with httpx.Client(headers={"User-Agent": _UA}, follow_redirects=True) as client:
        url = _resolve_homes_url(address, client)
        if not url:
            return None
        try:
            html = client.get(url, timeout=20).text
        except Exception:
            return None
    est = _extract_homes(html)
    if not est or not est.get("value"):
        return None
    est["url"] = url
    return est
