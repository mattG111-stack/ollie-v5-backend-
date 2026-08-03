"""Operator-triggered stages for the staged weekly release.

The four-stage flow — LOAD → ENRICH → PRICE → PUBLISH — with each stage
independently re-runnable and its progress persisted to the database so it
survives a page refresh or a browser disconnect:

    1. LOAD     upload stages raw rows and prices them on what's present. Fast,
                no external calls. (app.ingest.ingest_for_sale, fill_missing=False)
    2. ENRICH   this module: fill blank floor / land / CV from CoreLogic, on the
                stored rows, re-runnable — a re-run only fills what is STILL blank,
                so a stage that died at 60% resumes instead of restarting.
    3. PRICE    this module: re-run the pricing pipeline over the staged batch
                (app.reprice.reprice_batch), so a fix to the pricing code re-values
                the batch without a re-upload.
    4. PUBLISH  app.release.publish_release promotes the staged batch to live.

Each ENRICH / PRICE run is tracked by an IngestJob row: rows_total / rows_inserted
(processed) / rows_filled / rows_missed and a terminal status, all committed as the
stage runs. The long-running work happens on a background thread that owns its own
DB session, so the request returns immediately and /health keeps answering while
the stage runs (the container is no longer killed for a blocked request process).
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone

import json

from sqlalchemy import func
from sqlalchemy.orm import Session

from .db import SessionLocal
from .ingest import _blank, _needs_lookup
from .models import BatchType, ImportBatch, IngestJob, PropertyForSale
from .propertyvalue import PV_BLOCKED, PV_OK, pv_lookup_status
from .release import hold_flagged_rows
from .reprice import reprice_batch


# A scraped CV that disagrees with CoreLogic's by more than this is treated as
# stale/wrong and corrected. CV *is* the council rating value and CoreLogic sources
# it directly, so a real disagreement means the scrape captured the wrong figure
# (6 Cassino Terrace: scraped $500k, real RV $1.25M). 10% ignores rounding noise.
_CV_CORRECT_TOL = 0.10


def corrected_cv(scraped, corelogic, tol: float = _CV_CORRECT_TOL):
    """CoreLogic's capital value is the authoritative council rating value. Return
    it when it materially disagrees with a present scraped CV — the scrape is then
    stale or wrong and CoreLogic overrides it. Returns None (keep the scraped CV)
    when they agree, or when either side is missing. A blank scraped CV is filled
    by the ordinary fill-pairs path, not here."""
    try:
        cl = float(corelogic)
        sc = float(scraped)
    except (TypeError, ValueError):
        return None
    if cl <= 0 or sc <= 0:
        return None
    if abs(sc - cl) > tol * cl:
        return cl
    return None


def _apply_pv_record(p: PropertyForSale, pv: dict) -> None:
    """Store CoreLogic's own cached fields on the row: its AVM range, its CV, and
    the last recorded sale (price + date). These are refreshed to the latest lookup
    — unlike the pricing ATTRIBUTES (floor/land/CV/beds/baths/zoning), which only
    fill blanks and must never overwrite a real scraped value. pv_estimate_mid also
    feeds the pricing engine's CV-fallback anchor when a council CV is missing."""
    p.pv_estimate_low = pv.get("estimate_low")
    p.pv_estimate_high = pv.get("estimate_high")
    p.pv_estimate_mid = pv.get("estimate_mid")
    p.pv_cv = pv.get("cv")
    p.pv_url = pv.get("url")
    p.pv_last_sale_price = pv.get("last_sale_price")
    p.pv_last_sale_date = pv.get("last_sale_date")
    try:
        p.pv_data = json.dumps(pv)
    except (TypeError, ValueError):
        pass

# CoreLogic record key → our PropertyForSale attribute. Mirrors the pairs in
# ingest._fill_df_from_corelogic, but writing to the stored row's attributes.
_FILL_PAIRS = (
    ("floor_area_m2", "floor_area_m2"),
    ("land_area_m2", "land_area_m2"),
    ("beds", "beds"),
    ("baths", "baths"),
    ("cv_numeric", "cv"),
    ("zoning", "zoning"),
)


def _staged_forsale_batch(db: Session, region: str) -> ImportBatch | None:
    return (db.query(ImportBatch)
            .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                    ImportBatch.region == region,
                    ImportBatch.status == "staged")
            .order_by(ImportBatch.id.desc()).first())


def _row_input(p: PropertyForSale) -> dict:
    """The subset of scrape-shaped fields _needs_lookup reads from a stored row."""
    return {
        "key_floor_area": p.floor_area_m2,
        "key_land_area": p.land_area_m2,
        "cv_numeric": p.cv_numeric,
        "address": p.address,
    }


def create_stage_job(db: Session, *, stage: str, batch_id: int, region: str,
                     uploaded_by_id: int | None) -> IngestJob:
    """Create the IngestJob that tracks an ENRICH / PRICE run and return it."""
    job = IngestJob(
        batch_type=BatchType.FOR_SALE.value,
        filename=f"{stage} (batch {batch_id})",
        status="pending",
        stage=stage,
        batch_id=batch_id,
        uploaded_by_id=uploaded_by_id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _update(db: Session, job_id: int, **kwargs) -> None:
    db.query(IngestJob).filter(IngestJob.id == job_id).update(kwargs)
    db.commit()


# ---- shared guard -----------------------------------------------------------
def stage_running(db: Session, batch_id: int, stage: str) -> bool:
    """True if an enrich/price stage is already running for this batch. Prevents a
    second (heavy) background worker from stacking on the first — the single most
    likely cause of an out-of-memory container kill when an operator clicks a
    slow-looking button repeatedly."""
    return (db.query(IngestJob)
            .filter(IngestJob.batch_id == batch_id,
                    IngestJob.stage == stage,
                    IngestJob.status == "running")
            .count() > 0)


# ---- ENRICH -----------------------------------------------------------------
def run_enrich_job(job_id: int, batch_id: int, region: str,
                   *, delay: float = 0.5, cap: int = 20000) -> None:
    """Background worker: fill blank floor / land / CV on the staged batch's rows
    from CoreLogic. Re-runnable — only rows still missing a pricing-critical field
    are looked up, so a re-run resumes rather than restarts.

    Memory-flat by design: it never materialises the whole batch as ORM objects.
    A lightweight scan (id + the three fields that decide "needs a lookup") builds
    the work list, then each row is fetched, filled and released one at a time,
    with the identity map cleared every 25 lookups. An 11k-row batch stays flat in
    memory instead of holding 11k objects for 30 minutes — which is what let a few
    stacked runs OOM-kill the container."""
    db = SessionLocal()
    try:
        _update(db, job_id, status="running", stage="enrich",
                started_at=datetime.now(timezone.utc), progress_pct=0,
                rows_filled=0, rows_missed=0)

        # Lightweight scan — tuples, not ORM objects.
        scan = (db.query(PropertyForSale.id, PropertyForSale.floor_area_m2,
                         PropertyForSale.land_area_m2, PropertyForSale.cv_numeric,
                         PropertyForSale.asking_price,
                         PropertyForSale.address, PropertyForSale.suburb)
                .filter(PropertyForSale.import_batch_id == batch_id)
                .order_by(PropertyForSale.id).all())

        def _cv_suspect(cv, asking) -> bool:
            # The scraper fills a by-negotiation asking from the CV, so cv == asking
            # to the dollar flags a CV that may be the copied/wrong figure (6 Cassino
            # Terrace). Look these up so CoreLogic can confirm or correct the CV even
            # when floor/land are already present.
            return bool(cv and asking and abs(cv - asking) < 0.005 * cv)

        todo = [(r.id, r.address, r.suburb) for r in scan
                if (_blank(r.floor_area_m2) or _blank(r.land_area_m2)
                    or _blank(r.cv_numeric) or _cv_suspect(r.cv_numeric, r.asking_price))
                and not _blank(r.address)]
        del scan
        need = len(todo)
        _update(db, job_id, rows_total=need, rows_inserted=0)

        looked = filled = misses = consec_fail = corrected = 0
        blocked = consec_block = 0
        for pid, address, suburb in todo:
            if looked >= cap:
                break
            q = ", ".join(x for x in (str(address), str(suburb or "").strip(), "Auckland")
                          if x and x.lower() != "nan")
            looked += 1
            try:
                pv, status = pv_lookup_status(q)
            except Exception:
                pv, status = None, "error"

            # Definitive block detection: a 401/403/429 from CoreLogic is being
            # refused, NOT an address it lacks. A short run of these is unambiguous,
            # so stop fast with a message that says exactly that — no guessing from
            # 40 empty results (which could just be 40 unknown addresses).
            if status == PV_BLOCKED:
                blocked += 1
                consec_block += 1
                if consec_block >= 5:
                    db.commit()
                    _update(db, job_id, status="failed", stage="error",
                            rows_inserted=looked, rows_filled=filled, rows_missed=misses,
                            error_message=(
                                f"BLOCKED by CoreLogic — {consec_block} straight HTTP "
                                f"401/403/429 responses at lookup {looked}/{need}. This is "
                                f"a rate-limit/IP block, not missing addresses. Wait and "
                                f"re-run, slow the pacing, or use proper CoreLogic API "
                                f"access."),
                            completed_at=datetime.now(timezone.utc))
                    return
            else:
                consec_block = 0

            p = db.get(PropertyForSale, pid)
            if p is not None:
                # Stamp every looked-up row (hit OR miss) so the review grid can
                # tell a row CoreLogic never reached ("Not enriched") from one it
                # reached but had nothing for ("CoreLogic missed").
                p.pv_checked_at = datetime.now(timezone.utc)
                if pv:
                    # Fill blank pricing attributes (never overwrite real values)...
                    for our_attr, pv_key in _FILL_PAIRS:
                        if _blank(getattr(p, our_attr, None)) and pv.get(pv_key):
                            setattr(p, our_attr, pv.get(pv_key))
                            filled += 1
                    # ...correct a present-but-wrong CV from CoreLogic's authoritative
                    # rating value (the one exception to "never overwrite": CV *is*
                    # the council RV, and a scraped CV that disagrees is wrong)...
                    new_cv = corrected_cv(p.cv_numeric, pv.get("cv"))
                    if new_cv is not None:
                        p.cv_numeric = new_cv
                        corrected += 1
                    # ...and capture CoreLogic's AVM / CV / last-sold on every hit.
                    _apply_pv_record(p, pv)

            if not pv:
                misses += 1
                consec_fail += 1
                # Circuit breaker: 40 misses in a row means CoreLogic is refusing
                # (rate-limited), not that these addresses are genuinely unknown.
                if consec_fail >= 40:
                    db.commit()
                    _update(db, job_id, status="failed", stage="error",
                            rows_inserted=looked, rows_filled=filled, rows_missed=misses,
                            error_message=f"stopped after {consec_fail} consecutive misses "
                                          f"(CoreLogic likely unreachable or rate-limited) at "
                                          f"lookup {looked}/{need} — re-run to resume",
                            completed_at=datetime.now(timezone.utc))
                    return
            else:
                consec_fail = 0

            if looked % 25 == 0:
                db.commit()               # persist this chunk's fills durably
                db.expunge_all()          # release the identity map — keeps memory flat
                pct = int(100 * looked / need) if need else 100
                _update(db, job_id, rows_inserted=looked, rows_filled=filled,
                        rows_missed=misses, progress_pct=min(pct, 99))
            time.sleep(delay)

        db.commit()
        if corrected:
            print(f"[enrich] corrected {corrected} wrong CV(s) from CoreLogic's "
                  f"rating value", flush=True)
        # Distinguish genuine "no record" from transport blocks in the summary, so
        # the operator can tell "CoreLogic just doesn't have these" from "CoreLogic
        # refused us". `blocked` counts 401/403/429; a nonzero value with few fills
        # means you're being rate-limited even if the run didn't hit the fast-stop.
        not_found = misses - blocked
        summary = (f"done: {filled} field(s) filled, {corrected} CV(s) corrected, "
                   f"{not_found} address(es) not in CoreLogic, {blocked} blocked "
                   f"(HTTP 401/403/429)")
        print(f"[enrich] {summary}", flush=True)
        _update(db, job_id, status="completed", stage="done", progress_pct=100,
                rows_inserted=looked, rows_filled=filled, rows_missed=misses,
                error_message=(f"BLOCKED on {blocked} lookup(s) (HTTP 401/403/429) — "
                               f"CoreLogic is rate-limiting; those rows kept their scraped "
                               f"values. Re-run later to fill them." if blocked else None),
                completed_at=datetime.now(timezone.utc))
    except Exception as e:
        _update(db, job_id, status="failed", stage="error",
                error_message=f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[:2000]}",
                completed_at=datetime.now(timezone.utc))
    finally:
        db.close()


# ---- PRICE ------------------------------------------------------------------
def run_price_job(job_id: int, batch_id: int, region: str) -> None:
    """Background worker: re-run the pricing pipeline over the staged batch using
    its CURRENT stored attributes (i.e. after enrich), committing the new
    valuations. Then re-apply the pre-publish holds so the review reflects the
    fresh numbers. Re-runnable."""
    db = SessionLocal()
    try:
        _update(db, job_id, status="running", stage="price",
                started_at=datetime.now(timezone.utc), progress_pct=10)
        res = reprice_batch(db, batch_id, region=region, commit=True)
        if res.error:
            raise RuntimeError(res.error)
        _update(db, job_id, stage="price", progress_pct=85, rows_total=res.rows,
                rows_inserted=res.rows)
        held = hold_flagged_rows(db, batch_id)
        _update(db, job_id, status="completed", stage="done", progress_pct=100,
                rows_rejected=held, completed_at=datetime.now(timezone.utc))
    except Exception as e:
        _update(db, job_id, status="failed", stage="error",
                error_message=f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[:2000]}",
                completed_at=datetime.now(timezone.utc))
    finally:
        db.close()


# ---- self-heal: re-price stale AVM-inflated valuations on deploy -------------
from sqlalchemy import func  # noqa: E402


def _has_stale_avm_rows(db: Session, batch_id: int) -> bool:
    """Cheap existence check for the pre-guard pricing bug's fingerprint.

    The old anchor guard set a CV-anchored valuation UP to ~95% of the external
    AVM (CoreLogic/homes) whenever that AVM was high — surfacing a $70k section
    at $1.22M, a land-only-CV new build at $3.9M. The signature is precise:
    fair_value ≈ 0.95 × the AVM AND far above the CV.

    The current code NEVER produces fair_value = 0.95 × AVM when a CV is present
    (it drops the AVM from the anchor, and prices land-only CVs from comps), so a
    correctly re-priced batch — including comp-valued new builds that legitimately
    sit well above their land-only CV — will not match. That makes the heal
    converge after one pass instead of re-pricing every boot."""
    P = PropertyForSale
    avm = func.coalesce(P.pv_estimate_mid, P.homes_valuation)
    q = (db.query(P.id)
         .filter(P.import_batch_id == batch_id,
                 P.cv_numeric.isnot(None), P.cv_numeric > 0,
                 P.fair_value.isnot(None),
                 P.fair_value > 1.6 * P.cv_numeric,
                 avm.isnot(None), avm > 0,
                 func.abs(P.fair_value - 0.95 * avm) <= 0.02 * P.fair_value)
         .limit(1))
    return db.query(q.exists()).scalar()


def _has_correctable_cv(db: Session, batch_id: int) -> bool:
    """Any row whose stored CV disagrees with CoreLogic's already-fetched rating
    value (pv_cv) by more than the correction tolerance. Cheap existence check —
    no API calls; the pv_cv was captured by a prior enrich."""
    P = PropertyForSale
    q = (db.query(P.id)
         .filter(P.import_batch_id == batch_id,
                 P.pv_cv.isnot(None), P.pv_cv > 0,
                 P.cv_numeric.isnot(None), P.cv_numeric > 0,
                 func.abs(P.cv_numeric - P.pv_cv) > _CV_CORRECT_TOL * P.pv_cv)
         .limit(1))
    return db.query(q.exists()).scalar()


def _correct_cvs_from_pv(db: Session, batch_id: int) -> int:
    """Correct stored CVs from CoreLogic's already-fetched rating value (pv_cv),
    using data already on the row — no API calls. Returns count corrected."""
    rows = (db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch_id,
                    PropertyForSale.pv_cv.isnot(None)).all())
    n = 0
    for p in rows:
        new_cv = corrected_cv(p.cv_numeric, p.pv_cv)
        if new_cv is not None:
            p.cv_numeric = new_cv
            n += 1
    if n:
        db.commit()
    return n


def auto_reprice_stale_batches() -> int:
    """Self-heal on startup: any staged for-sale batch still holding CV-over
    valuations from a pre-guard pricing run, OR a stored CV that CoreLogic's
    fetched rating value contradicts, is corrected + re-priced in place, once, so a
    deploy fixes the stored numbers without anyone having to click a button. Skips
    batches that are already clean or already being re-priced, and NEVER raises — a
    self-heal must not break boot. Returns batches repriced."""
    healed = 0
    db = SessionLocal()
    try:
        batches = (db.query(ImportBatch)
                   .filter(ImportBatch.batch_type == BatchType.FOR_SALE.value,
                           ImportBatch.status == "staged")
                   .order_by(ImportBatch.id.desc()).all())
        for b in batches:
            try:
                if stage_running(db, b.id, "price"):
                    continue
                stale_avm = _has_stale_avm_rows(db, b.id)
                bad_cv = _has_correctable_cv(db, b.id)
                if not stale_avm and not bad_cv:
                    continue
                region = b.region or "Auckland"
                fixed_cv = _correct_cvs_from_pv(db, b.id) if bad_cv else 0
                print(f"[auto_reprice] batch {b.id}: {fixed_cv} CV(s) corrected from "
                      f"CoreLogic, re-pricing in background ({region})", flush=True)
                res = reprice_batch(db, b.id, region=region, commit=True)
                if not res.error:
                    hold_flagged_rows(db, b.id)
                    healed += 1
                    print(f"[auto_reprice] batch {b.id} repriced: {res.rows} rows, "
                          f"{res.changed_fair_value} valuations changed", flush=True)
                else:
                    print(f"[auto_reprice] batch {b.id} skipped: {res.error}", flush=True)
            except Exception as e:
                print(f"[auto_reprice] batch {b.id} failed: {type(e).__name__}: {e}",
                      flush=True)
    except Exception as e:
        print(f"[auto_reprice] could not scan batches: {type(e).__name__}: {e}", flush=True)
    finally:
        db.close()
    return healed
