"""Dashboard aggregation endpoints — power the home + deal-finder pages."""
from __future__ import annotations

import json
import statistics
import pandas as pd
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ImportBatch, PropertyForSale, PropertySold
from ..pricing.conversion import conversion_opportunities
from ..pricing.valueadd import by_district
from .properties import _SECTION_TYPES, _hide_bad_data

router = APIRouter(prefix="/api/dashboards", tags=["dashboards"])

# Same rule as the browse list (properties._hide_bad_data), expressed as a raw-SQL
# predicate for the PERCENTILE queries: keep a listing only if it has a floor area,
# or is a bare-land/section type (which legitimately has none). Drops the ~130
# dwelling-type-with-no-floor rows so they don't skew medians, the pulse, or counts.
_CLEAN_FS_SQL = "(is_held = false AND (floor_area_m2 IS NOT NULL OR property_type IN ({})))".format(
    ", ".join("'{}'".format(t) for t in _SECTION_TYPES)
)


def _active(db: Session, batch_type: str, region: str) -> int | None:
    b = (
        db.query(ImportBatch)
        .filter(ImportBatch.batch_type == batch_type, ImportBatch.region == region, ImportBatch.is_active.is_(True))
        .order_by(ImportBatch.id.desc())
        .first()
    )
    return b.id if b else None


class TodayCounts(BaseModel):
    underpriced: int
    cashflow_positive: int
    subdividable: int
    total_for_sale: int


class TodayTopSignal(BaseModel):
    id: int
    address: str | None
    suburb: str | None
    property_type: str | None
    asking_price: float | None
    market_value: float | None
    opportunity_score_pct: float | None
    is_underpriced: bool
    is_cashflow_positive: bool
    is_subdividable: bool


class MarketPulse(BaseModel):
    total_listings: int
    median_asking: float | None
    median_predicted_dom: float | None
    listings_change: int | None  # rows_added - rows_removed vs previous batch
    median_asking_change_pct: float | None  # median % change vs previous batch (paired)


class WeekChanges(BaseModel):
    new_listings: int
    removed_listings: int
    still_on_market: int


class PriceMover(BaseModel):
    id: int | None
    slug_id: str | None
    address: str | None
    suburb: str | None
    asking_was: float | None
    asking_now: float | None
    change_pct: float | None


class TodayBrief(BaseModel):
    counts: TodayCounts
    top_signals: list[TodayTopSignal]
    market_pulse: MarketPulse | None = None
    week_changes: WeekChanges | None = None
    biggest_drops: list[PriceMover] = []
    biggest_rises: list[PriceMover] = []


def _previous_batch_id(db: Session, batch_type: str, region: str, active_batch_id: int) -> int | None:
    b = (
        db.query(ImportBatch)
        .filter(
            ImportBatch.batch_type == batch_type,
            ImportBatch.region == region,
            ImportBatch.id != active_batch_id,
        )
        .order_by(desc(ImportBatch.created_at))
        .first()
    )
    return b.id if b else None


@router.get("/today", response_model=TodayBrief)
def today_brief(
    region: str = "Auckland",
    db: Session = Depends(get_db),
) -> TodayBrief:
    import logging
    import traceback
    from sqlalchemy import text

    log = logging.getLogger(__name__)
    empty = TodayBrief(counts=TodayCounts(underpriced=0, cashflow_positive=0, subdividable=0, total_for_sale=0), top_signals=[])

    try:
        batch_id = _active(db, "for_sale", region)
    except Exception as e:
        log.error(f"today brief: active batch lookup failed: {e}\n{traceback.format_exc()}")
        return empty
    if batch_id is None:
        return empty

    try:
        base = _hide_bad_data(
            db.query(PropertyForSale).filter(PropertyForSale.import_batch_id == batch_id))
        counts = TodayCounts(
            underpriced=base.filter(PropertyForSale.is_underpriced.is_(True)).count(),
            cashflow_positive=base.filter(PropertyForSale.is_cashflow_positive.is_(True)).count(),
            subdividable=base.filter(PropertyForSale.is_subdividable.is_(True)).count(),
            total_for_sale=base.count(),
        )
    except Exception as e:
        log.error(f"today brief: counts failed: {e}\n{traceback.format_exc()}")
        counts = TodayCounts(underpriced=0, cashflow_positive=0, subdividable=0, total_for_sale=0)

    try:
        # Only rank listings that actually have a buy score. Postgres sorts NULLs
        # first on DESC, so without this gate the scoreless listings float to the
        # top of "top signals" and render as blank rows.
        top = (base
               .filter(PropertyForSale.opportunity_score_pct.isnot(None))
               .order_by(desc(PropertyForSale.opportunity_score_pct))
               .limit(10).all())
    except Exception as e:
        log.error(f"today brief: top signals failed: {e}\n{traceback.format_exc()}")
        top = []

    # Current market pulse — PERCENTILE_DISC is much faster than CONT on big datasets
    try:
        pulse_row = db.execute(text(
            """
            SELECT
                COUNT(*) AS total,
                PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY asking_price) AS median_ask,
                PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY predicted_days) AS median_dom
            FROM properties_for_sale
            WHERE import_batch_id = :b AND """ + _CLEAN_FS_SQL + """
            """
        ), {"b": batch_id}).fetchone()
    except Exception as e:
        log.warning(f"today brief: pulse query failed: {e}")
        pulse_row = None

    prev_id = _previous_batch_id(db, "for_sale", region, batch_id)
    listings_change = None
    median_change_pct = None
    week_changes = None
    drops: list[PriceMover] = []
    rises: list[PriceMover] = []

    if prev_id:
        # Diff vs previous batch — single FULL OUTER JOIN pass, PERCENTILE_DISC for speed
        try:
            diff = db.execute(text(
                """
                WITH a AS (SELECT slug_id, asking_price FROM properties_for_sale
                           WHERE import_batch_id = :prev AND slug_id IS NOT NULL),
                     b AS (SELECT slug_id, asking_price FROM properties_for_sale
                           WHERE import_batch_id = :curr AND slug_id IS NOT NULL),
                     joined AS (
                       SELECT a.slug_id AS a_slug, b.slug_id AS b_slug,
                              a.asking_price AS pa, b.asking_price AS pb
                       FROM a FULL OUTER JOIN b ON a.slug_id = b.slug_id
                     )
                SELECT
                    COUNT(*) FILTER (WHERE a_slug IS NULL AND b_slug IS NOT NULL) AS added,
                    COUNT(*) FILTER (WHERE a_slug IS NOT NULL AND b_slug IS NULL) AS removed,
                    COUNT(*) FILTER (WHERE a_slug IS NOT NULL AND b_slug IS NOT NULL) AS both,
                    PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY (pb - pa) / NULLIF(pa, 0))
                      FILTER (WHERE pa IS NOT NULL AND pb IS NOT NULL) AS median_change
                FROM joined
                """
            ), {"prev": prev_id, "curr": batch_id}).fetchone()

            if diff:
                added = int(diff[0] or 0)
                removed = int(diff[1] or 0)
                still = int(diff[2] or 0)
                week_changes = WeekChanges(new_listings=added, removed_listings=removed, still_on_market=still)
                listings_change = added - removed
                if diff[3] is not None:
                    median_change_pct = float(diff[3])
        except Exception as e:
            log.warning(f"today brief: diff query failed: {e}")

        # Biggest drops + rises in ONE query (was two passes)
        try:
            mover_rows = db.execute(text(
                """
                WITH a AS (SELECT slug_id, id, address, suburb, asking_price FROM properties_for_sale
                           WHERE import_batch_id = :prev AND slug_id IS NOT NULL AND asking_price >= 10000),
                     b AS (SELECT slug_id, id, address, suburb, asking_price FROM properties_for_sale
                           WHERE import_batch_id = :curr AND slug_id IS NOT NULL AND asking_price >= 10000),
                     d AS (
                       SELECT b.id AS id, b.slug_id, b.address, b.suburb,
                              a.asking_price AS pa, b.asking_price AS pb,
                              (b.asking_price - a.asking_price) / NULLIF(a.asking_price, 0) AS change_pct
                       FROM a JOIN b USING (slug_id)
                       WHERE a.asking_price <> b.asking_price
                     ),
                     dr AS (SELECT *, 'drop'::text AS kind FROM d
                            WHERE change_pct IS NOT NULL
                            ORDER BY change_pct ASC LIMIT 5),
                     ri AS (SELECT *, 'rise'::text AS kind FROM d
                            WHERE change_pct IS NOT NULL
                            ORDER BY change_pct DESC LIMIT 5)
                SELECT id, slug_id, address, suburb, pa, pb, change_pct, kind FROM dr
                UNION ALL
                SELECT id, slug_id, address, suburb, pa, pb, change_pct, kind FROM ri
                """
            ), {"prev": prev_id, "curr": batch_id}).fetchall()
            for r in mover_rows:
                mv = PriceMover(
                    id=r[0], slug_id=r[1], address=r[2], suburb=r[3],
                    asking_was=float(r[4]) if r[4] is not None else None,
                    asking_now=float(r[5]) if r[5] is not None else None,
                    change_pct=float(r[6]) if r[6] is not None else None,
                )
                (drops if r[7] == "drop" else rises).append(mv)
        except Exception as e:
            log.warning(f"today brief: movers query failed: {e}")

    market_pulse = MarketPulse(
        total_listings=int(pulse_row[0] or 0),
        median_asking=float(pulse_row[1]) if pulse_row and pulse_row[1] is not None else None,
        median_predicted_dom=float(pulse_row[2]) if pulse_row and pulse_row[2] is not None else None,
        listings_change=listings_change,
        median_asking_change_pct=median_change_pct,
    )

    try:
        signals = [
            TodayTopSignal(
                id=t.id, address=t.address, suburb=t.suburb, property_type=t.property_type,
                asking_price=t.asking_price,
                # "Est" — Ollie's value lives in fair_value, with market_value as a
                # fallback for premium listings the fair_value pipeline suppresses.
                market_value=t.market_value if t.market_value is not None else t.fair_value,
                opportunity_score_pct=t.opportunity_score_pct,
                is_underpriced=t.is_underpriced,
                is_cashflow_positive=t.is_cashflow_positive,
                is_subdividable=t.is_subdividable,
            )
            for t in top
        ]
    except Exception as e:
        log.error(f"today brief: signal serialise failed: {e}\n{traceback.format_exc()}")
        signals = []

    return TodayBrief(
        counts=counts,
        top_signals=signals,
        market_pulse=market_pulse,
        week_changes=week_changes,
        biggest_drops=drops,
        biggest_rises=rises,
    )


class SuburbAggregate(BaseModel):
    suburb: str
    median_asking: float | None
    median_sale: float | None
    listing_count: int


@router.get("/suburb-medians", response_model=list[SuburbAggregate])
def suburb_medians(
    region: str = "Auckland",
    limit: int = Query(30, ge=1, le=200),
    beds: int | None = Query(None, ge=0, le=10),
    baths: int | None = Query(None, ge=0, le=10),
    db: Session = Depends(get_db),
) -> list[SuburbAggregate]:
    """Median asking (for-sale) + median sale (sold) per suburb. Optionally drill
    down by bedroom/bathroom count so the medians are like-for-like — a suburb's
    all-in median blends 2-beds with 5-beds and means little; beds/baths segments
    it into a comparable population."""
    fs_id = _active(db, "for_sale", region)
    sold_id = _active(db, "sold", region)
    if fs_id is None and sold_id is None:
        return []

    # Bed/bath drill-down — applied identically to the for-sale and sold sides.
    seg = ""
    params = {"fs_id": fs_id, "sold_id": sold_id or -1, "lim": limit}
    if beds is not None:
        seg += " AND beds = :beds"; params["beds"] = beds
    if baths is not None:
        seg += " AND baths = :baths"; params["baths"] = baths
    # Each bed/bath slice is smaller, so relax the "enough listings to mean
    # something" floor when a segment filter is on.
    params["minc"] = 2 if (beds is not None or baths is not None) else 5

    rows = []
    if fs_id is not None:
        from sqlalchemy import text
        sql = text(
            """
            WITH fs AS (
                SELECT suburb,
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY asking_price) AS median_asking,
                       COUNT(*) AS listing_count
                FROM properties_for_sale
                WHERE import_batch_id = :fs_id AND asking_price >= 10000 AND suburb IS NOT NULL
                      AND """ + _CLEAN_FS_SQL + seg + """
                GROUP BY suburb
            ),
            sd AS (
                SELECT suburb,
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sale_price) AS median_sale
                FROM properties_sold
                WHERE import_batch_id = :sold_id AND sale_price IS NOT NULL AND suburb IS NOT NULL
                      """ + seg + """
                GROUP BY suburb
            )
            SELECT fs.suburb, fs.median_asking, sd.median_sale, fs.listing_count
            FROM fs LEFT JOIN sd USING (suburb)
            WHERE fs.listing_count >= :minc
            ORDER BY fs.median_asking DESC NULLS LAST
            LIMIT :lim;
            """
        )
        rows = db.execute(sql, params).fetchall()

    return [
        SuburbAggregate(
            suburb=r[0],
            median_asking=float(r[1]) if r[1] is not None else None,
            median_sale=float(r[2]) if r[2] is not None else None,
            listing_count=int(r[3]),
        )
        for r in rows
    ]


class TrendPoint(BaseModel):
    batch_id: int
    batch_date: str
    median_asking: float | None
    median_market_value: float | None
    listing_count: int


class SuburbTrend(BaseModel):
    suburb: str
    region: str
    points: list[TrendPoint]
    long_term_yearly_json: str | None = None
    long_term_monthly_json: str | None = None
    sample_property_id: int | None = None
    listing_count: int | None = None
    median_asking_current: float | None = None


_TREND_OUTLIER_MULT = 4.0   # a monthly median above 4x the suburb's own median is corrupt source data


def _clean_trend_json(raw: str | None, *, change_key: str) -> str | None:
    """Drop trend points whose median is an implausible multiple of the suburb's
    own median (corrupted source months), then recompute the change field so it
    ties to the points that remain. High side only — a genuinely cheap month is
    plausible, a median 9x higher than every neighbour is not. Returns the input
    unchanged on any problem or when nothing needs cleaning."""
    if not raw:
        return raw
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    pts = data.get("points") if isinstance(data, dict) else (data if isinstance(data, list) else None)
    if not isinstance(pts, list) or len(pts) < 4:
        return raw

    def med_of(p):
        v = p.get("median") if isinstance(p, dict) else None
        return v if isinstance(v, (int, float)) and v > 0 else None

    meds = [m for m in (med_of(p) for p in pts) if m is not None]
    if len(meds) < 4:
        return raw
    hi = statistics.median(meds) * _TREND_OUTLIER_MULT
    kept = [p for p in pts if (med_of(p) is None) or med_of(p) <= hi]
    if len(kept) == len(pts):
        return raw  # nothing corrupt

    prev = None
    for p in kept:
        cur = med_of(p)
        p[change_key] = round((cur - prev) / prev * 100, 1) if (prev and cur) else 0.0
        prev = cur if cur is not None else prev
    if isinstance(data, dict):
        data["points"] = kept
        return json.dumps(data)
    return json.dumps(kept)


@router.get("/suburb-trend", response_model=SuburbTrend)
def suburb_trend(
    suburb: str = Query(..., min_length=1),
    region: str = "Auckland",
    db: Session = Depends(get_db),
) -> SuburbTrend:
    """Two trend sources for a suburb:
      - our weekly batches (median asking + estimate per batch)
      - long-term multi-year suburb aggregates (pulled from any active for-sale listing
        in that suburb that has the trend payload populated)
    """
    from sqlalchemy import text

    # === Our weekly batch trend ===
    sql = text(
        """
        SELECT ib.id AS batch_id,
               ib.created_at::date AS batch_date,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY p.asking_price) AS median_asking,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY p.market_value) AS median_market_value,
               COUNT(*) AS listing_count
        FROM import_batches ib
        JOIN properties_for_sale p ON p.import_batch_id = ib.id
        WHERE ib.batch_type = 'for_sale' AND ib.region = :region AND p.suburb = :suburb
              AND """ + _CLEAN_FS_SQL + """
        GROUP BY ib.id, ib.created_at
        ORDER BY ib.created_at ASC;
        """
    )
    rows = db.execute(sql, {"region": region, "suburb": suburb}).fetchall()
    points = [
        TrendPoint(
            batch_id=r[0],
            batch_date=r[1].isoformat() if r[1] else "",
            median_asking=float(r[2]) if r[2] is not None else None,
            median_market_value=float(r[3]) if r[3] is not None else None,
            listing_count=int(r[4]),
        )
        for r in rows
    ]

    # The scraped trend payloads occasionally carry a corrupted month — e.g. a
    # median of $6.75M in Howick where every other month is ~$700k (a batch of bad
    # high-value rows in the source). A median can't legitimately jump like that, so
    # drop any point more than 4x off the suburb's own median (robust: a genuinely
    # expensive suburb has a high median too, so real data is never filtered).

    # === Long-term suburb trend — pulled from any active listing's trend payload ===
    active_batch_id = _active(db, "for_sale", region)
    lt_yearly = None
    lt_monthly = None
    sample_id = None
    if active_batch_id:
        sample = db.execute(text(
            """
            SELECT id, valuation_trend_yearly_json, valuation_trend_monthly_json
            FROM properties_for_sale
            WHERE import_batch_id = :b AND suburb = :s
              AND (valuation_trend_yearly_json IS NOT NULL OR valuation_trend_monthly_json IS NOT NULL)
            ORDER BY
              (valuation_trend_yearly_json IS NOT NULL)::int DESC,
              (valuation_trend_monthly_json IS NOT NULL)::int DESC
            LIMIT 1
            """
        ), {"b": active_batch_id, "s": suburb}).fetchone()
        if sample:
            sample_id = int(sample[0])
            lt_yearly = _clean_trend_json(sample[1], change_key="change_pct")
            lt_monthly = _clean_trend_json(sample[2], change_key="change")

    median_asking_current = points[-1].median_asking if points else None
    listing_count = points[-1].listing_count if points else None

    return SuburbTrend(
        suburb=suburb,
        region=region,
        points=points,
        long_term_yearly_json=lt_yearly,  # cleaned above

        long_term_monthly_json=lt_monthly,
        sample_property_id=sample_id,
        listing_count=listing_count,
        median_asking_current=median_asking_current,
    )


class BatchSummary(BaseModel):
    id: int
    batch_type: str
    region: str
    created_at: str
    is_active: bool
    rows_inserted: int


@router.get("/batches", response_model=list[BatchSummary])
def list_batches(
    region: str = "Auckland",
    batch_type: str = "for_sale",
    db: Session = Depends(get_db),
) -> list[BatchSummary]:
    rows = (
        db.query(ImportBatch)
        .filter(ImportBatch.batch_type == batch_type, ImportBatch.region == region)
        .order_by(desc(ImportBatch.created_at))
        .all()
    )
    return [
        BatchSummary(
            id=b.id, batch_type=b.batch_type, region=b.region,
            created_at=b.created_at.isoformat() if b.created_at else "",
            is_active=b.is_active, rows_inserted=b.rows_inserted,
        )
        for b in rows
    ]


class BatchCompareSummary(BaseModel):
    batch_a: int
    batch_b: int
    rows_added: int
    rows_removed: int
    rows_in_paired: int
    median_asking_change_pct: float | None
    median_market_value_change_pct: float | None
    biggest_price_drop: list[dict]  # top 10 by % asking drop
    biggest_price_rise: list[dict]


@router.get("/batches/compare", response_model=BatchCompareSummary)
def compare_batches(
    a: int = Query(..., description="Older batch id"),
    b: int = Query(..., description="Newer batch id"),
    db: Session = Depends(get_db),
) -> BatchCompareSummary:
    from sqlalchemy import text
    summary = db.execute(text(
        """
        WITH a AS (SELECT slug_id, asking_price, market_value FROM properties_for_sale
                   WHERE import_batch_id = :a AND slug_id IS NOT NULL),
             b AS (SELECT slug_id, asking_price, market_value FROM properties_for_sale
                   WHERE import_batch_id = :b AND slug_id IS NOT NULL),
             joined AS (
               SELECT a.slug_id AS a_slug, b.slug_id AS b_slug,
                      a.asking_price AS pa, b.asking_price AS pb,
                      a.market_value AS ma, b.market_value AS mb
               FROM a FULL OUTER JOIN b ON a.slug_id = b.slug_id
             )
        SELECT
            COUNT(*) FILTER (WHERE a_slug IS NULL AND b_slug IS NOT NULL) AS rows_added,
            COUNT(*) FILTER (WHERE a_slug IS NOT NULL AND b_slug IS NULL) AS rows_removed,
            COUNT(*) FILTER (WHERE a_slug IS NOT NULL AND b_slug IS NOT NULL) AS rows_in_both,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY (pb - pa) / NULLIF(pa, 0))
              FILTER (WHERE pa IS NOT NULL AND pb IS NOT NULL) AS median_asking_change,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY (mb - ma) / NULLIF(ma, 0))
              FILTER (WHERE ma IS NOT NULL AND mb IS NOT NULL) AS median_mv_change
        FROM joined
        """
    ), {"a": a, "b": b}).fetchone()

    movers = db.execute(text(
        """
        WITH a AS (SELECT slug_id, address, suburb, asking_price FROM properties_for_sale
                   WHERE import_batch_id = :a AND slug_id IS NOT NULL AND asking_price >= 10000),
             b AS (SELECT slug_id, address, suburb, asking_price FROM properties_for_sale
                   WHERE import_batch_id = :b AND slug_id IS NOT NULL AND asking_price >= 10000),
             diff AS (
               SELECT a.slug_id, a.address, a.suburb, a.asking_price AS pa, b.asking_price AS pb,
                      (b.asking_price - a.asking_price) / NULLIF(a.asking_price, 0) AS change_pct
               FROM a JOIN b USING (slug_id)
             )
        SELECT slug_id, address, suburb, pa, pb, change_pct FROM diff
        WHERE change_pct IS NOT NULL
        ORDER BY change_pct ASC LIMIT 10;
        """
    ), {"a": a, "b": b}).fetchall()
    risers = db.execute(text(
        """
        WITH a AS (SELECT slug_id, address, suburb, asking_price FROM properties_for_sale
                   WHERE import_batch_id = :a AND slug_id IS NOT NULL AND asking_price >= 10000),
             b AS (SELECT slug_id, address, suburb, asking_price FROM properties_for_sale
                   WHERE import_batch_id = :b AND slug_id IS NOT NULL AND asking_price >= 10000),
             diff AS (
               SELECT a.slug_id, a.address, a.suburb, a.asking_price AS pa, b.asking_price AS pb,
                      (b.asking_price - a.asking_price) / NULLIF(a.asking_price, 0) AS change_pct
               FROM a JOIN b USING (slug_id)
             )
        SELECT slug_id, address, suburb, pa, pb, change_pct FROM diff
        WHERE change_pct IS NOT NULL
        ORDER BY change_pct DESC LIMIT 10;
        """
    ), {"a": a, "b": b}).fetchall()

    def shape(rows):
        return [
            {"slug_id": r[0], "address": r[1], "suburb": r[2],
             "asking_a": float(r[3]) if r[3] else None,
             "asking_b": float(r[4]) if r[4] else None,
             "change_pct": float(r[5]) if r[5] is not None else None}
            for r in rows
        ]

    return BatchCompareSummary(
        batch_a=a, batch_b=b,
        rows_added=int(summary[0] or 0) if summary else 0,
        rows_removed=int(summary[1] or 0) if summary else 0,
        rows_in_paired=int(summary[2] or 0) if summary else 0,
        median_asking_change_pct=float(summary[3]) if summary and summary[3] is not None else None,
        median_market_value_change_pct=float(summary[4]) if summary and summary[4] is not None else None,
        biggest_price_drop=shape(movers),
        biggest_price_rise=shape(risers),
    )


# ---------------------------------------------------------------------------
# Market velocity — how long homes are taking to sell, over time.
# A single average hides the direction of travel: 40 days is reassuring if last
# quarter was 60 and worrying if it was 25. Plotted as date (x) vs days (y).
# ---------------------------------------------------------------------------
class DomPoint(BaseModel):
    period: str            # YYYY-MM
    median_days: float | None    # None = no sales that month → an honest gap in the line
    sales: int
    region_median_days: float | None = None   # same month, whole region
    # A median over a handful of sales is noise, not a trend. The point is still
    # returned so gaps do not silently close up, but the UI should render it
    # faintly / dashed rather than as a solid reading.
    is_thin: bool = False


class DomTrend(BaseModel):
    suburb: str | None
    months: int
    points: list[DomPoint]
    # Headline numbers for the copy above the chart.
    current_median_days: float | None = None
    prior_median_days: float | None = None     # the 3 months before the latest 3
    change_days: float | None = None           # +ve = slowing down
    region_median_days: float | None = None
    total_sales: int = 0


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    v = sorted(vals)
    n = len(v)
    return float(v[n // 2]) if n % 2 else float((v[n // 2 - 1] + v[n // 2]) / 2)


def _period(sold_date: str | None) -> str | None:
    """'5/14/2026' -> '2026-05'. The scraper stores M/D/YYYY as text."""
    if not sold_date:
        return None
    parts = str(sold_date).strip().split("/")
    if len(parts) != 3:
        return None
    try:
        m, y = int(parts[0]), int(parts[2])
    except ValueError:
        return None
    if not (1 <= m <= 12 and 1900 < y < 2100):
        return None
    return f"{y:04d}-{m:02d}"


def _shift_period(period: str, back_months: int) -> str:
    """'2026-06' shifted back 17 months -> '2025-01'. Keeps the YYYY-MM ordering."""
    y, m = int(period[:4]), int(period[5:7])
    total = y * 12 + (m - 1) - back_months
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _months_range(start: str, end: str) -> list[str]:
    """Every calendar month from start to end inclusive, as YYYY-MM. Emitting the
    full run (not just months with sales) keeps the x-axis time-proportional, so a
    year with no sales reads as a year-wide gap instead of one compressed step."""
    s = int(start[:4]) * 12 + int(start[5:7]) - 1
    e = int(end[:4]) * 12 + int(end[5:7]) - 1
    return [f"{t // 12:04d}-{t % 12 + 1:02d}" for t in range(s, e + 1)]


@router.get("/dom-trend", response_model=DomTrend)
def dom_trend(
    suburb: str | None = Query(None, description="Omit for the whole region"),
    region: str = "Auckland",
    months: int = Query(24, ge=3, le=120),
    min_sales: int = Query(15, ge=1, le=500,
                           description="Below this, a month is flagged is_thin"),
    db: Session = Depends(get_db),
) -> DomTrend:
    """Median days-to-sell per month — the market's direction of travel."""
    batch = (
        db.query(ImportBatch)
        .filter(ImportBatch.batch_type == "sold", ImportBatch.region == region,
                ImportBatch.is_active.is_(True))
        .order_by(ImportBatch.id.desc()).first()
    )
    if not batch:
        return DomTrend(suburb=suburb, months=months, points=[])

    q = (db.query(PropertySold.sold_date, PropertySold.days_on_market, PropertySold.suburb)
         .filter(PropertySold.import_batch_id == batch.id,
                 PropertySold.days_on_market.isnot(None),
                 PropertySold.days_on_market > 0,
                 PropertySold.days_on_market < 730))   # 2yr+ listings are stale data

    by_period: dict[str, list[float]] = {}
    region_by_period: dict[str, list[float]] = {}
    for sold_date, dom, sub in q.all():
        per = _period(sold_date)
        if not per:
            continue
        region_by_period.setdefault(per, []).append(float(dom))
        if suburb is None or (sub or "").strip() == suburb.strip():
            by_period.setdefault(per, []).append(float(dom))

    # Window by CALENDAR months, not by "the last N months that happen to have a
    # sale". A sparse suburb can have sales scattered across decades (Ellerslie:
    # 17 months of data spanning 1995–2026); taking the last N populated periods
    # would plot a 1995 sale next to a 2026 one as if consecutive. Anchor to the
    # dataset's most recent month and drop anything older than `months` before it.
    anchor = max(region_by_period) if region_by_period else (max(by_period) if by_period else None)
    if anchor is None:
        return DomTrend(suburb=suburb, months=months, points=[])
    cutoff = _shift_period(anchor, months - 1)   # never show older than `months` (6yr cap)
    # Auto-fit to the data: start the monthly axis at the most recent CONTIGUOUS
    # run of months that have sales, so a sparse suburb (a lone 2009 sale, then
    # nothing until 2025) shows a clean monthly line over its real span instead of
    # mostly-empty years. A dense series has no big gaps, so its run reaches all the
    # way back to the 6-year cutoff. Internal gaps of ≤3 months stay in-run (they
    # render as honest breaks in the line); a bigger gap ends the run.
    def _mi(p: str) -> int:
        return int(p[:4]) * 12 + int(p[5:7]) - 1
    pop = [p for p in sorted(by_period) if p >= cutoff and by_period.get(p)]
    start = cutoff
    if pop:
        start = pop[-1]
        for i in range(len(pop) - 1, 0, -1):
            if _mi(pop[i]) - _mi(pop[i - 1]) <= 4:   # ≤3 empty months → same run
                start = pop[i - 1]
            else:
                break
    # Continuous months across the fitted span — months with no sales are emitted
    # with median_days=None so the line breaks over them honestly.
    periods = _months_range(start, anchor)
    points = [
        DomPoint(period=p,
                 median_days=round(_median(by_period[p]), 1) if by_period.get(p) else None,
                 sales=len(by_period.get(p, [])),
                 region_median_days=(round(_median(region_by_period.get(p, [])), 1)
                                     if region_by_period.get(p) else None),
                 is_thin=0 < len(by_period.get(p, [])) < min_sales)
        for p in periods
    ]

    # Nothing to trend: fewer than two months in the window actually have sales
    # (e.g. a rural suburb whose only comps predate the 6-year cap). Return no
    # points so the UI shows "not enough recent sales" rather than an empty axis.
    if sum(1 for p in points if p.median_days is not None) < 2:
        return DomTrend(suburb=suburb, months=months, points=[])

    # Headline: the last 6 calendar months vs the 6 before. Pooling by 6 months
    # (not per-month) so a suburb selling 7–14/month clears a sound sample, while
    # keeping the window genuinely RECENT (anchored to the calendar, not reaching
    # back years to find enough sales). Each bucket must clear the sample bar on
    # its own — if the prior baseline is too thin we show the current figure but
    # NO trend arrow, rather than claiming a direction of travel we can't back.
    recent = [d for p in periods[-6:] for d in by_period.get(p, [])]
    prior = [d for p in periods[-12:-6] for d in by_period.get(p, [])]
    cur_med = _median(recent) if len(recent) >= min_sales else None
    pri_med = _median(prior) if len(prior) >= min_sales else None
    change = (round(cur_med - pri_med, 1)
              if (cur_med is not None and pri_med is not None) else None)

    return DomTrend(
        suburb=suburb, months=months, points=points,
        current_median_days=cur_med, prior_median_days=pri_med,
        change_days=change,
        region_median_days=_median([d for v in region_by_period.values() for d in v]),
        total_sales=sum(p.sales for p in points),
    )


class DistrictValueAdd(BaseModel):
    district: str
    bedroom: float | None
    bedroom_cells: int
    bathroom: float | None
    bathroom_cells: int
    pool: float | None
    pool_cells: int


@router.get("/value-add-by-district", response_model=list[DistrictValueAdd])
def value_add_by_district(region: str = "Auckland", db: Session = Depends(get_db)):
    """Size-controlled renovation uplift per district.

    Fixed comparisons (3->4 bed, 1->2 bath, pool vs none) so districts read
    against each other. Each holds the other room count constant.
    """
    batch = _active(db, "sold", region)
    if not batch:
        return []
    rows = db.query(
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
    # Districts with nothing measurable are noise on a dashboard.
    return [DistrictValueAdd(**r) for r in by_district(df) if r["bedroom"] is not None]


class HeadlineDeal(BaseModel):
    id: int
    address: str | None
    suburb: str | None
    asking_price: float | None
    fair_value: float | None
    margin: float | None
    margin_dollars: float | None
    beds: int | None
    baths: int | None
    image_url: str | None


class Headline(BaseModel):
    """The numbers worth leading a dashboard with — money, not counts."""
    gems: int
    gems_margin_total: float | None
    underpriced: int
    underpriced_margin_total: float | None
    subdividable: int
    subdivision_profit_total: float | None
    best: HeadlineDeal | None


@router.get("/headline", response_model=Headline)
def headline(region: str = "Auckland", db: Session = Depends(get_db)) -> Headline:
    batch = _active(db, "for_sale", region)
    if batch is None:
        return Headline(gems=0, gems_margin_total=None, underpriced=0,
                        underpriced_margin_total=None, subdividable=0,
                        subdivision_profit_total=None, best=None)

    P = PropertyForSale
    gap = P.fair_value - P.asking_price
    base = [P.import_batch_id == batch, P.is_underpriced.is_(True),
            P.fair_value.isnot(None), P.asking_price.isnot(None)]
    # "Gems" are the deals we would actually stand behind: margin above the
    # model's own median error, backed by enough sold comps to trust.
    gem = base + [P.margin >= 0.15, P.comps_used >= 8]

    row = (db.query(P).filter(*base).order_by(gap.desc()).first())
    best = None
    if row:
        best = HeadlineDeal(
            id=row.id, address=row.address, suburb=row.suburb,
            asking_price=row.asking_price, fair_value=row.fair_value,
            margin=row.margin,
            margin_dollars=(row.fair_value - row.asking_price),
            beds=row.beds, baths=row.baths, image_url=row.image_url,
        )

    q = lambda f, *w: db.query(f).filter(*w).scalar()
    return Headline(
        gems=q(func.count(P.id), *gem) or 0,
        gems_margin_total=q(func.sum(gap), *gem),
        underpriced=q(func.count(P.id), *base) or 0,
        underpriced_margin_total=q(func.sum(gap), *base),
        subdividable=q(func.count(P.id), P.import_batch_id == batch,
                       P.is_subdividable.is_(True)) or 0,
        subdivision_profit_total=q(func.sum(P.best_net_gain),
                                   P.import_batch_id == batch,
                                   P.is_subdividable.is_(True)),
        best=best,
    )


class ConversionOut(BaseModel):
    id: int
    address: str | None
    suburb: str | None
    district: str | None
    beds: int | None
    floor_area_m2: float | None
    typical_floor_next: float | None
    asking_price: float | None
    fair_value: float | None
    uplift_pct: float
    uplift_dollars: float
    is_underpriced: bool
    margin: float | None
    image_url: str | None


class ConversionResponse(BaseModel):
    count: int
    total_uplift: float
    median_uplift: float | None
    double_plays: int      # already underpriced AND convertible
    rows: list[ConversionOut]


def _sold_frame(db: Session, batch: int) -> pd.DataFrame:
    rows = db.query(
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


@router.get("/conversion", response_model=ConversionResponse)
def conversion(region: str = "Auckland", limit: int = Query(60, ge=1, le=400),
               db: Session = Depends(get_db)) -> ConversionResponse:
    """Listings that already hold the floor area for another bedroom."""
    sold_b = _active(db, "sold", region)
    live_b = _active(db, "for_sale", region)
    if not sold_b or not live_b:
        return ConversionResponse(count=0, total_uplift=0, median_uplift=None,
                                  double_plays=0, rows=[])

    P = PropertyForSale
    live_rows = db.query(
        P.id, P.address, P.suburb, P.district, P.beds, P.floor_area_m2,
        P.asking_price, P.fair_value, P.margin, P.is_underpriced, P.image_url,
    ).filter(P.import_batch_id == live_b,
             P.floor_area_m2.isnot(None), P.beds.isnot(None),
             P.fair_value.isnot(None)).all()
    live = pd.DataFrame(live_rows, columns=[
        "id", "address", "suburb", "district", "beds", "floor_area_m2",
        "asking_price", "fair_value", "margin", "is_underpriced", "image_url"])
    for c in ("beds", "floor_area_m2", "asking_price", "fair_value", "margin"):
        live[c] = pd.to_numeric(live[c], errors="coerce")

    found = conversion_opportunities(_sold_frame(db, sold_b), live)
    ups = sorted(c.uplift_dollars for c in found)
    return ConversionResponse(
        count=len(found),
        total_uplift=float(sum(ups)),
        median_uplift=(ups[len(ups) // 2] if ups else None),
        double_plays=sum(1 for c in found if c.is_underpriced),
        rows=[ConversionOut(**c.__dict__) for c in found[:limit]],
    )
