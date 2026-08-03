"""Wish lists — a user's saved searches over the live listings.

Each wish list is a set of criteria (area, price range, beds, type, and a
"developments under $X" option = subdividable sites under a buy price). Matched
against the active for-sale batch. "New" matches are listings that appeared, or
whose price dropped into range, since the batch the user last viewed — that drives
the in-app notification badge. Everything is per-user and auth-scoped.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ImportBatch, PropertyForSale, User, WishList
from ..pricing.glm import canonical_type
from ..security import require_active

router = APIRouter(prefix="/api/wishlists", tags=["wishlists"])

_CATEGORY_MAP: dict[str, set[str]] = {
    "house": {"House", "Residence", "Home and Income"},
    "townhouse": {"Townhouse"},
    "apartment": {"Apartment"},
    "unit": {"Unit"},
    "section": {"Section"},
    "lifestyle": {"Lifestyle Property", "Lifestyle Section"},
}
MAX_MATCHES = 60


# ---------- schemas ----------
class WishListIn(BaseModel):
    name: str
    district: str | None = None
    suburb: str | None = None
    property_category: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    min_beds: int | None = None
    underpriced_only: bool = False
    subdividable_only: bool = False
    max_dev_buy_price: float | None = None


class WishListOut(WishListIn):
    id: int
    match_count: int
    new_count: int


class MatchRow(BaseModel):
    id: int
    address: str | None
    suburb: str | None
    district: str | None
    beds: int | None
    baths: int | None
    asking_price: float | None
    fair_value: float | None
    buy_price: float | None
    margin: float | None
    is_underpriced: bool
    is_subdividable: bool
    best_net_gain: float | None
    max_addl_lots: float | None
    days_on_market: float | None
    image_url: str | None
    is_new: bool = False


# ---------- helpers ----------
def _active_batch(db: Session, region: str = "Auckland") -> int | None:
    return (db.query(ImportBatch.id)
            .filter(ImportBatch.batch_type == "for_sale", ImportBatch.region == region,
                    ImportBatch.is_active.is_(True))
            .order_by(ImportBatch.id.desc()).limit(1).scalar())


def _prev_batch(db: Session, active_id: int, region: str = "Auckland") -> int | None:
    return (db.query(ImportBatch.id)
            .filter(ImportBatch.batch_type == "for_sale", ImportBatch.region == region,
                    ImportBatch.id < active_id)
            .order_by(ImportBatch.id.desc()).limit(1).scalar())


def _match_query(db: Session, wl: WishList, batch_id: int):
    from .properties import _hide_bad_data
    P = PropertyForSale
    q = _hide_bad_data(db.query(P).filter(P.import_batch_id == batch_id))
    if wl.district:
        q = q.filter(P.district == wl.district)
    if wl.suburb:
        q = q.filter(P.suburb.ilike(f"%{wl.suburb}%"))
    if wl.property_category:
        wanted = _CATEGORY_MAP.get(wl.property_category.strip().lower())
        if wanted:
            raw = [t for (t,) in db.query(P.property_type)
                   .filter(P.import_batch_id == batch_id).distinct() if t]
            matching = [t for t in raw if canonical_type(t) in wanted]
            q = q.filter(P.property_type.in_(matching or ["__none__"]))
    if wl.min_price is not None:
        q = q.filter(P.asking_price >= wl.min_price)
    if wl.max_price is not None:
        q = q.filter(P.asking_price <= wl.max_price)
    if wl.min_beds is not None:
        q = q.filter(P.beds.isnot(None), P.beds >= wl.min_beds)
    if wl.underpriced_only:
        q = q.filter(P.is_underpriced.is_(True))
    if wl.subdividable_only:
        q = q.filter(P.is_subdividable.is_(True))
    if wl.max_dev_buy_price is not None:
        q = q.filter(P.is_subdividable.is_(True), P.buy_price.isnot(None),
                     P.buy_price <= wl.max_dev_buy_price)
    return q


def _new_slugs(db: Session, wl: WishList, active_id: int) -> set[str]:
    """Slugs of current matches that are new since the reference batch — either a
    brand-new listing, or one whose asking price dropped into the wish list's range."""
    ref = wl.last_seen_batch_id or _prev_batch(db, active_id)
    if not ref or ref >= active_id:
        return set()
    cur = _match_query(db, wl, active_id).with_entities(
        PropertyForSale.slug_id, PropertyForSale.asking_price).all()
    prev = dict(db.query(PropertyForSale.slug_id, PropertyForSale.asking_price)
                .filter(PropertyForSale.import_batch_id == ref).all())
    out: set[str] = set()
    for slug, ask in cur:
        if not slug:
            continue
        if slug not in prev:
            out.add(slug)                                   # new listing appeared
        elif (wl.max_price is not None and prev[slug] is not None and ask is not None
              and prev[slug] > wl.max_price and ask <= wl.max_price):
            out.add(slug)                                   # price dropped into range
    return out


def _to_out(db: Session, wl: WishList, active_id: int | None) -> WishListOut:
    mc = _match_query(db, wl, active_id).count() if active_id else 0
    nc = len(_new_slugs(db, wl, active_id)) if active_id else 0
    return WishListOut(
        id=wl.id, name=wl.name, district=wl.district, suburb=wl.suburb,
        property_category=wl.property_category, min_price=wl.min_price, max_price=wl.max_price,
        min_beds=wl.min_beds, underpriced_only=wl.underpriced_only,
        subdividable_only=wl.subdividable_only, max_dev_buy_price=wl.max_dev_buy_price,
        match_count=mc, new_count=nc,
    )


# ---------- endpoints ----------
@router.get("", response_model=list[WishListOut])
def list_wishlists(me: User = Depends(require_active), db: Session = Depends(get_db)):
    active = _active_batch(db)
    rows = db.query(WishList).filter(WishList.user_id == me.id).order_by(WishList.id.desc()).all()
    return [_to_out(db, w, active) for w in rows]


@router.post("", response_model=WishListOut)
def create_wishlist(body: WishListIn, me: User = Depends(require_active), db: Session = Depends(get_db)):
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="A name is required.")
    wl = WishList(user_id=me.id, **body.model_dump())
    wl.name = wl.name.strip()
    # last_seen left NULL so the first view shows this week's new matches vs the prior batch.
    db.add(wl); db.commit(); db.refresh(wl)
    return _to_out(db, wl, _active_batch(db))


@router.delete("/{wishlist_id}", status_code=204)
def delete_wishlist(wishlist_id: int, me: User = Depends(require_active), db: Session = Depends(get_db)):
    wl = db.query(WishList).filter(WishList.id == wishlist_id, WishList.user_id == me.id).first()
    if not wl:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(wl); db.commit()


@router.get("/notifications")
def notifications(me: User = Depends(require_active), db: Session = Depends(get_db)):
    """Total new matches across all of the user's wish lists — the badge count."""
    active = _active_batch(db)
    if not active:
        return {"total_new": 0}
    total = sum(len(_new_slugs(db, w, active))
                for w in db.query(WishList).filter(WishList.user_id == me.id).all())
    return {"total_new": total}


@router.get("/{wishlist_id}/matches", response_model=list[MatchRow])
def wishlist_matches(wishlist_id: int, me: User = Depends(require_active), db: Session = Depends(get_db)):
    wl = db.query(WishList).filter(WishList.id == wishlist_id, WishList.user_id == me.id).first()
    if not wl:
        raise HTTPException(status_code=404, detail="Not found")
    active = _active_batch(db)
    if not active:
        return []
    new_slugs = _new_slugs(db, wl, active)
    rows = (_match_query(db, wl, active)
            .order_by(PropertyForSale.margin.desc().nullslast())
            .limit(MAX_MATCHES).all())
    return [MatchRow(
        id=r.id, address=r.address, suburb=r.suburb, district=r.district,
        beds=r.beds, baths=r.baths, asking_price=r.asking_price, fair_value=r.fair_value,
        buy_price=r.buy_price, margin=r.margin, is_underpriced=r.is_underpriced,
        is_subdividable=r.is_subdividable, best_net_gain=r.best_net_gain,
        max_addl_lots=r.max_addl_lots, days_on_market=r.days_on_market,
        image_url=r.image_url, is_new=(r.slug_id in new_slugs),
    ) for r in rows]


@router.post("/{wishlist_id}/seen", status_code=204)
def mark_seen(wishlist_id: int, me: User = Depends(require_active), db: Session = Depends(get_db)):
    """Clear the 'new' badge for a wish list by pinning it to the active batch."""
    wl = db.query(WishList).filter(WishList.id == wishlist_id, WishList.user_id == me.id).first()
    if not wl:
        raise HTTPException(status_code=404, detail="Not found")
    wl.last_seen_batch_id = _active_batch(db)
    db.commit()
