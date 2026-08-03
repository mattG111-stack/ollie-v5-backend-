"""Admin endpoints for the two-stage weekly publish.

Flow: upload (stages the data) → GET /staged (review the flags) → fix any held
rows (PATCH) → POST /publish (goes live). Held rows can be published individually
once fixed (POST /listings/{id}/publish).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    BatchType,
    ImportBatch,
    IngestJob,
    PropertyForSale,
    PropertyRent,
    PropertySold,
    User,
)
from ..release import publish_release, staged_summary
from ..security import require_admin
from ..staged_stages import (
    _staged_forsale_batch,
    create_stage_job,
    run_enrich_job,
    run_price_job,
    stage_running,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---- review the staged release ----------------------------------------------
class StagedOut(BaseModel):
    has_staged: bool
    sold_batch_id: int | None
    forsale_batch_id: int | None
    sold_rows: int
    forsale_rows: int
    forsale_rejected: int
    held_total: int
    hold_reasons: dict[str, int]
    pv_checked: int
    pv_pending: int
    uploaded_at: str | None


@router.get("/release/staged", response_model=StagedOut)
def get_staged(region: str = "Auckland", _: User = Depends(require_admin),
               db: Session = Depends(get_db)) -> StagedOut:
    return StagedOut(**staged_summary(db, region).__dict__)


# ---- staged review grid ------------------------------------------------------
# The row grid beneath the filter chips: every figure needed to inspect a batch
# before publish. The four profit figures are DISTINCT columns and must not be
# collapsed (see StagedGridRow docstring).
class StagedGridRow(BaseModel):
    """One staged listing, with the four distinct deal figures spelled out:

      valuation             — what it's worth as-is (fair_value, anchor-guarded).
      margin_dollars / _pct — fair_value − asking: a straight underpriced-house buy.
      subdivision_profit     — what you clear after developing the lots ($).
      subdivision_profit_pct — that profit as a return on total development cost.

    valuation is NOT market_value (that is asking × 0.95): it is the anchor-guarded
    fair_value, so a broken CV can't surface a fake number. `buy_price` (what you
    can pay) is a separate figure from `valuation` (what it's worth) — two labels,
    never one, so the grid never reads as an instruction to overpay.
    """
    id: int
    address: str | None
    suburb: str | None
    property_type: str | None
    asking_price: float | None
    cv_numeric: float | None
    valuation: float | None            # fair_value (anchor-guarded)
    buy_price: float | None            # what you can pay (≤ asking); distinct from valuation
    vs_cv_pct: float | None            # valuation / CV − 1 (the data-quality sort)
    margin_dollars: float | None       # valuation − asking
    margin_pct: float | None           # margin
    subdivision_profit: float | None   # $ cleared after developing
    subdivision_profit_pct: float | None  # return on total development cost
    gross_realisation: float | None    # gross_sales — the lots' sale total
    development_cost: float | None      # services_cost — the inputs behind the profit
    lots: float | None                 # max_addl_lots
    buy_score: float | None            # opportunity_score_pct
    last_sold_price: float | None      # CoreLogic's last sale, else the scraper's
    last_sold_date: str | None
    floor_area_m2: float | None
    land_area_m2: float | None
    comps_used: int | None
    confidence: str | None
    is_subdividable: bool | None
    best_strategy: str | None
    is_held: bool | None
    hold_reason: str | None
    pv_checked: bool


class StagedGrid(BaseModel):
    batch_id: int | None
    total: int              # rows in the staged batch
    filtered: int           # rows matching the active chip
    counts: dict[str, int]  # per-chip counts, for the chip labels
    rows: list[StagedGridRow]


_GRID_FILTERS = ("all", "held", "unpriced", "not_enriched", "corelogic_missed")


def _needs_enrich(p: PropertyForSale) -> bool:
    """A pricing-critical field (floor / land / CV) is still blank."""
    return (p.floor_area_m2 is None or p.floor_area_m2 == 0
            or p.land_area_m2 is None or p.land_area_m2 == 0
            or p.cv_numeric is None or p.cv_numeric == 0)


def _grid_row(p: PropertyForSale) -> StagedGridRow:
    val = p.fair_value
    asking = p.asking_price
    # Margin $ only means something when the pricing engine endorsed a margin.
    # It withholds the margin (p.margin is None) when the asking isn't a real
    # price — a by-negotiation placeholder (asking == CV to the dollar), a guide/
    # "offers over" lure, a stale listing, or a premium home. Showing val − asking
    # anyway prints an absurd figure (6 Cassino Terrace: a $2.16M "margin" off a
    # $500k placeholder asking). Tie the dollar figure to the endorsed margin so
    # the two columns can't disagree.
    margin_dollars = ((val - asking)
                      if (val is not None and asking is not None and p.margin is not None)
                      else None)
    vs_cv = ((val / p.cv_numeric) - 1) if (val and p.cv_numeric) else None
    # Subdivision profit % = profit as a return on TOTAL development cost. Total
    # cost = everything spent = gross realisation − profit, so the ratio needs no
    # extra inputs. Guards against a non-positive cost base.
    sp, gr = p.subdivision_profit, p.gross_sales
    sub_pct = None
    if sp is not None and gr is not None:
        cost = gr - sp
        if cost > 0:
            sub_pct = sp / cost
    return StagedGridRow(
        id=p.id, address=p.address, suburb=p.suburb, property_type=p.property_type,
        asking_price=asking, cv_numeric=p.cv_numeric,
        valuation=val, buy_price=p.buy_price,
        vs_cv_pct=vs_cv, margin_dollars=margin_dollars, margin_pct=p.margin,
        subdivision_profit=sp, subdivision_profit_pct=sub_pct,
        gross_realisation=gr, development_cost=p.services_cost,
        lots=p.max_addl_lots, buy_score=p.opportunity_score_pct,
        # Last sold: CoreLogic's record (captured during enrich) when we have it,
        # otherwise the scraper's own last-sold from the CSV.
        last_sold_price=(p.pv_last_sale_price if p.pv_last_sale_price is not None
                         else p.valuation_last_sold_value),
        last_sold_date=(p.pv_last_sale_date or p.valuation_last_sold_date),
        floor_area_m2=p.floor_area_m2, land_area_m2=p.land_area_m2,
        comps_used=p.comps_used, confidence=p.confidence,
        is_subdividable=p.is_subdividable, best_strategy=p.best_strategy,
        is_held=p.is_held, hold_reason=p.hold_reason,
        pv_checked=p.pv_checked_at is not None,
    )


@router.get("/release/rows", response_model=StagedGrid)
def staged_rows(region: str = "Auckland", filter: str = "all",
                limit: int = 20000, _: User = Depends(require_admin),
                db: Session = Depends(get_db)) -> StagedGrid:
    """Every staged for-sale row for the review grid, filtered by the active chip.

    The frontend sorts these client-side (four distinct profit columns, default
    margin descending) and exports the filtered+sorted set to CSV — so a batch can
    be inspected, and checked in Excel, before it ever goes live."""
    if filter not in _GRID_FILTERS:
        raise HTTPException(status_code=400, detail=f"filter must be one of {_GRID_FILTERS}")
    batch = _staged_forsale_batch(db, region)
    if batch is None:
        return StagedGrid(batch_id=None, total=0, filtered=0,
                          counts={k: 0 for k in _GRID_FILTERS}, rows=[])
    recs = (db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch.id)
            .order_by(PropertyForSale.id).all())

    def _match(p: PropertyForSale, f: str) -> bool:
        if f == "all":
            return True
        if f == "held":
            return bool(p.is_held)
        if f == "unpriced":
            return p.fair_value is None
        if f == "not_enriched":
            return p.pv_checked_at is None and _needs_enrich(p)
        if f == "corelogic_missed":
            return p.pv_checked_at is not None and _needs_enrich(p)
        return True

    counts = {f: sum(1 for p in recs if _match(p, f)) for f in _GRID_FILTERS}
    selected = [p for p in recs if _match(p, filter)][:limit]
    return StagedGrid(
        batch_id=batch.id, total=len(recs), filtered=counts[filter],
        counts=counts, rows=[_grid_row(p) for p in selected],
    )


class HeldRow(BaseModel):
    id: int
    address: str | None
    suburb: str | None
    property_type: str | None
    hold_reason: str | None
    beds: int | None
    baths: int | None
    floor_area_m2: float | None
    land_area_m2: float | None
    cv_numeric: float | None
    zoning: str | None
    asking_price: float | None
    # CoreLogic's values, to fix against
    pv_cv: float | None
    pv_estimate_mid: float | None

    class Config:
        from_attributes = True


@router.get("/release/held", response_model=list[HeldRow])
def list_held(region: str = "Auckland", batch_id: int | None = None,
              _: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[PropertyForSale]:
    """Every held listing in the staged (or a given) batch, for the review list."""
    q = db.query(PropertyForSale).filter(PropertyForSale.is_held.is_(True))
    if batch_id:
        q = q.filter(PropertyForSale.import_batch_id == batch_id)
    return q.order_by(PropertyForSale.hold_reason, PropertyForSale.id).limit(1000).all()


# ---- operator-triggered stages: ENRICH + PRICE ------------------------------
# Each stage runs on a background thread with its own DB session, so the request
# returns at once and /health keeps answering while the (long) stage runs. Poll
# GET /api/admin/jobs/{job_id} for durable progress. Both are re-runnable.
class StageStarted(BaseModel):
    job_id: int
    batch_id: int
    stage: str


@router.post("/release/enrich", response_model=StageStarted)
def enrich_staged(region: str = "Auckland", admin: User = Depends(require_admin),
                  db: Session = Depends(get_db)) -> StageStarted:
    """Stage 2 — fill blank floor / land / CV on the staged for-sale batch from
    CoreLogic. Re-runnable: a re-run only looks up rows that are STILL missing a
    pricing-critical field, so a stage that died at 60% resumes from there."""
    batch = _staged_forsale_batch(db, region)
    if batch is None:
        raise HTTPException(status_code=409, detail="No staged for-sale batch to enrich")
    # One enrich at a time per batch: a second heavy worker stacked on the first is
    # the main way this OOM-killed the container. Return the in-flight job instead.
    if stage_running(db, batch.id, "enrich"):
        raise HTTPException(status_code=409, detail="Enrich is already running for this batch")
    job = create_stage_job(db, stage="enrich", batch_id=batch.id, region=region,
                           uploaded_by_id=admin.id)
    bid, jid = batch.id, job.id
    threading.Thread(target=run_enrich_job, args=(jid, bid, region), daemon=True).start()
    return StageStarted(job_id=jid, batch_id=bid, stage="enrich")


@router.post("/release/price", response_model=StageStarted)
def price_staged(region: str = "Auckland", admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)) -> StageStarted:
    """Stage 3 — re-run the pricing pipeline over the staged batch using its
    current stored attributes (i.e. after enrich). Re-runnable, so a fix to the
    pricing code re-values the batch without a re-upload."""
    batch = _staged_forsale_batch(db, region)
    if batch is None:
        raise HTTPException(status_code=409, detail="No staged for-sale batch to price")
    if stage_running(db, batch.id, "price"):
        raise HTTPException(status_code=409, detail="Re-price is already running for this batch")
    job = create_stage_job(db, stage="price", batch_id=batch.id, region=region,
                           uploaded_by_id=admin.id)
    bid, jid = batch.id, job.id
    threading.Thread(target=run_price_job, args=(jid, bid, region), daemon=True).start()
    return StageStarted(job_id=jid, batch_id=bid, stage="price")


# ---- publish the release ----------------------------------------------------
@router.post("/release/publish")
def publish(region: str = "Auckland", admin: User = Depends(require_admin),
            db: Session = Depends(get_db)) -> dict:
    summary = staged_summary(db, region)
    if not summary.has_staged:
        raise HTTPException(status_code=409, detail="Nothing staged to publish")
    fs_batch_id = summary.forsale_batch_id
    result = publish_release(db, region)
    result["held_back"] = summary.held_total
    # Record the publish result in its OWN json column — never serialised into the
    # short `stage` label, which is varchar(64) and would truncate (the original
    # StringDataRightTruncation bug).
    job = IngestJob(
        batch_type=BatchType.FOR_SALE.value,
        filename=f"publish (batch {fs_batch_id})" if fs_batch_id else "publish",
        status="completed",
        stage="publish",
        progress_pct=100,
        batch_id=fs_batch_id,
        uploaded_by_id=admin.id,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        result_json=json.dumps(result),
    )
    db.add(job)
    db.commit()
    return result


# ---- reset: wipe all batches + listings back to zero -------------------------
class ResetResult(BaseModel):
    for_sale_deleted: int
    sold_deleted: int
    rent_deleted: int
    batches_deleted: int
    jobs_deleted: int


@router.post("/reset-all", response_model=ResetResult)
def reset_all(confirm: str = "", _: User = Depends(require_admin),
              db: Session = Depends(get_db)) -> ResetResult:
    """Delete every batch, listing and ingest job — a clean slate for re-upload.
    Users, billing and settings are untouched. Requires ?confirm=RESET so it can't
    fire by accident. This is the button behind a messy import history: it takes
    the batch numbering and every stat back to zero."""
    if confirm != "RESET":
        raise HTTPException(status_code=400,
                            detail="Pass confirm=RESET to wipe all batches and listings")
    fs = db.query(PropertyForSale).delete(synchronize_session=False)
    so = db.query(PropertySold).delete(synchronize_session=False)
    re = db.query(PropertyRent).delete(synchronize_session=False)
    jo = db.query(IngestJob).delete(synchronize_session=False)
    ba = db.query(ImportBatch).delete(synchronize_session=False)
    db.commit()
    return ResetResult(for_sale_deleted=fs, sold_deleted=so, rent_deleted=re,
                       batches_deleted=ba, jobs_deleted=jo)


# ---- fix + publish individual held rows -------------------------------------
class ListingPatch(BaseModel):
    beds: int | None = None
    baths: int | None = None
    floor_area_m2: float | None = None
    land_area_m2: float | None = None
    cv_numeric: float | None = None
    zoning: str | None = None
    asking_price: float | None = None


@router.patch("/listings/{listing_id}", response_model=HeldRow)
def edit_listing(listing_id: int, body: ListingPatch, _: User = Depends(require_admin),
                 db: Session = Depends(get_db)) -> PropertyForSale:
    """Fix data-quality fields on a listing (typically a held row before publish)."""
    p = db.get(PropertyForSale, listing_id)
    if not p:
        raise HTTPException(status_code=404, detail="Listing not found")
    for field, val in body.model_dump(exclude_unset=True).items():
        setattr(p, field, val)
    db.commit(); db.refresh(p)
    return p


@router.post("/listings/{listing_id}/publish", response_model=HeldRow)
def publish_listing(listing_id: int, _: User = Depends(require_admin),
                    db: Session = Depends(get_db)) -> PropertyForSale:
    """Release a held listing to the live site (clear the hold)."""
    p = db.get(PropertyForSale, listing_id)
    if not p:
        raise HTTPException(status_code=404, detail="Listing not found")
    p.is_held = False
    p.hold_reason = None
    db.commit(); db.refresh(p)
    return p


@router.post("/listings/{listing_id}/hold", response_model=HeldRow)
def hold_listing(listing_id: int, reason: str = "Held by admin",
                 _: User = Depends(require_admin), db: Session = Depends(get_db)) -> PropertyForSale:
    """Manually hold a listing back from the live site."""
    p = db.get(PropertyForSale, listing_id)
    if not p:
        raise HTTPException(status_code=404, detail="Listing not found")
    p.is_held = True
    p.hold_reason = reason
    db.commit(); db.refresh(p)
    return p
