"""Top-level pipeline: run v3.8 GLM + cashflow + subdivision + scoring over a batch.

Input: for-sale DataFrame (raw scraper columns + zoning) and a SoldDataset
       (used now ONLY for the suburb-DOM lookup and the bare-section $/m2 table —
       comp-matching is no longer the primary pricing engine).
Output: enriched DataFrame with all model columns ready to write to Postgres.
"""

from __future__ import annotations

import pandas as pd

from . import cashflow as CF
from . import scoring as SC
from . import subdivision as SD
from . import zones as Z
from .comps import SoldDataset
from .glm import building_key, canonical_type
from .glm import predict as glm_predict


def _pos_num(x) -> float | None:
    """Return x as a positive float, or None (also catches pandas NaN)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if (x == x and x > 0) else None


def _external_anchor(r) -> float | None:
    """An independent third-party valuation for this row, if one is stored.

    CoreLogic's AVM midpoint (`pv_estimate_mid`) is preferred, then homes.co.nz's
    estimate (`homes_valuation`), then a generic `external_estimate` column. Used
    as the pricing anchor when the council CV is missing or land-value-only, so a
    listing with no usable CV is bounded by a real external figure instead of an
    uncapped comp average (the old `elif bp.area_value` fall-through)."""
    for col in ("pv_estimate_mid", "homes_valuation", "external_estimate"):
        v = _pos_num(r.get(col))
        if v:
            return v
    return None


def _parse_money(s) -> float | None:
    """Parse a scraped money string like '$$231K' / '$1.22M' / '850000' → float."""
    if s is None:
        return None
    t = str(s).replace("$", "").replace(",", "").strip().upper()
    if not t or t in ("NAN", "NONE"):
        return None
    mult = 1.0
    if t.endswith("K"):
        mult, t = 1_000.0, t[:-1]
    elif t.endswith("M"):
        mult, t = 1_000_000.0, t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def _years_ago(ts) -> float | None:
    """A scraped Unix timestamp (seconds) → years before now, or None."""
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    from datetime import datetime, timezone
    try:
        d = datetime.fromtimestamp(t, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return (datetime.now(timezone.utc) - d).days / 365.25

# Above this (asking or CV), the hedonic over-extrapolates — top ~2% of the
# market. We keep the buy price but withhold the model valuation / deal flag.
PREMIUM_THRESHOLD = 5_000_000

# Cap the sale/CV ratio for the displayed value: a value more than ~15% above the
# council CV isn't believable — it's almost always a stale CV (new builds whose CV
# still reflects the old bare site), which would invent a fake margin.
RATIO_CAP = 1.10

# Global anchor guard. If the computed value lands further than ANCHOR_TOLERANCE
# from the best anchor we hold (CV or asking), in either direction, we stop
# trusting our own arithmetic and fall back to anchor x ANCHOR_FALLBACK.
# 0.40 = 40%. Set deliberately tight: every over-valuation seen in production
# (+216%, +433%, +547% vs CV) traced to a data fault upstream, not a real find,
# and the old 3.0x ceiling let all of them through.
ANCHOR_TOLERANCE = 0.40
ANCHOR_FALLBACK = 0.95

# Client rule: a computed valuation more than 60% above the council CV is not a
# genuine find — it traces to an inflated external AVM or a data fault. When we
# hold a credible full CV, the valuation is re-based onto CV × the area's real
# sale/CV ratio and can never exceed CV by more than this tolerance.
CV_OVER_TOLERANCE = 0.60

# A listing on the market longer than this (days) is treated as "the market has
# priced it" — the deal signal is suppressed. Auckland's normal selling time is
# ~40 days, so 90+ means it's not the bargain the CV implies.
STALE_DAYS = 90

# A firm asking price below this fraction of CV is almost never a genuine steal —
# it's an auction / "enquiries over" lure, a broken CV, or a hidden defect. Below
# this we keep the value but drop the deal flag. 0.80 keeps real down-market deals
# (e.g. ask/CV ~0.83) while cutting the lures at ~0.65–0.78 that were flagging 40%+
# margins and floating junk to the top of the underpriced feed.
DEAL_ASKING_CV_FLOOR = 0.80

# An expensive listing with NO floor area can't be size-checked (the size cap can't
# fire), so the CV-ratio value has no independent sanity check. Above this value we
# drop the deal flag rather than publish a firm margin we can't stand behind. The
# value still shows — we just don't call it a deal. Chosen to hit only the high-value
# tail where a missing floor most distorts the margin (e.g. 577 Riddell, $4.6M CV).
NO_FLOOR_DEAL_MAX = 2_500_000

# Size-aware cap — DISABLED (see below). When enabled at 1.20 it capped the value
# at floor × the suburb's sold $/m² × 1.20.
#
# Measured on 5 holdout splits of 2026 sales, predicting actual sale prices:
#     v4 with the cap at 1.20   8.25%   <- worse than doing nothing
#     raw council CV            7.63%
#     v4 with the cap off       7.53%
# It cost 0.72pp on its own — RATIO_CAP and the last-sold cap cost nothing — and
# was the single reason the valuation scored worse than quoting the CV. It was
# added to catch inflated CVs on small homes; it caught those and clamped a great
# deal of legitimate value with them (a small house on an expensive street, or
# any listing whose floor area is understated).
#
# Set to a number (e.g. 1.20) to re-enable. tests/test_valuation_accuracy.py
# fails if the valuation ever drops back below raw CV.
FLOOR_BUFFER = None
# Apartments deliberately excluded: their $/m² is too noisy (CBD vs suburban,
# floor level, view), so a floor cap hurt accuracy in backtest (24% vs 12% on CV).
FLOOR_CAP_TYPES = {"House", "Townhouse", "Unit", "Residence", "Home and Income"}
# Expected-sale constants, from the v4 tool.
#   SOLD_TO_ASK  — a listed property transacts at ~95% of its asking price.
#   BAND_LISTED / BAND_UNLISTED — the tool's own confidence bands. Valuing from
#   the asking price is ~3.5x tighter than valuing from scratch, so the two paths
#   must not be shown with the same implied precision.
SOLD_TO_ASK = 0.95
BAND_LISTED = 0.04
BAND_UNLISTED = 0.14
# Matched-sold-price path: measured ~11.5% median error, so it carries a wider
# band than either CV-anchored path.
BAND_MATCHED = 0.20

# A council valuation record is INCOMPLETE when it carries a land value but no
# improvement value — CV == land value with a house standing on it. 1,265 live
# listings (8.8%) look like this: 26 Sandpiper Avenue asks $4,000,000 against a
# $14,000 "CV" that is only the dirt. Anything derived from such a CV is fiction,
# and a listing whose published figures are that broken is not one to trade off,
# so it is excluded rather than valued.

# "CV similarity band" from the v4 tool. An asking price this far from the
# council valuation is not a reliable guide to what the property will fetch —
# it is an auction lure, a mis-scrape, a stale CV, or a property with something
# wrong with it. Beyond this we ignore the asking price and value from CV.
ASK_VS_CV_BAND = 0.20

# A recent (≤ this many years) prior sale is a strong value anchor in a ~flat
# market — cap the value near it (× buffer for any renovation since).
LAST_SOLD_YEARS = 2.0
LAST_SOLD_BUFFER = 1.25


def cv_over_guard(value: float | None, cv_v: float | None, asking_v: float | None,
                  area_ratio: float, tol: float = CV_OVER_TOLERANCE):
    """Client rule: a valuation more than `tol` above a credible council CV is
    not a genuine find — it's an inflated external AVM or a data fault. Re-base
    it onto CV × the area's real sale/CV ratio, capped so it can never exceed the
    CV by more than `tol`. Returns (value, fired).

    No-op when:
      * there's no CV to anchor to, or
      * the CV reads as land-only — the asking sits more than `tol` above it, so
        the CV isn't a full valuation and the market (asking) is the real anchor.
    """
    if value is None or not cv_v:
        return value, False
    if asking_v is not None and asking_v > (1.0 + tol) * cv_v:
        return value, False
    ceiling = (1.0 + tol) * cv_v
    if value <= ceiling:
        return value, False
    rebased = cv_v * min(area_ratio, 1.0 + tol)
    return round(min(value, rebased, ceiling)), True


OUTPUT_COLS = [
    "market_value", "predicted_list", "predicted_days",
    "comps_used", "confidence",
    "pred_vs_cv", "pred_vs_listing",
    # v4 production AVM diagnostics
    "fair_value", "margin", "is_premium",
    "expected_sale", "expected_sale_path", "expected_sale_band",
    "buy_price", "area_value", "comp_tier", "comps_matched",
    "listing_type", "pricing_path", "range_low", "range_high",
    # Legacy v3.5/v3.8 diagnostics (still populated on the v3.5 fallback path)
    "pred_v35", "pred_v38", "z_weight",
    "beta_tier", "cv_anchor", "cv_ratio_tier", "correction_used",
    "min_lot_m2", "max_addl_lots",
    "sections", "dwellings", "section_rate", "gross_sales", "subdivision_profit",
    "section_price_per_m2", "section_value_method", "services_cost",
    "total_subdivided_value", "uplift_vs_asking", "subdivision_premium",
    "est_weekly_rent", "est_gross_yield",
    "annual_gross_rent", "annual_net_rent", "annual_mortgage",
    "annual_cashflow", "cash_on_cash", "breakeven_deposit_pct",
    "opportunity_score", "opportunity_score_pct",
    "best_strategy", "best_net_gain",
    "is_underpriced", "is_cashflow_positive", "is_subdividable",
]


def _suburb_dom(sold: SoldDataset, suburb: str | None) -> float | None:
    if not suburb:
        return None
    slice_ = sold._by_suburb.get(str(suburb).strip())
    if slice_ is None or "days_on_market" not in slice_.columns:
        return None
    dom = pd.to_numeric(slice_["days_on_market"], errors="coerce")
    if not isinstance(dom, pd.Series):
        return None
    dom = dom[dom.notna() & (dom > 0)]
    return float(dom.median()) if not dom.empty else None


def run(for_sale_df: pd.DataFrame, sold: SoldDataset,
        rent_rates: "CF.RentRates | None" = None) -> pd.DataFrame:
    df = for_sale_df.copy()

    # Normalise numeric inputs.
    for c in ("key_bedrooms", "key_bathrooms", "key_carspaces",
              "key_floor_area", "key_land_area",
              "price_numeric", "cv_numeric", "building_age"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # Import lazily — keeps the import graph clean.
    from app.ingest import _detect_listing_type  # noqa
    from .buyprice import CompEngine
    from .subdivision import SectionRates

    # Build the comp engine + section-rate table once from the active sold batch.
    print("  [pipeline] building comp engine + section rates ...")
    comp_engine = CompEngine(sold.df)
    section_rates = SectionRates(sold.df)

    rows: list[dict] = []
    for _, r in df.iterrows():
        # ---- 1) v4 production AVM (asking × 0.95 or v3.5 fallback) ----
        age = None
        yb = r.get("building_age")
        if yb and pd.notna(yb) and 1800 <= yb <= 2030:
            age = 2026 - int(yb)
        elif yb and pd.notna(yb):
            age = int(yb) if yb < 200 else None  # already an age, not a year

        listing_type = _detect_listing_type(r.get("price_display"), r.get("price_numeric"))

        v = glm_predict(
            suburb=r.get("suburb"),
            district=r.get("district"),
            property_type=r.get("property_type"),
            cv=r.get("cv_numeric"),
            floor=r.get("key_floor_area"),
            land=r.get("key_land_area"),
            beds=r.get("key_bedrooms"),
            baths=r.get("key_bathrooms"),
            cars=r.get("key_carspaces"),
            age=age,
            title=r.get("type_of_title"),
            method=None,  # for-sale listings don't have a sale method yet
            pool=bool(r.get("has_swimming_pool") in (True, "True", "true", 1, "1")),
            address=r.get("address"),
            asking_price=r.get("price_numeric"),
            listing_type=listing_type,
        )

        cv = r.get("cv_numeric")
        asking = r.get("price_numeric")
        # NaN-safe numerics: pandas NaN is truthy, so `asking or 0` would return
        # NaN and poison the comparisons below (a $22M-CV home with no asking
        # slipped past the premium guard). Use these everywhere we need a number.
        cv_v = _pos_num(cv)
        asking_v = _pos_num(asking)
        # Independent external valuation (CoreLogic AVM / homes.co.nz), used as a
        # fallback anchor when there is no usable council CV (see bug 4).
        ext_v = _external_anchor(r)

        # Premium / ultra-prime guard: above the model's reliable range (top ~2%
        # of the market), the hedonic over-extrapolates — a $24M trophy home
        # gets "valued" at $35M and falsely flagged a 47% deal. For these we
        # keep the buy price (anchored to the real asking) but DON'T show a
        # model valuation, margin, or deal flag.
        is_premium = max(cv_v or 0.0, asking_v or 0.0) > PREMIUM_THRESHOLD

        # Placeholder asking: when the scraped "asking price" equals the CV to the
        # dollar, it's the scraper filling in CV for a by-negotiation listing —
        # not a real asking. The model says the area sells above CV, so these get
        # a fake ~60% margin. Treat the asking as not-real: keep the buy price
        # (still 95% of the listed figure) but suppress the deal flag/margin.
        asking_is_placeholder = bool(asking and cv and abs(asking - cv) < 0.005 * cv)

        # Guide / come-on prices ("Offers over $X", "Enquiries over $X", "From $X"):
        # the displayed number is a floor to attract bids, not a firm asking, so a
        # margin computed off it is fake. Keep the value + buy price, drop the deal.
        _disp = str(r.get("price_display") or "").lower()
        asking_is_guide = any(k in _disp for k in (
            "over $", "over$", "enquir", "offers ab", "in excess", "above $",
            "from $", "buyer enq", "beo", "starting"))

        # ---- 3) Buy price (acquisition: cascade comps, capped at asking) ----
        # Uses the real v4 value internally so the buy price stays accurate even
        # for premium listings (it's capped at asking, never silly).
        bp = comp_engine.buy_price(
            suburb=r.get("suburb"), district=r.get("district"),
            property_type=r.get("property_type"),
            beds=r.get("key_bedrooms"), baths=r.get("key_bathrooms"),
            land=r.get("key_land_area"), asking=asking,
            v4_value=v.fair_value, cv=cv,
        )

        # ---- Cashflow (needs the buy price: cashflow is what YOU pay) ----
        # Observed rent from the rental scrape (suburb -> beds -> type cascade).
        # Falls back to the CV yield tier when the suburb has no rental comps.
        obs_rent, rent_src = (rent_rates.weekly_rent_for(
            suburb=r.get("suburb"), district=r.get("district"),
            property_type=r.get("property_type"), beds=r.get("key_bedrooms"),
        ) if rent_rates is not None else (None, None))

        cf = CF.compute(
            asking_price=r.get("price_numeric"),
            market_value=v.market_value,
            cv=r.get("cv_numeric"),
            observed_weekly_rent=obs_rent,
            rent_source=rent_src,
            buy_price=bp.buy_price,
        )

        # Premium: comps at the top of the market are too thin/unrepresentative
        # to trust (they can drag the buy price well below asking, or zero it).
        # The most reliable buy price for a trophy home is simply 95% of asking.
        if is_premium and asking and asking > 0:
            bp.buy_price = round(0.95 * float(asking))
            bp.area_value = None
            bp.comp_tier = None
            bp.comps_matched = 0

        # ---- Ollie valuation (Matthew's method — priced straight off sold data) ----
        # NOT the hedonic. Reproducible arithmetic that matches the client's Excel tool:
        #   • bare sections / lifestyle land → land area × the suburb's sold $/m² rate
        #   • everything else               → CV × the area's real sale/CV ratio
        #       (from tightly-matched comps, else the suburb/district/type median)
        # This removes the black-box over-valuation (e.g. a $4.8M-CV house the
        # hedonic pushed to $5.4M now prices at CV × 0.97 ≈ $4.65M).
        ctype = canonical_type(r.get("property_type"))
        land_v = _pos_num(r.get("key_land_area"))

        ollie_value = None
        anchor_fallback_used = False
        cv_is_land_only = False
        if not is_premium:
            if ctype in ("Section", "Lifestyle Section"):
                # Matthew's method: land area × the suburb's sold $/m² rate.
                rate = section_rates.rate_for(r.get("suburb"))
                land_val = round(land_v * rate) if (land_v and rate) else None
                # That $/m² is an URBAN section rate; on multi-hectare rural blocks
                # it explodes (a 47 ha block → $500M). Bound the land-rate value by a
                # real anchor — the CV-based value, else the asking price. With
                # neither anchor we can't sanity-check a big block, so suppress it.
                caps = []
                if cv_v:
                    ratio, _src = comp_engine.cv_ratio_for(
                        suburb=r.get("suburb"), district=r.get("district"),
                        property_type=r.get("property_type"),
                        beds=r.get("key_bedrooms"), baths=r.get("key_bathrooms"), land=land_v,
                        title=r.get("type_of_title"))
                    # Bare land: never value above council CV.
                    caps.append(round(cv_v * min(ratio, 1.0)))
                if asking_v:
                    caps.append(round(asking_v))
                if land_val is not None and caps:
                    ollie_value = min([land_val] + caps)
                elif caps:
                    ollie_value = min(caps)
                elif land_val is not None and land_v and land_v <= 3000 and land_val <= 3_000_000:
                    ollie_value = land_val  # small urban section, no CV/asking anchor
                else:
                    ollie_value = None  # can't sanity-check → don't show a number
            elif cv_v:
                # PRIMARY: like-for-like sold comps — same suburb, type, beds and
                # baths, land and floor within 25% — priced off their sale/CV.
                # 6.94% median error against actual sale prices, vs 7.55% for the
                # suburb-wide area ratio, which is the fallback below.
                _sv, _stier, _sn = comp_engine.spec_value(
                    suburb=r.get("suburb"), district=r.get("district"),
                    property_type=r.get("property_type"),
                    beds=r.get("key_bedrooms"), baths=r.get("key_bathrooms"),
                    land=land_v, floor=_pos_num(r.get("key_floor_area")), cv=cv_v)
                ratio, _src = comp_engine.cv_ratio_for(
                    suburb=r.get("suburb"), district=r.get("district"),
                    property_type=r.get("property_type"),
                    beds=r.get("key_bedrooms"), baths=r.get("key_bathrooms"), land=land_v,
                    title=r.get("type_of_title"))
                # NB spec_value (like-for-like beds+baths+size comps) is computed
                # above but deliberately NOT used: measured on the SAME 1,987
                # properties where it fires, it scores 7.69% against the area
                # ratio's 7.05%, winning on only 46% of them. Its apparent 6.94%
                # in isolation was a selection effect — tight comps exist for
                # ordinary, well-traded houses, which every method prices well.
                ollie_value = round(cv_v * min(ratio, RATIO_CAP))
                # Size-aware cap. With a real floor area we cap the value at what
                # same-size homes sell for per m² — this catches inflated CVs on
                # SMALL homes without penalising genuinely LARGE ones (big floor →
                # big cap), fixing both the false positives and the premium-home
                # demotion. Falls back to the bed-count cap for attached dwellings
                # that still lack floor area.
                floor_v = _pos_num(r.get("key_floor_area"))
                frate = (comp_engine.floor_rate_for(
                    suburb=r.get("suburb"), property_type=r.get("property_type"))
                    if (FLOOR_BUFFER and floor_v and ctype in FLOOR_CAP_TYPES) else None)
                if floor_v and frate:
                    ollie_value = min(ollie_value, round(floor_v * frate * FLOOR_BUFFER))
                elif ctype in ("Townhouse", "Unit"):
                    cap = comp_engine.sold_price_cap(
                        suburb=r.get("suburb"), district=r.get("district"),
                        property_type=r.get("property_type"), beds=r.get("key_bedrooms"))
                    if cap:
                        ollie_value = min(ollie_value, round(cap))

                # Recent prior sale is a strong anchor in a flat market — a value
                # far above what the property itself sold for ~1-2 years ago is
                # almost always an inflated CV, not a bargain.
                ls_val = _parse_money(r.get("valuation_last_sold_value"))
                ls_age = _years_ago(r.get("valuation_last_sold_date"))
                if ls_val and ls_age is not None and 0 <= ls_age <= LAST_SOLD_YEARS:
                    ollie_value = min(ollie_value, round(ls_val * LAST_SOLD_BUFFER))

                # Land-only / stale CV — new builds, and sections still carrying
                # the pre-subdivision land CV. The council figure reflects only the
                # dirt, so cv × ratio badly under-values a real house standing on
                # it (a new 307m² Millwater home, CV $610k = land, must not price
                # at $610k). When genuinely like-for-like SOLD comps — same type,
                # beds, baths, land and floor, NO CV involved — say the built home
                # is worth more than CV_OVER_TOLERANCE above this CV, the CV is land
                # only: value from those comps and exempt the row from the CV
                # anchor/over guards, which both assume a credible full CV.
                if (floor_v and _pos_num(r.get("key_bedrooms"))
                        and ctype not in ("Section", "Lifestyle Section")):
                    _mp, _mtier, _mn = comp_engine.matched_sold_price(
                        suburb=r.get("suburb"), district=r.get("district"),
                        property_type=r.get("property_type"),
                        beds=r.get("key_bedrooms"), baths=r.get("key_bathrooms"),
                        land=land_v, floor=floor_v)
                    if _mp and _mn >= 3 and _mp > (1.0 + CV_OVER_TOLERANCE) * cv_v:
                        ollie_value = round(_mp)
                        cv_is_land_only = True
            elif ext_v:
                # No usable council CV, but an independent external valuation
                # exists (CoreLogic AVM / homes.co.nz). Anchor on it — CV-style,
                # capped at RATIO_CAP and bounded by the comp-derived area_value —
                # rather than falling through to the uncapped area_value below.
                ollie_value = round(ext_v * min(1.0, RATIO_CAP))
                if bp.area_value:
                    ollie_value = min(ollie_value, round(bp.area_value))
            elif bp.area_value:
                ollie_value = bp.area_value  # last-ditch when there's no CV at all

        # Global backstop: when our value drifts far from the best anchor we have
        # (CV or asking), trust the anchor. A council CV is a real, independently
        # produced figure; a value 3x away from it is a data fault (mis-scraped
        # land area, land-value-only CV record, missing floor area), not a find.
        #
        # Was `> 3.0 * _anchor -> None`, which (a) let +200% valuations through
        # and (b) showed nothing at all when it did fire. Now: anything more than
        # ANCHOR_TOLERANCE away, in EITHER direction, falls back to anchor x 0.95.
        # Both directions matters — a value far BELOW the anchor surfaces as a
        # bargain, which is the more damaging error.
        # The CV anchor/over guards below both assume the CV is a credible full
        # valuation. A land-only CV (detected above) is not — its value comes from
        # comps, not the CV — so it's exempt from both, or they'd drag the honest
        # comp value back down to the dirt.
        if ollie_value is not None and not cv_is_land_only:
            # ext_v (external AVM: CoreLogic/homes) is a FALLBACK anchor only for
            # when there's no council CV (bug 4). With a real CV present it must
            # never be part of the anchor — otherwise a wildly high external
            # estimate pulls a correct CV-anchored value UP to it (a $70k bare
            # section, CV $70k, was surfacing a $1.22M valuation because an AVM
            # said $1.28M). Drop ext_v from the anchor whenever we hold a CV.
            _anchor = max(cv_v or 0.0, asking_v or 0.0,
                          0.0 if cv_v else (ext_v or 0.0))
            if _anchor and abs(ollie_value - _anchor) > ANCHOR_TOLERANCE * _anchor:
                ollie_value = round(_anchor * ANCHOR_FALLBACK)
                anchor_fallback_used = True

        # CV-over guard (client rule): re-base any valuation that lands more than
        # CV_OVER_TOLERANCE above a credible full CV onto CV × the area's real
        # sale/CV ratio — "CV minus a % of what the area's selling for" — applied
        # as a hard ceiling after every pricing path. See cv_over_guard().
        if ollie_value is not None and cv_v and not cv_is_land_only:
            _ratio, _src = comp_engine.cv_ratio_for(
                suburb=r.get("suburb"), district=r.get("district"),
                property_type=r.get("property_type"),
                beds=r.get("key_bedrooms"), baths=r.get("key_bathrooms"),
                land=land_v, title=r.get("type_of_title"))
            ollie_value, _fired = cv_over_guard(ollie_value, cv_v, asking_v, _ratio)
            if _fired:
                anchor_fallback_used = True

        # Displayed valuation (withheld only for premium). The deal-signal value
        # is additionally withheld for placeholder askings — we still show what
        # it's worth, but don't call it a deal off a fake asking.
        # Stale listings: a genuinely underpriced property sells fast (Auckland
        # norm ~40 days). One that's sat 90+ days is NOT underpriced — the market
        # has already priced it; our CV-based estimate is just too high for it
        # (stale CV or a defect we can't see). Suppress the deal signal so it
        # can't float to the top of a Margin% sort, but keep it listed.
        dom = _pos_num(r.get("days_on_market"))
        is_stale = bool(dom and dom > STALE_DAYS)

        # No floor area on an expensive home = the size cap can't fire, so we can't
        # size-check the value → drop the deal flag (keep the value shown).
        no_floor_highvalue = (
            not _pos_num(r.get("key_floor_area"))
            and max(cv_v or 0.0, asking_v or 0.0, ollie_value or 0.0) >= NO_FLOOR_DEAL_MAX
        )

        display_value = None if is_premium else ollie_value
        deal_value = None if (is_premium or asking_is_placeholder or asking_is_guide or is_stale or no_floor_highvalue) else ollie_value

        # Deal-signal sanity guards. The Auckland market asks ~CV (median 0.998),
        # so an asking far below CV is almost never a genuine firm steal — it's a
        # broken CV, a non-standard sale, a guide price, or a hidden defect.
        #   • CV > 2.5× asking  → the CV itself is broken: drop value AND deal.
        #   • asking < DEAL_ASKING_CV_FLOOR× CV → lure/implausible discount: keep the value, drop the deal.
        #   • value > 1.8× asking → implausible margin: drop the deal.
        # The broken-CV check invalidates the *displayed value*, not just the deal
        # flag, so it must run regardless of whether deal_value was already dropped
        # for an unrelated reason. It used to sit inside `if deal_value is not None`,
        # so a stale / guide-priced / no-floor listing with a broken CV skipped it
        # and still published a fair_value derived from that same broken CV —
        # surfacing as an implausible 70-90% discount.
        if asking_v and cv_v and cv_v > 2.5 * asking_v:
            display_value = deal_value = None
        elif asking_v and deal_value is not None:
            if cv_v and asking_v < DEAL_ASKING_CV_FLOOR * cv_v:
                deal_value = None
            elif deal_value > 1.8 * asking_v:
                deal_value = None

        # ---- 4) Subdivision + profit (needs the buy price + suburb section rate) ----
        sd = SD.compute(
            zone=Z.corrected_zoning(r.get("zoning"), suburb=r.get("suburb"),
                                    land_area=r.get("key_land_area"), address=r.get("address")),
            land_area=r.get("key_land_area"),
            buy_price=bp.buy_price,
            section_rate=section_rates.rate_for(r.get("suburb")),
            rate_source=section_rates.source_for(r.get("suburb")),
            address=r.get("address"),
            property_type=r.get("property_type"),
            title_type=r.get("type_of_title"),
            improvement_value=r.get("improvement_value_numeric"),
            land_value=r.get("land_value_numeric"),
            cv=cv,
            beds=r.get("key_bedrooms"),
            baths=r.get("key_bathrooms"),
            floor_area=r.get("key_floor_area"),
        )

        # ---- Expected sale price ("what will it sell for") ----
        # Two different questions, two different numbers — as in the v4 tool,
        # which shows "WHAT TO PAY" and "WHAT IS IT WORTH" side by side:
        #
        #   expected_sale  = what this will transact at.  When the vendor has
        #                    published a price that IS the strongest signal
        #                    available, so use asking x DISCOUNT. Only value it
        #                    from scratch when there is no price.
        #   fair_value     = what it is worth, computed WITHOUT the asking price,
        #                    so the margin means something. Anchoring this to
        #                    asking would make every margin exactly -5% and no
        #                    listing could ever be flagged underpriced.
        #
        # A placeholder asking (scraper filled in the CV) is not a real price, so
        # it takes the unlisted path and carries the wider confidence band.
        # Incomplete council record: CV is the land value alone, improvements
        # missing. Everything downstream (the CV ratio, the hedonic, the margin)
        # inherits the error, so the listing is dropped instead of valued.
        _lv = _pos_num(r.get("land_value_numeric"))
        _iv = _pos_num(r.get("improvement_value_numeric"))
        # Vacant land legitimately has no improvement value — only a record with
        # a building and no improvements is broken.
        cv_incomplete = bool(cv_v and _lv and _iv is None and abs(cv_v - _lv) < 1
                             and ctype not in ("Section", "Lifestyle Section"))

        ask_far_from_cv = bool(
            asking_v and cv_v and abs(asking_v - cv_v) > ASK_VS_CV_BAND * cv_v)

        if cv_incomplete:
            # The council record is missing the buildings, so every CV-anchored
            # number is poisoned — but comparable buildings ARE selling in this
            # suburb. Value it from those directly: same type, beds and baths,
            # land and floor within 20%. No CV involved.
            mp, mtier, mn = comp_engine.matched_sold_price(
                suburb=r.get("suburb"), district=r.get("district"),
                property_type=r.get("property_type"),
                beds=r.get("key_bedrooms"), baths=r.get("key_bathrooms"),
                land=land_v, floor=_pos_num(r.get("key_floor_area")))
            expected_sale = round(mp) if mp else None
            expected_sale_path = f"matched_sold:{mtier}" if mp else "cv_incomplete"
            expected_sale_band = BAND_MATCHED if mp else None
            # Displayed value comes from the same source; no deal signal, because
            # a margin needs a value we can stand behind to ~5%.
            display_value = round(mp) if mp else None
            deal_value = None
        elif asking_v and not asking_is_placeholder and not ask_far_from_cv:
            expected_sale = round(asking_v * SOLD_TO_ASK)
            expected_sale_path = "listed"
            expected_sale_band = BAND_LISTED
        elif ask_far_from_cv:
            # Asking and CV disagree by more than the band, so ONE of them is
            # wrong and we cannot tell which — a $4M Point Wells listing with a
            # $14,000 CV is a broken CV, not a broken asking price. Falling back
            # to CV x ratio here published $14,116 for that property.
            # Value it from what comparable houses ACTUALLY SOLD FOR instead:
            #   1. sold $/m2 of floor for the suburb+type x this floor area
            #   2. median sold price for the same suburb+type+bed count
            # Both come straight from sold records and never touch CV or asking.
            floor_v2 = _pos_num(r.get("key_floor_area"))
            frate2 = comp_engine.floor_rate_for(
                suburb=r.get("suburb"), property_type=r.get("property_type"))
            by_floor = round(floor_v2 * frate2) if (floor_v2 and frate2) else None
            by_beds = comp_engine.sold_price_cap(
                suburb=r.get("suburb"), district=r.get("district"),
                property_type=r.get("property_type"), beds=r.get("key_bedrooms"))
            sold_based = by_floor or (round(by_beds) if by_beds else None)
            expected_sale = sold_based
            expected_sale_path = ("sold_comps" if sold_based else "unresolved")
            expected_sale_band = BAND_UNLISTED if sold_based else None
        else:
            expected_sale = ollie_value          # CV x the area's sale/CV ratio
            expected_sale_path = "unlisted"
            expected_sale_band = BAND_UNLISTED

        # ---- 5) Opportunity score ----
        sig = SC.signals(
            asking_price=r.get("price_numeric"),
            market_value=v.market_value,
            annual_cashflow=cf.annual_cashflow,
            cash_on_cash=cf.cash_on_cash,
            best_net_gain=sd.best_net_gain,
            confidence=v.confidence,
            fair_value=deal_value,
            title_type=r.get("type_of_title"),
        )

        # Derived
        # These compare OUR valuation to the market, so they must use the
        # sold-data value (display_value / fair_value), never market_value —
        # market_value is asking x 0.95, so using it made "vs CV" really mean
        # "asking vs CV" and told the user nothing about our estimate.
        _val = display_value if display_value else None
        pred_vs_cv = (_val / cv - 1) if (_val and cv) else None
        pred_vs_list = (_val / asking - 1) if (_val and asking) else None
        predicted_list = round(v.market_value * 1.05) if v.market_value else None
        predicted_days = _suburb_dom(sold, r.get("suburb"))
        # Margin = how far the independent fair value sits above the asking price.
        margin = ((deal_value / asking) - 1.0) if (deal_value and asking) else None

        rows.append({
            "market_value": v.market_value,
            "predicted_list": predicted_list,
            "predicted_days": predicted_days,
            "comps_used": v.n_subtype,
            "confidence": v.confidence,
            "pred_vs_cv": round(pred_vs_cv, 4) if pred_vs_cv is not None else None,
            "pred_vs_listing": round(pred_vs_list, 4) if pred_vs_list is not None else None,
            # v4 AVM diagnostics
            "fair_value": display_value,
            "expected_sale": expected_sale,
            "expected_sale_path": expected_sale_path,
            "expected_sale_band": expected_sale_band,
            "margin": round(margin, 4) if margin is not None else None,
            "is_premium": is_premium,
            "listing_type": listing_type,
            "pricing_path": v.pricing_path,
            "range_low": v.range_low,
            "range_high": v.range_high,
            # Legacy v3.5/v3.8 diagnostics
            "pred_v35": v.pred_v35,
            "pred_v38": v.pred_v38,
            "z_weight": v.z_weight,
            "beta_tier": v.beta_tier,
            "cv_anchor": v.cv_anchor,
            "cv_ratio_tier": v.cv_ratio_tier,
            "correction_used": v.correction_used,
            # Acquisition layer — buy price
            "buy_price": bp.buy_price,
            "area_value": bp.area_value,
            "comp_tier": bp.comp_tier,
            "comps_matched": bp.comps_matched,
            # Subdivision
            "min_lot_m2": sd.min_lot_m2,
            "max_addl_lots": sd.max_addl_lots,
            "sections": sd.sections,
            "dwellings": sd.dwellings,
            "section_rate": sd.section_rate,
            "gross_sales": sd.gross_sales,
            "subdivision_profit": sd.subdivision_profit,
            "section_price_per_m2": sd.section_price_per_m2,
            "section_value_method": sd.section_value_method,
            "services_cost": sd.services_cost,
            "total_subdivided_value": sd.total_subdivided_value,
            "uplift_vs_asking": sd.uplift_vs_asking,
            "subdivision_premium": sd.subdivision_premium,
            # Cashflow
            "est_weekly_rent": cf.est_weekly_rent,
            "est_gross_yield": cf.est_gross_yield,
            "annual_gross_rent": cf.annual_gross_rent,
            "annual_net_rent": cf.annual_net_rent,
            "annual_mortgage": cf.annual_mortgage,
            "annual_cashflow": cf.annual_cashflow,
            "cash_on_cash": cf.cash_on_cash,
            "breakeven_deposit_pct": cf.breakeven_deposit_pct,
            # Scoring
            "opportunity_score": sig.raw_score,
            "opportunity_score_pct": None,  # filled in batch below
            "best_strategy": sd.best_strategy,
            "best_net_gain": sd.best_net_gain,
            "is_underpriced": sig.is_underpriced,
            "is_cashflow_positive": sig.is_cashflow_positive,
            "is_subdividable": sd.is_subdividable,
        })

    out = pd.DataFrame(rows, index=df.index)
    out["opportunity_score_pct"] = SC.percentile_rank_0_100(out["opportunity_score"])
    result = pd.concat([df.reset_index(drop=True), out.reset_index(drop=True)], axis=1)

    # De-dup the deal signal by building. A development with many units would
    # otherwise flood a Margin% sort (e.g. 8 units of one complex). Keep the
    # margin / underpriced flag on only the single best-margin unit per building;
    # blank it on the rest — they stay listed, just don't each shout "deal".
    if "address" in result.columns:
        bk = result["address"].map(building_key)
        flagged = result["margin"].notna() & bk.notna()
        if flagged.any():
            keep = (
                result[flagged].assign(_bk=bk[flagged])
                .sort_values("margin", ascending=False)
                .drop_duplicates("_bk", keep="first").index
            )
            demote = result.index[flagged].difference(keep)
            result.loc[demote, "margin"] = None
            result.loc[demote, "is_underpriced"] = False
    return result
