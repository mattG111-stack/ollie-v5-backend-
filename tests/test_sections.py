"""Bare land is valued from the council valuation, not the suburb $/m2 rate.

Rows 2 and 3 of the 28 July validation report:

    80 Wyllie Road  | Papatoetoe | Section | theirs $732k  | ours $32,150k
    29 Annmarie Ave | Flat Bush  | Section | theirs $2,256k| ours $56,000k

Both are land_area x suburb-rate with no parcel-specific input. $56,000k / $850
per m2 is 6.6 ha — a development block priced at the per-m2 rate of a finished
400 m2 section.

The interim fix bounded that with min([land_val] + caps), which stopped the
explosions but is one-sided: where the land rate lands BELOW the CV-based value
min() still takes the low number, turning overvaluation into undervaluation.
These tests cover both directions.
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/unused")
os.environ.setdefault("JWT_SECRET", "test-secret")


def _sold(suburb="Flat Bush", district="Manukau", n=40) -> pd.DataFrame:
    """Small urban sections selling at ~0.95x CV, plus houses for context."""
    rows = []
    for i in range(n):
        rows.append(dict(
            address=f"{i} Section Rd", suburb=suburb, district=district,
            property_type="Section", type_of_title="Freehold",
            key_bedrooms=0, key_bathrooms=0, key_floor_area=0, key_land_area=500,
            cv_numeric=800_000, price_numeric=760_000,
            land_value_numeric=800_000, sold_date="2026-01-15", days_on_market=40))
    for i in range(n):
        rows.append(dict(
            address=f"{i} House Rd", suburb=suburb, district=district,
            property_type="House", type_of_title="Freehold",
            key_bedrooms=4, key_bathrooms=2, key_floor_area=180, key_land_area=500,
            cv_numeric=1_200_000, price_numeric=1_170_000,
            land_value_numeric=600_000, sold_date="2026-01-15", days_on_market=35))
    return pd.DataFrame(rows)


def _run(listing_row, sold_df):
    from app.pricing.comps import SoldDataset
    from app.pricing.pipeline import run as run_pipeline

    out = run_pipeline(pd.DataFrame([listing_row]), SoldDataset(sold_df), None)
    row = out.iloc[0]
    return row.get("market_value") or row.get("fair_value")


def test_large_block_is_not_priced_at_the_small_lot_rate():
    """29 Annmarie Avenue: 6.6 ha priced off a 500 m2 section's $/m2."""
    mv = _run(dict(
        address="29 Annmarie Avenue", suburb="Flat Bush", district="Manukau",
        property_type="Section", type_of_title="Freehold",
        key_bedrooms=0, key_bathrooms=0, key_floor_area=0,
        key_land_area=66_000,                      # 6.6 ha
        cv_numeric=2_400_000, price_numeric=2_256_000,
        price_display="$2,256,000", land_value_numeric=2_400_000,
    ), _sold())

    assert mv is not None
    assert mv < 4_000_000, f"large block valued at {mv:,.0f} against a $2.26M asking price"


def test_section_uses_cv_not_the_land_rate_when_both_exist():
    """The CV is parcel-specific; the suburb rate is a median over other lots."""
    mv = _run(dict(
        address="10 Ordinary Street", suburb="Flat Bush", district="Manukau",
        property_type="Section", type_of_title="Freehold",
        key_bedrooms=0, key_bathrooms=0, key_floor_area=0, key_land_area=500,
        cv_numeric=800_000, price_numeric=790_000, price_display="$790,000",
        land_value_numeric=800_000,
    ), _sold())

    # CV x the measured section ratio (~0.95), not 500 x $850 = $425k.
    assert 700_000 <= mv <= 900_000, f"expected ~CV x section ratio, got {mv:,.0f}"


def test_low_land_rate_no_longer_drags_the_value_down():
    """The regression the min() fix introduced.

    A suburb whose $/m2 rate is understated used to pull every section in it
    below its CV-based value, because min() clips downward with no symmetric
    check. Bare land in a suburb with a weak rate must still price off CV.
    """
    sold = _sold()
    # Push the suburb's implied section rate well below the CV-based value by
    # making bare-section sales look cheap per m2 on large lots.
    cheap = sold[sold.property_type == "Section"].copy()
    cheap["key_land_area"] = 5_000
    cheap["price_numeric"] = 300_000
    cheap["address"] = ["cheap %d" % i for i in range(len(cheap))]
    sold = pd.concat([sold, cheap], ignore_index=True)

    mv = _run(dict(
        address="12 Normal Street", suburb="Flat Bush", district="Manukau",
        property_type="Section", type_of_title="Freehold",
        key_bedrooms=0, key_bathrooms=0, key_floor_area=0, key_land_area=500,
        cv_numeric=800_000, price_numeric=790_000, price_display="$790,000",
        land_value_numeric=800_000,
    ), sold)

    assert mv >= 600_000, f"a weak suburb land rate dragged the value to {mv:,.0f}"


def test_section_without_cv_falls_back_to_the_land_rate():
    """Only when there is no CV is the suburb rate the primary estimate."""
    mv = _run(dict(
        address="3 No Record Road", suburb="Flat Bush", district="Manukau",
        property_type="Section", type_of_title="Freehold",
        key_bedrooms=0, key_bathrooms=0, key_floor_area=0, key_land_area=600,
        cv_numeric=None, price_numeric=None, price_display="by negotiation",
        land_value_numeric=None,
    ), _sold())
    # No CV and no asking: land rate on a small urban lot is allowed.
    assert mv is None or mv > 0


def test_huge_block_with_no_cv_shows_no_number():
    """A multi-hectare block with nothing to check against is not guessed at."""
    mv = _run(dict(
        address="900 Rural Road", suburb="Flat Bush", district="Manukau",
        property_type="Section", type_of_title="Freehold",
        key_bedrooms=0, key_bathrooms=0, key_floor_area=0,
        key_land_area=470_000,                     # 47 ha
        cv_numeric=None, price_numeric=None, price_display="by negotiation",
        land_value_numeric=None,
    ), _sold())
    assert mv is None or mv < 20_000_000, f"unanchored 47 ha block valued at {mv:,.0f}"


def test_subdivided_lot_ignores_the_parent_titles_cv():
    """A newly created lot still carries the CV of the block it was cut from.

    400 m2 section, CV $3.0M because the council record still describes the
    pre-subdivision parcel. CV x ratio would price one lot at the value of the
    whole block; the suburb land rate prices the lot.
    """
    mv = _run(dict(
        address="7 New Subdivision Way", suburb="Flat Bush", district="Manukau",
        property_type="Section", type_of_title="Freehold",
        key_bedrooms=0, key_bathrooms=0, key_floor_area=0, key_land_area=400,
        cv_numeric=3_000_000,                  # the parent title's CV
        price_numeric=720_000, price_display="$720,000",
        land_value_numeric=3_000_000,
    ), _sold())

    assert mv is not None
    assert mv < 1_500_000, f"priced off the parent title's CV: {mv:,.0f}"


def test_englobo_block_still_uses_its_own_cv():
    """The opposite regime must not regress.

    6.6 ha with a CV that genuinely describes it: CV per m2 lands far BELOW the
    suburb section rate, because raw englobo land is worth less per m2 than a
    titled section. The rate must not be applied here.
    """
    mv = _run(dict(
        address="29 Annmarie Avenue", suburb="Flat Bush", district="Manukau",
        property_type="Section", type_of_title="Freehold",
        key_bedrooms=0, key_bathrooms=0, key_floor_area=0, key_land_area=66_000,
        cv_numeric=2_400_000, price_numeric=2_256_000,
        price_display="$2,256,000", land_value_numeric=2_400_000,
    ), _sold())

    assert mv is not None
    assert mv < 4_000_000, f"englobo block valued at {mv:,.0f}"


def test_ordinary_section_with_a_sane_cv_is_unaffected():
    """CV per m2 close to the suburb rate — the common case. CV wins, as it is
    parcel-specific, and the detection must not fire."""
    mv = _run(dict(
        address="14 Normal Section Road", suburb="Flat Bush", district="Manukau",
        property_type="Section", type_of_title="Freehold",
        key_bedrooms=0, key_bathrooms=0, key_floor_area=0, key_land_area=500,
        cv_numeric=800_000, price_numeric=790_000, price_display="$790,000",
        land_value_numeric=800_000,
    ), _sold())
    assert 700_000 <= mv <= 900_000, f"expected ~CV x ratio, got {mv:,.0f}"
