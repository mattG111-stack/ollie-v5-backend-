"""Subdivision feasibility + profit (client v4 spec, confirmed June 2026).

Feasibility:
  - Zones that allow only 1 dwelling per lot (Single House, Large Lot, Rural)
    are NEVER subdividable, regardless of land size
    (client: "single house zone is exactly that one house cannot be subdivided at all").
  - Multi-dwelling zones (Mixed Housing, Terrace & Apartment) are land-driven:
      usable land  = land × (1 − road allowance)        [allowance 10%]
      sections     = FLOOR(usable land ÷ zone min lot)
      feasible     = sections ≥ 2
      subdividable = feasible AND profit > 0   (a site that loses money is not
                     an opportunity; the figures are still returned so the
                     detail page can show why the answer is no)
      dwellings    = sections × max dwellings per lot

Profit (§6):
      section value  = section $/m² rate × zone min lot
      gross sales    = sections × section value
      subdivision cost = (new sections) × $80,000   [new = sections − 1; dev contribution + services]
      selling cost   = gross sales × 4%   [real-estate cost per site]
      acquisition cost = buy price × 2%
      profit = gross sales − buy price − subdivision cost − selling cost − acquisition cost

Section $/m² rate is derived automatically from the sold data (see SectionRates),
never a manual table — bare section sales first, council land values second,
$850/m² as a fallback when the suburb has neither.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from . import assumptions as A
from . import zones as Z
from .comps import parse_area_series

ROAD_ALLOWANCE = 0.10        # legacy flat figure; superseded by _road_allowance()
SERVICES_PER_SECTION = 130_000   # earthworks + roads + 3-waters + power + consent, per lot
BUILD_RATE_PER_M2 = 2_800    # replacement build cost $/m² — values the retained house's building
HOLDING_RATE = 0.07          # finance/holding, PER YEAR, on (buy + services)
HOLDING_YEARS = 2.0          # a subdivision ties money up ~2 years before sections sell
CONTINGENCY_RATE = 0.05      # contingency on the development spend
GST_RATE = 0.15             # net GST on the development margin (NZ 15%)
SELLING_PCT = 0.04          # real-estate cost per site sold
ACQUISITION_PCT = 0.02
# Reality check: if the modelled gross sale of the finished sections towers over
# the site's current market value (CV) by more than this, the section rate is
# being applied to bulk/englobo land the market priced far lower — a fantasy gain
# the market would have bid away if it were real (1 Pleasant Rd: $13.5M gross on a
# $1.4M CV). Above it, the site is not flagged as an opportunity.
GROSS_VS_CV_CAP = 4.0
# Bad-data guard: an urban subdividable site whose CV works out below this per m²
# has a corrupt land area (or CV) — 1 Pleasant Rd's feed said 21,109 m² for a
# 2,109 m² site, a 10x digit error giving $66/m². Real urban Auckland land is
# never this cheap, so a subdivision computed off it is fiction.
CV_PER_M2_FLOOR = 150.0
NO_LIMIT_THRESHOLD = 99      # max-dwellings ≥ this = "no per-lot limit" (Terrace/Apartment)

# --- Terrace Housing & Apartment Building (THAB) ------------------------------
# THAB density is built-form driven (height, coverage, HIRB), NOT minimum lot
# size. Modelling it with the old 1,200 m² "min lot" meant a THAB site needed
# ~2,670 m² just to register 2 lots, so real terrace sites never flagged — e.g.
# 42 Cape Road, Mangere: 769 m² consented (LUC60402550 / SUB60402551) to FIVE
# terraces of 98–119 m² each, which the old model returned as "not subdividable".
# So THAB is modelled as a build-and-sell TERRACE development, not a bare-section
# split: yield off a realistic per-dwelling footprint, then a proper pro-forma
# (land + build → sale). All figures are tunable — override per property via the
# /subdivision-scenario endpoint.
THAB_LOT_M2 = 120.0            # fee-simple land per terrace dwelling
THAB_ACCESS_ALLOWANCE = 0.15  # share of the site lost to shared driveway / access lot
THAB_TERRACE_FLOOR = 105.0    # typical terrace floor area (m²) built per dwelling
# Finished-terrace sale price, per m² of floor. This is the value driver and the
# key tunable — new Auckland terraces sell on $/m², not cost-plus. Suburb-specific
# ideally (comp-driven); this is a sensible default until wired to sold comps.
THAB_TERRACE_SALE_PER_M2 = 7_500.0
THAB_SERVICES_PER_UNIT = 50_000  # consent + connections per terrace (shared civils)


def _road_allowance(prelim_lots: int) -> float:
    """Share of the site lost to roads, access and reserves. A rear-lot split
    barely touches it; a 15-lot block needs an internal road and stormwater
    reserves, so the loss climbs with scale."""
    if prelim_lots <= 2:
        return 0.10
    if prelim_lots <= 6:
        return 0.20
    return 0.30


def _building_value(floor_area, improvements, build_rate: float) -> float | None:
    """What the house's building is worth. Floor area × replacement rate is far
    closer to reality than the council 'improvement value', which runs well below
    real build cost; the council figure is only a fallback when floor is unknown."""
    if _is_number(floor_area) and float(floor_area) > 0:
        return float(floor_area) * build_rate
    if _is_number(improvements):
        return float(improvements)
    return None


def _is_number(x) -> bool:
    try:
        return float(x) == float(x)  # NaN != NaN
    except (TypeError, ValueError):
        return False


def _normalise_type(property_type: str | None) -> str:
    if not property_type:
        return ""
    return str(property_type).strip().lower()


# Multi-unit dwellings can't be subdivided by a single unit-owner (shared lot).
_NON_SUBDIVIDABLE_TYPES = {
    "apartment", "unit", "townhouse", "studio", "flat", "duplex",
    "公寓", "排房", "城市屋", "单元", "联排别墅",
}
# Only Freehold gives an owner the dividable land.
_SUBDIVIDABLE_TITLES = {"freehold"}

# Locations never treated as subdivision opportunities, whatever the maths says.
# Prime streets where the value is in the position rather than the land: the
# section-rate model prices the dirt at the suburb median, which is nowhere near
# what these sites actually cost, so any "profit" it reports is an artefact.
# Matched as a case-insensitive substring of the address. Extend as needed.
EXCLUDED_LOCATIONS = (
    "paritai drive",
)


@dataclass
class Subdivision:
    zone: str | None
    min_lot_m2: float | None
    is_subdividable: bool
    sections: int | None          # number of sections (lots) the site splits into
    dwellings: int | None         # sections × max dwellings per lot (None for "no limit" zones)
    section_rate: float | None    # $/m² used (from sold land values)
    gross_sales: float | None     # sections × section value
    subdivision_profit: float | None
    # Legacy fields kept so the rest of the pipeline/DB keep working.
    max_addl_lots: float | None = None       # sections − 1
    section_price_per_m2: float | None = None
    section_value_method: str = "none"
    services_cost: float = 0.0
    total_subdivided_value: float | None = None  # = gross_sales
    uplift_vs_asking: float | None = None
    best_strategy: str | None = None
    best_net_gain: float | None = None           # = subdivision_profit (feeds the buy score)
    subdivision_premium: float | None = None     # = subdivision_profit (kept for older UI)


def _not_subdividable(zone, min_lot, method="none") -> Subdivision:
    return Subdivision(
        zone=zone, min_lot_m2=min_lot, is_subdividable=False,
        sections=None, dwellings=None, section_rate=None,
        gross_sales=None, subdivision_profit=None,
        max_addl_lots=None, section_value_method=method,
    )


@dataclass(frozen=True)
class SubdivisionAssumptions:
    """Every tunable number behind the profit figure, in one place.

    Defaults are the client's confirmed figures. A developer can override any of
    them per property (see the /subdivision-scenario endpoint) to run their own
    numbers without the stored batch values changing.
    """
    services_per_section: float = SERVICES_PER_SECTION   # consent + dev contribution + services
    selling_pct: float = SELLING_PCT                     # real-estate cost per site sold
    acquisition_pct: float = ACQUISITION_PCT             # legal/finance on the purchase
    refurb_allowance: float = 100_000                    # subdivision work + refurb on the retained house
    house_resale_pct: float = 1.00                       # multiplier on the computed house resale
    section_rate: float | None = None                    # finished section $/m²; None = suburb rate
    incidentals_per_section: float = 0.0                 # anything the model doesn't know about
    build_rate: float = BUILD_RATE_PER_M2                # $/m² replacement cost for the retained building
    holding_rate: float = HOLDING_RATE                   # finance/holding PER YEAR, on (buy + services)
    holding_years: float = HOLDING_YEARS                 # how long the money is tied up
    contingency_rate: float = CONTINGENCY_RATE           # contingency on the development spend
    gst_rate: float = GST_RATE                           # net GST on the development margin
    # --- the land/house split (council figures by default, all overridable) ---
    improvement_value: float | None = None               # what the buildings are worth
    raw_land_rate: float | None = None                   # $/m² of land INSIDE the parent title
    market_ratio: float | None = None                    # scales council values to market

    def merged_rate(self, suburb_rate: float | None) -> float:
        if self.section_rate and self.section_rate > 0:
            return float(self.section_rate)
        return float(suburb_rate) if (suburb_rate and suburb_rate > 0) else A.SECTION_RATE_FALLBACK


MIN_SALES_FOR_RATE = 3


class SectionRates:
    """Per-suburb section $/m², resolved in priority order:

      1. **Bare section sales** — median (sale price ÷ land area) over vacant-land
         sold records. What a section in that suburb actually sells for, so it is
         always preferred when available.
      2. **Council land values** — median (land value ÷ land area) over all sold
         records. A rating figure, not a market price, so it is second best.
      3. **Fallback** — A.SECTION_RATE_FALLBACK ($850/m²), used only when a suburb
         has neither. Not a floor: a real rate below it is still used as-is.

    Each tier needs MIN_SALES_FOR_RATE records in the suburb to be trusted.
    Built once per ingest.
    """

    def __init__(self, sold_df: pd.DataFrame, default_rate: float | None = None):
        df = sold_df.copy()
        self._bare: dict[str, float] = {}
        self._council: dict[str, float] = {}
        self.default = float(default_rate or A.SECTION_RATE_FALLBACK)

        if "suburb" not in df.columns:
            return
        suburb = df["suburb"].astype(str).str.strip()

        land_area = None
        for c in ("land_area_m2", "key_land_area"):
            if c in df.columns:
                land_area = parse_area_series(df[c])
                break
        if land_area is None:
            return

        # --- tier 1: bare section sales ---
        if "property_type" in df.columns and "price_numeric" in df.columns:
            is_vacant = df["property_type"].apply(A.is_vacant_type)
            price = pd.to_numeric(df["price_numeric"], errors="coerce")
            self._bare = self._median_by_suburb(suburb[is_vacant], (price / land_area)[is_vacant])

        # --- tier 2: council land values ---
        land_val = None
        for c in ("land_value_numeric", "land_value"):
            if c in df.columns:
                land_val = parse_area_series(df[c])
                break
        if land_val is not None:
            self._council = self._median_by_suburb(suburb, land_val / land_area)

    @staticmethod
    def _median_by_suburb(suburb: pd.Series, rate: pd.Series) -> dict[str, float]:
        work = pd.DataFrame({"suburb": suburb, "rate": rate}).dropna()
        work = work[(work["rate"] > 50) & (work["rate"] < 50_000)]
        if work.empty:
            return {}
        grp = work.groupby("suburb")["rate"]
        med, counts = grp.median(), grp.size()
        return {s: float(med[s]) for s in med.index if counts[s] >= MIN_SALES_FOR_RATE}

    def rate_for(self, suburb: str | None) -> float:
        if suburb:
            key = str(suburb).strip()
            return self._bare.get(key) or self._council.get(key) or self.default
        return self.default

    def source_for(self, suburb: str | None) -> str:
        """Which tier supplied rate_for() — for display and admin QA."""
        if suburb:
            key = str(suburb).strip()
            if self._bare.get(key):
                return "bare_section_sales"
            if self._council.get(key):
                return "council_land_value"
        return "fallback"

    def as_table(self) -> list[dict]:
        """For the admin upload tab — sorted list of suburb rates + their source."""
        merged = {**self._council, **self._bare}  # bare wins where both exist
        return [
            {"suburb": s, "rate": round(r), "source": self.source_for(s)}
            for s, r in sorted(merged.items(), key=lambda kv: -kv[1])
        ]


def _terrace_development(
    zone: str | None, min_lot: float, land_area: float, buy: float | None,
    section_rate: float, has_dwelling: bool, cv: float | None,
    ap: "SubdivisionAssumptions", rate_source: str | None,
) -> Subdivision:
    """THAB build-and-sell terrace development.

    Yield is set by a per-dwelling footprint, not the zone min lot: a THAB site
    fits FLOOR(usable land / THAB_LOT_M2) terraces after a shared-access
    allowance. Each terrace is built (floor × build rate) and sold at a market
    uplift over land + build; profit is gross realisation less land, build,
    services, selling, acquisition, holding, contingency and GST. An existing
    house is demolished (these are attached party-wall builds, not retain-and-
    subdivide). Returns the terrace lot size as `min_lot_m2` so the UI shows the
    real per-dwelling area, not the zone's nominal 1,200 m²."""
    usable = land_area * (1.0 - THAB_ACCESS_ALLOWANCE)
    n = int(usable // THAB_LOT_M2)
    n = min(n, A.MAX_PRACTICAL_LOTS_TOTAL)
    if n < 2:
        return _not_subdividable(zone, THAB_LOT_M2, "thab_too_small")

    strategy = (f"Demolish and build {n} terraces" if has_dwelling
                else f"Build {n} terraces")
    # Feasible, but with no buy price the return is unknowable — flag the yield,
    # withhold the profit (mirrors the bare-section path).
    if buy is None:
        return Subdivision(
            zone=zone, min_lot_m2=THAB_LOT_M2, is_subdividable=False,
            sections=n, dwellings=n, section_rate=round(section_rate),
            gross_sales=None, subdivision_profit=None, max_addl_lots=float(n),
            section_price_per_m2=round(section_rate), section_value_method="thab_terraces",
            best_strategy=strategy,
        )

    # Revenue is the market sale price of each finished terrace ($/m² of floor);
    # the land is already paid for via `buy`, so it is a cost, not a revenue line.
    build_per = THAB_TERRACE_FLOOR * ap.build_rate
    sale_per = THAB_TERRACE_FLOOR * THAB_TERRACE_SALE_PER_M2
    gross = n * sale_per
    build_total = n * build_per
    services = n * THAB_SERVICES_PER_UNIT
    demolition = ap.refurb_allowance if has_dwelling else 0.0
    selling = gross * ap.selling_pct
    acquisition = buy * ap.acquisition_pct
    holding = (buy + build_total + services) * ap.holding_rate * ap.holding_years
    profit = gross - buy - build_total - services - selling - acquisition - demolition - holding

    contingency = ap.contingency_rate * (build_total + services + holding + demolition)
    gst = ap.gst_rate * max(profit - contingency, 0.0)
    profit = profit - contingency - gst

    dev_cost = round(build_total + services)   # what's spent to build (beyond the land)
    return Subdivision(
        zone=zone, min_lot_m2=THAB_LOT_M2, is_subdividable=(profit > 0),
        sections=n, dwellings=n, section_rate=round(section_rate),
        gross_sales=round(gross), subdivision_profit=round(profit),
        max_addl_lots=float(n), section_price_per_m2=round(section_rate),
        section_value_method="thab_terraces", services_cost=dev_cost,
        total_subdivided_value=round(gross), best_strategy=strategy,
        best_net_gain=round(profit),
        subdivision_premium=round(profit) if profit > 0 else None,
    )


def compute(
    *,
    zone: str | None,
    land_area: float | None,
    buy_price: float | None,
    section_rate: float | None,
    property_type: str | None = None,
    title_type: str | None = None,
    rate_source: str | None = None,
    address: str | None = None,
    improvement_value: float | None = None,
    land_value: float | None = None,
    cv: float | None = None,
    beds: float | None = None,
    baths: float | None = None,
    floor_area: float | None = None,
    force_full_subdivision: bool = False,
    assumptions: "SubdivisionAssumptions | None" = None,
) -> Subdivision:
    rule = Z.lookup(zone)
    if rule is None or rule.min_lot_m2 is None:
        return _not_subdividable(zone, None)

    # Excluded location — checked before any maths so no figure is ever produced.
    addr = str(address).strip().lower() if address else ""
    if addr and any(loc in addr for loc in EXCLUDED_LOCATIONS):
        return _not_subdividable(zone, float(rule.min_lot_m2), "excluded_location")

    # Multi-unit types and non-freehold titles can't be subdivided.
    if any(t in _normalise_type(property_type) for t in _NON_SUBDIVIDABLE_TYPES):
        return _not_subdividable(zone, float(rule.min_lot_m2), "excluded_by_type")
    # Only freehold gives an owner the dividable land, so the title must be
    # positively known to be freehold. A missing title previously skipped this
    # check entirely and was treated as freehold by default — the permissive
    # direction, on the one attribute that decides eligibility outright.
    norm_title = str(title_type).strip().lower() if isinstance(title_type, str) else ""
    if norm_title not in _SUBDIVIDABLE_TITLES:
        reason = "excluded_by_title" if norm_title else "unknown_title"
        return _not_subdividable(zone, float(rule.min_lot_m2), reason)

    min_lot = float(rule.min_lot_m2)

    # 1-dwelling-per-lot zones (Single House, Large Lot, Rural) → never subdividable.
    if rule.max_dwellings is not None and int(rule.max_dwellings) <= 1:
        return _not_subdividable(zone, min_lot, "single_dwelling_zone")

    if not _is_number(land_area) or float(land_area) <= 0:
        return _not_subdividable(zone, min_lot)

    ap = assumptions or SubdivisionAssumptions()
    buy0 = float(buy_price) if _is_number(buy_price) and float(buy_price) > 0 else None
    has_dwelling0 = ((_is_number(beds) and float(beds) > 0)
                     or (_is_number(baths) and float(baths) > 0))

    # THAB (no per-lot limit) is a terrace/apartment development, not a bare-section
    # split — route it to the build-and-sell pro-forma so small terrace sites are
    # valued instead of returned as "not subdividable" (see _terrace_development).
    if rule.max_dwellings is not None and int(rule.max_dwellings) >= NO_LIMIT_THRESHOLD:
        return _terrace_development(
            zone, min_lot, float(land_area), buy0, ap.merged_rate(section_rate),
            has_dwelling0, cv, ap, rate_source)

    # --- feasibility (multi-dwelling, land-driven) ---
    # Road/reserve loss scales with how many lots the site could take: a bigger
    # block needs an internal road and stormwater reserves, not just a right-of-way.
    prelim_lots = math.floor(float(land_area) / min_lot)
    usable = float(land_area) * (1.0 - _road_allowance(prelim_lots))
    sections = math.floor(usable / min_lot)
    sections = min(sections, A.MAX_PRACTICAL_LOTS_TOTAL)
    if sections < 2:
        return _not_subdividable(zone, min_lot)

    per_lot = int(rule.max_dwellings) if rule.max_dwellings else 1
    dwellings = None if per_lot >= NO_LIMIT_THRESHOLD else sections * per_lot
    addl = sections - 1

    # --- profit ---
    # Keep the existing house, subdivide the surplus land into new sections,
    # resell the house on its reduced lot. The new sections are the profit.
    #
    # Land and a house on land are not worth the same per m², so the retained
    # property is rebuilt from its parts rather than from the purchase price:
    #
    #   house resale = (improvements + retained land × raw land $/m²)
    #                  × market ratio × resale% − refurb
    #   gross        = house resale + new sections × section $/m² × min lot
    #   profit       = gross − buy − services − selling − acquisition − incidentals
    #
    # Costing the house at "buy price − refurb" instead would sell the same land
    # twice: `buy` already includes all the land, so reselling the house at par
    # while also selling the sections books that land as revenue on both sides.
    # That version called ~98% of feasible Auckland sites profitable. Splitting
    # land from improvements values every m² once, and the profit becomes the
    # genuine uplift from raw land inside a title to a finished, titled section.
    ap = assumptions or SubdivisionAssumptions()
    rate = ap.merged_rate(section_rate)
    section_value = rate * min_lot
    new_sections_value = addl * section_value
    subdivision_cost = addl * ap.services_per_section
    incidentals = addl * ap.incidentals_per_section

    improvements = ap.improvement_value if ap.improvement_value is not None else improvement_value
    raw_rate = ap.raw_land_rate
    if raw_rate is None and _is_number(land_value) and float(land_value) > 0:
        raw_rate = float(land_value) / float(land_area)

    buy = float(buy_price) if _is_number(buy_price) and float(buy_price) > 0 else None

    # Is there actually a dwelling to keep? A listing with no bedrooms and no
    # bathrooms — a bare section or building site — has no house to retain and
    # resell, so the "retain house + sell the surplus" strategy is meaningless.
    # Booking a house resale for it (and subtracting a refurb on a house that
    # isn't there) overstates the return. Those sites are subdivided whole: every
    # lot sells as a bare section.
    has_dwelling = ((_is_number(beds) and float(beds) > 0)
                    or (_is_number(baths) and float(baths) > 0))

    # `force_full_subdivision` lets a developer model demolishing an existing
    # house to subdivide the whole site — the same whole-site maths as bare land,
    # but with a demolition/works allowance since a house had to come down.
    demolish = bool(force_full_subdivision) and has_dwelling
    subdivide_whole = bool(force_full_subdivision) or not has_dwelling

    # Finance/holding over the ~2-year project life, charged on the money tied up
    # (purchase + servicing). A real cost every subdivision carries and the model
    # used to ignore.
    holding_cost = ((float(buy) + subdivision_cost) * ap.holding_rate * ap.holding_years
                    if buy is not None else 0.0)
    # Only a demolition (a house was there to remove) carries the works allowance;
    # genuinely bare land has nothing to knock down.
    demolition_cost = ap.refurb_allowance if demolish else 0.0

    if subdivide_whole:
        best_strategy = (f"Demolish and subdivide into {sections} sections"
                         if demolish else f"Subdivide into {sections} sections")
        # Whole-site subdivision only needs a buy price to value; there is no
        # retained house, so the improvement split is irrelevant.
        if buy is None:
            profit = None
            gross_sales = None
        else:
            gross_sales = sections * section_value        # every lot sells as a section
            selling_cost = gross_sales * ap.selling_pct
            acquisition_cost = buy * ap.acquisition_pct
            profit = (gross_sales - buy - subdivision_cost - selling_cost
                      - acquisition_cost - incidentals - demolition_cost - holding_cost)
    else:
        best_strategy = "Retain house + sell new sections"
        # A developer keeps the house on a single minimum-sized lot and turns every
        # other usable metre into a section — a 140 m² house does not sit on 1,000+
        # m². So the retained lot is ONE min lot (never the road reserve, which is
        # excluded via `usable`), and it is a finished residential section, so its
        # land is worth the section rate — not the whole block's cheap council $/m².
        # The building is valued at replacement cost (floor × build rate), which is
        # far closer to reality than the council improvement figure.
        #
        # No buy price (typically "by negotiation" with no asking) means the profit
        # is unknowable, not zero-cost; without any building figure it is too.
        # An explicit "buildings worth" override wins; otherwise value at replacement.
        building_value = (float(ap.improvement_value) if ap.improvement_value is not None
                          else _building_value(floor_area, improvement_value, ap.build_rate))
        if buy is None or building_value is None:
            profit = None
            gross_sales = None
        else:
            # The retained house keeps ONE residential lot (plus any sub-lot
            # remainder) — never the surplus land the practical-lot cap left
            # undeveloped. On a large block `sections` is capped (see above), so
            # `usable − addl×min_lot` balloons to tens of thousands of m² and would
            # book a phantom multi-million-dollar "house". Bound it to two min lots:
            # in the uncapped case the remainder is already < 2×min_lot, so normal
            # sites are unaffected; only the capped monster gets clamped.
            retained_land = min(max(usable - addl * min_lot, min_lot), 2 * min_lot)
            house_resale = ((retained_land * rate + building_value)
                            * ap.house_resale_pct) - ap.refurb_allowance
            gross_sales = house_resale + new_sections_value
            selling_cost = gross_sales * ap.selling_pct
            acquisition_cost = buy * ap.acquisition_pct
            profit = (gross_sales - buy - subdivision_cost - selling_cost
                      - acquisition_cost - incidentals - holding_cost)

    # Contingency on the development spend, then net GST on the margin. Applied
    # once, after the base profit, so retain and whole-site share one treatment.
    if profit is not None:
        contingency = ap.contingency_rate * (subdivision_cost + holding_cost
                                              + demolition_cost + incidentals)
        gst = ap.gst_rate * max(profit - contingency, 0.0)
        profit = profit - contingency - gst

    # A site that can legally be split but loses money is not an opportunity, so
    # the flag means "worth subdividing", not merely "physically splittable".
    # Feasibility alone flagged 1,141 listings of which 961 lost money; an
    # unknown profit (no buy price) doesn't qualify either. The sections /
    # gross_sales / profit figures are still returned for every feasible site,
    # so the detail page can show the workings behind a negative answer.
    # A modelled gross that dwarfs the site's own market value isn't credible —
    # the market would have priced the land up if it were. Keep the figures (the
    # detail page still shows the workings) but don't call it an opportunity.
    gross_implausible = (
        gross_sales is not None and _is_number(cv) and float(cv) > 0
        and gross_sales > GROSS_VS_CV_CAP * float(cv)
    )
    # Corrupt land area (or CV): implausibly cheap urban land per m² — see
    # CV_PER_M2_FLOOR. The subdivision is computed off a bad size, so drop it.
    land_data_bad = (
        _is_number(cv) and float(cv) > 0 and _is_number(land_area) and float(land_area) > 0
        and float(cv) / float(land_area) < CV_PER_M2_FLOOR
    )
    is_profitable = (profit is not None and profit > 0
                     and not gross_implausible and not land_data_bad)

    return Subdivision(
        zone=zone, min_lot_m2=min_lot, is_subdividable=is_profitable,
        sections=sections, dwellings=dwellings,
        section_rate=round(rate),
        gross_sales=round(gross_sales) if gross_sales is not None else None,
        subdivision_profit=round(profit) if profit is not None else None,
        max_addl_lots=float(addl),
        section_price_per_m2=round(rate),
        section_value_method=(rate_source or "section_rate"),
        services_cost=subdivision_cost,
        total_subdivided_value=round(gross_sales) if gross_sales is not None else None,
        uplift_vs_asking=None,
        best_strategy=best_strategy,
        best_net_gain=round(profit) if profit is not None else None,
        subdivision_premium=round(profit) if (profit is not None and profit > 0) else None,
    )
