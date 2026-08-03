"""Tools the assistant can call. Every one hits the database.

The whole point of this layer: the model is never asked to recall or estimate a
property figure. It asks a tool, the tool runs a real query against the active
batch, and the model reports what came back. A question we have no tool for gets
"I don't have that" rather than a plausible number.

Each tool opens its own short-lived session — they run inside a request handler,
not a long-lived transaction, and a tool that fails should not poison the others.
"""

from __future__ import annotations

import json

import pandas as pd
from sqlalchemy import func, or_, select

from ..db import SessionLocal
from ..models import ImportBatch, PropertyForSale, PropertySold
from ..pricing.conversion import conversion_opportunities
from ..pricing.glm import canonical_type
from ..pricing.valueadd import by_district
from . import sql as sqltool

# English category -> the canonical property types it covers (mirrors the UI filter).
_CATEGORY_MAP: dict[str, set[str]] = {
    "house": {"House", "Residence", "Home and Income"},
    "townhouse": {"Townhouse"},
    "apartment": {"Apartment"},
    "unit": {"Unit"},
    "section": {"Section"},
    "lifestyle": {"Lifestyle Property", "Lifestyle Section"},
}

MAX_ROWS = 25


def _active(session, batch_type: str) -> int | None:
    return session.execute(
        select(ImportBatch.id).where(
            ImportBatch.is_active, ImportBatch.batch_type == batch_type
        )
    ).scalar()


def _money(v) -> str | None:
    """Property-scale money to the nearest $1,000.

    A valuation quoted as "$907,939" implies a precision we don't have — median
    error is ~7.9%. Small figures (weekly rent, fees) stay exact.
    """
    if v is None:
        return None
    v = round(v)
    if abs(v) >= 50_000:
        v = round(v / 1000) * 1000
    return f"${v:,}"


def _pct(v) -> str | None:
    return None if v is None else f"{v * 100:+.1f}%"


def search_listings(
    suburb: str | None = None,
    district: str | None = None,
    property_type: str | None = None,
    min_beds: int | None = None,
    max_price: float | None = None,
    min_price: float | None = None,
    underpriced_only: bool = False,
    subdividable_only: bool = False,
    cashflow_positive_only: bool = False,
    min_margin_pct: float | None = None,
    min_buy_score: float | None = None,
    sort_by: str = "margin",
    limit: int = 10,
) -> str:
    """Search the live for-sale listings and return matching properties.

    This is the same set of filters as the site's property finder, so it can
    answer any browse-style question — "underpriced 4-bed under $3M on the North
    Shore", "cheapest townhouses in Manukau", "subdividable sites with buy score
    over 80". Combine any filters.

    Args:
        suburb: Exact suburb name, e.g. "Browns Bay". Case-insensitive partial match.
        district: District name, e.g. "North Shore City", "Auckland City", "Manukau City".
        property_type: One of house, townhouse, apartment, unit, section, lifestyle.
            (The raw data is in Chinese; this maps it for you — always use the English word.)
        min_beds: Minimum bedroom count.
        max_price: Maximum asking price in dollars, e.g. 3000000 for "under $3M".
        min_price: Minimum asking price in dollars.
        underpriced_only: Only listings flagged as below our valuation.
        subdividable_only: Only listings that can profitably subdivide.
        cashflow_positive_only: Only listings whose rent covers the mortgage.
        min_margin_pct: Minimum margin as a percent, e.g. 15 for 15%.
        min_buy_score: Minimum opportunity/buy score, 0-100.
        sort_by: One of "margin", "price", "lots", "days_on_market", "score", "yield".
        limit: How many to return, max 25.
    """
    with SessionLocal() as s:
        batch = _active(s, "for_sale")
        if batch is None:
            return "No active listing batch loaded."
        P = PropertyForSale
        q = s.query(P).filter(P.import_batch_id == batch)
        if suburb:
            q = q.filter(P.suburb.ilike(f"%{suburb}%"))
        if district:
            q = q.filter(P.district.ilike(f"%{district}%"))
        if property_type:
            wanted = _CATEGORY_MAP.get(property_type.strip().lower())
            if wanted:
                raw = [t for (t,) in s.query(P.property_type)
                       .filter(P.import_batch_id == batch).distinct() if t]
                matching = [t for t in raw if canonical_type(t) in wanted]
                q = q.filter(P.property_type.in_(matching or ["__none__"]))
        if min_beds is not None:
            q = q.filter(P.beds >= min_beds)
        if max_price is not None:
            q = q.filter(P.asking_price <= max_price)
        if min_price is not None:
            q = q.filter(P.asking_price >= min_price)
        if underpriced_only:
            q = q.filter(P.is_underpriced.is_(True))
        if subdividable_only:
            q = q.filter(P.is_subdividable.is_(True))
        if cashflow_positive_only:
            q = q.filter(P.is_cashflow_positive.is_(True))
        if min_margin_pct is not None:
            q = q.filter(P.margin.isnot(None), P.margin >= min_margin_pct / 100)
        if min_buy_score is not None:
            q = q.filter(P.opportunity_score_pct.isnot(None),
                         P.opportunity_score_pct >= min_buy_score)

        order = {
            "margin": P.margin.desc(),
            "price": P.asking_price.asc(),
            "lots": P.max_addl_lots.desc(),
            "days_on_market": P.days_on_market.desc(),
            "score": P.opportunity_score_pct.desc(),
            "yield": P.est_gross_yield.desc(),
        }.get(sort_by, P.margin.desc())

        rows = q.order_by(order.nullslast()).limit(min(limit, MAX_ROWS)).all()
        if not rows:
            return "No listings match those filters."
        return json.dumps({
            "count": len(rows),
            "listings": [{
                "id": r.id, "address": r.address, "suburb": r.suburb,
                "district": r.district, "beds": r.beds, "baths": r.baths,
                "floor_m2": r.floor_area_m2, "land_m2": r.land_area_m2,
                "asking": _money(r.asking_price), "our_value": _money(r.fair_value),
                "buy_price": _money(r.buy_price), "margin": _pct(r.margin),
                "cv": _money(r.cv_numeric), "confidence": r.confidence,
                "comps_used": r.comps_used, "days_on_market": r.days_on_market,
                "subdividable": r.is_subdividable,
                "extra_lots": r.max_addl_lots,
                "subdivision_profit": _money(r.best_net_gain),
            } for r in rows],
        }, default=str)


def get_property(property_id: int) -> str:
    """Full detail on one listing, including valuation, cashflow and subdivision.

    Args:
        property_id: The listing id, as returned by search_listings.
    """
    with SessionLocal() as s:
        p = s.get(PropertyForSale, property_id)
        if not p:
            return f"No listing with id {property_id}."
        return json.dumps({
            "id": p.id, "address": p.address, "suburb": p.suburb,
            "district": p.district, "property_type": p.property_type,
            "title": p.type_of_title, "zoning": p.zoning,
            "beds": p.beds, "baths": p.baths, "cars": p.cars,
            "floor_m2": p.floor_area_m2, "land_m2": p.land_area_m2,
            "year_built": p.building_age,
            "asking": _money(p.asking_price), "cv": _money(p.cv_numeric),
            "our_value": _money(p.fair_value), "buy_price": _money(p.buy_price),
            "margin": _pct(p.margin), "vs_cv": _pct(p.pred_vs_cv),
            "confidence": p.confidence, "comps_used": p.comps_used,
            "likely_range": (
                f"{_money(p.range_low)}-{_money(p.range_high)}"
                if p.range_low and p.range_high else None
            ),
            "predicted_days_to_sell": p.predicted_days,
            "days_on_market": p.days_on_market,
            "weekly_rent": _money(p.est_weekly_rent),
            "gross_yield": _pct(p.est_gross_yield),
            "annual_cashflow": _money(p.annual_cashflow),
            "breakeven_deposit": _pct(p.breakeven_deposit_pct),
            "subdividable": p.is_subdividable,
            "extra_lots": p.max_addl_lots,
            "min_lot_m2": p.min_lot_m2,
            "subdivision_profit": _money(p.best_net_gain),
            "best_strategy": p.best_strategy,
            "listing_url": p.url,
        }, default=str)


def get_sold_comparables(property_id: int) -> str:
    """Recent sold comparables for a listing, with what they did against CV.

    Use when asked "what have similar places sold for", "is this a fair price",
    or anything needing evidence from actual sales.

    Args:
        property_id: The listing id to find comparables for.
    """
    from ..routers.properties import property_comparables

    with SessionLocal() as s:
        try:
            r = property_comparables(property_id, db=s)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            return f"Could not load comparables: {exc}"
        return json.dumps({
            "suburb": r.suburb,
            "median_sale": _money(r.median_sale_price),
            "average_sale": _money(r.mean_sale_price),
            "median_vs_cv": _pct(r.median_sale_vs_cv),
            "average_vs_cv": _pct(r.mean_sale_vs_cv),
            "middle_half_vs_cv": (
                f"{_pct(r.sale_vs_cv_low)} to {_pct(r.sale_vs_cv_high)}"
                if r.sale_vs_cv_low is not None else None
            ),
            "this_listing_ask_vs_cv": _pct(r.subject_ask_vs_cv),
            "median_days_to_sell": r.median_days_on_market,
            "average_days_to_sell": r.mean_days_on_market,
            "sale_method_across_suburb": [{
                "method": m.method, "sales": m.count,
                "vs_cv": _pct(m.median_vs_cv),
                "too_few_to_rely_on": m.is_thin,
            } for m in r.suburb_method_mix],
            "auction_premium_over_negotiation": (
                f"{r.method_gap_pts * 100:+.1f} points"
                if r.method_gap_pts is not None and not r.method_gap_is_thin else None
            ),
            "worth_if_sold_by_negotiation": _money(r.value_if_negotiation),
            "worth_if_sold_at_auction": _money(r.value_if_auction),
            "comparables": [{
                "address": c.address, "beds": c.beds, "baths": c.baths,
                "floor_m2": c.floor_area_m2, "land_m2": c.land_area_m2,
                "title": c.type_of_title, "sold_for": _money(c.sale_price),
                "cv": _money(c.cv_numeric), "sold_date": c.sold_date,
                "method": c.sale_method,
            } for c in r.comps],
        }, default=str)


def market_summary() -> str:
    """Headline numbers across the whole active batch.

    Use for "how's the market", "how many underpriced listings", "how much
    margin is available".
    """
    with SessionLocal() as s:
        batch = _active(s, "for_sale")
        if batch is None:
            return "No active listing batch loaded."
        P = PropertyForSale
        gap = P.fair_value - P.asking_price
        base = [P.import_batch_id == batch, P.is_underpriced.is_(True),
                P.fair_value.isnot(None), P.asking_price.isnot(None)]
        gem = base + [P.margin >= 0.15, P.comps_used >= 8]
        q = lambda f, *w: s.query(f).filter(*w).scalar()  # noqa: E731
        return json.dumps({
            "total_live_listings": q(func.count(P.id), P.import_batch_id == batch),
            "underpriced": q(func.count(P.id), *base),
            "underpriced_total_margin": _money(q(func.sum(gap), *base)),
            "high_conviction_deals": q(func.count(P.id), *gem),
            "high_conviction_total_margin": _money(q(func.sum(gap), *gem)),
            "high_conviction_definition": "15%+ below our value, backed by 8+ sold comps",
            "subdividable": q(func.count(P.id), P.import_batch_id == batch,
                              P.is_subdividable.is_(True)),
            "subdivision_total_profit": _money(
                q(func.sum(P.best_net_gain), P.import_batch_id == batch,
                  P.is_subdividable.is_(True))),
            "median_asking": _money(q(
                func.percentile_cont(0.5).within_group(P.asking_price),
                P.import_batch_id == batch)),
            "cashflow_positive": q(func.count(P.id), P.import_batch_id == batch,
                                   P.is_cashflow_positive.is_(True)),
        }, default=str)


def _sold_frame(s, batch: int) -> pd.DataFrame:
    rows = s.query(
        PropertySold.district, PropertySold.property_type, PropertySold.beds,
        PropertySold.baths, PropertySold.floor_area_m2, PropertySold.sale_price,
        PropertySold.has_swimming_pool,
    ).filter(PropertySold.import_batch_id == batch,
             PropertySold.sale_price.isnot(None)).all()
    df = pd.DataFrame(rows, columns=["district", "property_type", "beds", "baths",
                                     "floor", "price", "pool"])
    df["pool"] = df["pool"].fillna(False).astype(bool)
    for c in ("beds", "baths", "floor", "price"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def renovation_value_by_district() -> str:
    """What adding a bedroom, bathroom or pool is worth, per district.

    Every figure is size-controlled and holds the other room count constant, so
    it is the value of the feature rather than of a bigger house.
    """
    with SessionLocal() as s:
        batch = _active(s, "sold")
        if batch is None:
            return "No active sold batch loaded."
        rows = by_district(_sold_frame(s, batch))
        return json.dumps({
            "method": ("Compared against sold houses of the same district, property "
                       "type, floor area band and other room count. Naive comparisons "
                       "give +24.9% for a bedroom; controlled it is +6.2%."),
            "pool_caveat": ("The pool figure is the gap between houses that have one "
                            "and houses that don't. It survives controls for size, "
                            "bedrooms and land, so it reflects the calibre of house "
                            "that has a pool, not the pool. Not a renovation estimate."),
            "districts": [{
                "district": r["district"],
                "bedroom_3_to_4": _pct(r["bedroom"]),
                "bedroom_comparisons": r["bedroom_cells"],
                "bathroom_1_to_2": _pct(r["bathroom"]),
                "bathroom_comparisons": r["bathroom_cells"],
                "pool_gap": _pct(r["pool"]),
                "pool_comparisons": r["pool_cells"],
            } for r in rows],
        }, default=str)


def find_room_to_add_a_bedroom(limit: int = 10) -> str:
    """Listings that already have the floor area for another bedroom.

    Cross-references how much floor area a house of each bed count normally
    carries against what a bedroom is worth in that district. Only returns
    districts where a bedroom measurably pays.

    Args:
        limit: How many to return, max 25.
    """
    with SessionLocal() as s:
        sold_b, live_b = _active(s, "sold"), _active(s, "for_sale")
        if not sold_b or not live_b:
            return "Missing an active batch."
        P = PropertyForSale
        live_rows = s.query(
            P.id, P.address, P.suburb, P.district, P.beds, P.floor_area_m2,
            P.asking_price, P.fair_value, P.margin, P.is_underpriced, P.image_url,
        ).filter(P.import_batch_id == live_b, P.floor_area_m2.isnot(None),
                 P.beds.isnot(None), P.fair_value.isnot(None)).all()
        live = pd.DataFrame(live_rows, columns=[
            "id", "address", "suburb", "district", "beds", "floor_area_m2",
            "asking_price", "fair_value", "margin", "is_underpriced", "image_url"])
        for c in ("beds", "floor_area_m2", "asking_price", "fair_value", "margin"):
            live[c] = pd.to_numeric(live[c], errors="coerce")

        found = conversion_opportunities(_sold_frame(s, sold_b), live)
        return json.dumps({
            "count": len(found),
            "total_uplift": _money(sum(c.uplift_dollars for c in found)),
            "also_underpriced": sum(1 for c in found if c.is_underpriced),
            "caveat": ("Uplift is resale value only — conversion cost is not netted "
                       "off, and floor area cannot tell us whether the layout permits "
                       "the partition."),
            "properties": [{
                "id": c.id, "address": c.address, "suburb": c.suburb,
                "district": c.district, "beds_now": c.beds,
                "floor_m2": c.floor_area_m2,
                "typical_floor_for_one_more_bed": c.typical_floor_next,
                "asking": _money(c.asking_price), "our_value": _money(c.fair_value),
                "uplift": _money(c.uplift_dollars), "uplift_pct": _pct(c.uplift_pct),
                "also_underpriced": c.is_underpriced,
            } for c in found[:min(limit, MAX_ROWS)]],
        }, default=str)


def suburb_days_to_sell(suburb: str) -> str:
    """How long properties take to sell in a suburb, month by month.

    Args:
        suburb: Suburb name, e.g. "Browns Bay".
    """
    with SessionLocal() as s:
        batch = _active(s, "sold")
        if batch is None:
            return "No active sold batch loaded."
        rows = s.query(PropertySold.sold_date, PropertySold.days_on_market).filter(
            PropertySold.import_batch_id == batch,
            PropertySold.suburb.ilike(f"%{suburb}%"),
            PropertySold.days_on_market.isnot(None),
            PropertySold.days_on_market > 0,
        ).all()
        if not rows:
            return f"No sold records with a listing date for {suburb}."
        doms = sorted(r.days_on_market for r in rows)
        mid = len(doms) // 2
        return json.dumps({
            "suburb": suburb, "sales_with_a_listing_date": len(doms),
            "median_days_to_sell": doms[mid] if len(doms) % 2 else
                                   (doms[mid - 1] + doms[mid]) / 2,
            "average_days_to_sell": round(sum(doms) / len(doms), 1),
            "fastest": doms[0], "slowest": doms[-1],
        }, default=str)


def query_data(sql: str) -> str:
    """Run a read-only SQL SELECT against the property tables."""
    return sqltool.run(sql)


def distinct_values(table: str, column: str, limit: int = 40) -> str:
    """Show the real distinct values of a column, so nothing is guessed."""
    return sqltool.distinct_values(table, column, limit)


# --- provider-agnostic tool surface ---------------------------------------
# Plain JSON Schema rather than an SDK decorator, so Anthropic and OpenAI get
# byte-identical tool definitions and can't drift apart.

def _t(name, description, properties=None, required=None):
    return {"name": name, "description": description,
            "parameters": {"type": "object",
                           "properties": properties or {},
                           "required": required or []}}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}
_BOOL = {"type": "boolean"}

TOOL_SPECS = [
    _t("query_data",
       "Run a read-only SQL SELECT against the property tables. Use this for ANY "
       "question the other tools do not cover — aggregates, rankings, "
       "cross-suburb comparisons, correlations, counts, anything ad hoc. This is "
       "the most powerful tool; prefer it whenever a question does not map "
       "cleanly onto one of the others. You can call it several times to build "
       "up an answer.",
       {"sql": {**_STR, "description": "A single SELECT (or WITH ... SELECT) statement."}},
       ["sql"]),
    _t("distinct_values",
       "List the actual distinct values of a column with counts. Call this "
       "BEFORE filtering on any category or name you are not certain of — "
       "property_type is in Chinese, suburbs have exact spellings, titles are "
       "codes. Cheap insurance against a query that returns nothing because the "
       "value was spelled differently.",
       {"table": {**_STR, "enum": ["properties_for_sale", "properties_sold"]},
        "column": _STR, "limit": _INT},
       ["table", "column"]),
    _t("search_listings",
       "Search live for-sale listings by area, type, price, beds, buy score and "
       "deal flags — the same filters as the site's property finder.",
       {"suburb": _STR, "district": _STR,
        "property_type": {**_STR, "enum": ["house", "townhouse", "apartment", "unit", "section", "lifestyle"]},
        "min_beds": _INT,
        "max_price": {**_NUM, "description": "max asking $, e.g. 3000000 for under $3M"},
        "min_price": _NUM,
        "underpriced_only": _BOOL, "subdividable_only": _BOOL,
        "cashflow_positive_only": _BOOL,
        "min_margin_pct": {**_NUM, "description": "e.g. 15 for 15%"},
        "min_buy_score": {**_NUM, "description": "opportunity/buy score 0-100"},
        "sort_by": {**_STR, "enum": ["margin", "price", "lots", "days_on_market", "score", "yield"]},
        "limit": _INT}),
    _t("get_property", "Full detail on one listing.",
       {"property_id": _INT}, ["property_id"]),
    _t("get_sold_comparables",
       "Sold comparables for a listing, with vs-CV stats and sale-method mix.",
       {"property_id": _INT}, ["property_id"]),
    _t("market_summary", "Headline numbers across the whole active batch."),
    _t("renovation_value_by_district",
       "What a bedroom, bathroom or pool is worth per district, size-controlled."),
    _t("find_room_to_add_a_bedroom",
       "Listings that already hold the floor area for another bedroom.",
       {"limit": _INT}),
    _t("suburb_days_to_sell", "How long properties take to sell in a suburb.",
       {"suburb": _STR}, ["suburb"]),
]

_HANDLERS = {
    "query_data": query_data,
    "distinct_values": distinct_values,
    "search_listings": search_listings,
    "get_property": get_property,
    "get_sold_comparables": get_sold_comparables,
    "market_summary": market_summary,
    "renovation_value_by_district": renovation_value_by_district,
    "find_room_to_add_a_bedroom": find_room_to_add_a_bedroom,
    "suburb_days_to_sell": suburb_days_to_sell,
}


def dispatch(name: str, args: dict) -> str:
    """Execute a tool call. Errors come back as text so the model can recover."""
    fn = _HANDLERS.get(name)
    if fn is None:
        return f"No such tool: {name}"
    try:
        return fn(**args)
    except TypeError as exc:
        return f"Bad arguments for {name}: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"{name} failed: {type(exc).__name__}: {str(exc)[:300]}"
