"""Admin weekly upload — ASYNC.

Upload endpoint saves files to a temp dir, creates IngestJob rows, fires
a background thread, returns job IDs immediately. The frontend polls
/api/admin/jobs/{job_id} every couple seconds for status.

This way the browser never holds a long-running connection — uploads of
any size become reliable.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from .. import ingest
from ..db import SessionLocal, get_db
from ..models import BatchType, ImportBatch, IngestJob, PropertySold, User
from ..release import hold_flagged_rows
from ..security import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])

TEMP_DIR = Path(tempfile.gettempdir()) / "ollie_uploads"
TEMP_DIR.mkdir(exist_ok=True)


# ---------- response shapes ----------
class JobRow(BaseModel):
    id: int
    batch_type: str
    filename: str
    file_size_bytes: int
    status: str
    progress_pct: int
    stage: str | None
    rows_total: int | None
    rows_inserted: int | None
    rows_rejected: int | None
    rows_filled: int | None = None
    rows_missed: int | None = None
    result_json: str | None = None
    error_message: str | None
    audit_warnings: str | None = None
    batch_id: int | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    class Config:
        from_attributes = True


class HistoryRow(BaseModel):
    id: int
    batch_type: str
    region: str
    filename: str
    rows_total: int
    rows_inserted: int
    rows_rejected: int
    is_active: bool
    uploaded_by_id: int | None
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- background worker ----------
def _update(db: Session, job_id: int, **kwargs) -> None:
    db.query(IngestJob).filter(IngestJob.id == job_id).update(kwargs)
    db.commit()


def _load_active_sold_df(db: Session, region: str):
    """The sold dataset the for-sale pricing prices against. Prefers the NEWEST
    loaded sold batch — staged or published — so in a weekly release the fresh
    for-sale is priced against the fresh (staged) sold, not last week's live one."""
    import pandas as pd

    active = (
        db.query(ImportBatch)
        .filter(
            ImportBatch.batch_type == BatchType.SOLD.value,
            ImportBatch.region == region,
            ImportBatch.status.in_(("staged", "published")),
        )
        .order_by(desc(ImportBatch.id))
        .first()
    )
    if not active:
        return None
    rows = db.query(PropertySold).filter(PropertySold.import_batch_id == active.id).all()
    return pd.DataFrame([
        {
            "address": r.address, "suburb": r.suburb, "district": r.district,
            "property_type": r.property_type,
            "key_bedrooms": r.beds, "key_bathrooms": r.baths,
            "key_floor_area": r.floor_area_m2, "key_land_area": r.land_area_m2,
            "price_numeric": r.sale_price, "cv_numeric": r.cv_numeric,
            "land_value_numeric": r.land_value_numeric,
            "type_of_title": r.type_of_title, "sold_date": r.sold_date,
            "days_on_market": r.days_on_market,
        }
        for r in rows
    ])


def _run_job(job_id: int, region: str) -> None:
    """Runs in a background thread. Owns its own DB session."""
    import pandas as pd

    db = SessionLocal()
    try:
        job = db.get(IngestJob, job_id)
        if job is None:
            return

        _update(db, job_id, status="running", started_at=datetime.now(timezone.utc), stage="loading file", progress_pct=5)

        df = pd.read_csv(job.file_path, on_bad_lines="skip")
        _update(db, job_id, rows_total=len(df), stage="ingesting", progress_pct=15)

        # Weekly uploads are STAGED, not published — they land not-live for review,
        # then an admin publishes them (POST /api/admin/release/publish).
        if job.batch_type == BatchType.SOLD.value:
            r = ingest.ingest_sold(db, df, job.filename, region=region, uploaded_by_id=job.uploaded_by_id, publish=False)
        elif job.batch_type == BatchType.RENT.value:
            r = ingest.ingest_rent(db, df, job.filename, region=region, uploaded_by_id=job.uploaded_by_id)
        elif job.batch_type == BatchType.FOR_SALE.value:
            _update(db, job_id, stage="loading sold dataset for comp matching", progress_pct=20)
            sold_df = _load_active_sold_df(db, region)
            if sold_df is None:
                raise RuntimeError("No sold batch in DB — upload a sold CSV first")
            # Stage 1 (LOAD) is fast and makes NO external calls: rows are staged
            # and priced on whatever attributes the scrape carried. CoreLogic
            # enrichment is now a separate, operator-triggered, re-runnable stage
            # (POST /api/admin/release/enrich) followed by a re-price
            # (POST /api/admin/release/price) — so a slow CoreLogic pass can never
            # block this request and get the container killed.
            _update(db, job_id, stage="pricing staged rows", progress_pct=40)
            r = ingest.ingest_for_sale(db, df, sold_df, job.filename, region=region,
                                       uploaded_by_id=job.uploaded_by_id, publish=False, fill_missing=False)
            _update(db, job_id, stage="verifying staged data", progress_pct=85)
            hold_flagged_rows(db, r.batch_id)
        else:
            raise RuntimeError(f"unknown batch_type {job.batch_type}")

        _update(
            db, job_id,
            status="completed",
            progress_pct=100,
            stage="done",
            rows_inserted=r.rows_inserted,
            rows_rejected=r.rows_rejected,
            batch_id=r.batch_id,
            audit_warnings=getattr(r, "audit_warnings_json", None),
            completed_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        msg = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[:2000]}"
        _update(
            db, job_id,
            status="failed",
            stage="error",
            error_message=msg,
            completed_at=datetime.now(timezone.utc),
        )
    finally:
        # Clean up the temp file regardless of outcome
        try:
            job = db.get(IngestJob, job_id)
            if job and job.file_path and os.path.exists(job.file_path):
                os.remove(job.file_path)
        except Exception:
            pass
        db.close()


def _save_upload(upload: UploadFile, batch_type: str) -> tuple[str, int]:
    """Stream the upload to a temp file. Returns (path, size_bytes)."""
    suffix = "_" + (upload.filename or f"{batch_type}.csv")
    fd, path = tempfile.mkstemp(suffix=suffix, dir=str(TEMP_DIR))
    os.close(fd)
    size = 0
    with open(path, "wb") as f:
        while True:
            chunk = upload.file.read(1024 * 1024)  # 1MB
            if not chunk:
                break
            f.write(chunk)
            size += len(chunk)
    return path, size


# ---------- endpoints ----------
@router.post("/upload", response_model=list[JobRow])
def upload_csvs(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    for_sale: UploadFile | None = File(None),
    sold: UploadFile | None = File(None),
    rent: UploadFile | None = File(None),
    region: str = "Auckland",
) -> list[IngestJob]:
    if not any((for_sale, sold, rent)):
        raise HTTPException(status_code=400, detail="At least one CSV (for_sale / sold / rent) required")

    jobs: list[IngestJob] = []

    # Order matters: sold first (so for-sale comp matching uses the newest sold), then rent, then for-sale.
    queued: list[tuple[UploadFile, str]] = []
    if sold is not None:
        queued.append((sold, BatchType.SOLD.value))
    if rent is not None:
        queued.append((rent, BatchType.RENT.value))
    if for_sale is not None:
        queued.append((for_sale, BatchType.FOR_SALE.value))

    for upload, btype in queued:
        path, size = _save_upload(upload, btype)
        job = IngestJob(
            batch_type=btype,
            filename=upload.filename or f"{btype}.csv",
            file_size_bytes=size,
            file_path=path,
            status="pending",
            uploaded_by_id=admin.id,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        jobs.append(job)

    # Fire background threads. They run sequentially per upload group so for-sale
    # can use the sold batch we just ingested.
    job_ids = [j.id for j in jobs]

    def _run_in_order():
        for jid in job_ids:
            _run_job(jid, region)

    threading.Thread(target=_run_in_order, daemon=True).start()
    return jobs


@router.get("/jobs/{job_id}", response_model=JobRow)
def get_job(
    job_id: int,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> IngestJob:
    job = db.get(IngestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/jobs", response_model=list[JobRow])
def list_recent_jobs(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    limit: int = 20,
) -> list[IngestJob]:
    return db.query(IngestJob).order_by(desc(IngestJob.id)).limit(limit).all()


@router.get("/upload/history", response_model=list[HistoryRow])
def history(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[ImportBatch]:
    return db.query(ImportBatch).order_by(desc(ImportBatch.id)).limit(50).all()


@router.get("/section-rates")
def section_rates(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    region: str = "Auckland",
):
    """Per-suburb section $/m² rates the system derived from the active sold batch
    (council land value ÷ land area, median per suburb). Lets the admin sanity-check
    the numbers used in subdivision profit. Computed on demand — not stored."""
    sold_df = _load_active_sold_df(db, region)
    if sold_df is None:
        return {"default": None, "suburbs": [], "note": "No active sold batch."}
    from app.pricing.subdivision import SectionRates
    sr = SectionRates(sold_df)
    return {"default": round(sr.default), "count": len(sr.as_table()), "suburbs": sr.as_table()}
