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

from sqlalchemy import func
from sqlalchemy.orm import Session

from .db import SessionLocal
from .ingest import _blank, _needs_lookup
from .models import BatchType, ImportBatch, IngestJob, PropertyForSale
from .propertyvalue import pv_lookup
from .release import hold_flagged_rows
from .reprice import reprice_batch

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
                         PropertyForSale.address, PropertyForSale.suburb)
                .filter(PropertyForSale.import_batch_id == batch_id)
                .order_by(PropertyForSale.id).all())
        todo = [(r.id, r.address, r.suburb) for r in scan
                if (_blank(r.floor_area_m2) or _blank(r.land_area_m2) or _blank(r.cv_numeric))
                and not _blank(r.address)]
        del scan
        need = len(todo)
        _update(db, job_id, rows_total=need, rows_inserted=0)

        looked = filled = misses = consec_fail = 0
        for pid, address, suburb in todo:
            if looked >= cap:
                break
            q = ", ".join(x for x in (str(address), str(suburb or "").strip(), "Auckland")
                          if x and x.lower() != "nan")
            looked += 1
            try:
                pv = pv_lookup(q)
            except Exception:
                pv = None

            p = db.get(PropertyForSale, pid)
            if p is not None:
                # Stamp every looked-up row (hit OR miss) so the review grid can
                # tell a row CoreLogic never reached ("Not enriched") from one it
                # reached but had nothing for ("CoreLogic missed").
                p.pv_checked_at = datetime.now(timezone.utc)
                if pv:
                    for our_attr, pv_key in _FILL_PAIRS:
                        if _blank(getattr(p, our_attr, None)) and pv.get(pv_key):
                            setattr(p, our_attr, pv.get(pv_key))
                            filled += 1

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
        _update(db, job_id, status="completed", stage="done", progress_pct=100,
                rows_inserted=looked, rows_filled=filled, rows_missed=misses,
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
