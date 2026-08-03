"""Pipeline-level regressions for CV-anchor failures found in live data.

These run the REAL pricing pipeline (app.pricing.pipeline.run) over tiny
in-memory sold + for-sale frames — no database — pinning two opposite faults
that the same batch showed:

  * a $70k bare-CV section must NOT balloon to a $1.2M external AVM, and
  * a new build whose council CV is land-only ($610k) must NOT be pinned to
    that land value — it's valued from like-for-like sold comps (the client's
    "rule on house type, size, beds, baths and land size").
"""
from __future__ import annotations

import os

# settings instantiate at import; give them values so the lazy app.ingest import
# inside run() succeeds. No connection is ever opened — run() works on frames.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/x")
os.environ.setdefault("JWT_SECRET", "test")
os.environ.setdefault("SEED_ADMIN_EMAIL", "a@b.co")
os.environ.setdefault("SEED_ADMIN_PASSWORD", "pw")

import pandas as pd  # noqa: E402

from app.pricing.comps import SoldDataset  # noqa: E402
from app.pricing.pipeline import run  # noqa: E402


def _sold(rows: list[dict]) -> SoldDataset:
    return SoldDataset(pd.DataFrame(rows))


def test_external_avm_cannot_inflate_a_bare_cv_section():
    """CV == asking == $70k, an AVM of $1.284M. The old anchor guard pulled the
    value up to 0.95×AVM ≈ $1.22M; it must now stay anchored to the CV."""
    sold = _sold([{
        "suburb": "Testville", "district": "Testville", "property_type": "House",
        "key_bedrooms": 3, "key_bathrooms": 1,
        "key_floor_area": f"{110 + i} sqm", "key_land_area": f"{120 + i} sqm",
        "cv_numeric": 650000, "price_numeric": 650000, "sale_price": 650000,
        "land_value_numeric": 400000, "type_of_title": "Freehold",
        "sold_date": "2025-01-01",
    } for i in range(6)])
    # Subject shares no bed/size profile with the comps (studio-ish), so no
    # like-for-like comp exists to reclassify its CV as land-only.
    fs = pd.DataFrame([{
        "address": "1 Ahunga Road", "suburb": "Testville", "district": "Testville",
        "property_type": "House", "cv_numeric": 70000, "price_numeric": 70000,
        "key_floor_area": 40, "key_land_area": 115, "key_bedrooms": 1,
        "key_bathrooms": 1, "type_of_title": "Freehold", "pv_estimate_mid": 1_284_000,
    }])
    fv = run(fs, sold).iloc[0].get("fair_value")
    assert fv is not None
    assert fv <= 1.6 * 70000, f"AVM inflated a bare-CV section: {fv}"


def test_new_build_land_only_cv_is_valued_from_comps():
    """CV $610k is land only (new build, listed by negotiation → asking==CV).
    The value must come from like-for-like sold comps (~$2M), not the land CV
    ($610k) and not the over-cooked AVM ($4.15M)."""
    sold = _sold([{
        "suburb": "Millwater", "district": "Rodney", "property_type": "House",
        "key_bedrooms": 5, "key_bathrooms": 4,
        "key_floor_area": f"{300 + i * 4} sqm", "key_land_area": f"{750 + i * 5} sqm",
        "cv_numeric": 1_900_000, "price_numeric": 1_950_000 + i * 20000,
        "sale_price": 1_950_000 + i * 20000, "land_value_numeric": 900_000,
        "type_of_title": "Freehold", "sold_date": "2025-06-01",
    } for i in range(6)])
    fs = pd.DataFrame([{
        "address": "31 Pekanga Road", "suburb": "Millwater", "district": "Rodney",
        "property_type": "House", "cv_numeric": 610000, "price_numeric": 610000,
        "key_floor_area": 307, "key_land_area": 765, "key_bedrooms": 5,
        "key_bathrooms": 4, "type_of_title": "Freehold",
        "pv_estimate_mid": 4_150_000, "price_display": "Negotiation",
    }])
    fv = run(fs, sold).iloc[0].get("fair_value")
    assert fv is not None
    assert 1_400_000 <= fv <= 2_600_000, f"new build not comp-valued: {fv}"
    # Explicitly: neither the land-only CV nor the inflated AVM.
    assert fv > 1.6 * 610000
    assert abs(fv - 0.95 * 4_150_000) > 0.05 * fv


def test_ordinary_home_with_a_real_cv_is_unchanged_by_land_only_logic():
    """A normal home whose CV already matches the market must keep its CV-anchored
    value — the land-only branch must not fire when comps agree with the CV."""
    sold = _sold([{
        "suburb": "Normalton", "district": "Normalton", "property_type": "House",
        "key_bedrooms": 3, "key_bathrooms": 2,
        "key_floor_area": f"{140 + i} sqm", "key_land_area": f"{500 + i * 3} sqm",
        "cv_numeric": 800000, "price_numeric": 800000 + i * 5000,
        "sale_price": 800000 + i * 5000, "land_value_numeric": 450000,
        "type_of_title": "Freehold", "sold_date": "2025-03-01",
    } for i in range(6)])
    fs = pd.DataFrame([{
        "address": "5 Normal St", "suburb": "Normalton", "district": "Normalton",
        "property_type": "House", "cv_numeric": 800000, "price_numeric": 820000,
        "key_floor_area": 145, "key_land_area": 505, "key_bedrooms": 3,
        "key_bathrooms": 2, "type_of_title": "Freehold",
    }])
    fv = run(fs, sold).iloc[0].get("fair_value")
    assert fv is not None
    assert 0.85 * 800000 <= fv <= 1.25 * 800000, f"ordinary home mispriced: {fv}"


def test_grid_hides_margin_dollars_when_engine_withheld_the_margin():
    """6 Cassino Terrace: a by-negotiation listing (asking == CV == $500k
    placeholder). The engine withholds the margin (p.margin is None); the grid
    must not print val − asking as a $2.16M 'margin'."""
    from app.models import PropertyForSale
    from app.routers.release import _grid_row

    placeholder = PropertyForSale(
        id=1, address="6 Cassino Terrace", suburb="Mount Albert",
        property_type="House", asking_price=500000, cv_numeric=500000,
        fair_value=1_800_000, buy_price=475000, margin=None,  # engine withheld
    )
    assert _grid_row(placeholder).margin_dollars is None

    real = PropertyForSale(
        id=2, address="5 Real St", suburb="Normalton", property_type="House",
        asking_price=800000, cv_numeric=800000, fair_value=900000,
        buy_price=760000, margin=0.125,  # engine endorsed a margin
    )
    assert _grid_row(real).margin_dollars == 100000


def test_corrected_cv_overrides_a_wrong_scraped_cv():
    """6 Cassino Terrace: scraped CV $500k, CoreLogic's rating value $1.25M —
    CoreLogic (the council RV source) overrides the wrong scrape."""
    from app.staged_stages import corrected_cv
    assert corrected_cv(500000, 1_250_000) == 1_250_000


def test_corrected_cv_keeps_an_agreeing_scraped_cv():
    from app.staged_stages import corrected_cv
    assert corrected_cv(1_240_000, 1_250_000) is None   # within 10% → keep ours
    assert corrected_cv(1_250_000, 1_250_000) is None


def test_corrected_cv_needs_both_values():
    from app.staged_stages import corrected_cv
    assert corrected_cv(0, 1_250_000) is None      # blank ours → fill path handles it
    assert corrected_cv(500000, None) is None       # CoreLogic had no CV
    assert corrected_cv(500000, 0) is None
