"""Weekly pre-publish data verification.

Before a new for-sale batch goes live, every listing is checked against its own
source page: we fetch the listing, read the land area the page actually shows,
and compare it to what the scrape stored. Mismatches are flagged (and, when the
gap is large, the listing is held back from the deal signals that depend on land
area — subdivision most of all, where a single bad area produced $M phantom
"gems" like 139 Long Drive at 5,665 m² when the real site is 416 m²).

Extraction is plain HTML + regex — the page renders the figures server-side
("416m² Land area"), so no browser or LLM is needed and it scales to a whole
batch. If Hougarden changes its markup this returns None (couldn't verify), which
is treated as "unverified", never as a false match.
"""
from __future__ import annotations

import asyncio
import re

import httpx

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_NUM = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
_UNIT = r"(m(?:²|&sup2;|2)?|ha)"       # m², m&sup2;, m2, or ha

# Two Hougarden layouts:
#   houses  → "416m² Land area"                 (figure before the label)
#   sections→ "Land area</span><span>16858m&sup2;</span>"  (label before figure)
_NUM_FIRST = re.compile(_NUM + r"\s*" + _UNIT + r"\s*Land\s*area", re.I)
_LABEL_FIRST = re.compile(r"Land\s*area\s*</span>\s*<span[^>]*>\s*" + _NUM + r"\s*" + _UNIT, re.I)


def _to_m2(num: str, unit: str) -> float | None:
    try:
        v = float(num.replace(",", ""))
    except ValueError:
        return None
    return v * 10_000 if unit.lower() == "ha" else v


def extract_land_area_m2(html: str) -> float | None:
    """The land area (m²) the listing page displays, or None if not found.
    Returning None means 'couldn't verify' — never treated as a match."""
    for pat in (_NUM_FIRST, _LABEL_FIRST):
        m = pat.search(html)
        if m:
            v = _to_m2(m.group(1), m.group(2))
            if v:
                return v
    return None


async def _fetch(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url, timeout=25, follow_redirects=True)
        if r.status_code == 200:
            return r.text
    except Exception:
        return None
    return None


class Check:
    __slots__ = ("id", "url", "stored", "listing", "status")

    def __init__(self, id, url, stored, listing, status):
        self.id = id
        self.url = url
        self.stored = stored          # land area we hold
        self.listing = listing        # land area the page shows (None = couldn't read)
        self.status = status          # "ok" | "mismatch" | "unverified"


def _classify(stored: float | None, listing: float | None, tol: float) -> str:
    if listing is None:
        return "unverified"
    if not stored or stored <= 0:
        return "mismatch"             # page has an area, we don't — worth a look
    gap = abs(stored - listing) / listing
    return "ok" if gap <= tol else "mismatch"


async def verify_many(items: list[tuple[int, str, float | None]], *,
                      concurrency: int = 8, tol: float = 0.10,
                      delay: float = 0.15) -> list[Check]:
    """items = [(id, url, stored_land_area_m2)]. Returns a Check per item.

    `tol` is the allowed land-area gap (10% default). `concurrency`/`delay` keep
    the fetch polite so a full-batch run doesn't get the source IP throttled.
    """
    sem = asyncio.Semaphore(concurrency)
    out: list[Check] = []

    async with httpx.AsyncClient(headers={"User-Agent": _UA}) as client:
        async def one(id, url, stored):
            async with sem:
                html = await _fetch(client, url) if url else None
                await asyncio.sleep(delay)
            listing = extract_land_area_m2(html) if html else None
            out.append(Check(id, url, stored, listing, _classify(stored, listing, tol)))

        await asyncio.gather(*(one(*it) for it in items))
    return out
