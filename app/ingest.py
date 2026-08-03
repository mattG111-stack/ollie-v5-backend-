"""CSV ingestion service.

Takes raw scraper CSVs (for-sale, sold, rent), runs the pricing pipeline,
writes results to the DB under a new import_batch_id, archives the prior batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .models import (
    BatchType,
    ImportBatch,
    IngestJob,
    PropertyForSale,
    PropertyRent,
    PropertySold,
)
from .pricing import assumptions as A
from .pricing import zones as Z
from .pricing.cashflow import RentRates
from .pricing.comps import SoldDataset
from .pricing.pipeline import run as run_pipeline


@dataclass
class IngestResult:
    batch_type: str
    batch_id: int
    rows_inserted: int
    rows_rejected: int
    notes: str = ""
    audit_warnings_json: str | None = None  # set by for-sale audit; surfaced on admin UI


def _parse_area(v):
    if pd.isna(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower().replace(",", "")
    digits = ""
    for c in s:
        if c.isdigit() or c == ".":
            digits += c
        elif digits:
            break
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def _to_int(v):
    try:
        f = float(v)
        if f != f:
            return None
        return int(f)
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _to_str(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    return str(v).strip() or None


def _archive_prior(db: Session, batch_type: str, region: str) -> None:
    prior = (
        db.query(ImportBatch)
        .filter(ImportBatch.batch_type == batch_type, ImportBatch.region == region, ImportBatch.is_active.is_(True))
        .all()
    )
    for p in prior:
        p.is_active = False
        p.status = "archived"


def _dedupe_by_slug(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop duplicate slug_id rows within a single upload, keeping the LAST occurrence
    (the assumption being later rows in the CSV are newer/more up-to-date scrapes).

    Returns the de-duped frame plus a count of rows dropped.
    """
    if "slug_id" not in df.columns:
        return df, 0
    before = len(df)
    df = df.dropna(subset=["slug_id"]).drop_duplicates(subset=["slug_id"], keep="last")
    # also keep any rows that had no slug_id at all (they can't collide with anything)
    no_slug = df[df["slug_id"].isna()] if "slug_id" in df.columns else df.iloc[0:0]
    deduped = pd.concat([df, no_slug], ignore_index=True) if len(no_slug) else df
    return deduped, max(0, before - len(deduped))


def _prune_old_batches(db: Session, batch_type: str, region: str, *, keep_last: int) -> int:
    """Delete batches beyond the retention window (oldest first). Returns count pruned.
    Cascades to all property rows tagged with those batch_ids via ON DELETE CASCADE."""
    if keep_last <= 0:
        return 0
    keep_ids = [
        row.id
        for row in db.query(ImportBatch.id)
        .filter(ImportBatch.batch_type == batch_type, ImportBatch.region == region)
        .order_by(ImportBatch.created_at.desc())
        .limit(keep_last)
        .all()
    ]
    if not keep_ids:
        return 0
    to_delete = (
        db.query(ImportBatch)
        .filter(ImportBatch.batch_type == batch_type, ImportBatch.region == region, ~ImportBatch.id.in_(keep_ids))
        .all()
    )
    n = len(to_delete)
    # Need to manually remove rows from child tables because we don't have ON DELETE CASCADE on the FK.
    for b in to_delete:
        if batch_type == BatchType.FOR_SALE.value:
            db.query(PropertyForSale).filter(PropertyForSale.import_batch_id == b.id).delete(synchronize_session=False)
        elif batch_type == BatchType.SOLD.value:
            db.query(PropertySold).filter(PropertySold.import_batch_id == b.id).delete(synchronize_session=False)
        elif batch_type == BatchType.RENT.value:
            db.query(PropertyRent).filter(PropertyRent.import_batch_id == b.id).delete(synchronize_session=False)
        # An ingest_job may reference this batch (FK with no cascade) — detach it
        # first so the batch delete doesn't violate the foreign key.
        db.query(IngestJob).filter(IngestJob.batch_id == b.id).update(
            {IngestJob.batch_id: None}, synchronize_session=False
        )
        db.delete(b)
    return n


def _detect_listing_type(price_display: str | None, price_numeric) -> str:
    """Classify a listing's sale method from the scraped price_display text.

    Returns one of: fixed | auction | tender | negotiation | unknown

    Per the v4 spec pseudocode — "if ask is a real number AND
    listing_type != AUCTION → asking × 0.95" — ANY listing with a real
    asking number that isn't an auction is eligible for the asking path.
    A "$829,000 Negotiable" or "Enquiries over $640,000" listing carries a
    genuine asking price; the qualifier word does not make it unusable.
    Only genuine auctions (where the displayed number is usually RV/CV, not
    an asking price) and listings with no number at all fall back to v3.5.
    """
    s = (str(price_display).strip().lower() if price_display else "")
    try:
        has_price = price_numeric is not None and float(price_numeric or 0) > 0
    except (TypeError, ValueError):
        has_price = False

    # Auctions never carry a usable asking price (the shown figure is RV/CV).
    if "auction" in s:
        return "auction"
    # Real number present + not an auction → usable asking (the primary path).
    if has_price:
        return "fixed"
    # No usable number — classify the price-on-application variants.
    if "tender" in s or "deadline" in s:
        return "tender"
    if "negotia" in s or "by neg" in s:
        return "negotiation"
    return "unknown"


def _to_bool(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "y", "t"):
        return True
    if s in ("false", "0", "no", "n", "f"):
        return False
    return None


def _collect_images(row: dict) -> str | None:
    """image_1_url .. image_30_url -> one newline-separated string.

    Stops at the first gap: the scrape numbers them contiguously, so a missing
    slot marks the end of the gallery rather than a hole in it.
    """
    urls = []
    for i in range(1, 31):
        u = _to_str(row.get(f"image_{i}_url"))
        if not u:
            break
        urls.append(u)
    return "\n".join(urls) if urls else None


def _common_property_payload(row: dict, *, region: str = "Auckland") -> dict:
    return {
        "address": _to_str(row.get("address")),
        "name": _to_str(row.get("name")),
        "suburb": _to_str(row.get("suburb")),
        "district": _to_str(row.get("district")),
        "region": _to_str(row.get("region")) or region,
        "postcode": _to_str(row.get("postcode")),
        "latitude": _to_float(row.get("latitude")),
        "longitude": _to_float(row.get("longitude")),
        "property_type": _to_str(row.get("property_type")),
        "type_of_title": _to_str(row.get("type_of_title")),
        # Repair known-mislabelled scrape zonings (e.g. large Waiuku lifestyle
        # blocks tagged "Mixed Housing Suburban") so the stored zone matches what
        # the subdivision engine now uses. See zones.corrected_zoning.
        "zoning": Z.corrected_zoning(
            _to_str(row.get("zoning")), suburb=_to_str(row.get("suburb")),
            land_area=_parse_area(row.get("key_land_area")), address=_to_str(row.get("address"))),
        "land_slope_contour": _to_str(row.get("land_slope_contour")),
        "beds": _to_int(row.get("key_bedrooms")),
        "baths": _to_int(row.get("key_bathrooms")),
        "cars": _to_int(row.get("key_carspaces")),
        "floor_area_m2": _parse_area(row.get("key_floor_area")),
        "land_area_m2": _parse_area(row.get("key_land_area")),
        "cv_numeric": _to_float(row.get("cv_numeric") or row.get("property_valuation_cv_numeric")),
        "land_value_numeric": _to_float(row.get("land_value_numeric")),
        "improvement_value_numeric": _to_float(row.get("improvement_value_numeric")),
        "url": _to_str(row.get("url")),
        "slug_id": _to_str(row.get("slug_id")),
        "image_url": _to_str(row.get("image_1_url")),
        "image_urls": _collect_images(row),
        "image_count": _to_int(row.get("image_count")),
        "listing_date": _to_str(row.get("listing_date")),
        "days_on_market": _to_float(row.get("days_on_market")),
        # Raw scraper fields preserved
        "key_facts": _to_str(row.get("key_facts")),
        "key_time_on_market": _to_str(row.get("key_time_on_market")),
        "estate_description": _to_str(row.get("estate_description")),
        "council_valuation_summary": _to_str(row.get("council_valuation_summary")),
        "property_trend": _to_str(row.get("property_trend")),
        "sale_status": _to_str(row.get("sale_status")),
        "last_updated": _to_str(row.get("last_updated")),
        # Third-party reference valuation from the source portal
        "third_party_valuation": _to_float(row.get("property_valuation_numeric")),
        "third_party_valuation_high": _to_float(row.get("property_valuation_high_numeric")),
        "third_party_valuation_low": _to_float(row.get("property_valuation_low_numeric")),
        "valuation_last_date": _to_str(row.get("valuation_last_date")),
        # CV change
        "valuation_rateable_change_pct": _to_float(row.get("valuation_rateable_change_pct")),
        "valuation_land_change_pct": _to_float(row.get("valuation_land_change_pct")),
        "valuation_improvement_change_pct": _to_float(row.get("valuation_improvement_change_pct")),
        # Last sale of this property
        "valuation_last_sold_value": _to_float(row.get("valuation_last_sold_value")),
        "valuation_last_sold_date": _to_str(row.get("valuation_last_sold_date")),
        "sold_listing_date": _to_str(row.get("sold_listing_date")),
        "sold_listing_price_label": _to_str(row.get("sold_listing_price_label")),
        # Trend JSONs for charts
        "valuation_trend_yearly_json": _to_str(row.get("valuation_trend_yearly_json")),
        "valuation_trend_monthly_json": _to_str(row.get("valuation_trend_monthly_json")),
        "sale_history_json": _to_str(row.get("sale_history_json")),
        "cv_history_json": _to_str(row.get("cv_history_json")),
        "schools_json": _to_str(row.get("schools_json")),
        # Agent contacts
        "agent1_name": _to_str(row.get("agent1_name")),
        "agent1_phone": _to_str(row.get("agent1_phone")),
        "agent1_email": _to_str(row.get("agent1_email")),
        "agent1_job_title": _to_str(row.get("agent1_job_title")),
        "agent1_company_name": _to_str(row.get("agent1_company_name")),
        "agent2_name": _to_str(row.get("agent2_name")),
        "agent2_phone": _to_str(row.get("agent2_phone")),
        "agent2_email": _to_str(row.get("agent2_email")),
        "agent2_job_title": _to_str(row.get("agent2_job_title")),
        "agent2_company_name": _to_str(row.get("agent2_company_name")),
        "company_name": _to_str(row.get("company_name")),
        # Other potentially-present scraper fields
        "building_age": _to_str(row.get("building_age")),
        "parking_covered": _to_int(row.get("parking_covered")),
        "parking_other": _to_int(row.get("parking_other")),
        "has_swimming_pool": _to_bool(row.get("has_swimming_pool")),
        "is_new_construction": _to_bool(row.get("is_new_construction")),
        "is_coastal_waterfront": _to_bool(row.get("is_coastal_waterfront")),
        "storey_count": _to_int(row.get("storey_count")),
        "other_features": _to_str(row.get("other_features")),
        "description": _to_str(row.get("description")),
        "listing_title": _to_str(row.get("listing_title")),
        "listing_published_date": _to_str(row.get("listing_published_date")),
    }


# ---- Sold ingestion ----
def ingest_sold(db: Session, sold_df: pd.DataFrame, filename: str, *, region: str = "Auckland", uploaded_by_id: int | None = None, publish: bool = True) -> IngestResult:
    sold_df, dropped = _dedupe_by_slug(sold_df)
    if publish:
        _archive_prior(db, BatchType.SOLD.value, region)
    batch = ImportBatch(
        batch_type=BatchType.SOLD.value, region=region, filename=filename,
        rows_total=len(sold_df), is_active=publish,
        status="published" if publish else "staged",
        published_at=(func.now() if publish else None),
        uploaded_by_id=uploaded_by_id,
        note=(f"deduped {dropped} duplicate slug_ids" if dropped else None),
    )
    db.add(batch); db.flush()

    inserted = 0; rejected = 0
    for _, row in sold_df.iterrows():
        payload = _common_property_payload(row.to_dict(), region=region)
        sale_price = _to_float(row.get("price_numeric"))
        # A sold row is useless for comp matching without suburb + price.
        # Also drop bedless/floorless rows — they can't be matched against anyway.
        if not payload.get("suburb") or not sale_price or sale_price < 10000:
            rejected += 1; continue
        if payload.get("beds") is None and payload.get("floor_area_m2") is None:
            rejected += 1; continue
        rec = PropertySold(
            **payload,
            import_batch_id=batch.id,
            sale_price=sale_price,
            sold_date=_to_str(row.get("sold_listing_date")),
            sale_method=_to_str(row.get("sale_method")),
        )
        db.add(rec); inserted += 1
        if inserted % 500 == 0:
            db.flush()

    batch.rows_inserted = inserted; batch.rows_rejected = rejected
    pruned = _prune_old_batches(db, BatchType.SOLD.value, region, keep_last=settings.batch_retention_limit)
    if pruned:
        batch.note = (batch.note or "") + f" · pruned {pruned} old batches"
    db.commit()
    return IngestResult(BatchType.SOLD.value, batch.id, inserted, rejected, batch.note or "")


# ---- Rent ingestion ----
def ingest_rent(db: Session, rent_df: pd.DataFrame, filename: str, *, region: str = "Auckland", uploaded_by_id: int | None = None) -> IngestResult:
    rent_df, dropped = _dedupe_by_slug(rent_df)
    _archive_prior(db, BatchType.RENT.value, region)
    batch = ImportBatch(
        batch_type=BatchType.RENT.value, region=region, filename=filename,
        rows_total=len(rent_df), is_active=True, status="published",
        published_at=func.now(), uploaded_by_id=uploaded_by_id,
        note=(f"deduped {dropped} duplicate slug_ids" if dropped else None),
    )
    db.add(batch); db.flush()

    inserted = 0; rejected = 0
    for _, row in rent_df.iterrows():
        payload = _common_property_payload(row.to_dict(), region=region)
        weekly_rent = _to_float(row.get("price_numeric"))
        # A rental row is useless without suburb + rent + at least one of beds/floor.
        if not payload.get("suburb") or not weekly_rent or weekly_rent < 100 or weekly_rent > 10000:
            rejected += 1; continue
        if payload.get("beds") is None and payload.get("floor_area_m2") is None:
            rejected += 1; continue
        rec = PropertyRent(
            **payload,
            import_batch_id=batch.id,
            weekly_rent=weekly_rent,
            listing_date_rent=_to_str(row.get("listing_date")),
        )
        db.add(rec); inserted += 1
        if inserted % 500 == 0:
            db.flush()

    batch.rows_inserted = inserted; batch.rows_rejected = rejected
    pruned = _prune_old_batches(db, BatchType.RENT.value, region, keep_last=settings.batch_retention_limit)
    if pruned:
        batch.note = (batch.note or "") + f" · pruned {pruned} old batches"
    db.commit()
    return IngestResult(BatchType.RENT.value, batch.id, inserted, rejected, batch.note or "")


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and v != v) or v in ("", 0, "0", "nan")


def _needs_lookup(row) -> bool:
    """A row needs a CoreLogic lookup when a pricing-critical field is blank.

    Floor and land drive the size checks; the CV is the primary pricing anchor
    (`ollie_value = cv_v × min(ratio, RATIO_CAP)`). A row with floor and land but
    a blank CV used to be skipped even though its valuation had no anchor — so a
    blank CV now triggers a lookup as well."""
    return (_blank(row.get("key_floor_area"))
            or _blank(row.get("key_land_area"))
            or _blank(row.get("cv_numeric")))


def _fill_df_from_corelogic(df: pd.DataFrame, *, delay: float = 0.5, cap: int = 20000,
                            progress_cb=None) -> tuple[int, int]:
    """Overlay CoreLogic (propertyvalue.co.nz) data onto the pipeline INPUT for
    listings missing a floor area, land area or CV — the pricing-critical fields —
    BEFORE the pipeline runs, so the valuation is computed on the filled numbers.

    Only looks up rows that are actually missing one of those fields (a bounded
    subset). Throttled, circuit-breaks if CoreLogic starts refusing, and degrades
    to a no-op if it's unreachable (the row just prices on what it has, as before).

    The cap is a runaway backstop, not a work limit: it is set comfortably above a
    full weekly batch (~14k rows) so enrich finishes in one pass. Resumability —
    re-running only fills what is still blank — bounds the real work now, and the
    40-consecutive-miss circuit breaker still guards against a rate-limit storm.

    `progress_cb(looked, need, filled, misses)` is called periodically so a caller
    can persist durable progress (rows processed / filled / missed) to the DB while
    the stage runs, instead of the state living only in this process's stdout.

    Returns (cells_filled, rows_looked_up)."""
    import time
    from .propertyvalue import pv_lookup

    pairs = (("key_floor_area", "floor_area_m2"), ("key_land_area", "land_area_m2"),
             ("key_bedrooms", "beds"), ("key_bathrooms", "baths"),
             ("cv_numeric", "cv"), ("zoning", "zoning"))
    filled = looked = consec_fail = misses = 0

    # How many rows will actually need a lookup, so the log shows progress against
    # a real denominator rather than an unknown. Counted up front — it is a cheap
    # pass over the frame and makes "1400/4820" legible in the deploy log.
    need = sum(
        1 for _, r in df.iterrows()
        if _needs_lookup(r) and not _blank(r.get("address"))
    )
    target = min(need, cap)
    t0 = time.time()
    print(f"  [ingest] CoreLogic fill starting: {need} rows need a lookup, "
          f"cap={cap}, will attempt {target} at {delay}s each "
          f"(~{target * delay / 60:.0f} min minimum)", flush=True)

    for idx, row in df.iterrows():
        if looked >= cap:
            print(f"  [ingest] CoreLogic fill hit the cap of {cap} lookups", flush=True)
            break
        # Only spend a lookup when a pricing-critical field (floor, land or CV) is
        # missing — see _needs_lookup.
        if not _needs_lookup(row):
            continue
        addr = row.get("address")
        if _blank(addr):
            continue
        q = ", ".join(x for x in (str(addr), str(row.get("suburb") or "").strip(), "Auckland")
                      if x and x.lower() != "nan")
        looked += 1
        try:
            pv = pv_lookup(q)
        except Exception:
            pv = None
        if not pv:
            misses += 1
            consec_fail += 1
            if progress_cb is not None:
                try:
                    progress_cb(looked, need, filled, misses)
                except Exception:
                    pass
            if consec_fail >= 40:
                print(f"  [ingest] CoreLogic fill stopped after {consec_fail} misses "
                      f"(likely rate-limited) at lookup {looked}/{target}", flush=True)
                break
            time.sleep(delay)
            continue
        consec_fail = 0
        for dfcol, pvkey in pairs:
            if _blank(row.get(dfcol)) and pv.get(pvkey):
                df.at[idx, dfcol] = pv.get(pvkey)
                filled += 1

        # Heartbeat. Without this the loop is silent for up to 45 minutes and a
        # crash is indistinguishable from still-running. Every 25 lookups we
        # print position, hit rate and elapsed time, so the deploy log shows
        # exactly how far it got before it stopped.
        if looked % 25 == 0:
            elapsed = time.time() - t0
            rate = looked / elapsed if elapsed > 0 else 0
            remaining = (target - looked) / rate / 60 if rate > 0 else 0
            print(f"  [ingest] CoreLogic {looked}/{target} lookups, "
                  f"{filled} cells filled, {elapsed / 60:.1f} min elapsed, "
                  f"~{remaining:.0f} min left", flush=True)
            if progress_cb is not None:
                try:
                    progress_cb(looked, need, filled, misses)
                except Exception:
                    pass

        time.sleep(delay)

    elapsed = time.time() - t0
    print(f"  [ingest] CoreLogic fill FINISHED: {looked} lookups, "
          f"{filled} cells filled, {elapsed / 60:.1f} min total", flush=True)
    return filled, looked


# ---- For-sale ingestion (runs the full pricing pipeline) ----
def ingest_for_sale(
    db: Session,
    for_sale_df: pd.DataFrame,
    sold_df: pd.DataFrame,
    filename: str,
    *,
    region: str = "Auckland",
    uploaded_by_id: int | None = None,
    publish: bool = True,
    fill_missing: bool = False,
) -> IngestResult:
    for_sale_df, dropped = _dedupe_by_slug(for_sale_df)
    # Fill blank sizes from CoreLogic BEFORE pricing so the valuation uses them.
    if fill_missing:
        cells, looked = _fill_df_from_corelogic(for_sale_df)
        print(f"  [ingest] CoreLogic pre-fill: {cells} cells filled across {looked} listings")
    if publish:
        _archive_prior(db, BatchType.FOR_SALE.value, region)
    batch = ImportBatch(
        batch_type=BatchType.FOR_SALE.value, region=region, filename=filename,
        rows_total=len(for_sale_df), is_active=publish,
        status="published" if publish else "staged",
        published_at=(func.now() if publish else None),
        uploaded_by_id=uploaded_by_id,
        note=(f"deduped {dropped} duplicate slug_ids" if dropped else None),
    )
    db.add(batch); db.flush()

    # Rental comps from the active rent batch, if one has been uploaded. Without
    # it the yield falls back to the flat CV tier and cashflow cannot vary
    # independently of price.
    rent_rows = (
        db.query(PropertyRent)
        .join(ImportBatch, ImportBatch.id == PropertyRent.import_batch_id)
        .filter(ImportBatch.batch_type == BatchType.RENT.value,
                ImportBatch.region == region, ImportBatch.is_active.is_(True))
        .all()
    )
    rent_rates = None
    if rent_rows:
        rent_rates = RentRates(pd.DataFrame([{
            "weekly_rent": x.weekly_rent, "suburb": x.suburb, "district": x.district,
            "property_type": x.property_type, "beds": x.beds,
        } for x in rent_rows]))
        print(f"  [ingest] rental comps: {len(rent_rows)} active rentals")
    else:
        print("  [ingest] no active rent batch - yields fall back to the CV tier")

    print(f"  [ingest] running pricing pipeline on {len(for_sale_df)} rows ...")
    sold = SoldDataset(sold_df)
    enriched = run_pipeline(for_sale_df, sold, rent_rates)

    print(f"  [ingest] writing {len(enriched)} rows to DB ...")
    inserted = 0; rejected = 0
    rejected_reasons: dict[str, int] = {}
    for _, row in enriched.iterrows():
        payload = _common_property_payload(row.to_dict(), region=region)
        asking = _to_float(row.get("price_numeric"))

        # Fix 3 — drop non-Auckland rows. Our sold dataset is Auckland-only,
        # so comp-matching against listings in Christchurch/Wellington gives garbage.
        if payload.get("region") and payload["region"] != region:
            rejected += 1
            rejected_reasons["non_target_region"] = rejected_reasons.get("non_target_region", 0) + 1
            continue
        # No suburb -> can't comp-match
        if not payload.get("suburb"):
            rejected += 1
            rejected_reasons["no_suburb"] = rejected_reasons.get("no_suburb", 0) + 1
            continue
        # No council valuation -> no valuation is possible. Every method we have
        # is anchored on the CV, and the CV-free fallback (type + beds + baths +
        # land/floor within 20%) reaches only a fifth of them and scores 11.5%.
        # 20.6% of the raw feed has no CV; carrying them as listings we can never
        # price adds nothing, so they are dropped at the door.
        if not payload.get("cv_numeric"):
            rejected += 1
            rejected_reasons["no_cv"] = rejected_reasons.get("no_cv", 0) + 1
            continue

        # Incomplete council record: the CV is the land value alone, with no
        # improvement value, on a property that has a building. 26 Sandpiper
        # Avenue asks $4,000,000 against a $14,000 "CV" that is only the dirt.
        # Everything downstream inherits that error, and a listing whose own
        # published figures are that broken is not one to trade off.
        # NB a bare section legitimately has no improvement value — CV == land
        # value is CORRECT for vacant land, and those are valued off the suburb's
        # bare-section $/m2 rate. Only a record with a BUILDING and no improvement
        # value is broken (626 of the 1,267 matches were vacant land).
        _lv = _to_float(row.get("land_value_numeric"))
        _iv = _to_float(row.get("improvement_value_numeric"))
        _cv = payload.get("cv_numeric")
        _is_vacant = A.is_vacant_type(row.get("property_type"))
        # NOT dropped: an incomplete council record still has a real building in a
        # real suburb, and comparable buildings are selling there. The pipeline
        # values these from matched sold prices instead (see matched_sold_price).

        # Asking price and CV wildly apart. Beyond this one of the two published
        # figures is simply wrong and there is no way to tell which — a $4M ask
        # against a $92k CV is not a bargain, it is a broken record. Between 20%
        # and 50% we keep the listing but stop trusting the asking price (see
        # ASK_VS_CV_BAND in pipeline.py); past 50% we drop it entirely.
        if asking and _cv and abs(float(asking) - float(_cv)) > 0.50 * float(_cv):
            rejected += 1
            rejected_reasons["asking_vs_cv_50pct"] = rejected_reasons.get("asking_vs_cv_50pct", 0) + 1
            continue

        # Fix 1 — placeholder asking ($1, $2) from "by negotiation" listings.
        # These cause "underpriced by 1,500,000×" outliers.
        if asking is not None and asking < 10_000:
            rejected += 1
            rejected_reasons["placeholder_asking"] = rejected_reasons.get("placeholder_asking", 0) + 1
            continue
        # Reject totally empty rows — no beds, no floor, no CV, no asking
        if (payload.get("beds") is None and payload.get("floor_area_m2") is None
                and payload.get("cv_numeric") is None and asking is None):
            rejected += 1
            rejected_reasons["empty_row"] = rejected_reasons.get("empty_row", 0) + 1
            continue
        # Incomplete dwelling record: a building with no floor area can't be
        # size-valued and renders as a blank/unrankable row downstream. Bare land
        # legitimately has no floor (valued off the suburb's $/m2), so only
        # non-vacant types are rejected here. This is the ~130 that used to slip
        # through and skew the lists and market insights.
        if payload.get("floor_area_m2") is None and not _is_vacant:
            rejected += 1
            rejected_reasons["dwelling_no_floor"] = rejected_reasons.get("dwelling_no_floor", 0) + 1
            continue
        rec = PropertyForSale(
            **payload,
            import_batch_id=batch.id,
            asking_price=_to_float(row.get("price_numeric")),
            market_value=_to_float(row.get("market_value")),
            predicted_list=_to_float(row.get("predicted_list")),
            predicted_days=_to_float(row.get("predicted_days")),
            comps_used=_to_int(row.get("comps_used")),
            confidence=_to_str(row.get("confidence")),
            pred_vs_cv=_to_float(row.get("pred_vs_cv")),
            pred_vs_listing=_to_float(row.get("pred_vs_listing")),
            # v3.8 diagnostic fields
            pred_v35=_to_float(row.get("pred_v35")),
            pred_v38=_to_float(row.get("pred_v38")),
            z_weight=_to_float(row.get("z_weight")),
            beta_tier=_to_str(row.get("beta_tier")),
            cv_anchor=_to_float(row.get("cv_anchor")),
            cv_ratio_tier=_to_str(row.get("cv_ratio_tier")),
            correction_used=_to_str(row.get("correction_used")),
            # v4 production AVM
            listing_type=_to_str(row.get("listing_type")),
            pricing_path=_to_str(row.get("pricing_path")),
            range_low=_to_float(row.get("range_low")),
            range_high=_to_float(row.get("range_high")),
            subdivision_premium=_to_float(row.get("subdivision_premium")),
            fair_value=_to_float(row.get("fair_value")),
            margin=_to_float(row.get("margin")),
            is_premium=bool(row.get("is_premium", False)),
            buy_price=_to_float(row.get("buy_price")),
            area_value=_to_float(row.get("area_value")),
            comp_tier=_to_int(row.get("comp_tier")),
            comps_matched=_to_int(row.get("comps_matched")),
            min_lot_m2=_to_float(row.get("min_lot_m2")),
            max_addl_lots=_to_float(row.get("max_addl_lots")),
            sections=_to_int(row.get("sections")),
            dwellings=_to_int(row.get("dwellings")),
            section_rate=_to_float(row.get("section_rate")),
            gross_sales=_to_float(row.get("gross_sales")),
            subdivision_profit=_to_float(row.get("subdivision_profit")),
            section_price_per_m2=_to_float(row.get("section_price_per_m2")),
            section_value_method=_to_str(row.get("section_value_method")),
            services_cost=_to_float(row.get("services_cost")),
            total_subdivided_value=_to_float(row.get("total_subdivided_value")),
            uplift_vs_asking=_to_float(row.get("uplift_vs_asking")),
            est_weekly_rent=_to_float(row.get("est_weekly_rent")),
            est_gross_yield=_to_float(row.get("est_gross_yield")),
            annual_gross_rent=_to_float(row.get("annual_gross_rent")),
            annual_net_rent=_to_float(row.get("annual_net_rent")),
            annual_mortgage=_to_float(row.get("annual_mortgage")),
            annual_cashflow=_to_float(row.get("annual_cashflow")),
            cash_on_cash=_to_float(row.get("cash_on_cash")),
            breakeven_deposit_pct=_to_float(row.get("breakeven_deposit_pct")),
            expected_sale=_to_float(row.get("expected_sale")),
            expected_sale_path=_to_str(row.get("expected_sale_path")),
            expected_sale_band=_to_float(row.get("expected_sale_band")),
            opportunity_score=_to_float(row.get("opportunity_score")),
            opportunity_score_pct=_to_float(row.get("opportunity_score_pct")),
            best_strategy=_to_str(row.get("best_strategy")),
            best_net_gain=_to_float(row.get("best_net_gain")),
            is_underpriced=bool(row.get("is_underpriced", False)),
            is_cashflow_positive=bool(row.get("is_cashflow_positive", False)),
            is_subdividable=bool(row.get("is_subdividable", False)),
        )
        db.add(rec); inserted += 1
        if inserted % 500 == 0:
            db.flush()

    batch.rows_inserted = inserted; batch.rows_rejected = rejected
    pruned = _prune_old_batches(db, BatchType.FOR_SALE.value, region, keep_last=settings.batch_retention_limit)
    extra_note_parts = []
    if pruned:
        extra_note_parts.append(f"pruned {pruned} old batches")
    if rejected_reasons:
        extra_note_parts.append("rejected: " + ", ".join(f"{k}={v}" for k, v in rejected_reasons.items()))
    if extra_note_parts:
        batch.note = (batch.note or "") + " · " + " · ".join(extra_note_parts)
    db.commit()

    # Post-ingest audit — runs against the rows we actually INSERTED, not the
    # raw input. Including rejected rows (non-Auckland, no-CV, placeholders)
    # double-counts as "insufficient" and gives misleading high-severity flags
    # for data we never showed to the user.
    from app.pricing.audit import audit_for_sale_batch, serialise
    try:
        inserted_rows = db.query(PropertyForSale).filter(
            PropertyForSale.import_batch_id == batch.id
        ).all()
        audit_input = [
            {
                "address": r.address,
                "suburb": r.suburb,
                "cv_numeric": r.cv_numeric,
                "price_numeric": r.asking_price,
                "market_value": r.market_value,
                "confidence": r.confidence,
            }
            for r in inserted_rows
        ]
        warnings = audit_for_sale_batch(audit_input)
        if warnings:
            print(f"  [audit] {len(warnings)} warning(s) on {len(inserted_rows):,} inserted rows:")
            for w in warnings:
                print(f"    - [{w['severity']}] {w['code']}: {w['message']}")
        warnings_json = serialise(warnings)
    except Exception as e:
        print(f"  [audit] failed: {e}")
        warnings_json = None

    return IngestResult(
        BatchType.FOR_SALE.value, batch.id, inserted, rejected,
        batch.note or "", audit_warnings_json=warnings_json,
    )


def read_csv_bytes(data: bytes | str) -> pd.DataFrame:
    if isinstance(data, bytes):
        return pd.read_csv(BytesIO(data), on_bad_lines="skip")
    return pd.read_csv(StringIO(data), on_bad_lines="skip")
