"""New builds on subdivided sites whose CV still covers the whole original lot.

The council rating record lags the subdivision, so a new house on one of four
new titles carries the CV of the undivided parent site. Every CV-anchored number
inherits that: the valuation lands far above what the house is worth, the margin
against asking looks enormous, and it surfaces at the top of the deal list.

The tell is that the ASKING PRICE and the SOLD DATA agree with each other and the
CV does not — a vendor prices a new build against its neighbours, and only the
council record is stale. An expensive house, by contrast, has an asking price
that agrees with its high CV, and must not be flagged.
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/unused")
os.environ.setdefault("JWT_SECRET", "test-secret")


def _sold(n=60, suburb="Hobsonville", district="Waitakere"):
    """A suburb where 150 m2 houses sell for about $1.05m (~$7,000/m2)."""
    rows = []
    for i in range(n):
        rows.append(dict(
            address=f"{i} Comparable Way", suburb=suburb, district=district,
            property_type="House", type_of_title="Freehold",
            key_bedrooms=4, key_bathrooms=2,
            key_floor_area=150, key_land_area=350,
            cv_numeric=1_100_000, price_numeric=1_050_000,
            land_value_numeric=500_000, sold_date="2026-01-15", days_on_market=35))
    return pd.DataFrame(rows)


def _run(listing):
    from app.pricing.comps import SoldDataset
    from app.pricing.pipeline import run as run_pipeline

    out = run_pipeline(pd.DataFrame([listing]), SoldDataset(_sold()), None)
    row = out.iloc[0]
    return row.get("market_value") or row.get("fair_value"), row.get("cv_flag")


def _new_build(**kw):
    """A new house on a subdivided lot. CV is the whole parent site."""
    base = dict(
        address="2/14 Subdivided Road", suburb="Hobsonville", district="Waitakere",
        property_type="House", type_of_title="Freehold",
        key_bedrooms=4, key_bathrooms=2,
        key_floor_area=150, key_land_area=350,
        cv_numeric=3_200_000,           # the ENTIRE pre-subdivision site
        price_numeric=1_050_000,        # priced against the neighbours
        price_display="$1,050,000",
        land_value_numeric=2_600_000,
        improvement_value_numeric=600_000,
    )
    base.update(kw)
    return base


def test_parent_lot_cv_is_flagged():
    _value, flag = _run(_new_build())
    assert flag == "suspect", "a parent-lot CV went through unflagged"


def test_parent_lot_cv_does_not_mint_a_fake_deal():
    """Without this the value lands near CV and shows a ~200% margin."""
    value, _flag = _run(_new_build())
    assert value is not None
    assert value < 1_500_000, (
        f"valued at {value:,.0f} against a $1.05m asking price — the stale CV "
        f"still drove the number")


def test_expensive_house_is_not_flagged():
    """A genuinely superior property has an asking price that AGREES with its
    high CV. Only a disagreement between CV and asking indicates a stale record."""
    _value, flag = _run(_new_build(
        cv_numeric=3_200_000,
        price_numeric=3_100_000,      # vendor agrees with the CV
        price_display="$3,100,000",
    ))
    assert flag != "suspect", "an expensive house was flagged as a data fault"


def test_ordinary_house_is_not_flagged():
    _value, flag = _run(_new_build(
        cv_numeric=1_100_000, price_numeric=1_050_000, price_display="$1,050,000"))
    assert flag != "suspect"


def test_flagged_row_is_held_from_publishing():
    """The flag must reach the publish gate, not just sit in a column."""
    from app.models import PropertyForSale
    from app.release import _hold_reason

    p = PropertyForSale(
        import_batch_id=1, address="2/14 Subdivided Road", suburb="Hobsonville",
        property_type="House", floor_area_m2=150.0, land_area_m2=350.0,
        cv_numeric=3_200_000.0, asking_price=1_050_000.0,
        fair_value=1_050_000.0, confidence="high", cv_flag="suspect",
    )
    reason = _hold_reason(p)
    assert reason is not None
    assert "subdivision" in reason
