"""Admin endpoints for the four-stage ingest pipeline.

One endpoint per stage, each returning immediately with a job id. The admin page
polls GET /api/admin/stages/{batch_id} for state; nothing about a stage's
progress depends on the triggering request staying open.

  POST /api/admin/stages/load                  upload a CSV → staged rows
  POST /api/admin/stages/{batch_id}/enrich     run CoreLogic
  POST /api/admin/stages/{batch_id}/price      run the valuation pipeline
  POST /api/admin/stages/{batch_id}/publish    flip live
  POST /api/admin/stages/jobs/{job_id}/cancel  ask a running stage to stop
  GET  /api/admin/stages/{batch_id}            all four stage states
  GET  /api/admin/stages/{batch_id}/rows       the grid
  GET  /api/admin/stages/batches               staged batches to pick from
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

import os
import tempfile
from pathlib import Path

from .. import stages
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
from ..security import require_admin

router = APIRouter(prefix="/api/admin/stages", tags=["admin", "stages"])

TEMP_DIR = Path(tempfile.gettempdir()) / "ollie_uploads"
TEMP_DIR.mkdir(exist_ok=True)


def _save_upload(upload: UploadFile, batch_type: str) -> tuple[str, int]:
    """Stream the upload to a temp file. Returns (path, size_bytes).

    Streamed in 1 MB chunks rather than read whole: a for-sale export runs to
    well over 100 MB and holding that in memory on a small container is how an
    upload dies with no error anyone can read.
    """
    suffix = "_" + (upload.filename or f"{batch_type}.csv")
    fd, path = tempfile.mkstemp(suffix=suffix, dir=str(TEMP_DIR))
    os.close(fd)
    size = 0
    with open(path, "wb") as f:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            size += len(chunk)
    return path, size


# ---------- shapes ----------
class StageOut(BaseModel):
    stage: str
    status: str
    job_id: int | None
    progress_pct: int
    rows_processed: int
    rows_total: int | None
    rows_filled: int
    rows_missed: int
    rows_skipped: int
    detail: str | None
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    heartbeat_at: datetime | None
    can_run: bool
    blocked_reason: str | None


class BatchStagesOut(BaseModel):
    batch_id: int
    filename: str
    batch_type: str
    region: str
    batch_status: str
    is_active: bool
    rows_total: int
    rows_inserted: int
    rows_rejected: int
    created_at: datetime
    note: str | None
    stages: list[StageOut]
    enrich_coverage: dict
    unpriced_rows: int
    held_rows: int
    # Server clock, so the browser can say "last seen 4 minutes ago" without
    # depending on the client's own clock being correct.
    server_time: datetime


class JobOut(BaseModel):
    id: int
    stage_name: str | None
    batch_id: int | None
    status: str
    progress_pct: int
    stage: str | None
    rows_processed: int
    rows_total: int | None
    rows_filled: int
    rows_missed: int
    rows_skipped: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    heartbeat_at: datetime | None

    class Config:
        from_attributes = True


class RowOut(BaseModel):
    id: int
    address: str | None
    suburb: str | None
    property_type: str | None
    beds: int | None
    baths: int | None
    floor_area_m2: float | None
    land_area_m2: float | None
    cv_numeric: float | None
    asking_price: float | None
    fair_value: float | None
    market_value: float | None
    margin: float | None
    confidence: str | None
    enrich_status: str
    enrich_cells_filled: int
    priced_at: datetime | None
    is_held: bool
    hold_reason: str | None

    class Config:
        from_attributes = True


class BatchSummary(BaseModel):
    id: int
    batch_type: str
    filename: str
    region: str
    status: str
    is_active: bool
    rows_total: int
    rows_inserted: int
    rows_rejected: int
    created_at: datetime

    class Config:
        from_attributes = True


def _get_batch(db: Session, batch_id: int) -> ImportBatch:
    b = db.get(ImportBatch, batch_id)
    if not b:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")
    return b


def _trigger(db: Session, batch: ImportBatch, stage_name: str, admin: User,
             region: str) -> IngestJob:
    """Shared guard + spawn for the three post-load stages."""
    running = stages.active_job(db, batch.id)
    if running:
        raise HTTPException(
            status_code=409,
            detail=(f"Stage '{running.stage_name}' (job {running.id}) is already "
                    f"running on batch {batch.id}. Cancel it or wait for it to finish."),
        )
    state = {s.stage: s for s in stages.batch_stage_states(db, batch.id)}[stage_name]
    if not state.can_run:
        raise HTTPException(status_code=409, detail=state.blocked_reason or "stage cannot run")

    job = stages.new_job(
        db, stage_name=stage_name, batch_type=batch.batch_type,
        filename=batch.filename, batch_id=batch.id, uploaded_by_id=admin.id,
    )
    stages.spawn(stage_name, job.id, batch.id, region)
    return job


# ---------- stage 1: load ----------
@router.post("/load", response_model=list[JobOut])
def stage_load(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    for_sale: UploadFile | None = File(None),
    sold: UploadFile | None = File(None),
    rent: UploadFile | None = File(None),
    region: str = "Auckland",
) -> list[IngestJob]:
    """Parse CSVs into staged rows. No CoreLogic, no pricing — this returns in
    seconds even on a large file, and nothing it writes is live."""
    if not any((for_sale, sold, rent)):
        raise HTTPException(status_code=400, detail="At least one CSV required")

    # Sold first: for-sale pricing comps against the newest sold batch.
    queued = [(f, t) for f, t in (
        (sold, BatchType.SOLD.value),
        (rent, BatchType.RENT.value),
        (for_sale, BatchType.FOR_SALE.value),
    ) if f is not None]

    jobs: list[IngestJob] = []
    for upload, btype in queued:
        path, size = _save_upload(upload, btype)
        jobs.append(stages.new_job(
            db, stage_name=stages.LOAD, batch_type=btype,
            filename=upload.filename or f"{btype}.csv",
            uploaded_by_id=admin.id, file_path=path, file_size_bytes=size,
        ))

    import threading
    ids = [j.id for j in jobs]

    def _in_order():
        for jid in ids:
            stages.run_load(jid, region)

    threading.Thread(target=_in_order, daemon=True).start()
    return jobs


# ---------- stages 2-4 ----------
@router.post("/{batch_id}/enrich", response_model=JobOut)
def stage_enrich(batch_id: int, admin: User = Depends(require_admin),
                 db: Session = Depends(get_db), region: str = "Auckland") -> IngestJob:
    """Run CoreLogic over rows still marked pending. Safe to re-run: already
    processed rows are skipped, so a run that died at 60% resumes, not restarts."""
    return _trigger(db, _get_batch(db, batch_id), stages.ENRICH, admin, region)


@router.post("/{batch_id}/price", response_model=JobOut)
def stage_price(batch_id: int, admin: User = Depends(require_admin),
                db: Session = Depends(get_db), region: str = "Auckland") -> IngestJob:
    """Run the valuation pipeline over the staged rows. Re-runnable — fix the
    pricing code and re-price without reloading the CSV or re-paying CoreLogic."""
    return _trigger(db, _get_batch(db, batch_id), stages.PRICE, admin, region)


@router.post("/{batch_id}/publish", response_model=JobOut)
def stage_publish(batch_id: int, admin: User = Depends(require_admin),
                  db: Session = Depends(get_db), region: str = "Auckland") -> IngestJob:
    """Flip the batch live. Refuses if any row is unpriced."""
    return _trigger(db, _get_batch(db, batch_id), stages.PUBLISH, admin, region)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: int, _: User = Depends(require_admin),
               db: Session = Depends(get_db)) -> IngestJob:
    """Ask a running stage to stop at the next row boundary.

    Co-operative, not immediate: the worker finishes the row it's on, commits,
    and writes status='cancelled'. Work completed before the cancel is kept, so
    a cancelled enrich can be resumed rather than redone.
    """
    job = db.get(IngestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in stages.TERMINAL:
        raise HTTPException(status_code=409, detail=f"Job already {job.status}")
    job.cancel_requested = True
    db.commit()
    db.refresh(job)
    return job


# ---------- status ----------
@router.delete("/{batch_id}")
def delete_batch(batch_id: int, _: User = Depends(require_admin),
                 db: Session = Depends(get_db)) -> dict:
    """Delete a batch and everything attached to it — its listings and its job
    history. Used to clear out old or failed imports so the history shows only
    real work.

    Refuses to delete a LIVE batch. Publishing points the product at a batch; if
    it were deleted out from under itself every listing would vanish for users
    with no way back. Archive or publish a replacement first.

    Refuses while a stage is running on it, since the worker would keep writing
    rows against a batch that no longer exists.
    """
    b = _get_batch(db, batch_id)

    if b.is_active or b.status == "published":
        raise HTTPException(
            status_code=409,
            detail=(f"Batch {batch_id} is live. Publish a replacement or archive "
                    f"it first — deleting it would empty the site."),
        )
    running = stages.active_job(db, batch_id)
    if running:
        raise HTTPException(
            status_code=409,
            detail=f"Stage '{running.stage_name}' is running on this batch. Cancel it first.",
        )

    counts = {}
    for model in (PropertyForSale, PropertySold, PropertyRent):
        n = (db.query(model)
             .filter(model.import_batch_id == batch_id)
             .delete(synchronize_session=False))
        if n:
            counts[model.__tablename__] = n
    jobs = (db.query(IngestJob)
            .filter(IngestJob.batch_id == batch_id)
            .delete(synchronize_session=False))
    db.delete(b)
    db.commit()
    return {"deleted_batch": batch_id, "rows": counts, "jobs": jobs}


@router.delete("/jobs/orphaned")
def delete_orphaned_jobs(_: User = Depends(require_admin),
                         db: Session = Depends(get_db)) -> dict:
    """Remove job rows that never produced a batch — failed loads, cancelled
    runs, and anything left over from an earlier build. These clutter the
    history without describing any data that still exists."""
    n = (db.query(IngestJob)
         .filter(IngestJob.batch_id.is_(None),
                 IngestJob.status.in_(("failed", "cancelled")))
         .delete(synchronize_session=False))
    db.commit()
    return {"deleted_jobs": n}


@router.get("/batches", response_model=list[BatchSummary])
def list_batches(_: User = Depends(require_admin), db: Session = Depends(get_db),
                 batch_type: str | None = None, limit: int = 25) -> list[ImportBatch]:
    q = db.query(ImportBatch)
    if batch_type:
        q = q.filter(ImportBatch.batch_type == batch_type)
    return q.order_by(desc(ImportBatch.id)).limit(limit).all()


@router.get("/{batch_id}", response_model=BatchStagesOut)
def batch_stages(batch_id: int, _: User = Depends(require_admin),
                 db: Session = Depends(get_db)) -> BatchStagesOut:
    """Everything the admin page needs for one batch, in one poll.

    All of it is read from the database, so it is identical whether the browser
    has been open the whole time or was just refreshed.
    """
    b = _get_batch(db, batch_id)
    st = stages.batch_stage_states(db, batch_id)
    unpriced = (db.query(func.count(PropertyForSale.id))
                .filter(PropertyForSale.import_batch_id == batch_id,
                        PropertyForSale.priced_at.is_(None)).scalar() or 0)
    held = (db.query(func.count(PropertyForSale.id))
            .filter(PropertyForSale.import_batch_id == batch_id,
                    PropertyForSale.is_held.is_(True)).scalar() or 0)
    return BatchStagesOut(
        batch_id=b.id, filename=b.filename, batch_type=b.batch_type,
        region=b.region, batch_status=b.status, is_active=b.is_active,
        rows_total=b.rows_total, rows_inserted=b.rows_inserted,
        rows_rejected=b.rows_rejected, created_at=b.created_at, note=b.note,
        stages=[StageOut(**s.__dict__) for s in st],
        enrich_coverage=stages.enrich_coverage(db, batch_id),
        unpriced_rows=unpriced, held_rows=held,
        server_time=stages._now(),
    )


@router.get("/{batch_id}/rows", response_model=list[RowOut])
def batch_rows(
    batch_id: int,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    enrich_status: str | None = Query(None, description="pending|filled|missed|skipped"),
    held_only: bool = False,
    unpriced_only: bool = False,
    limit: int = Query(200, le=1000),
    offset: int = 0,
) -> list[PropertyForSale]:
    """The grid. Available the moment LOAD finishes — the valuation columns are
    simply null until PRICE has run, which is the point: you look at what
    arrived before spending an hour of CoreLogic calls on it."""
    q = db.query(PropertyForSale).filter(PropertyForSale.import_batch_id == batch_id)
    if enrich_status:
        q = q.filter(PropertyForSale.enrich_status == enrich_status)
    if held_only:
        q = q.filter(PropertyForSale.is_held.is_(True))
    if unpriced_only:
        q = q.filter(PropertyForSale.priced_at.is_(None))
    return q.order_by(PropertyForSale.id).offset(offset).limit(limit).all()


@router.get("/jobs/recent", response_model=list[JobOut])
def recent_jobs(_: User = Depends(require_admin), db: Session = Depends(get_db),
                limit: int = 30) -> list[IngestJob]:
    stages.reap_stale_jobs(db)
    return db.query(IngestJob).order_by(desc(IngestJob.id)).limit(limit).all()
