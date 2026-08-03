"""Regression tests for the pricing engine.

Each test here pins a bug that was found in live Auckland data and fixed. They
are written as "this input must not produce that output" rather than golden
numbers, so they survive tuning of the assumptions but still fail if the
underlying defect returns.

Run: .venv/bin/pytest tests/ -q
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.pricing import assumptions as A
from app.pricing import zones as Z
from app.pricing.comps import parse_area_series
from app.pricing.subdivision import (
    EXCLUDED_LOCATIONS,
    SectionRates,
    SubdivisionAssumptions,
    compute,
)

MHS = "Residential - Mixed Housing Suburban Zone"


def _site(**over):
    """A feasible, freehold, profitable-ish site. Override per test."""
    base = dict(
        zone=MHS, land_area=2000.0, buy_price=1_000_000.0, section_rate=1500.0,
        property_type="House", title_type="Freehold", address="1 Test Road",
        improvement_value=300_000.0, land_value=700_000.0, cv=1_000_000.0,
        beds=4, baths=2,   # a dwelling => retain-house path (bare land is tested separately)
    )
    base.update(over)
    return compute(**base)


# --- area parsing: sold CSV stores land area as "1444 sqm" -------------------
# A bare pd.to_numeric turned every one of these into NaN, which emptied the
# per-suburb section-rate table and sent all of Auckland to the flat fallback.
def test_area_parser_handles_unit_suffixes():
    s = pd.Series(["1444 sqm", "612 sqm", "1,012 sqm", None, "bad"])
    out = parse_area_series(s)
    assert out.tolist()[:3] == [1444.0, 612.0, 1012.0]
    assert pd.isna(out.iloc[3]) and pd.isna(out.iloc[4])


def test_area_parser_passes_numeric_through():
    out = parse_area_series(pd.Series([410.0, 317.0, None]))
    assert out.tolist()[:2] == [410.0, 317.0]


def test_section_rates_build_from_unit_suffixed_land():
    sold = pd.DataFrame({
        "suburb": ["Testville"] * 4,
        "key_land_area": ["600 sqm", "800 sqm", "700 sqm", "900 sqm"],
        "land_value_numeric": [600_000, 800_000, 700_000, 900_000],
        "property_type": ["House"] * 4,
        "price_numeric": [900_000, 1_100_000, 1_000_000, 1_200_000],
    })
    sr = SectionRates(sold)
    assert sr.rate_for("Testville") == pytest.approx(1000.0)
    assert sr.source_for("Testville") == "council_land_value"


def test_section_rate_fallback_is_not_a_floor():
    """$850 applies only when a suburb has no data; a real lower rate stands."""
    sold = pd.DataFrame({
        "suburb": ["Cheapville"] * 3,
        "key_land_area": ["1000 sqm"] * 3,
        "land_value_numeric": [300_000] * 3,      # $300/m², well under the fallback
        "property_type": ["Section"] * 3,
        "price_numeric": [300_000] * 3,
    })
    sr = SectionRates(sold)
    assert sr.rate_for("Cheapville") == pytest.approx(300.0)
    assert sr.rate_for("Nowhere") == A.SECTION_RATE_FALLBACK
    assert sr.source_for("Nowhere") == "fallback"


def test_bare_section_sales_beat_council_values():
    sold = pd.DataFrame({
        "suburb": ["Testville"] * 6,
        "key_land_area": ["500 sqm"] * 6,
        "land_value_numeric": [1_000_000] * 6,           # council: $2000/m²
        "property_type": ["Section"] * 3 + ["House"] * 3,
        "price_numeric": [750_000] * 3 + [900_000] * 3,  # bare sales: $1500/m²
    })
    sr = SectionRates(sold)
    assert sr.source_for("Testville") == "bare_section_sales"
    assert sr.rate_for("Testville") == pytest.approx(1500.0)


# --- eligibility ------------------------------------------------------------
def test_non_freehold_titles_are_never_subdividable():
    for title in ("Leasehold", "Cross-Lease", "Unit Title"):
        assert _site(title_type=title).is_subdividable is False


def test_unknown_title_is_not_assumed_freehold():
    """A missing title used to skip the check entirely and pass as freehold."""
    sd = _site(title_type=None)
    assert sd.is_subdividable is False
    assert sd.section_value_method == "unknown_title"


def test_single_dwelling_zones_are_never_subdividable():
    sd = _site(zone="Residential - Single House Zone", land_area=5000.0)
    assert sd.is_subdividable is False
    assert sd.section_value_method == "single_dwelling_zone"


def test_dwelling_without_floor_is_rejected_but_bare_land_is_kept():
    """The ingest gate drops a building with no floor area (can't be size-valued
    and renders blank downstream) while keeping genuine bare land, which has no
    floor by nature. Mirrors the `dwelling_no_floor` reject in app.ingest."""
    def rejected(property_type, floor_area_m2):
        return floor_area_m2 is None and not A.is_vacant_type(property_type)

    # Dwellings with no floor -> rejected at the door (the ~130 that used to leak in)
    for t in ("独立屋", "城市屋", "公寓", "排房", "Residence"):
        assert rejected(t, None) is True
        assert rejected(t, 120.0) is False        # same type WITH a floor is kept
    # Bare-land / section types legitimately have no floor -> always kept
    for t in ("建地", "乡村住宅建地", "土地", "地皮", "Section", "Vacant land"):
        assert rejected(t, None) is False


def test_excluded_locations_produce_no_figures_at_all():
    """Prime streets must not yield numbers a caller could misread."""
    for street in EXCLUDED_LOCATIONS:
        sd = _site(address=f"66 {street.title()}", buy_price=20_000_000.0)
        assert sd.is_subdividable is False
        assert sd.sections is None and sd.subdivision_profit is None
        assert sd.section_value_method == "excluded_location"


def test_excluded_location_survives_assumption_overrides():
    sd = _site(address="66 Paritai Drive",
               assumptions=SubdivisionAssumptions(section_rate=99_999))
    assert sd.subdivision_profit is None


# --- profit -----------------------------------------------------------------
def test_missing_buy_price_gives_unknown_profit_not_free_land():
    """buy defaulted to 0.0, publishing gross sales as profit."""
    sd = _site(buy_price=None)
    assert sd.subdivision_profit is None
    assert sd.is_subdividable is False


def test_missing_land_improvement_split_gives_unknown_profit():
    assert _site(improvement_value=None, land_value=None).subdivision_profit is None


def test_bare_land_is_subdivided_whole_not_retained_as_a_house():
    """No bedrooms/bathrooms => no house to keep; sell every lot as a section.

    A bare section must not book a house resale (nor subtract a refurb on a house
    that isn't there). Its strategy is a whole-site subdivision, its gross is
    simply sections x section value, and — unlike the retain-house path — it needs
    no land/improvement split to price.
    """
    bare = _site(beds=0, baths=0, improvement_value=None, land_value=None)
    assert bare.best_strategy == f"Subdivide into {bare.sections} sections"
    assert bare.subdivision_profit is not None          # priced from buy price alone
    assert bare.gross_sales == bare.sections * bare.section_rate * bare.min_lot_m2

    # The retain-house path still applies the moment there is a dwelling.
    house = _site(beds=3, baths=1)
    assert house.best_strategy == "Retain house + sell new sections"


def test_flag_requires_positive_profit():
    """is_subdividable means 'worth subdividing', not merely splittable.

    Loss case = land already worth more per m² inside the title ($1,500) than a
    finished section fetches, so there is no uplift to pay for the works.
    """
    loss = _site(buy_price=5_000_000.0, cv=5_000_000.0,
                 improvement_value=2_000_000.0, land_value=3_000_000.0)
    assert loss.subdivision_profit is not None and loss.subdivision_profit < 0
    assert loss.is_subdividable is False
    assert loss.sections and loss.sections >= 2      # figures still returned


def _coherent_site(land_rate_per_m2: float, improvements: float = 600_000.0, **over):
    """A site whose council figures actually add up.

    CV must equal land value + improvements, and the buy price must track the
    CV — otherwise `market_ratio = buy / cv` scales a property that cannot
    exist and the arithmetic is meaningless. (Learned the hard way: an earlier
    version of this test held buy and CV fixed while varying land value, which
    priced land at 30x its section rate and inverted the result.)
    """
    land_area = over.pop("land_area", 2000.0)
    land_value = land_rate_per_m2 * land_area
    cv = land_value + improvements
    return _site(land_area=land_area, improvement_value=improvements,
                 land_value=land_value, cv=cv, buy_price=cv * 0.95, **over)


def test_profit_tracks_the_uplift_from_raw_land_to_titled_section():
    """Land and a house on land are not worth the same per m².

    The profit is the uplift between land inside the parent title and a
    finished, titled section — NOT the improvement share. A site whose land is
    already worth section money has nothing left to gain. Costing the house at
    'buy - refurb' erased this distinction and called ~98% of feasible Auckland
    sites profitable.
    """
    cheap_land = _coherent_site(200.0)     # $200/m² raw vs $1,500 section = big uplift
    dear_land = _coherent_site(1_500.0)    # raw already at section money = none
    assert cheap_land.subdivision_profit > dear_land.subdivision_profit
    assert cheap_land.is_subdividable is True
    assert dear_land.is_subdividable is False


def test_land_dearer_than_a_section_cannot_profit():
    """The Paritai-type case, without relying on the street exclusion."""
    sd = _coherent_site(3_000.0, improvements=2_000_000.0, section_rate=1500.0)
    assert sd.subdivision_profit < 0
    assert sd.is_subdividable is False


def test_profit_responds_to_every_assumption():
    base = _site().subdivision_profit
    worse = [
        SubdivisionAssumptions(services_per_section=200_000),
        SubdivisionAssumptions(selling_pct=0.10),
        SubdivisionAssumptions(acquisition_pct=0.10),
        SubdivisionAssumptions(refurb_allowance=500_000),
        SubdivisionAssumptions(incidentals_per_section=100_000),
        SubdivisionAssumptions(house_resale_pct=0.5),
    ]
    for ap in worse:
        assert _site(assumptions=ap).subdivision_profit < base
    assert _site(assumptions=SubdivisionAssumptions(section_rate=3000)).subdivision_profit > base


# --- CV-over guard ----------------------------------------------------------
# A $70k bare section (CV $70k) was surfacing a $1.22M valuation: the CV-based
# price was correct at ~$70k, but the global anchor guard pulled it UP to an
# inflated external AVM (~$1.28M) because ext_v was part of the anchor. The
# client rule: a value >60% over a credible CV re-bases onto CV × the area's
# sale/CV ratio.
def test_cv_over_guard_rebases_avm_inflated_bare_section():
    from app.pricing.pipeline import cv_over_guard
    # cv == asking (placeholder CV), area sells ~at CV, value ballooned to 1.22M.
    value, fired = cv_over_guard(1_220_000, cv_v=70_000, asking_v=70_000, area_ratio=1.0)
    assert fired is True
    assert value <= 70_000 * 1.6            # never more than 60% over CV
    assert value == 70_000                  # cv x ratio(1.0)


def test_cv_over_guard_respects_the_60pct_ceiling_in_a_hot_area():
    """A hot area's ratio can exceed 1.6, but the ceiling still caps at +60%."""
    from app.pricing.pipeline import cv_over_guard
    value, fired = cv_over_guard(5_000_000, cv_v=1_000_000, asking_v=1_000_000, area_ratio=2.5)
    assert fired is True
    assert value == 1_600_000               # min(rebased 1.6M, ceiling 1.6M)


def test_cv_over_guard_leaves_land_only_cv_alone():
    """When asking sits well above CV, the CV is land-only, not a full valuation —
    the market (asking) is the anchor and the guard must not fire."""
    from app.pricing.pipeline import cv_over_guard
    value, fired = cv_over_guard(1_900_000, cv_v=500_000, asking_v=2_000_000, area_ratio=1.0)
    assert fired is False
    assert value == 1_900_000


def test_cv_over_guard_is_a_noop_within_tolerance():
    from app.pricing.pipeline import cv_over_guard
    value, fired = cv_over_guard(1_050_000, cv_v=1_000_000, asking_v=1_000_000, area_ratio=1.05)
    assert fired is False and value == 1_050_000


def test_cv_over_guard_needs_a_cv():
    from app.pricing.pipeline import cv_over_guard
    assert cv_over_guard(1_220_000, cv_v=None, asking_v=70_000, area_ratio=1.0) == (1_220_000, False)


# --- zoning -----------------------------------------------------------------
def test_zone_classification_separates_excluded_from_unknown():
    assert Z.classify_zone(MHS) == "subdividable"
    assert Z.classify_zone("Business - City Centre Zone") == "excluded"
    assert Z.classify_zone("Some Brand New Zone") == "unknown"
    assert Z.classify_zone(None) == "missing"
    assert Z.classify_zone("") == "missing"


def test_every_zone_rule_is_also_absent_from_the_exclusion_list():
    """A zone can't be both subdividable and excluded."""
    overlap = set(Z.ZONE_RULES) & set(Z.NON_RESIDENTIAL_ZONES)
    assert not overlap, f"zones in both lists: {overlap}"


# --- rental comps ------------------------------------------------------------
# The rent CSV was missing from the handover, so cashflow fell back to
# price x a flat CV yield tier. That is circular: rent derives from price, so
# cash-on-cash had only 3 distinct values across 14k listings. These pin the
# cascade that makes rent an observation instead.
def _rent_df(rows):
    import pandas as pd
    return pd.DataFrame([
        {"weekly_rent": r, "suburb": s, "district": d, "property_type": t, "beds": b}
        for s, d, t, b, r in rows
    ])


def test_rent_lookup_prefers_suburb_type_beds():
    from app.pricing.cashflow import RentRates
    rr = RentRates(_rent_df(
        [("Papakura", "Papakura", "House", 3, r) for r in (600, 620, 640)]
        + [("Papakura", "Papakura", "Townhouse", 3, r) for r in (700, 720, 740)]
    ))
    rent, tier = rr.weekly_rent_for(suburb="Papakura", district="Papakura",
                                    property_type="House", beds=3)
    assert rent == 620 and tier == "suburb_type_beds"


def test_rent_lookup_falls_back_through_the_cascade():
    from app.pricing.cashflow import RentRates
    rr = RentRates(_rent_df([("Papakura", "Papakura", "House", 3, r) for r in (600, 620, 640)]))
    # A different type in a known suburb+beds drops to the suburb_beds tier.
    # NB canonical_type() maps anything unrecognised to "House", so this needs a
    # type that really canonicalises differently -- "Castle" would match House.
    _, tier = rr.weekly_rent_for(suburb="Papakura", district="Papakura",
                                 property_type="Apartment", beds=3)
    assert tier in ("suburb_beds", "suburb")
    # Nothing known at all -> no observation, caller uses the yield tier.
    assert rr.weekly_rent_for(suburb="Nowhere", district="Nowhere",
                              property_type="House", beds=3) == (None, None)


def test_thin_suburbs_are_not_trusted():
    """Fewer than MIN_RENTS_FOR_MEDIAN listings must not set a suburb rate."""
    from app.pricing.cashflow import RentRates
    rr = RentRates(_rent_df([("Thinville", "D", "House", 3, 600),
                             ("Thinville", "D", "House", 3, 620)]))
    assert rr.weekly_rent_for(suburb="Thinville", district="D",
                              property_type="House", beds=3) == (None, None)


def test_observed_rent_overrides_the_yield_tier():
    from app.pricing.cashflow import compute
    fallback = compute(asking_price=800_000, market_value=800_000, cv=800_000)
    observed = compute(asking_price=800_000, market_value=800_000, cv=800_000,
                       observed_weekly_rent=1_200, rent_source="suburb_type_beds")
    assert fallback.rent_source is None
    assert observed.rent_source == "suburb_type_beds"
    assert observed.est_weekly_rent == 1_200
    # With real rent the yield is an observation, so cashflow can beat the tier.
    assert observed.annual_cashflow > fallback.annual_cashflow


def test_cashflow_can_go_positive_with_strong_enough_rent():
    """Impossible under the flat yield tiers, which cap out at 5.5%.

    Break-even depends on the deposit: ~9.9% gross yield at the 10% default,
    ~7.1% at 35%. Both are above every tier, so only observed rent can get
    there.
    """
    from app.pricing.cashflow import CashflowAssumptions, compute
    args = dict(asking_price=500_000, market_value=500_000, cv=500_000,
                rent_source="suburb_beds")

    # Deposit is passed explicitly rather than relying on the default, so this
    # keeps testing the relationship if the default deposit changes again.
    thin = dict(args, observed_weekly_rent=800)          # 8.3% gross yield
    assert compute(**thin, assumptions=CashflowAssumptions(deposit_pct=0.10)
                   ).annual_cashflow < 0                  # needs ~9.9%
    assert compute(**thin, assumptions=CashflowAssumptions(deposit_pct=0.30)
                   ).annual_cashflow > 0                  # needs ~7.7%

    # A high enough rent clears break-even at any of these deposits.
    strong = dict(args, observed_weekly_rent=1_200)      # 12.5% gross yield
    for dep in (0.10, 0.30, 0.35):
        assert compute(**strong, assumptions=CashflowAssumptions(deposit_pct=dep)
                       ).annual_cashflow > 0


def test_smaller_deposit_makes_cashflow_harder():
    """More leverage = bigger loan = higher rent needed to break even."""
    from app.pricing.cashflow import CashflowAssumptions, compute
    args = dict(asking_price=800_000, market_value=800_000, cv=800_000,
                observed_weekly_rent=900)
    lo = compute(**args, assumptions=CashflowAssumptions(deposit_pct=0.10))
    hi = compute(**args, assumptions=CashflowAssumptions(deposit_pct=0.35))
    assert lo.annual_cashflow < hi.annual_cashflow


def test_cashflow_runs_off_the_buy_price_not_the_asking():
    from app.pricing.cashflow import compute
    cheap_buy = compute(asking_price=1_000_000, market_value=1_000_000,
                        cv=1_000_000, buy_price=700_000, observed_weekly_rent=900)
    on_asking = compute(asking_price=1_000_000, market_value=1_000_000,
                        cv=1_000_000, observed_weekly_rent=900)
    assert cheap_buy.annual_mortgage < on_asking.annual_mortgage
    assert cheap_buy.annual_cashflow > on_asking.annual_cashflow


# --- break-even deposit ------------------------------------------------------
# "How much cash does this actually need?" beats a yes/no against one assumed
# deposit — especially outside Auckland, where yields clear more easily.
def test_breakeven_deposit_agrees_with_the_cashflow_calc():
    """The two calculations are independent, so they must agree."""
    from app.pricing.cashflow import CashflowAssumptions, compute
    cf = compute(asking_price=500_000, market_value=500_000, cv=500_000,
                 observed_weekly_rent=738)
    bd = cf.breakeven_deposit_pct
    assert bd is not None
    at_breakeven = compute(asking_price=500_000, market_value=500_000, cv=500_000,
                           observed_weekly_rent=738,
                           assumptions=CashflowAssumptions(deposit_pct=bd))
    assert abs(at_breakeven.annual_cashflow) < 500      # ~zero by construction


def test_stronger_rent_needs_less_deposit():
    from app.pricing.cashflow import compute
    args = dict(asking_price=600_000, market_value=600_000, cv=600_000)
    weak = compute(**args, observed_weekly_rent=600).breakeven_deposit_pct
    strong = compute(**args, observed_weekly_rent=1_000).breakeven_deposit_pct
    assert strong < weak


def test_self_servicing_property_needs_no_deposit():
    """Rent covering a fully-financed mortgage clamps the deposit at 0, not below."""
    from app.pricing.cashflow import compute
    cf = compute(asking_price=300_000, market_value=300_000, cv=300_000,
                 observed_weekly_rent=1_500)
    assert cf.breakeven_deposit_pct == 0.0


def test_no_rent_means_no_breakeven_deposit():
    from app.pricing.cashflow import breakeven_deposit
    assert breakeven_deposit(0, 500_000, 0.0675, 30) is None
    assert breakeven_deposit(-1_000, 500_000, 0.0675, 30) is None
