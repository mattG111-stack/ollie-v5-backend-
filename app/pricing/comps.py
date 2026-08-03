"""Comp-matching valuation engine.

Replicates the client's 'Pricing Tool' Excel logic:
  Match against 2026 sold listings: same suburb, beds +/- 1, baths +/- 1,
  floor_area +/- 25%, with a min_price filter. Median sale price of comps
  is the predicted market value.

Type-aware routing:
  - Vacant/Section properties bypass comp-matching and use bare-section $/m^2
  - Commercial/Industrial properties fall back to CV
  - Everything else uses comp-matching with cascading fallbacks
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import assumptions as A


# Normalised type buckets — comp-matching should compare apples to apples.
def type_bucket(property_type: str | None) -> str:
    if not property_type:
        return "house"
    s = str(property_type).lower()
    if "apartment" in s or "ownership home unit" in s:
        return "apartment"
    if "townhouse" in s or "terrace" in s:
        return "townhouse"
    if "unit" in s:
        return "unit"
    if "lifestyle" in s and "improved" in s:
        return "lifestyle"
    if "lifestyle" in s and ("vacant" in s or "section" in s or "bare" in s):
        return "lifestyle_bare"
    if "vacant" in s or "section" in s:
        return "section"
    if "commercial" in s or "industrial" in s:
        return "commercial"
    return "house"


@dataclass
class Valuation:
    market_value: float | None
    comps_used: int
    confidence: str  # high / medium / low / insufficient
    method: str     # 'comps' | 'bare_section' | 'cv_fallback' | 'none'
    median_sale_per_m2: float | None = None
    fallback_used: bool = False
    warnings: tuple[str, ...] = ()


def parse_area_series(series: pd.Series) -> pd.Series:
    """Parse an area column that may carry a unit suffix, e.g. '1444 sqm'.

    The sold CSV stores land area as a string with units. A bare pd.to_numeric
    coerces every one of those rows to NaN, which silently emptied the
    per-suburb section-rate table and sent all of Auckland to the default $/m²
    — making every subdivision profit figure meaningless. Floor area happens to
    arrive already numeric, but is parsed through here too so a future scrape
    that adds units can't reintroduce the same failure.
    """
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")
    cleaned = series.astype(str).str.replace(",", "", regex=False)
    return pd.to_numeric(cleaned.str.extract(r"(\d+\.?\d*)", expand=False), errors="coerce")


class SoldDataset:
    """In-memory index over the sold dataset, optimised for repeated comp queries."""

    def __init__(self, sold_df: pd.DataFrame):
        df = sold_df.copy()
        # Normalise the columns we need.
        df["suburb"] = df["suburb"].astype(str).str.strip()
        df["price_numeric"] = pd.to_numeric(df["price_numeric"], errors="coerce")
        df["key_bedrooms"] = pd.to_numeric(df["key_bedrooms"], errors="coerce")
        df["key_bathrooms"] = pd.to_numeric(df["key_bathrooms"], errors="coerce")
        df["key_floor_area"] = parse_area_series(df["key_floor_area"])
        df["key_land_area"] = parse_area_series(df["key_land_area"])
        df["property_type"] = df["property_type"].astype(str).str.strip()
        df["cv_numeric"] = pd.to_numeric(df.get("cv_numeric"), errors="coerce")
        df = df[df["price_numeric"].notna() & (df["price_numeric"] >= A.COMP_MIN_PRICE)]
        df = df[df["suburb"].notna() & (df["suburb"] != "nan")]
        df["type_bucket"] = df["property_type"].map(type_bucket)
        self.df = df.reset_index(drop=True)

        # Suburb index for O(1) slice lookups.
        self._by_suburb: dict[str, pd.DataFrame] = {
            s: g for s, g in self.df.groupby("suburb")
        }

    # ---------- core lookup ----------
    def find_comps(
        self,
        suburb: str,
        beds: float | None,
        baths: float | None,
        floor_area: float | None,
        bucket: str | None = None,
        beds_tol: int = A.COMP_BEDS_TOL,
        baths_tol: int = A.COMP_BATHS_TOL,
        floor_pct: float = A.COMP_FLOOR_PCT,
        land_area: float | None = None,
        land_pct: float = A.COMP_LAND_PCT,
    ) -> pd.DataFrame:
        slice_ = self._by_suburb.get(str(suburb).strip())
        if slice_ is None or slice_.empty:
            return slice_ if slice_ is not None else self.df.iloc[0:0]
        mask = pd.Series(True, index=slice_.index)
        if bucket:
            mask &= slice_["type_bucket"] == bucket
        if beds is not None and pd.notna(beds):
            mask &= (slice_["key_bedrooms"] - beds).abs() <= beds_tol
        if baths is not None and pd.notna(baths):
            mask &= (slice_["key_bathrooms"] - baths).abs() <= baths_tol
        if floor_area is not None and pd.notna(floor_area) and floor_area > 0:
            lo = floor_area * (1 - floor_pct)
            hi = floor_area * (1 + floor_pct)
            mask &= slice_["key_floor_area"].between(lo, hi) | slice_["key_floor_area"].isna()
        if land_area is not None and pd.notna(land_area) and land_area > 0:
            mask &= (slice_["key_land_area"].between(
                land_area * (1 - land_pct), land_area * (1 + land_pct))
                | slice_["key_land_area"].isna())
        return slice_[mask]

    # ---------- bare-section pricing ----------
    def bare_section_per_m2(self, suburb: str) -> tuple[float, int]:
        slice_ = self._by_suburb.get(str(suburb).strip())
        if slice_ is None:
            return A.DEFAULT_SECTION_PRICE_PER_M2, 0
        vacant = slice_[slice_["property_type"].str.lower().str.contains("vacant|section", na=False)]
        vacant = vacant[vacant["key_land_area"].notna() & (vacant["key_land_area"] > 50)]  # filter tiny lots
        if vacant.empty:
            return A.DEFAULT_SECTION_PRICE_PER_M2, 0
        rates = vacant["price_numeric"] / vacant["key_land_area"]
        # Drop extremes so a single misclassified row doesn't blow up the median.
        rates = rates[(rates >= A.SECTION_PRICE_MIN) & (rates <= A.SECTION_PRICE_MAX)]
        if rates.empty:
            return A.DEFAULT_SECTION_PRICE_PER_M2, 0
        return float(rates.median()), int(len(rates))


def valuate(
    *,
    suburb: str | None,
    property_type: str | None,
    beds: float | None,
    baths: float | None,
    floor_area: float | None,
    land_area: float | None,
    cv: float | None,
    sold: SoldDataset,
    asking_price: float | None = None,
) -> Valuation:
    """Run the full type-aware valuation pipeline for one subject property."""
    warnings: list[str] = []

    if not suburb or not str(suburb).strip() or str(suburb).strip().lower() == "nan":
        return Valuation(None, 0, "insufficient", "none", warnings=("no_suburb",))

    # === Bare-land / section route ===
    if A.is_vacant_type(property_type):
        if land_area is None or not pd.notna(land_area) or land_area <= 0:
            return Valuation(None, 0, "insufficient", "bare_section", warnings=("no_land_data",))
        per_m2, n_comps = sold.bare_section_per_m2(suburb)
        if n_comps == 0:
            warnings.append("default_section_rate")
        mv = per_m2 * land_area
        # Lifestyle / rural blocks blow out the simple land*rate formula because per_m2
        # is calibrated for residential 500-1000 m² sections. Clamp against CV when we have it.
        if cv is not None and pd.notna(cv) and cv > 0 and mv > cv * 2.5:
            mv = float(cv)
            warnings.append("cv_clamped")
        if mv > 15_000_000:  # absolute residential ceiling
            mv = float(cv) if (cv is not None and pd.notna(cv) and cv > 0) else 15_000_000
            warnings.append("ceiling_clamped")
        return Valuation(
            market_value=round(mv, -3),
            comps_used=n_comps,
            confidence=A.confidence_tier(n_comps),
            method="bare_section",
            median_sale_per_m2=per_m2,
            warnings=tuple(warnings),
        )

    # === Commercial / industrial route ===
    if A.is_commercial_type(property_type):
        if cv is None or not pd.notna(cv) or cv <= 0:
            return Valuation(None, 0, "insufficient", "cv_fallback", warnings=("no_cv",))
        return Valuation(
            market_value=round(float(cv), -3),
            comps_used=0,
            confidence="low",
            method="cv_fallback",
            warnings=("commercial_uses_cv",),
        )

    # === Standard residential comp-matching ===
    bucket = type_bucket(property_type)
    comps = sold.find_comps(suburb, beds, baths, floor_area, bucket=bucket)
    if comps.empty or len(comps) < 2:
        # Fallback 1: drop the type-bucket filter (compare across types).
        comps = sold.find_comps(suburb, beds, baths, floor_area)
        if not comps.empty:
            warnings.append("fallback_type_relaxed")
    if comps.empty:
        # Fallback 2: relax beds/baths/floor tolerances.
        comps = sold.find_comps(
            suburb, beds, baths, floor_area, beds_tol=2, baths_tol=2, floor_pct=0.5,
        )
        if not comps.empty:
            warnings.append("fallback_filters_relaxed")
    if comps.empty:
        # Fallback 3: suburb-wide median.
        comps = sold.find_comps(suburb, None, None, None, beds_tol=99, baths_tol=99, floor_pct=99)
        if not comps.empty:
            warnings.append("fallback_suburb_wide")

    if comps.empty:
        # Last resort: CV fallback
        if cv is not None and pd.notna(cv) and cv > 0:
            return Valuation(
                market_value=round(float(cv), -3),
                comps_used=0,
                confidence="insufficient",
                method="cv_fallback",
                warnings=("no_comps_fell_back_to_cv",),
            )
        return Valuation(None, 0, "insufficient", "none", warnings=("no_comps_no_cv",))

    median_price = float(comps["price_numeric"].median())
    floor_areas = comps["key_floor_area"].dropna()
    floor_areas = floor_areas[floor_areas > 0]
    if not floor_areas.empty:
        per_m2_series = comps.loc[floor_areas.index, "price_numeric"] / floor_areas
        median_per_m2 = float(per_m2_series.median())
    else:
        median_per_m2 = None

    n = int(len(comps))
    market_value = round(median_price, -3)
    confidence = A.confidence_tier(n)

    # Sanity gate — if our estimate is way off the asking price, the comps
    # were probably inappropriate. Downgrade to insufficient so the listing
    # doesn't get flagged as "underpriced" off bad data.
    if asking_price and asking_price > 0:
        ratio = market_value / asking_price
        if ratio > A.ESTIMATE_VS_ASKING_MAX_RATIO or ratio < A.ESTIMATE_VS_ASKING_MIN_RATIO:
            warnings.append("estimate_vs_asking_out_of_band")
            confidence = "insufficient"

    return Valuation(
        market_value=market_value,
        comps_used=n,
        confidence=confidence,
        method="comps",
        median_sale_per_m2=median_per_m2,
        fallback_used="fallback_filters_relaxed" in warnings,
        warnings=tuple(warnings),
    )
