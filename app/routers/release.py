"""Admin endpoints for the two-stage weekly publish.

Flow: upload (stages the data) → GET /staged (review the flags) → fix any held
rows (PATCH) → POST /publish (goes live). Held rows can be published individually
once fixed (POST /listings/{id}/publish).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PropertyForSale, User
from ..release import publish_release, staged_summary
from ..security import require_admin

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


# ---- publish the release ----------------------------------------------------
@router.post("/release/publish")
def publish(region: str = "Auckland", _: User = Depends(require_admin),
            db: Session = Depends(get_db)) -> dict:
    summary = staged_summary(db, region)
    if not summary.has_staged:
        raise HTTPException(status_code=409, detail="Nothing staged to publish")
    result = publish_release(db, region)
    result["held_back"] = summary.held_total
    return result


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
