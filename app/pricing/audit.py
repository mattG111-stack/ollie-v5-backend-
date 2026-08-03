"""Post-ingest anomaly audit for for-sale batches.

Runs after the pricing pipeline finishes. Looks for warning signs that the
model has misbehaved on the new dataset (blown-up predictions, suspicious
correction lookups, suburbs that all return NULL, etc). Warnings are stored
on the IngestJob.audit_warnings field as JSON and surfaced on the admin
uploads page so problems get caught before the client sees them.

We deliberately keep this list short and high-signal. Every warning should
correspond to "human, look at this row".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any


# Thresholds — tuned so a clean ingest produces zero warnings, but obvious
# data corruption (the Huapai $110B incident) trips multiple.
EXTREME_VS_CV_MULT = 8.0       # market_value more than 8x CV → flag
EXTREME_VS_ASK_MULT = 6.0      # market_value more than 6x asking → flag
EXTREME_ABSOLUTE = 50_000_000  # market_value over $50M for a residential property → flag
MIN_HIGH_CONF_PCT = 1.0        # less than 1% high-confidence rows is suspicious
MAX_INSUFFICIENT_PCT = 80.0    # more than 80% insufficient is suspicious
MAX_NULL_PRED_PCT = 70.0       # more than 70% NULL predictions = pipeline broken


@dataclass
class Warning:
    code: str
    severity: str    # 'high' | 'medium' | 'low'
    message: str
    sample_addresses: list[str]
    count: int


def audit_for_sale_batch(rows: list[dict[str, Any]]) -> list[dict]:
    """Returns a list of warning dicts. Empty list = clean ingest."""
    if not rows:
        return []

    warnings: list[Warning] = []
    n = len(rows)

    # --- 1) Extreme market value vs CV ---
    extreme_cv = [
        r for r in rows
        if r.get("market_value") and r.get("cv_numeric")
        and r["market_value"] > EXTREME_VS_CV_MULT * r["cv_numeric"]
    ]
    if extreme_cv:
        warnings.append(Warning(
            code="market_value_vs_cv",
            severity="high",
            message=f"{len(extreme_cv)} listings have market_value > {EXTREME_VS_CV_MULT}x their CV. "
                    f"Almost always a data error (mis-extracted coefficient or bad CV).",
            sample_addresses=[(r.get("address") or "")[:60] for r in extreme_cv[:5]],
            count=len(extreme_cv),
        ))

    # --- 1b) Implausible CV vs asking (garbage council valuation) ---
    # A CV far above the asking price (e.g. $45M CV on a $745k apartment) is a
    # scraping error. It corrupts the deal-finding fair value, so we already
    # suppress those, but flag them here so the bad source rows are visible.
    bad_cv = [
        r for r in rows
        if r.get("cv_numeric") and r.get("price_numeric")
        and r["cv_numeric"] > 2.5 * r["price_numeric"]
    ]
    if bad_cv:
        warnings.append(Warning(
            code="cv_implausible_vs_asking",
            severity="medium",
            message=f"{len(bad_cv)} listings have a council CV more than 2.5x their asking price — "
                    f"almost always a bad CV in the source data. Margin/underpriced suppressed for these.",
            sample_addresses=[(r.get("address") or "")[:60] for r in bad_cv[:5]],
            count=len(bad_cv),
        ))

    # --- 2) Extreme market value vs asking ---
    extreme_ask = [
        r for r in rows
        if r.get("market_value") and r.get("price_numeric")
        and r["market_value"] > EXTREME_VS_ASK_MULT * r["price_numeric"]
    ]
    if extreme_ask:
        warnings.append(Warning(
            code="market_value_vs_asking",
            severity="medium",
            message=f"{len(extreme_ask)} listings have market_value > {EXTREME_VS_ASK_MULT}x asking. "
                    f"Could be legit deals — sanity-check the top ones.",
            sample_addresses=[(r.get("address") or "")[:60] for r in extreme_ask[:5]],
            count=len(extreme_ask),
        ))

    # --- 3) Absurd absolute values ---
    huge = [r for r in rows if (r.get("market_value") or 0) > EXTREME_ABSOLUTE]
    if huge:
        warnings.append(Warning(
            code="market_value_above_50m",
            severity="high" if len(huge) > 20 else "medium",
            message=f"{len(huge)} listings have market_value above ${EXTREME_ABSOLUTE//1_000_000}M. "
                    f"Auckland trophy homes top out around $30M — anything higher is usually a bug.",
            sample_addresses=[(r.get("address") or "")[:60] for r in huge[:5]],
            count=len(huge),
        ))

    # --- 4) Confidence distribution ---
    by_conf: dict[str, int] = {}
    for r in rows:
        c = r.get("confidence") or "unknown"
        by_conf[c] = by_conf.get(c, 0) + 1
    high_pct = (by_conf.get("high", 0) / n) * 100
    insuf_pct = (by_conf.get("insufficient", 0) / n) * 100
    if high_pct < MIN_HIGH_CONF_PCT:
        warnings.append(Warning(
            code="too_few_high_confidence",
            severity="medium",
            message=f"Only {high_pct:.1f}% of listings hit High confidence "
                    f"(<{MIN_HIGH_CONF_PCT}%). The bucket-N tables may be stale or your data is unusual.",
            sample_addresses=[], count=by_conf.get("high", 0),
        ))
    if insuf_pct > MAX_INSUFFICIENT_PCT:
        warnings.append(Warning(
            code="too_many_insufficient",
            severity="medium",
            message=f"{insuf_pct:.1f}% of listings are Insufficient confidence "
                    f"(>{MAX_INSUFFICIENT_PCT}%). Check if CV column is being read correctly.",
            sample_addresses=[], count=by_conf.get("insufficient", 0),
        ))

    # --- 5) NULL prediction rate ---
    null_preds = sum(1 for r in rows if r.get("market_value") in (None, 0))
    null_pct = (null_preds / n) * 100
    if null_pct > MAX_NULL_PRED_PCT:
        warnings.append(Warning(
            code="too_many_null_predictions",
            severity="high",
            message=f"{null_pct:.1f}% of listings got no market value at all "
                    f"(>{MAX_NULL_PRED_PCT}%). The pricing pipeline likely failed for a class of input.",
            sample_addresses=[], count=null_preds,
        ))

    # --- 6) Suburbs where the model is plainly broken ---
    # We're looking for "correction table for this suburb is corrupted" (think
    # the Huapai incident). NOT for "this suburb is industrial so the residential
    # GLM can't price it" — that's expected and benign.
    # Rule: at least 8 listings in the suburb AND at least one has CV (i.e. the
    # rows look residential) AND every priced row is extreme.
    by_suburb_total: dict[str, int] = {}
    by_suburb_with_cv: dict[str, int] = {}
    by_suburb_extreme: dict[str, int] = {}
    for r in rows:
        sub = r.get("suburb") or "?"
        by_suburb_total[sub] = by_suburb_total.get(sub, 0) + 1
        cv = r.get("cv_numeric")
        mv = r.get("market_value")
        if cv:
            by_suburb_with_cv[sub] = by_suburb_with_cv.get(sub, 0) + 1
        is_extreme = (
            (cv and mv and mv > EXTREME_VS_CV_MULT * cv) or
            (mv and mv > EXTREME_ABSOLUTE)
        )
        if is_extreme:
            by_suburb_extreme[sub] = by_suburb_extreme.get(sub, 0) + 1
    broken = [
        s for s, total in by_suburb_total.items()
        if total >= 8
        and by_suburb_with_cv.get(s, 0) >= 4
        and by_suburb_extreme.get(s, 0) >= by_suburb_with_cv.get(s, 0)
    ]
    if broken:
        warnings.append(Warning(
            code="suburb_entirely_broken",
            severity="high",
            message=f"{len(broken)} suburb(s) had EVERY priced listing come back extreme: "
                    f"{', '.join(broken[:5])}. Likely a bad correction-table row for these suburbs.",
            sample_addresses=broken[:5], count=len(broken),
        ))

    # --- Unrecognised zoning ---
    # A zone string in neither ZONE_RULES nor NON_RESIDENTIAL_ZONES silently
    # makes a listing not-subdividable, identically to a deliberate exclusion.
    # Surface it so a council rename or new scrape format can't quietly shrink
    # the subdivision feed.
    from . import zones as Z
    unknown_zones: dict[str, int] = {}
    for r in rows:
        if Z.classify_zone(r.get("zoning")) == "unknown":
            z = str(r.get("zoning")).strip()
            unknown_zones[z] = unknown_zones.get(z, 0) + 1
    if unknown_zones:
        affected = sum(unknown_zones.values())
        top = sorted(unknown_zones.items(), key=lambda kv: -kv[1])
        warnings.append(Warning(
            code="unrecognised_zoning",
            severity="medium",
            message=f"{len(unknown_zones)} zoning value(s) on {affected} listing(s) match no rule "
                    f"and no exclusion — they are treated as not-subdividable by default. "
                    f"Add them to zones.py if any should be subdividable.",
            sample_addresses=[f"{z} ({n})" for z, n in top[:5]],
            count=affected,
        ))

    return [asdict(w) for w in warnings]


def serialise(warnings: list[dict]) -> str | None:
    return json.dumps(warnings, ensure_ascii=False) if warnings else None
