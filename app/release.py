"""Two-stage weekly publish: stage → review → publish, with per-row holds.

Uploads land as STAGED batches (not live). This module scores the staged data,
holds the flagged rows back, summarises everything for review, and — on the
admin's confirmation — publishes the release atomically. Held rows stay hidden
until an admin fixes and publishes them individually.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import BatchType, ImportBatch, PropertyForSale

# Section/bare-land types legitimately have no floor area — never hold those for
# a missing floor. (Mirror of routers.properties._SECTION_TYPES.)
_SECTION_TYPES = ("建地", "乡村住宅建地", "土地", "地皮", "Section", "Vacant land", "Land")

# ---- publish gate -----------------------------------------------------------
# THE rule: if our figure is wildly out from the asking price, it does not go
# live. The asking price is the one independent check we have on every listing —
# a real person has priced this property, and a valuation that disagrees with
# them by multiples is not a find, it is a fault.
#
# Note this is NOT a confidence test. `confidence` is a comp COUNT
# (assumptions.confidence_tier): >=10 comps is "high", >=3 "medium". It measures
# how much data was available, not whether the answer is right. 501/125 Customs
# Street West was published at HIGH confidence and 8.5x the asking price — it had
# plenty of comps, they were simply the wrong tenure. Counting comps would never
# have caught it. Comparing against asking does, immediately.
#
# The band is 3x in each direction: held if the valuation is more than triple the
# asking price, or less than a third of it.
PUBLISH_MAX_RATIO = 3.0          # value > asking x 3    → held
PUBLISH_MIN_RATIO = 1 / 3.0      # value < asking / 3    → held

# Rows with no comps at all still produce no usable number, so they are held —
# but a thin comp count on its own no longer blocks a listing.
PUBLISH_BLOCKED_CONFIDENCE = ("insufficient",)


# ---- holding flagged rows ---------------------------------------------------
def _hold_reason(p: PropertyForSale) -> str | None:
    """Why this listing should be held from publishing, or None if it's clean."""
    if p.land_area_flag:
        return f"Land area flagged ({p.land_area_flag})"
    if p.cv_flag == "suspect":
        return ("CV looks wrong vs the local market — often a new build whose "
                "council record still covers the whole pre-subdivision site")
    if p.floor_area_m2 is None and (p.property_type not in _SECTION_TYPES):
        return "Missing floor area"

    # No number at all. The pipeline declined to value this — for leasehold with
    # too few comps, or an unanchored block — and that decision is respected.
    value = p.fair_value or p.market_value
    if value is None:
        return "No valuation produced"

    # No comps whatsoever behind the number.
    if (p.confidence or "insufficient") in PUBLISH_BLOCKED_CONFIDENCE:
        return "No comparable sales behind this valuation"

    # THE gate: our figure against the asking price. Held either way — a value
    # far above asking manufactures a fake bargain, and a value far below it
    # makes a normal listing look overpriced. Both are our error, not the
    # market's.
    if p.asking_price and p.asking_price > 0:
        ratio = value / p.asking_price
        if ratio > PUBLISH_MAX_RATIO:
            return f"Valued at {ratio:.1f}x the asking price"
        if ratio < PUBLISH_MIN_RATIO:
            return f"Valued at {ratio:.2f}x the asking price"

    return None


def hold_flagged_rows(db: Session, batch_id: int | None) -> int:
    """Mark every flagged row in a staged batch as held. Returns how many held."""
    if not batch_id:
        return 0
    held = 0
    for p in db.query(PropertyForSale).filter(PropertyForSale.import_batch_id == batch_id):
        reason = _hold_reason(p)
        if reason:
            p.is_held = True
            p.hold_reason = reason
            held += 1
        else:
            p.is_held = False
            p.hold_reason = None
    db.commit()
    return held


# ---- review summary ---------------------------------------------------------
@dataclass
class StagedSummary:
    has_staged: bool = False
    sold_batch_id: int | None = None
    forsale_batch_id: int | None = None
    sold_rows: int = 0
    forsale_rows: int = 0
    forsale_rejected: int = 0
    held_total: int = 0
    hold_reasons: dict[str, int] = field(default_factory=dict)
    pv_checked: int = 0
    pv_pending: int = 0
    uploaded_at: str | None = None


def _staged_batch(db: Session, batch_type: str, region: str) -> ImportBatch | None:
    return (db.query(ImportBatch)
            .filter(ImportBatch.batch_type == batch_type,
                    ImportBatch.region == region,
                    ImportBatch.status == "staged")
            .order_by(ImportBatch.id.desc()).first())


def staged_summary(db: Session, region: str = "Auckland") -> StagedSummary:
    sold = _staged_batch(db, BatchType.SOLD.value, region)
    fs = _staged_batch(db, BatchType.FOR_SALE.value, region)
    s = StagedSummary(
        has_staged=bool(sold or fs),
        sold_batch_id=sold.id if sold else None,
        forsale_batch_id=fs.id if fs else None,
        sold_rows=sold.rows_inserted if sold else 0,
        forsale_rejected=fs.rows_rejected if fs else 0,
        uploaded_at=(fs or sold).created_at.isoformat() if (fs or sold) and (fs or sold).created_at else None,
    )
    if fs:
        base = db.query(PropertyForSale).filter(PropertyForSale.import_batch_id == fs.id)
        s.forsale_rows = base.count()
        s.held_total = base.filter(PropertyForSale.is_held.is_(True)).count()
        rows = (db.query(PropertyForSale.hold_reason, func.count(PropertyForSale.id))
                  .filter(PropertyForSale.import_batch_id == fs.id, PropertyForSale.is_held.is_(True))
                  .group_by(PropertyForSale.hold_reason).all())
        s.hold_reasons = {r[0] or "other": r[1] for r in rows}
        s.pv_checked = base.filter(PropertyForSale.pv_checked_at.isnot(None)).count()
        s.pv_pending = s.forsale_rows - s.pv_checked
    return s


# ---- publish ----------------------------------------------------------------
def publish_release(db: Session, region: str = "Auckland") -> dict:
    """Promote the staged sold + for-sale batches to live, atomically. The old
    live batches are archived. Held rows stay held (hidden) but ride along in the
    now-live batch so they can be fixed and published later."""
    now = datetime.now(timezone.utc)
    published = []
    for bt in (BatchType.SOLD.value, BatchType.FOR_SALE.value):
        staged = _staged_batch(db, bt, region)
        if not staged:
            continue
        # Archive whatever is live for this type + region.
        for prior in (db.query(ImportBatch)
                        .filter(ImportBatch.batch_type == bt, ImportBatch.region == region,
                                ImportBatch.is_active.is_(True)).all()):
            prior.is_active = False
            prior.status = "archived"
        staged.is_active = True
        staged.status = "published"
        staged.published_at = now
        published.append({"batch_type": bt, "batch_id": staged.id})
    db.commit()
    return {"published": published, "count": len(published)}
