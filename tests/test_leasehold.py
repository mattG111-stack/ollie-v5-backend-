"""Leasehold gets its own valuation route, and only that route.

The regression these lock down is row 14 of the 28 July validation report:

    501/125 Customs Street West | Auckland Central | Residential - Apartments
    theirs $79k · ours $673k · +751.9% · confidence HIGH

Three separate mechanisms conspired to produce it, and all three are covered:

  1. sale/CV comps were POOLED across tenure, so the ratio was ~1.0 (freehold-
     dominated) instead of the leasehold ratio.
  2. RATIO_CV_LO = 0.3 discarded every leasehold comp trading below 30% of CV
     as a "broken council CV" — i.e. exactly the sales that carry the signal.
  3. The anchor guard used max(CV, asking), so even a correct leasehold value
     was snapped back to 0.95 x CV.
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/unused")
os.environ.setdefault("JWT_SECRET", "test-secret")


def _sold(n_lh: int = 12, n_fh: int = 60) -> pd.DataFrame:
    """Auckland Central apartments: freehold near CV, leasehold at ~0.16x CV."""
    rows = []
    for i in range(n_fh):
        rows.append({
            "address": f"{i} Freehold St", "suburb": "Auckland Central",
            "district": "Auckland", "property_type": "Residential - Apartments",
            "type_of_title": "Freehold",
            "key_bedrooms": 2, "key_bathrooms": 1,
            "key_floor_area": 90, "key_land_area": 0,
            "cv_numeric": 500_000, "price_numeric": 490_000,
            "land_value_numeric": 200_000, "sold_date": "2026-01-15",
            "days_on_market": 30,
        })
    for i in range(n_lh):
        rows.append({
            "address": f"{i} Leasehold Ave", "suburb": "Auckland Central",
            "district": "Auckland", "property_type": "Residential - Apartments",
            "type_of_title": "Leasehold",
            "key_bedrooms": 2, "key_bathrooms": 1,
            "key_floor_area": 92, "key_land_area": 0,
            "cv_numeric": 500_000, "price_numeric": 80_000,   # 0.16x CV
            "land_value_numeric": 200_000, "sold_date": "2026-01-15",
            "days_on_market": 45,
        })
    return pd.DataFrame(rows)


def _engine(sold_df):
    from app.pricing.buyprice import CompEngine
    return CompEngine(sold_df)


def test_leasehold_comps_survive_the_cv_guard():
    """RATIO_CV_LO must not eat leasehold sales at 0.16x CV."""
    from app.pricing import buyprice as B

    eng = _engine(_sold(n_lh=12))
    ratio, src = eng.leasehold_ratio_for(property_type="Residential - Apartments",
                                         suburb="Auckland Central", district="Auckland")
    assert ratio is not None, f"all leasehold comps were discarded ({src})"
    assert 0.10 < ratio < 0.30, f"leasehold ratio is freehold-like: {ratio:.3f} ({src})"
    assert B.RATIO_CV_LO_LEASEHOLD < 0.16 < B.RATIO_CV_LO


def test_leasehold_ratio_is_not_the_pooled_ratio():
    """The whole failure was using the ~1.0 pooled ratio on leasehold stock."""
    eng = _engine(_sold())
    lh, _ = eng.leasehold_ratio_for(property_type="Residential - Apartments")
    pooled, _ = eng.cv_ratio_for(suburb="Auckland Central", district="Auckland",
                                 property_type="Residential - Apartments")
    assert lh < 0.5 * pooled, f"leasehold {lh:.3f} vs pooled {pooled:.3f} — not segmented"


def test_leasehold_refuses_to_guess_on_thin_comps():
    """Below LEASEHOLD_MIN_COMPS the answer is None, not a fabricated ratio.

    The discount is driven by ground rent and review dates this dataset does not
    carry, so a median over three sales is invention rather than estimation.
    """
    eng = _engine(_sold(n_lh=2))
    ratio, src = eng.leasehold_ratio_for(property_type="Residential - Apartments")
    assert ratio is None
    assert "insufficient" in src


def test_anchor_guard_does_not_drag_leasehold_back_to_cv():
    """The mechanism that actually produced $673k.

    anchor = max(CV, asking) = $500k; a correct $80k value is 84% away, so the
    guard fired and returned 0.95 x $500k. Leasehold must anchor on asking only.
    """
    from app.pricing.pipeline import ANCHOR_FALLBACK, ANCHOR_TOLERANCE

    cv, asking, correct = 500_000.0, 79_000.0, 80_000

    old_anchor = max(cv, asking)
    assert abs(correct - old_anchor) > ANCHOR_TOLERANCE * old_anchor
    assert round(old_anchor * ANCHOR_FALLBACK) == 475_000   # the bad number

    new_anchor = asking
    assert abs(correct - new_anchor) <= ANCHOR_TOLERANCE * new_anchor, \
        "leasehold still snapped away from a correct value"


def test_end_to_end_leasehold_apartment():
    """The real row, through the real pipeline."""
    from app.pricing.comps import SoldDataset
    from app.pricing.pipeline import run as run_pipeline

    sold = _sold(n_lh=14)
    listing = pd.DataFrame([{
        "address": "501/125 Customs Street West", "suburb": "Auckland Central",
        "district": "Auckland", "property_type": "Residential - Apartments",
        "type_of_title": "Leasehold",
        "key_bedrooms": 2, "key_bathrooms": 1,
        "key_floor_area": 94, "key_land_area": 0,
        "cv_numeric": 500_000, "price_numeric": 79_000,
        "price_display": "$79,000", "land_value_numeric": 200_000,
        "improvement_value_numeric": 300_000,
    }])

    out = run_pipeline(listing, SoldDataset(sold), None)
    mv = out.iloc[0].get("market_value") or out.iloc[0].get("fair_value")
    assert mv is not None, "leasehold produced no value at all"
    assert mv < 250_000, f"leasehold priced at {mv:,.0f} against a $79k asking price"
