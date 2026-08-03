"""Properties endpoints.

The Database page hits /api/properties for the paginated filterable list.
Individual listings come from /api/properties/{id}.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..external_estimates import homes_estimate
from ..propertyvalue import cross_check, gaps as pv_gaps_fn, missing_fills, pv_lookup
from ..models import AgentContact, ImportBatch, PropertyForSale, PropertySold, User
from ..pricing.valueadd import value_add
from ..pricing.glm import canonical_type
from ..pricing.buyprice import _title_bucket
from ..security import require_active, require_admin

router = APIRouter(prefix="/api/properties", tags=["properties"])

# NZ title types. Sold data stores numeric codes ("1"/"3"); for-sale stores text
# ("Freehold"/"Cross-Lease"). _title_bucket canonicalises both to FH/LH/CL/UT. We
# surface a readable label on each comp and prefer comps of the subject's title type
# (a cross-lease sells well below an equivalent freehold, so they can't be blended).
_TITLE_LABEL = {"FH": "Freehold", "LH": "Leasehold", "CL": "Cross-Lease", "UT": "Unit Title"}
_BUCKET_CODES = {"FH": ("1", "1.0"), "LH": ("2", "2.0"), "CL": ("3", "3.0"), "UT": ("4", "4.0")}


def _title_label(t) -> str | None:
    if t is None or str(t).strip() == "":
        return None
    return _TITLE_LABEL.get(_title_bucket(t))

# User-facing property categories → the canonical types they cover. The raw
# property_type is messy (English + Chinese), so we filter by canonicalising each
# distinct raw value with the same logic the pricing engine uses.
CATEGORY_MAP: dict[str, set[str]] = {
    "House": {"House", "Residence", "Home and Income"},
    "Townhouse": {"Townhouse"},
    "Apartment": {"Apartment"},
    "Unit": {"Unit"},
    "Section": {"Section"},
    "Lifestyle": {"Lifestyle Property", "Lifestyle Section"},
}


# ---------- Pydantic shapes ----------
class ForSaleRow(BaseModel):
    id: int
    address: str | None
    suburb: str | None
    district: str | None
    region: str | None
    property_type: str | None
    type_of_title: str | None
    zoning: str | None
    land_slope_contour: str | None
    beds: int | None
    baths: int | None
    cars: int | None
    floor_area_m2: float | None
    land_area_m2: float | None
    cv_numeric: float | None
    land_value_numeric: float | None
    improvement_value_numeric: float | None
    asking_price: float | None
    market_value: float | None
    predicted_list: float | None
    predicted_days: float | None
    comps_used: int | None
    confidence: str | None
    pred_vs_cv: float | None
    pred_vs_listing: float | None
    # v4 production AVM
    listing_type: str | None = None
    pricing_path: str | None = None
    range_low: float | None = None
    range_high: float | None = None
    subdivision_premium: float | None = None
    fair_value: float | None = None
    margin: float | None = None
    is_premium: bool | None = None
    buy_price: float | None = None
    area_value: float | None = None
    comp_tier: int | None = None
    comps_matched: int | None = None
    sections: int | None = None
    dwellings: int | None = None
    section_rate: float | None = None
    section_value_method: str | None = None  # bare_section_sales | council_land_value | fallback
    gross_sales: float | None = None
    subdivision_profit: float | None = None
    # v3.5/v3.8 legacy diagnostics
    pred_v35: float | None = None
    pred_v38: float | None = None
    z_weight: float | None = None
    beta_tier: str | None = None
    cv_anchor: float | None = None
    cv_ratio_tier: str | None = None
    correction_used: str | None = None
    min_lot_m2: float | None
    max_addl_lots: float | None
    total_subdivided_value: float | None
    uplift_vs_asking: float | None
    est_weekly_rent: float | None
    est_gross_yield: float | None
    annual_cashflow: float | None
    cash_on_cash: float | None
    breakeven_deposit_pct: float | None = None
    expected_sale: float | None = None
    expected_sale_path: str | None = None
    expected_sale_band: float | None = None
    opportunity_score: float | None
    opportunity_score_pct: float | None
    best_strategy: str | None
    best_net_gain: float | None
    is_underpriced: bool
    is_cashflow_positive: bool
    is_subdividable: bool
    url: str | None
    image_url: str | None
    image_count: int | None
    # Full gallery, newline-separated. Serialised as a list for the client.
    image_urls: str | None = None
    # Exposed for the Location map on the detail page. Present on 100% of rows.
    latitude: float | None = None
    longitude: float | None = None
    listing_date: str | None
    days_on_market: float | None

    # === New: scraper fields preserved ===
    key_facts: str | None = None
    key_time_on_market: str | None = None
    estate_description: str | None = None
    council_valuation_summary: str | None = None
    property_trend: str | None = None
    sale_status: str | None = None
    last_updated: str | None = None

    # === Third-party reference valuation ===
    third_party_valuation: float | None = None
    third_party_valuation_high: float | None = None
    third_party_valuation_low: float | None = None
    valuation_last_date: str | None = None

    # === CV change tracking ===
    valuation_rateable_change_pct: float | None = None
    valuation_land_change_pct: float | None = None
    valuation_improvement_change_pct: float | None = None

    # === Last sale of this property ===
    valuation_last_sold_value: float | None = None
    valuation_last_sold_date: str | None = None
    sold_listing_date: str | None = None
    sold_listing_price_label: str | None = None

    # === Trend JSONs (raw — frontend parses) ===
    valuation_trend_yearly_json: str | None = None
    valuation_trend_monthly_json: str | None = None
    sale_history_json: str | None = None
    cv_history_json: str | None = None
    schools_json: str | None = None

    # === Agent contact ===
    agent1_name: str | None = None
    agent1_phone: str | None = None
    agent1_email: str | None = None
    agent1_job_title: str | None = None
    agent1_company_name: str | None = None
    agent2_name: str | None = None
    agent2_phone: str | None = None
    agent2_email: str | None = None
    agent2_company_name: str | None = None
    company_name: str | None = None

    # === Other features ===
    building_age: str | None = None
    has_swimming_pool: bool | None = None
    is_new_construction: bool | None = None
    is_coastal_waterfront: bool | None = None
    storey_count: int | None = None
    parking_covered: int | None = None
    parking_other: int | None = None
    other_features: str | None = None
    description: str | None = None
    listing_title: str | None = None
    listing_published_date: str | None = None

    class Config:
        from_attributes = True


class ForSaleList(BaseModel):
    total: int
    page: int
    page_size: int
    rows: list[ForSaleRow]


class SoldRow(BaseModel):
    id: int
    address: str | None
    suburb: str | None
    property_type: str | None
    beds: int | None
    baths: int | None
    floor_area_m2: float | None
    land_area_m2: float | None
    cv_numeric: float | None
    sale_price: float | None
    sold_date: str | None
    sale_method: str | None
    days_on_market: float | None
    url: str | None

    class Config:
        from_attributes = True


class SoldList(BaseModel):
    total: int
    page: int
    page_size: int
    rows: list[SoldRow]


def _active_batch(db: Session, batch_type: str, region: str) -> int | None:
    b = (
        db.query(ImportBatch)
        .filter(ImportBatch.batch_type == batch_type, ImportBatch.region == region, ImportBatch.is_active.is_(True))
        .order_by(ImportBatch.id.desc())
        .first()
    )
    return b.id if b else None


# ---------- For-sale list ----------
@router.get("/export.csv")
def export_for_sale_csv(
    region: str = "Auckland",
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Stream the active for-sale batch as a CSV. Includes every column the algorithm
    computes plus the raw scraper fields, for client review in Excel."""
    import csv
    import io
    from fastapi.responses import StreamingResponse

    batch_id = _active_batch(db, "for_sale", region)
    if batch_id is None:
        return StreamingResponse(iter([""]), media_type="text/csv")

    cols = [
        # Identity
        "id", "slug_id", "address", "suburb", "district", "region", "postcode",
        "property_type", "type_of_title", "zoning", "land_slope_contour",
        # Specs
        "beds", "baths", "cars", "floor_area_m2", "land_area_m2", "building_age",
        # Valuation
        "cv_numeric", "land_value_numeric", "improvement_value_numeric",
        "asking_price", "market_value", "predicted_list", "predicted_days",
        "comps_used", "confidence",
        "pred_vs_cv", "pred_vs_listing",
        # Third-party reference valuation
        "third_party_valuation", "third_party_valuation_low", "third_party_valuation_high",
        # Subdivision
        "min_lot_m2", "max_addl_lots", "section_price_per_m2",
        "total_subdivided_value", "uplift_vs_asking",
        "best_strategy", "best_net_gain",
        # Cashflow
        "est_weekly_rent", "est_gross_yield", "annual_cashflow", "cash_on_cash",
        # Scoring
        "opportunity_score", "opportunity_score_pct",
        "is_underpriced", "is_cashflow_positive", "is_subdividable",
        # Last sale of this property
        "valuation_last_sold_value", "valuation_last_sold_date",
        # Listing meta
        "url", "listing_date", "days_on_market", "agent1_name", "agent1_phone", "agent1_company_name",
    ]

    def row_stream():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)

        # Server-side pagination to avoid memory blow-up on 35k rows
        page_size = 1000
        offset = 0
        while True:
            page = (
                db.query(PropertyForSale)
                .filter(PropertyForSale.import_batch_id == batch_id)
                .order_by(PropertyForSale.id.asc())
                .offset(offset).limit(page_size).all()
            )
            if not page:
                break
            for r in page:
                writer.writerow([getattr(r, c, "") if getattr(r, c, None) is not None else "" for c in cols])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)
            offset += page_size

    return StreamingResponse(
        row_stream(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="ollie_priced_export_{batch_id}.csv"',
        },
    )


# Property types that legitimately carry no building (bare land) — a missing floor
# area is expected. Any OTHER type with no floor area is an incomplete/bad record.
_SECTION_TYPES = ("建地", "乡村住宅建地", "土地", "地皮", "Section", "Vacant land", "Land")


def _hide_bad_data(q):
    """Hide rows that shouldn't be live: dwelling-type listings missing a floor
    area (incomplete data; bare-land/section types are kept), and any row HELD
    back during the pre-publish review until an admin fixes and publishes it."""
    return q.filter(
        PropertyForSale.is_held.is_(False),
        or_(
            PropertyForSale.floor_area_m2.isnot(None),
            PropertyForSale.property_type.in_(_SECTION_TYPES),
        ),
    )


def _filtered_query(
    db: Session,
    batch_id: int,
    *,
    suburb=None, type=None, category=None, underpriced=None,
    cashflow_positive=None, subdividable=None, min_margin=None, min_comps=None,
    max_breakeven_deposit=None, min_score=None, min_price=None, max_price=None,
    min_beds=None, district=None, search=None,
):
    """The shared filter chain for the listing list and its summary tiles.

    Kept in one place so the stat tiles above a deal-finder page can never
    describe a different population than the rows underneath them.
    """
    q = db.query(PropertyForSale).filter(PropertyForSale.import_batch_id == batch_id)
    q = _hide_bad_data(q)
    if suburb:
        q = q.filter(PropertyForSale.suburb == suburb)
    if type:
        q = q.filter(PropertyForSale.property_type == type)
    if category and category in CATEGORY_MAP:
        wanted = CATEGORY_MAP[category]
        raw_types = [
            t for (t,) in db.query(PropertyForSale.property_type)
            .filter(PropertyForSale.import_batch_id == batch_id)
            .distinct().all() if t
        ]
        matching = [t for t in raw_types if canonical_type(t) in wanted]
        q = q.filter(PropertyForSale.property_type.in_(matching or ["__none__"]))
    if underpriced is not None:
        q = q.filter(PropertyForSale.is_underpriced.is_(underpriced))
    if cashflow_positive is not None:
        q = q.filter(PropertyForSale.is_cashflow_positive.is_(cashflow_positive))
    if subdividable is not None:
        q = q.filter(PropertyForSale.is_subdividable.is_(subdividable))
    if min_margin is not None:
        q = q.filter(PropertyForSale.margin.isnot(None),
                     PropertyForSale.margin >= min_margin)
    if min_comps is not None:
        q = q.filter(PropertyForSale.comps_used.isnot(None),
                     PropertyForSale.comps_used >= min_comps)
    if max_breakeven_deposit is not None:
        q = q.filter(PropertyForSale.breakeven_deposit_pct.isnot(None),
                     PropertyForSale.breakeven_deposit_pct <= max_breakeven_deposit)
    if min_score is not None:
        q = q.filter(PropertyForSale.opportunity_score_pct >= min_score)
    if min_price is not None:
        q = q.filter(PropertyForSale.asking_price >= min_price)
    if max_price is not None:
        q = q.filter(PropertyForSale.asking_price <= max_price)
    if min_beds is not None:
        q = q.filter(PropertyForSale.beds.isnot(None), PropertyForSale.beds >= min_beds)
    if district:
        q = q.filter(PropertyForSale.district == district)
    if search:
        like = f"%{search}%"
        q = q.filter(or_(PropertyForSale.address.ilike(like), PropertyForSale.suburb.ilike(like)))

    return q


class ForSaleSummary(BaseModel):
    """Headline tiles for a deal-finder page — same filters as the row list."""
    total: int
    median_margin: float | None
    median_margin_dollars: float | None
    median_lots: float | None
    top_id: int | None


@router.get("/summary", response_model=ForSaleSummary)
def summarise_for_sale(
    region: str = "Auckland",
    suburb: str | None = None,
    type: str | None = None,
    category: str | None = None,
    underpriced: bool | None = None,
    cashflow_positive: bool | None = None,
    subdividable: bool | None = None,
    min_margin: float | None = Query(None, ge=0, le=1),
    min_comps: int | None = Query(None, ge=0, le=100),
    max_breakeven_deposit: float | None = Query(None, ge=0, le=1),
    min_score: float | None = Query(None, ge=0, le=100),
    min_price: float | None = None,
    max_price: float | None = None,
    min_beds: int | None = Query(None, ge=0, le=20),
    district: str | None = None,
    search: str | None = None,
    order_by: str = Query("margin"),
    db: Session = Depends(get_db),
) -> ForSaleSummary:
    batch_id = _active_batch(db, "for_sale", region)
    if batch_id is None:
        return ForSaleSummary(total=0, median_margin=None, median_margin_dollars=None,
                              median_lots=None, top_id=None)

    q = _filtered_query(
        db, batch_id,
        suburb=suburb, type=type, category=category, underpriced=underpriced,
        cashflow_positive=cashflow_positive, subdividable=subdividable,
        min_margin=min_margin, min_comps=min_comps,
        max_breakeven_deposit=max_breakeven_deposit, min_score=min_score,
        min_price=min_price, max_price=max_price,
        min_beds=min_beds, district=district, search=search,
    )
    rows = q.with_entities(
        PropertyForSale.id, PropertyForSale.margin,
        PropertyForSale.fair_value, PropertyForSale.asking_price,
        PropertyForSale.max_addl_lots,
    ).all()
    if not rows:
        return ForSaleSummary(total=0, median_margin=None, median_margin_dollars=None,
                              median_lots=None, top_id=None)

    def _median(vals):
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return None
        mid = len(vals) // 2
        return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2

    margins = [r.margin for r in rows]
    dollars = [r.fair_value - r.asking_price for r in rows
               if r.fair_value is not None and r.asking_price is not None]
    lots = [r.max_addl_lots for r in rows]

    # The hero card shows the single sharpest deal, chosen on the same axis the
    # page is sorted by rather than always on margin.
    key = (lambda r: r.max_addl_lots) if order_by == "max_addl_lots" else (lambda r: r.margin)
    ranked = [r for r in rows if key(r) is not None]
    top = max(ranked, key=key).id if ranked else None

    return ForSaleSummary(
        total=len(rows),
        median_margin=_median(margins),
        median_margin_dollars=_median(dollars),
        median_lots=_median(lots),
        top_id=top,
    )


@router.get("", response_model=ForSaleList)
def list_for_sale(
    region: str = "Auckland",
    suburb: str | None = None,
    type: str | None = None,
    category: str | None = None,
    underpriced: bool | None = None,
    cashflow_positive: bool | None = None,
    subdividable: bool | None = None,
    min_margin: float | None = Query(
        None, ge=0, le=1,
        description="Only listings with at least this margin, e.g. 0.15"),
    min_comps: int | None = Query(
        None, ge=0, le=100,
        description="Only listings backed by at least this many sold comps"),
    max_breakeven_deposit: float | None = Query(
        None, ge=0, le=1,
        description="Only listings that break even at or below this deposit fraction"),
    min_score: float | None = Query(None, ge=0, le=100),
    min_price: float | None = None,
    max_price: float | None = None,
    min_beds: int | None = Query(None, ge=0, le=20),
    district: str | None = None,
    search: str | None = None,
    order_by: str = Query("opportunity_score_pct", pattern="^(opportunity_score_pct|asking_price|market_value|fair_value|buy_price|cash_on_cash|breakeven_deposit_pct|max_addl_lots|predicted_days|days_on_market|address|margin|margin_dollars)$"),
    order_dir: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> ForSaleList:
    batch_id = _active_batch(db, "for_sale", region)
    if batch_id is None:
        return ForSaleList(total=0, page=page, page_size=page_size, rows=[])

    q = _filtered_query(
        db, batch_id,
        suburb=suburb, type=type, category=category, underpriced=underpriced,
        cashflow_positive=cashflow_positive, subdividable=subdividable,
        min_margin=min_margin, min_comps=min_comps,
        max_breakeven_deposit=max_breakeven_deposit, min_score=min_score,
        min_price=min_price, max_price=max_price,
        min_beds=min_beds, district=district, search=search,
    )
    total = q.count()
    # "margin_dollars" is the dollar gap (fair_value − asking), computed on the
    # fly; everything else is a real column.
    if order_by == "margin_dollars":
        sort_col = PropertyForSale.fair_value - PropertyForSale.asking_price
    else:
        sort_col = getattr(PropertyForSale, order_by)
    sort_col = desc(sort_col) if order_dir == "desc" else sort_col.asc()
    # Push NULLs to the end either way so empty values don't dominate the top.
    rows = q.order_by(sort_col.nullslast()).offset((page - 1) * page_size).limit(page_size).all()
    return ForSaleList(total=total, page=page, page_size=page_size, rows=rows)


class MapPoint(BaseModel):
    id: int
    lat: float
    lng: float
    address: str | None = None
    suburb: str | None = None
    price: float | None = None        # asking (for-sale) or sale price (sold)
    est: float | None = None          # Ollie estimate — for-sale only
    score: float | None = None        # buy score — for-sale only
    beds: int | None = None
    underpriced: bool = False
    subdividable: bool = False
    sold_date: str | None = None      # sold only


class MapResponse(BaseModel):
    dataset: str
    count: int
    points: list[MapPoint]


@router.get("/map", response_model=MapResponse)
def map_points(
    dataset: str = Query("for_sale", pattern="^(for_sale|sold)$"),
    region: str = "Auckland",
    suburb: str | None = None,
    category: str | None = None,
    underpriced: bool | None = None,
    subdividable: bool | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    min_beds: int | None = Query(None, ge=0, le=20),
    district: str | None = None,
    search: str | None = None,
    limit: int = Query(20000, ge=1, le=30000),
    db: Session = Depends(get_db),
) -> MapResponse:
    """Geocoded points for the map view. Honours the same area/beds/budget/deal
    filters as the list, so the map and the table describe the same population.
    `dataset=sold` plots recent sales in the area instead of live listings."""
    P = PropertyForSale
    if dataset == "sold":
        batch_id = _active_batch(db, "sold", region)
        if batch_id is None:
            return MapResponse(dataset=dataset, count=0, points=[])
        S = PropertySold
        q = db.query(
            S.id, S.latitude, S.longitude, S.address, S.suburb,
            S.sale_price, S.beds, S.sold_date,
        ).filter(
            S.import_batch_id == batch_id,
            # NZ bounding box — drops the handful of rows with a broken geocode
            # (0,0, swapped, or a stray northern-hemisphere coord) that would
            # otherwise scatter pins across Europe/India and force a world zoom.
            S.latitude.between(-47.5, -34.0), S.longitude.between(166.0, 179.5),
        )
        if suburb:
            q = q.filter(S.suburb == suburb)
        if district:
            q = q.filter(S.district == district)
        if min_beds is not None:
            q = q.filter(S.beds.isnot(None), S.beds >= min_beds)
        if min_price is not None:
            q = q.filter(S.sale_price >= min_price)
        if max_price is not None:
            q = q.filter(S.sale_price <= max_price)
        if search:
            like = f"%{search}%"
            q = q.filter(or_(S.address.ilike(like), S.suburb.ilike(like)))
        rows = q.limit(limit).all()
        points = [
            MapPoint(id=r.id, lat=r.latitude, lng=r.longitude, address=r.address,
                     suburb=r.suburb, price=r.sale_price, beds=r.beds,
                     sold_date=str(r.sold_date) if r.sold_date else None)
            for r in rows
        ]
        return MapResponse(dataset=dataset, count=len(points), points=points)

    # for-sale — reuse the shared filter chain, then keep only geocoded rows
    batch_id = _active_batch(db, "for_sale", region)
    if batch_id is None:
        return MapResponse(dataset=dataset, count=0, points=[])
    q = _filtered_query(
        db, batch_id,
        suburb=suburb, category=category, underpriced=underpriced,
        subdividable=subdividable, min_price=min_price, max_price=max_price,
        min_beds=min_beds, district=district, search=search,
    ).filter(
        # NZ bounding box — see the sold branch; drops broken geocodes.
        P.latitude.between(-47.5, -34.0), P.longitude.between(166.0, 179.5),
    ).with_entities(
        P.id, P.latitude, P.longitude, P.address, P.suburb, P.asking_price,
        P.fair_value, P.market_value, P.opportunity_score_pct, P.beds,
        P.is_underpriced, P.is_subdividable,
    )
    rows = q.limit(limit).all()
    points = [
        MapPoint(
            id=r.id, lat=r.latitude, lng=r.longitude, address=r.address,
            suburb=r.suburb, price=r.asking_price,
            est=r.fair_value if r.fair_value is not None else r.market_value,
            score=r.opportunity_score_pct, beds=r.beds,
            underpriced=bool(r.is_underpriced), subdividable=bool(r.is_subdividable),
        )
        for r in rows
    ]
    return MapResponse(dataset=dataset, count=len(points), points=points)


class PvDiscrepancy(BaseModel):
    field: str
    ours: float | str | None = None
    theirs: float | str | None = None
    severity: str


class PvGap(BaseModel):
    field: str
    theirs: float | str | None = None


class ExternalEstimates(BaseModel):
    homes_valuation: float | None = None
    homes_valuation_low: float | None = None
    homes_valuation_high: float | None = None
    homes_cv: float | None = None
    homes_url: str | None = None
    # realestate.co.nz — slot reserved; null until a source is wired in.
    realestate_valuation: float | None = None
    realestate_valuation_low: float | None = None
    realestate_valuation_high: float | None = None
    realestate_url: str | None = None
    # propertyvalue.co.nz (CoreLogic): AVM range + council CV + zoning, plus a
    # verification cross-check of our attributes against theirs.
    pv_estimate_low: float | None = None
    pv_estimate_high: float | None = None
    pv_estimate_mid: float | None = None
    pv_cv: float | None = None
    pv_zoning: str | None = None
    pv_url: str | None = None
    pv_last_sale_price: float | None = None
    pv_last_sale_date: str | None = None
    pv_discrepancies: list[PvDiscrepancy] = []
    pv_gaps: list[PvGap] = []      # fields CoreLogic has that we're missing


_HOMES_TTL = timedelta(days=14)   # re-check a property at most every fortnight


@router.get("/{property_id}/external-estimates", response_model=ExternalEstimates)
def external_estimates(property_id: int, db: Session = Depends(get_db)) -> ExternalEstimates:
    """Third-party estimates for the Compare-estimates panel, fetched on demand
    the way an agent would — search the address, open the listing, read the
    figure — then cached per property so it isn't re-fetched on every view.
    Currently: homes.co.nz. Best-effort: a miss returns nulls, never an error."""
    p = db.get(PropertyForSale, property_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")

    now = datetime.now(timezone.utc)
    # Full address (street + suburb) makes the search resolve to the right page.
    query_addr = ", ".join(x for x in (p.address, p.suburb, "Auckland") if x)

    # --- homes.co.nz -------------------------------------------------------------
    # Cache a hit for a fortnight; re-check a miss (e.g. search throttled) in 12h.
    ttl = _HOMES_TTL if p.homes_valuation is not None else timedelta(hours=12)
    if p.address and not (p.homes_checked_at and now - p.homes_checked_at < ttl):
        est = homes_estimate(query_addr)    # sync fetch; endpoint runs in a threadpool
        if est:
            p.homes_valuation = est.get("value")
            p.homes_valuation_low = est.get("low")
            p.homes_valuation_high = est.get("high")
            p.homes_cv = est.get("cv")
            p.homes_url = est.get("url")
        p.homes_checked_at = now            # stamp even on a miss
        db.commit()

    # --- propertyvalue.co.nz (CoreLogic) ----------------------------------------
    pv_ttl = _HOMES_TTL if p.pv_data is not None else timedelta(hours=12)
    if p.address and not (p.pv_checked_at and now - p.pv_checked_at < pv_ttl):
        pv = pv_lookup(query_addr)
        if pv:
            p.pv_estimate_low = pv.get("estimate_low")
            p.pv_estimate_high = pv.get("estimate_high")
            p.pv_estimate_mid = pv.get("estimate_mid")
            p.pv_cv = pv.get("cv")
            p.pv_url = pv.get("url")
            p.pv_last_sale_price = pv.get("last_sale_price")
            p.pv_last_sale_date = pv.get("last_sale_date")
            p.pv_data = json.dumps(pv)
        p.pv_checked_at = now
        db.commit()

    # Cross-check our stored attributes against CoreLogic's (from the cached blob),
    # and note fields they have that we're missing.
    discrepancies: list[PvDiscrepancy] = []
    gap_list: list[PvGap] = []
    pv_zoning = pv_last_price = pv_last_date = None
    if p.pv_data:
        try:
            pv = json.loads(p.pv_data)
            pv_zoning = pv.get("zoning")
            pv_last_price = pv.get("last_sale_price")
            pv_last_date = pv.get("last_sale_date")
            # Fill our blanks (floor/land/beds/baths/cv/zoning) from CoreLogic so
            # the listing shows real numbers instead of 0/—. Gaps-only, never
            # overwrites. After filling, those fields stop showing as gaps too.
            fills = missing_fills({
                "floor_area_m2": p.floor_area_m2, "land_area_m2": p.land_area_m2,
                "beds": p.beds, "baths": p.baths, "cv_numeric": p.cv_numeric, "zoning": p.zoning,
            }, pv)
            if fills:
                for k, v in fills.items():
                    setattr(p, k, v)
                db.commit()
            ours = {
                "land_area_m2": p.land_area_m2, "floor_area_m2": p.floor_area_m2,
                "beds": p.beds, "baths": p.baths, "cv": p.cv_numeric, "zoning": p.zoning,
                "year_built": None, "last_sale_price": None,
            }
            discrepancies = [PvDiscrepancy(**d) for d in cross_check(ours, pv)]
            gap_list = [PvGap(**g) for g in pv_gaps_fn(ours, pv)]
        except (ValueError, TypeError):
            pass

    return ExternalEstimates(
        homes_valuation=p.homes_valuation, homes_valuation_low=p.homes_valuation_low,
        homes_valuation_high=p.homes_valuation_high, homes_cv=p.homes_cv,
        homes_url=p.homes_url,
        realestate_valuation=p.realestate_valuation,
        realestate_valuation_low=p.realestate_valuation_low,
        realestate_valuation_high=p.realestate_valuation_high,
        realestate_url=p.realestate_url,
        pv_estimate_low=p.pv_estimate_low, pv_estimate_high=p.pv_estimate_high,
        pv_estimate_mid=p.pv_estimate_mid, pv_cv=p.pv_cv, pv_zoning=pv_zoning,
        pv_url=p.pv_url, pv_last_sale_price=pv_last_price, pv_last_sale_date=pv_last_date,
        pv_discrepancies=discrepancies, pv_gaps=gap_list,
    )


class AgentContactIn(BaseModel):
    channel: str | None = None      # "email" | "phone"


@router.post("/{property_id}/agent-contact", status_code=201)
def log_agent_contact(property_id: int, body: AgentContactIn,
                      me: User = Depends(require_active), db: Session = Depends(get_db)):
    """Record a buyer's-agent enquiry so the admin dashboard can count them."""
    p = db.get(PropertyForSale, property_id)
    db.add(AgentContact(
        user_id=me.id, property_id=property_id,
        address=p.address if p else None, suburb=p.suburb if p else None,
        channel=body.channel,
    ))
    db.commit()
    return {"ok": True}


class ScenarioIn(BaseModel):
    """Developer overrides. Any field left null keeps the stored default."""
    services_per_section: float | None = None
    selling_pct: float | None = None
    acquisition_pct: float | None = None
    refurb_allowance: float | None = None
    house_resale_pct: float | None = None
    section_rate: float | None = None
    incidentals_per_section: float | None = None
    buy_price: float | None = None       # override the modelled acquisition price
    improvement_value: float | None = None   # what the buildings are worth
    raw_land_rate: float | None = None       # $/m² of land inside the parent title
    market_ratio: float | None = None        # scales council values to market
    build_rate: float | None = None          # $/m² replacement build cost
    holding_rate: float | None = None        # finance/holding per year on project cost
    holding_years: float | None = None       # how long the money is tied up
    contingency_rate: float | None = None    # contingency on the development spend
    gst_rate: float | None = None            # net GST on the margin
    full_subdivision: bool | None = None     # demolish the house & subdivide the whole site


class ScenarioOut(BaseModel):
    sections: int | None
    max_addl_lots: float | None
    section_rate: float | None
    house_resale: float | None
    retained_land_m2: float | None
    improvement_value: float | None
    raw_land_rate: float | None
    market_ratio: float | None
    new_sections_value: float | None
    gross_sales: float | None
    services_cost: float | None
    selling_cost: float | None
    acquisition_cost: float | None
    incidentals_cost: float | None
    demolition_cost: float | None            # only in demolish/full-subdivision mode
    holding_cost: float | None               # finance/holding over the project life
    holding_years: float | None              # the period the holding cost is charged over
    contingency_cost: float | None           # contingency on the development spend
    gst_cost: float | None                   # net GST on the development margin
    buy_price: float | None
    subdivision_profit: float | None
    is_profitable: bool
    implausible_vs_value: bool = False       # gross dwarfs the site's own market value
    has_house: bool                          # whether a retained-house option even exists
    full_subdivision: bool                   # which mode these numbers reflect
    defaults: dict


@router.post("/{property_id}/subdivision-scenario", response_model=ScenarioOut)
def subdivision_scenario(
    property_id: int, body: ScenarioIn, db: Session = Depends(get_db)
) -> ScenarioOut:
    """Recompute a property's subdivision profit under developer-supplied numbers.

    Deliberately server-side: the profit formula lives in exactly one place
    (pricing.subdivision.compute), so the editable UI can't drift away from what
    the batch ingest computes. Nothing is persisted — this is a what-if.
    """
    from ..pricing.subdivision import (
        SubdivisionAssumptions, compute as sd_compute,
        _road_allowance, _building_value,
        GROSS_VS_CV_CAP as SD_GROSS_VS_CV_CAP,
    )

    p = db.get(PropertyForSale, property_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")

    defaults = SubdivisionAssumptions()

    buy = body.buy_price if (body.buy_price and body.buy_price > 0) else p.buy_price

    # market_ratio scales council figures to market. It normally derives from
    # buy / CV, but when a developer overrides the buy price it must NOT follow
    # it: choosing to pay more does not make the house worth more. Pin it to the
    # modelled price so an override costs what it should, unless the developer
    # sets the ratio explicitly too. Resolved BEFORE the (frozen) assumptions are
    # built, or the override silently has no effect.
    market_ratio_override = body.market_ratio
    if market_ratio_override is None and body.buy_price and p.buy_price and p.cv_numeric:
        market_ratio_override = p.buy_price / p.cv_numeric

    ap = SubdivisionAssumptions(
        services_per_section=_pick(body.services_per_section, defaults.services_per_section),
        selling_pct=_pick(body.selling_pct, defaults.selling_pct),
        acquisition_pct=_pick(body.acquisition_pct, defaults.acquisition_pct),
        refurb_allowance=_pick(body.refurb_allowance, defaults.refurb_allowance),
        house_resale_pct=_pick(body.house_resale_pct, defaults.house_resale_pct),
        section_rate=body.section_rate if body.section_rate else None,
        incidentals_per_section=_pick(body.incidentals_per_section, 0.0),
        build_rate=_pick(body.build_rate, defaults.build_rate),
        holding_rate=_pick(body.holding_rate, defaults.holding_rate),
        holding_years=_pick(body.holding_years, defaults.holding_years),
        contingency_rate=_pick(body.contingency_rate, defaults.contingency_rate),
        gst_rate=_pick(body.gst_rate, defaults.gst_rate),
        improvement_value=body.improvement_value,
        raw_land_rate=body.raw_land_rate,
        market_ratio=market_ratio_override,
    )
    has_dwelling = bool((p.beds or 0) > 0 or (p.baths or 0) > 0)
    full = bool(body.full_subdivision) or not has_dwelling   # whole-site mode
    demolish = bool(body.full_subdivision) and has_dwelling   # a house is being knocked down

    sd = sd_compute(
        zone=p.zoning, land_area=p.land_area_m2, buy_price=buy,
        section_rate=p.section_rate, property_type=p.property_type,
        title_type=p.type_of_title, address=p.address,
        improvement_value=p.improvement_value_numeric,
        land_value=p.land_value_numeric, cv=p.cv_numeric,
        beds=p.beds, baths=p.baths, floor_area=p.floor_area_m2,
        force_full_subdivision=bool(body.full_subdivision),
        assumptions=ap,
    )

    # Re-derive the individual lines so the UI can show the workings. In whole-site
    # mode there is no retained house: every lot sells as a section, and a house
    # (if there was one) carries a demolition/works allowance instead of a resale.
    addl = int(sd.max_addl_lots or 0)
    rate = sd.section_rate
    lots_for_value = sd.sections if full else addl
    new_sections_value = (lots_for_value * rate * sd.min_lot_m2) if (rate and sd.min_lot_m2) else None
    # The retained building is valued at replacement cost (floor × build rate),
    # unless the developer has typed an explicit "buildings worth".
    building_value = (ap.improvement_value if ap.improvement_value is not None
                      else _building_value(p.floor_area_m2, p.improvement_value_numeric, ap.build_rate))
    raw_rate = ap.raw_land_rate
    if raw_rate is None and p.land_value_numeric and p.land_area_m2:
        raw_rate = p.land_value_numeric / p.land_area_m2
    market_ratio = ap.market_ratio
    if market_ratio is None:
        market_ratio = (buy / p.cv_numeric) if (buy and p.cv_numeric) else 1.0
    retained_land = None
    house_resale = None
    demolition_cost = None
    if demolish:
        demolition_cost = ap.refurb_allowance
    elif not full:  # retain-house mode
        min_lot_v = sd.min_lot_m2 or 0
        prelim = int((p.land_area_m2 or 0) / min_lot_v) if min_lot_v else 0
        usable = (p.land_area_m2 * (1 - _road_allowance(prelim))) if p.land_area_m2 else None
        # Clamp to two min lots — mirrors compute() in pricing/subdivision.py. On a
        # large block `sections` (hence `addl`) is capped by MAX_PRACTICAL_LOTS_TOTAL,
        # so `usable − addl×min_lot` balloons to tens of thousands of m² and books a
        # phantom multi-million-dollar retained "house". A retained house keeps ONE
        # residential lot, never the undeveloped surplus.
        retained_land = (min(max(usable - addl * min_lot_v, min_lot_v), 2 * min_lot_v)
                         if usable is not None else None)
        if buy and building_value is not None and rate and retained_land is not None:
            house_resale = ((retained_land * rate + building_value)
                            * ap.house_resale_pct) - ap.refurb_allowance
    selling_cost = (sd.gross_sales * ap.selling_pct) if sd.gross_sales is not None else None
    acquisition_cost = (buy * ap.acquisition_pct) if buy else None
    holding_cost = ((buy + sd.services_cost) * ap.holding_rate * ap.holding_years) if buy else None
    incidentals_cost = addl * ap.incidentals_per_section

    # Reconstruct the contingency and GST lines so the workings tie back to the
    # stored net (compute() applies both internally after the base profit).
    contingency_cost = None
    gst_cost = None
    if sd.gross_sales is not None and buy:
        base_profit = (sd.gross_sales - buy - sd.services_cost - (selling_cost or 0)
                       - (acquisition_cost or 0) - incidentals_cost
                       - (demolition_cost or 0) - (holding_cost or 0))
        contingency_cost = ap.contingency_rate * (sd.services_cost + (holding_cost or 0)
                                                  + (demolition_cost or 0) + incidentals_cost)
        gst_cost = ap.gst_rate * max(base_profit - contingency_cost, 0.0)

    return ScenarioOut(
        sections=sd.sections, max_addl_lots=sd.max_addl_lots, section_rate=rate,
        house_resale=house_resale, retained_land_m2=retained_land,
        improvement_value=building_value, raw_land_rate=raw_rate,
        market_ratio=market_ratio, new_sections_value=new_sections_value,
        gross_sales=sd.gross_sales, services_cost=sd.services_cost,
        selling_cost=selling_cost, acquisition_cost=acquisition_cost,
        incidentals_cost=incidentals_cost,
        demolition_cost=demolition_cost, holding_cost=holding_cost,
        holding_years=ap.holding_years,
        contingency_cost=contingency_cost, gst_cost=gst_cost,
        buy_price=buy, subdivision_profit=sd.subdivision_profit,
        is_profitable=sd.is_subdividable,
        implausible_vs_value=bool(
            sd.gross_sales and p.cv_numeric and p.cv_numeric > 0
            and sd.gross_sales > SD_GROSS_VS_CV_CAP * p.cv_numeric),
        has_house=has_dwelling, full_subdivision=full,
        defaults={
            "services_per_section": defaults.services_per_section,
            "selling_pct": defaults.selling_pct,
            "acquisition_pct": defaults.acquisition_pct,
            "refurb_allowance": defaults.refurb_allowance,
            "house_resale_pct": defaults.house_resale_pct,
            "section_rate": p.section_rate,
            "incidentals_per_section": 0.0,
            "buy_price": p.buy_price,
            "improvement_value": building_value,
            "raw_land_rate": (p.land_value_numeric / p.land_area_m2)
                             if (p.land_value_numeric and p.land_area_m2) else None,
            "market_ratio": (p.buy_price / p.cv_numeric)
                            if (p.buy_price and p.cv_numeric) else None,
            "build_rate": defaults.build_rate,
            "holding_rate": defaults.holding_rate,
            "holding_years": defaults.holding_years,
            "contingency_rate": defaults.contingency_rate,
            "gst_rate": defaults.gst_rate,
        },
    )


def _pick(v, default):
    return float(v) if v is not None else float(default)


class CashflowIn(BaseModel):
    """Developer overrides. Any field left null keeps the stored default."""
    buy_price: float | None = None
    deposit_pct: float | None = None
    weekly_rent: float | None = None
    mortgage_rate: float | None = None
    loan_term_years: int | None = None
    opex_pct: float | None = None


class CashflowOut(BaseModel):
    buy_price: float | None
    deposit: float | None
    loan: float | None
    weekly_rent: float | None
    rent_source: str | None
    annual_gross_rent: float | None
    annual_net_rent: float | None
    annual_mortgage: float | None
    annual_cashflow: float | None
    weekly_cashflow: float | None
    cash_on_cash: float | None
    gross_yield: float | None
    breakeven_weekly_rent: float | None
    breakeven_deposit_pct: float | None
    is_cashflow_positive: bool
    defaults: dict


@router.post("/{property_id}/cashflow-scenario", response_model=CashflowOut)
def cashflow_scenario(
    property_id: int, body: CashflowIn, db: Session = Depends(get_db)
) -> CashflowOut:
    """Recompute cashflow on the buy price under developer-supplied numbers.

    Server-side for the same reason as the subdivision scenario: one copy of the
    formula. Nothing is persisted.
    """
    from ..pricing.cashflow import CashflowAssumptions, annual_mortgage_payment, compute as cf_compute

    p = db.get(PropertyForSale, property_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")

    d = CashflowAssumptions()
    ca = CashflowAssumptions(
        deposit_pct=_pick(body.deposit_pct, d.deposit_pct),
        mortgage_rate=_pick(body.mortgage_rate, d.mortgage_rate),
        loan_term_years=int(_pick(body.loan_term_years, d.loan_term_years)),
        opex_pct=_pick(body.opex_pct, d.opex_pct),
        weekly_rent=body.weekly_rent,
        buy_price=body.buy_price,
    )
    cf = cf_compute(
        asking_price=p.asking_price, market_value=p.market_value, cv=p.cv_numeric,
        observed_weekly_rent=p.est_weekly_rent, buy_price=p.buy_price, assumptions=ca,
    )
    price = (body.buy_price if (body.buy_price and body.buy_price > 0)
             else (p.buy_price or p.market_value or p.asking_price))
    deposit = price * ca.deposit_pct if price else None
    loan = (price - deposit) if (price and deposit is not None) else None

    # Rent needed to cover the mortgage after opex — the number that decides it.
    breakeven = None
    if loan is not None:
        breakeven = (annual_mortgage_payment(loan, ca.mortgage_rate, ca.loan_term_years)
                     / (1 - ca.opex_pct) / 52)

    return CashflowOut(
        buy_price=price, deposit=deposit, loan=loan,
        weekly_rent=cf.est_weekly_rent, rent_source=cf.rent_source,
        annual_gross_rent=cf.annual_gross_rent, annual_net_rent=cf.annual_net_rent,
        annual_mortgage=cf.annual_mortgage, annual_cashflow=cf.annual_cashflow,
        weekly_cashflow=(cf.annual_cashflow / 52) if cf.annual_cashflow is not None else None,
        cash_on_cash=cf.cash_on_cash, gross_yield=cf.est_gross_yield,
        breakeven_weekly_rent=breakeven,
        breakeven_deposit_pct=cf.breakeven_deposit_pct,
        is_cashflow_positive=bool(cf.annual_cashflow and cf.annual_cashflow > 0),
        defaults={
            "buy_price": p.buy_price, "deposit_pct": d.deposit_pct,
            "weekly_rent": p.est_weekly_rent, "mortgage_rate": d.mortgage_rate,
            "loan_term_years": d.loan_term_years, "opex_pct": d.opex_pct,
        },
    )


@router.get("/{property_id}", response_model=ForSaleRow)
def get_for_sale(property_id: int, db: Session = Depends(get_db)) -> PropertyForSale:
    p = db.get(PropertyForSale, property_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")
    # If the user landed on a stale URL (bookmark / shared link / old screenshot),
    # the row belongs to a prior batch and the numbers are from an old algo.
    # Auto-resolve to the same listing's row in the currently active batch so
    # the user always sees the latest pricing instead of a historical snapshot.
    active_batch_id = _active_batch(db, "for_sale", p.region or "Auckland")
    if active_batch_id and p.import_batch_id != active_batch_id:
        current = None
        if p.slug_id:
            current = (
                db.query(PropertyForSale)
                .filter(PropertyForSale.import_batch_id == active_batch_id)
                .filter(PropertyForSale.slug_id == p.slug_id)
                .first()
            )
        if not current and p.address and p.suburb:
            current = (
                db.query(PropertyForSale)
                .filter(PropertyForSale.import_batch_id == active_batch_id)
                .filter(PropertyForSale.address == p.address)
                .filter(PropertyForSale.suburb == p.suburb)
                .first()
            )
        if current:
            return current
    return p


class HistoryPoint(BaseModel):
    batch_id: int
    batch_date: str
    is_active: bool
    asking_price: float | None
    market_value: float | None
    opportunity_score_pct: float | None
    est_weekly_rent: float | None
    pred_vs_listing: float | None


class HistoryResponse(BaseModel):
    slug_id: str | None
    address: str | None
    suburb: str | None
    points: list[HistoryPoint]


class ComparableSale(BaseModel):
    id: int
    address: str | None
    suburb: str | None
    property_type: str | None
    beds: int | None
    baths: int | None
    floor_area_m2: float | None
    land_area_m2: float | None
    sale_price: float | None
    sold_date: str | None
    sale_method: str | None
    url: str | None
    image_url: str | None
    cv_numeric: float | None
    type_of_title: str | None
    # Closeness diagnostics
    beds_delta: int | None
    floor_pct_delta: float | None


class MethodShare(BaseModel):
    method: str            # "Auction" / "Price by Negotiation" / "Tender" / "Unknown"
    count: int
    median_vs_cv: float | None   # 0.04 = this group sold 4% over CV
    # Under 8 sales the median is one or two properties talking. Flagged rather
    # than hidden, so a thin split reads as thin instead of as a finding.
    is_thin: bool = False


class ComparablesResponse(BaseModel):
    subject_id: int
    suburb: str | None
    matched_using: dict  # tells the UI which filters produced the comps
    median_sale_price: float | None
    comps: list[ComparableSale]
    # What these sales actually did against their council valuation — the local
    # context the whole valuation method rests on. Positive = area sells ABOVE CV.
    median_sale_vs_cv: float | None = None      # 0.043 = sold 4.3% over CV
    mean_sale_vs_cv: float | None = None        # same, averaged rather than ranked
    mean_sale_price: float | None = None
    comps_with_cv: int = 0
    sale_vs_cv_low: float | None = None         # 25th percentile
    sale_vs_cv_high: float | None = None        # 75th percentile
    subject_ask_vs_cv: float | None = None      # where this listing sits
    median_days_on_market: float | None = None
    mean_days_on_market: float | None = None
    comps_with_dom: int = 0
    # How these comps were sold. Method moves the sale/CV ratio materially —
    # across the whole sold set auctions clear at 1.000x CV against 0.957x for
    # private treaty — so an auction-heavy comp set reads high for a buyer who
    # intends to negotiate.
    method_mix: list[MethodShare] = []
    # The same split across every sold record in the suburb, not just the
    # handful of comps. Auction runs ~3.9 points dearer than private treaty in
    # 75% of Auckland suburbs, so this is the negotiating-position number.
    suburb_method_mix: list[MethodShare] = []
    # The spread between the two methods in this suburb, and what the subject is
    # worth under each. Live listings carry no method, so rather than guess we
    # price both ways and let the buyer see the negotiating range.
    method_gap_pts: float | None = None       # 0.04 = auction clears 4 points dearer
    value_if_auction: float | None = None
    value_if_negotiation: float | None = None
    method_gap_is_thin: bool = True


_METHOD_LABELS = {"A": "Auction", "P": "Price by Negotiation", "T": "Tender"}


def _method_label(raw: str | None) -> str:
    return _METHOD_LABELS.get((raw or "").split("-")[0].strip().upper(), "Unknown")


def _method_mix_for_suburb(db: Session, sold_batch: int | None, suburb: str | None):
    """Sale method vs CV across every sold record in the suburb.

    Separate from the comp-level mix because a four-comp set can't support a
    per-method median — the suburb can.
    """
    if not sold_batch or not suburb:
        return []
    rows = db.query(PropertySold.sale_method, PropertySold.sale_price,
                    PropertySold.cv_numeric).filter(
        PropertySold.import_batch_id == sold_batch,
        PropertySold.suburb == suburb,
        PropertySold.sale_price.isnot(None),
        PropertySold.cv_numeric > 0,
    ).all()
    groups: dict[str, list[float]] = {}
    for method, price, cv in rows:
        ratio = price / cv
        if not (0.3 <= ratio <= 3.0):     # broken council CVs
            continue
        groups.setdefault(_method_label(method), []).append(ratio)

    out = []
    for label, ratios in groups.items():
        if label == "Unknown":
            continue                       # nothing actionable in an unlabelled sale
        ratios.sort()
        med = ratios[len(ratios) // 2] if ratios else None
        out.append(MethodShare(method=label, count=len(ratios),
                               median_vs_cv=(med - 1) if med else None,
                               is_thin=len(ratios) < 8))
    out.sort(key=lambda m: m.median_vs_cv if m.median_vs_cv is not None else 9)
    return out


@router.get("/{property_id}/comparables", response_model=ComparablesResponse)
def property_comparables(property_id: int, db: Session = Depends(get_db)) -> ComparablesResponse:
    """The actual sold properties that backed this listing's market_value estimate.

    Runs the same suburb + beds±1 + baths±1 + floor±25% filter as the pricing model,
    against the currently active sold batch. Cascades to broader filters if too few.
    """
    p = db.get(PropertyForSale, property_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")

    sold_batch = _active_batch(db, "sold", p.region or "Auckland")
    if not sold_batch:
        return ComparablesResponse(
            subject_id=property_id, suburb=p.suburb,
            matched_using={"error": "no_active_sold_batch"},
            median_sale_price=None, comps=[],
        )

    # Comparable sales should be recent. The sold-records dataset includes
    # historic sales going back 10+ years, but a 2016 comp tells a buyer
    # nothing about today's market. Client wants 2026, 2025 worst case —
    # we accept 2024+ to leave a small buffer for sparse suburbs.
    # The varchar sold_date is M/D/YYYY format from the scraper.
    _RECENT_YEARS = ("/2024", "/2025", "/2026")
    base_all = (
        db.query(PropertySold)
        .filter(PropertySold.import_batch_id == sold_batch)
        .filter(PropertySold.suburb == p.suburb)
        .filter(PropertySold.sale_price.isnot(None))
        .filter(PropertySold.sale_price >= 50_000)
        .filter(or_(*[PropertySold.sold_date.like(f"%{y}") for y in _RECENT_YEARS]))
    )

    def _tiers(base):
        # Tier 1 — a true like-for-like: SAME beds, SAME baths, and within 25% on
        # both floor and land. Beds/baths are exact because a 3-bed and a 4-bed
        # are different products, not a tolerance apart; size is a band because
        # no two houses match to the m2.
        mu = {"suburb": p.suburb, "beds": "exact", "baths": "exact",
              "floor_pct": 0.25, "land_pct": 0.25}
        q = base
        if p.beds is not None:
            q = q.filter(PropertySold.beds == p.beds)
        if p.baths is not None:
            q = q.filter(PropertySold.baths == p.baths)
        if p.floor_area_m2 and p.floor_area_m2 > 0:
            q = q.filter(PropertySold.floor_area_m2.between(
                p.floor_area_m2 * 0.75, p.floor_area_m2 * 1.25))
        if p.land_area_m2 and p.land_area_m2 > 0:
            q = q.filter(PropertySold.land_area_m2.between(
                p.land_area_m2 * 0.75, p.land_area_m2 * 1.25))
        rows = q.order_by(desc(PropertySold.sold_date)).limit(12).all()

        # Tier 2 — keep beds/baths exact, widen size to 50%. Size is the softer
        # constraint; room count is not.
        if len(rows) < 3:
            mu = {"suburb": p.suburb, "beds": "exact", "baths": "exact",
                  "floor_pct": 0.5, "land_pct": 0.5, "fallback": "wider_size"}
            q = base
            if p.beds is not None:
                q = q.filter(PropertySold.beds == p.beds)
            if p.baths is not None:
                q = q.filter(PropertySold.baths == p.baths)
            if p.floor_area_m2 and p.floor_area_m2 > 0:
                q = q.filter(PropertySold.floor_area_m2.between(
                    p.floor_area_m2 * 0.5, p.floor_area_m2 * 1.5))
            if p.land_area_m2 and p.land_area_m2 > 0:
                q = q.filter(PropertySold.land_area_m2.between(
                    p.land_area_m2 * 0.5, p.land_area_m2 * 1.5))
            rows = q.order_by(desc(PropertySold.sold_date)).limit(12).all()

        # Tier 3 — same beds only. Anything looser stops being a comparable.
        if len(rows) < 3:
            mu = {"suburb": p.suburb, "beds": "exact", "fallback": "beds_only"}
            q = base
            if p.beds is not None:
                q = q.filter(PropertySold.beds == p.beds)
            rows = q.order_by(desc(PropertySold.sold_date)).limit(12).all()

        # Tier 4 — suburb only, clearly flagged as not like-for-like.
        if len(rows) < 3:
            mu = {"suburb": p.suburb, "fallback": "suburb_wide"}
            rows = base.order_by(desc(PropertySold.sold_date)).limit(12).all()
        return rows, mu

    # Prefer comps of the SAME title type as the subject (freehold vs cross-lease etc.) —
    # they can't be blended. Fall back to mixed titles only if same-title is too thin.
    subj_bucket = _title_bucket(p.type_of_title)
    title_codes = _BUCKET_CODES.get(subj_bucket)
    if title_codes:
        comps, matched_using = _tiers(base_all.filter(PropertySold.type_of_title.in_(title_codes)))
        if len(comps) >= 3:
            matched_using["title"] = _TITLE_LABEL.get(subj_bucket)
        else:
            comps, matched_using = _tiers(base_all)
            matched_using["title"] = "mixed"
    else:
        comps, matched_using = _tiers(base_all)

    def shape(c: PropertySold) -> ComparableSale:
        beds_delta = (c.beds - p.beds) if (c.beds is not None and p.beds is not None) else None
        floor_pct = None
        if c.floor_area_m2 and p.floor_area_m2 and p.floor_area_m2 > 0:
            floor_pct = round((c.floor_area_m2 / p.floor_area_m2 - 1), 3)
        return ComparableSale(
            id=c.id, address=c.address, suburb=c.suburb, property_type=c.property_type,
            beds=c.beds, baths=c.baths, floor_area_m2=c.floor_area_m2, land_area_m2=c.land_area_m2,
            sale_price=c.sale_price, sold_date=c.sold_date, sale_method=c.sale_method,
            url=c.url, image_url=c.image_url, cv_numeric=c.cv_numeric,
            type_of_title=_title_label(c.type_of_title),
            beds_delta=beds_delta, floor_pct_delta=floor_pct,
        )

    prices = [c.sale_price for c in comps if c.sale_price]
    median = float(sorted(prices)[len(prices) // 2]) if prices else None

    # --- local context: what these comparable sales did against their CV ---
    # This is the basis of the whole valuation method, so it belongs on screen
    # rather than buried in the model.
    ratios = sorted(
        c.sale_price / c.cv_numeric
        for c in comps
        if c.sale_price and c.cv_numeric and c.cv_numeric > 0
        and 0.3 <= c.sale_price / c.cv_numeric <= 3.0   # drop broken council CVs
    )

    def _q(vals, frac):
        if not vals:
            return None
        i = max(0, min(len(vals) - 1, int(round(frac * (len(vals) - 1)))))
        return vals[i]

    med_vs_cv = _q(ratios, 0.5)
    lo_vs_cv = _q(ratios, 0.25)
    hi_vs_cv = _q(ratios, 0.75)
    # Mean alongside the median: the median says what the typical sale did, the
    # mean is pulled by the outliers. A gap between them is the signal that one
    # or two sales are dragging the area's ratio around.
    mean_vs_cv = (sum(ratios) / len(ratios)) if ratios else None
    sale_prices = [c.sale_price for c in comps if c.sale_price]
    mean_price = (sum(sale_prices) / len(sale_prices)) if sale_prices else None
    doms = sorted(c.days_on_market for c in comps
                  if c.days_on_market and c.days_on_market > 0)
    mean_dom = (sum(doms) / len(doms)) if doms else None

    # Group the comps by how they sold, with each group's own sale/CV median.
    _LABELS = {"A": "Auction", "P": "Price by Negotiation", "T": "Tender"}
    by_method: dict[str, list[float]] = {}
    for c in comps:
        code = (c.sale_method or "").split("-")[0].strip().upper()
        label = _LABELS.get(code, "Unknown")
        by_method.setdefault(label, [])
        if c.sale_price and c.cv_numeric and c.cv_numeric > 0:
            r = c.sale_price / c.cv_numeric
            if 0.3 <= r <= 3.0:
                by_method[label].append(r)
    mix = []
    for label, rs in by_method.items():
        n = sum(1 for c in comps
                if _LABELS.get((c.sale_method or "").split("-")[0].strip().upper(), "Unknown") == label)
        med = _q(sorted(rs), 0.5)
        mix.append(MethodShare(method=label, count=n,
                               median_vs_cv=(med - 1) if med else None))
    mix.sort(key=lambda m: -m.count)

    # Suburb-wide method split, and the subject priced under each method.
    smix = _method_mix_for_suburb(db, sold_batch, p.suburb)
    _by = {m.method: m for m in smix}
    auc, neg = _by.get("Auction"), _by.get("Price by Negotiation")
    gap_pts = v_auction = v_negotiation = None
    # Both sides need a real sample before a gap means anything — the 25-point
    # spreads in the data are genuine but so are the two-sale mirages.
    gap_thin = not (auc and neg and not auc.is_thin and not neg.is_thin)
    if auc and neg and auc.median_vs_cv is not None and neg.median_vs_cv is not None:
        gap_pts = auc.median_vs_cv - neg.median_vs_cv
        if p.cv_numeric and p.cv_numeric > 0:
            v_auction = round(p.cv_numeric * (1 + auc.median_vs_cv))
            v_negotiation = round(p.cv_numeric * (1 + neg.median_vs_cv))

    return ComparablesResponse(
        subject_id=property_id,
        suburb=p.suburb,
        matched_using=matched_using,
        median_sale_price=median,
        comps=[shape(c) for c in comps],
        median_sale_vs_cv=(med_vs_cv - 1) if med_vs_cv else None,
        mean_sale_vs_cv=(mean_vs_cv - 1) if mean_vs_cv else None,
        mean_sale_price=mean_price,
        comps_with_cv=len(ratios),
        sale_vs_cv_low=(lo_vs_cv - 1) if lo_vs_cv else None,
        sale_vs_cv_high=(hi_vs_cv - 1) if hi_vs_cv else None,
        subject_ask_vs_cv=((p.asking_price / p.cv_numeric) - 1)
                          if (p.asking_price and p.cv_numeric) else None,
        median_days_on_market=_q(doms, 0.5),
        mean_days_on_market=mean_dom,
        comps_with_dom=len(doms),
        method_mix=mix,
        suburb_method_mix=smix,
        method_gap_pts=gap_pts,
        value_if_auction=v_auction,
        value_if_negotiation=v_negotiation,
        method_gap_is_thin=gap_thin,
    )


class UpliftOut(BaseModel):
    label: str
    pct: float | None
    dollars: float | None
    cells: int
    scope: str
    is_thin: bool
    caveat: str | None = None
    is_association: bool = False


class ValueAddResponse(BaseModel):
    subject_id: int
    options: list[UpliftOut]


@router.get("/{property_id}/value-add", response_model=ValueAddResponse)
def property_value_add(property_id: int, db: Session = Depends(get_db)) -> ValueAddResponse:
    """What each renovation adds, measured on size-controlled sold comparisons."""
    p = db.get(PropertyForSale, property_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")
    sold_batch = _active_batch(db, "sold", p.region or "Auckland")
    if not sold_batch:
        return ValueAddResponse(subject_id=property_id, options=[])

    rows = db.query(
        PropertySold.suburb, PropertySold.district, PropertySold.property_type,
        PropertySold.beds, PropertySold.baths, PropertySold.floor_area_m2,
        PropertySold.sale_price, PropertySold.has_swimming_pool,
    ).filter(PropertySold.import_batch_id == sold_batch,
             PropertySold.sale_price.isnot(None)).all()
    sold = pd.DataFrame(rows, columns=["suburb", "district", "property_type", "beds",
                                       "baths", "floor", "price", "pool"])
    sold["pool"] = sold["pool"].fillna(False).astype(bool)
    for c in ("beds", "baths", "floor", "price"):
        sold[c] = pd.to_numeric(sold[c], errors="coerce")

    ups = value_add(
        sold, suburb=p.suburb, district=p.district, beds=p.beds, baths=p.baths,
        has_pool=bool(p.has_swimming_pool),
        value=p.fair_value or p.buy_price or p.asking_price,
    )
    return ValueAddResponse(
        subject_id=property_id,
        options=[UpliftOut(**u.__dict__) for u in ups],
    )


@router.get("/{property_id}/history", response_model=HistoryResponse)
def property_history(property_id: int, db: Session = Depends(get_db)) -> HistoryResponse:
    """Same property across all historical batches, oldest first.

    Match key is slug_id (the source-portal ID). If a property's slug_id is missing,
    we fall back to (address, suburb) match.
    """
    p = db.get(PropertyForSale, property_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")

    q = db.query(PropertyForSale).join(ImportBatch, ImportBatch.id == PropertyForSale.import_batch_id)
    if p.slug_id:
        q = q.filter(PropertyForSale.slug_id == p.slug_id)
    elif p.address and p.suburb:
        q = q.filter(PropertyForSale.address == p.address, PropertyForSale.suburb == p.suburb)
    else:
        q = q.filter(PropertyForSale.id == property_id)

    rows = q.order_by(ImportBatch.created_at.asc()).all()

    points: list[HistoryPoint] = []
    for r in rows:
        batch = db.get(ImportBatch, r.import_batch_id)
        if not batch:
            continue
        points.append(HistoryPoint(
            batch_id=batch.id,
            batch_date=batch.created_at.date().isoformat() if batch.created_at else "",
            is_active=batch.is_active,
            asking_price=r.asking_price,
            market_value=r.market_value,
            opportunity_score_pct=r.opportunity_score_pct,
            est_weekly_rent=r.est_weekly_rent,
            pred_vs_listing=r.pred_vs_listing,
        ))

    return HistoryResponse(
        slug_id=p.slug_id, address=p.address, suburb=p.suburb, points=points,
    )


# ---------- Sold list ----------
sold_router = APIRouter(prefix="/api/sold", tags=["sold"])


@sold_router.get("", response_model=SoldList)
def list_sold(
    region: str = "Auckland",
    suburb: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> SoldList:
    batch_id = _active_batch(db, "sold", region)
    if batch_id is None:
        return SoldList(total=0, page=page, page_size=page_size, rows=[])

    q = db.query(PropertySold).filter(PropertySold.import_batch_id == batch_id)
    if suburb:
        q = q.filter(PropertySold.suburb == suburb)
    if search:
        like = f"%{search}%"
        q = q.filter(or_(PropertySold.address.ilike(like), PropertySold.suburb.ilike(like)))
    total = q.count()
    rows = q.order_by(desc(PropertySold.sold_date)).offset((page - 1) * page_size).limit(page_size).all()
    return SoldList(total=total, page=page, page_size=page_size, rows=rows)
