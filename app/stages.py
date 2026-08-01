"""Four operator-triggered ingest stages: LOAD → ENRICH → PRICE → PUBLISH.

Why this exists
---------------
`ingest_for_sale` did all three of load, CoreLogic enrichment and pricing inside
one blocking job. On a full for-sale file that job runs for the better part of an
hour — CoreLogic is throttled to one lookup every 0.5s, capped at 5,000 rows —
and if anything interrupts it, the whole thing is redone from the CSV. Worse, the
only progress signal lived in the browser, so a dropped tab left no way to tell a
running job from a dead one.

Each stage here is separately triggerable and separately re-runnable:

  LOAD     CSV → staged rows. No external calls, no pricing. Seconds, not hours.
  ENRICH   CoreLogic fills blank floor/land/CV. Resumable — per-row state means a
           re-run picks up where it stopped rather than re-paying for lookups.
  PRICE    Runs the valuation pipeline over the staged rows, in place.
  PUBLISH  The existing release gate; flips the batch live.

Progress is DATABASE-RESIDENT, not client-side. Every counter, the terminal state
and both timestamps are written to `ingest_jobs` as the stage runs. Closing the
tab, refreshing, or losing the network changes nothing about what is recorded.

Terminal states are completed / failed / cancelled. A job in 'running' whose
heartbeat has gone stale was killed without getting to write one — a container
restart, an OOM — and `reap_stale_jobs()` converts those to 'failed' so they
can't sit in the UI looking alive forever.
"""
from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import BatchType, ImportBatch, IngestJob, PropertyForSale

# Stage names, in order.
LOAD, ENRICH, PRICE, PUBLISH = "load", "enrich", "price", "publish"
STAGES = (LOAD, ENRICH, PRICE, PUBLISH)

TERMINAL = ("completed", "failed", "cancelled")

# How often the worker writes progress to the DB. Enrich sleeps 0.5s per row, so
# every 10 rows is roughly a five-second write cadence — frequent enough that a
# hard kill loses almost nothing, rare enough not to hammer the database.
_FLUSH_EVERY = 10

# A 'running' job untouched for this long is assumed dead.
_STALE_AFTER = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Job context — owns durable progress, heartbeat and cancellation
# ---------------------------------------------------------------------------
class JobCtx:
    """Wraps an IngestJob row and writes progress through to the database.

    Deliberately holds no state the UI needs. Anything the operator has to be
    able to see after a refresh is committed, not cached in memory.
    """

    def __init__(self, db: Session, job_id: int):
        self.db = db
        self.job_id = job_id
        self._since_flush = 0

    def _write(self, **kw) -> None:
        self.db.query(IngestJob).filter(IngestJob.id == self.job_id).update(kw)
        self.db.commit()

    def start(self, *, rows_total: int | None = None, stage: str = "starting") -> None:
        self._write(
            status="running", started_at=_now(), heartbeat_at=_now(),
            stage=stage, progress_pct=0, rows_total=rows_total,
            rows_processed=0, rows_filled=0, rows_missed=0, rows_skipped=0,
            error_message=None,
        )

    def cancelled(self) -> bool:
        """Polled by the worker loop. Cancellation is co-operative: a thread
        mid-transaction can't be killed safely, so we ask it to stop instead."""
        flag = (
            self.db.query(IngestJob.cancel_requested)
            .filter(IngestJob.id == self.job_id)
            .scalar()
        )
        return bool(flag)

    def progress(self, *, processed: int, total: int, stage: str | None = None,
                 filled: int = 0, missed: int = 0, skipped: int = 0,
                 force: bool = False) -> None:
        """Advance the counters. Writes every _FLUSH_EVERY calls unless forced."""
        self._since_flush += 1
        if not force and self._since_flush < _FLUSH_EVERY:
            return
        self._since_flush = 0
        pct = int(100 * processed / total) if total else 0
        kw = dict(
            rows_processed=processed, rows_total=total,
            rows_filled=filled, rows_missed=missed, rows_skipped=skipped,
            progress_pct=min(pct, 99),   # 100 is reserved for the terminal write
            heartbeat_at=_now(),
        )
        if stage:
            kw["stage"] = stage
        self._write(**kw)

    def finish(self, *, stage: str = "done", **kw) -> None:
        self._write(status="completed", progress_pct=100, stage=stage,
                    completed_at=_now(), heartbeat_at=_now(), **kw)

    def cancel(self, **kw) -> None:
        self._write(status="cancelled", stage="cancelled by operator",
                    completed_at=_now(), heartbeat_at=_now(), **kw)

    def fail(self, exc: BaseException, **kw) -> None:
        msg = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()[:2000]}"
        self._write(status="failed", stage="error", error_message=msg,
                    completed_at=_now(), heartbeat_at=_now(), **kw)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
class StageError(RuntimeError):
    """A stage was asked to run when its preconditions weren't met."""


def active_job(db: Session, batch_id: int | None, stage_name: str | None = None) -> IngestJob | None:
    """The running/pending job for a batch, if any. Used to refuse a second
    concurrent run of the same stage — two enrich threads on one batch would
    double-spend CoreLogic calls and interleave their counters."""
    q = (
        db.query(IngestJob)
        .filter(IngestJob.batch_id == batch_id,
                IngestJob.status.in_(("pending", "running")))
    )
    if stage_name:
        q = q.filter(IngestJob.stage_name == stage_name)
    return q.order_by(desc(IngestJob.id)).first()


def reap_stale_jobs(db: Session) -> int:
    """Mark 'running' jobs whose heartbeat has gone cold as failed.

    This is the case a progress bar can never cover: the container was replaced
    mid-run, so nothing ever wrote a terminal state. Without this the admin page
    shows a job frozen at 62% forever with no way to tell it's dead.
    """
    cutoff = _now() - _STALE_AFTER
    stale = (
        db.query(IngestJob)
        .filter(IngestJob.status == "running",
                IngestJob.heartbeat_at.isnot(None),
                IngestJob.heartbeat_at < cutoff)
        .all()
    )
    for j in stale:
        j.status = "failed"
        j.stage = "died"
        j.completed_at = _now()
        j.error_message = (
            f"No heartbeat since {j.heartbeat_at:%Y-%m-%d %H:%M:%S UTC} "
            f"(> {int(_STALE_AFTER.total_seconds() // 60)} min). The worker was "
            f"killed before it could record an outcome — most likely a redeploy "
            f"or restart. Progress up to {j.rows_processed or 0} rows was saved; "
            f"re-run this stage to resume."
        )
    if stale:
        db.commit()
    return len(stale)


def new_job(db: Session, *, stage_name: str, batch_type: str, filename: str,
            batch_id: int | None = None, uploaded_by_id: int | None = None,
            file_path: str | None = None, file_size_bytes: int = 0) -> IngestJob:
    job = IngestJob(
        stage_name=stage_name, batch_type=batch_type, filename=filename,
        batch_id=batch_id, uploaded_by_id=uploaded_by_id,
        file_path=file_path, file_size_bytes=file_size_bytes,
        status="pending", progress_pct=0, stage="queued",
    )
    db.add(job); db.commit(); db.refresh(job)
    return job


# ---------------------------------------------------------------------------
# Stage 1 — LOAD
# ---------------------------------------------------------------------------
def run_load(job_id: int, region: str = "Auckland") -> None:
    """CSV → staged rows. No external calls, no pricing."""
    import pandas as pd

    from . import ingest

    db = SessionLocal()
    ctx = JobCtx(db, job_id)
    try:
        job = db.get(IngestJob, job_id)
        if job is None:
            return
        ctx.start(stage="reading CSV")
        df = pd.read_csv(job.file_path, on_bad_lines="skip")
        ctx.progress(processed=0, total=len(df), stage="writing staged rows", force=True)

        if job.batch_type == BatchType.SOLD.value:
            r = ingest.ingest_sold(db, df, job.filename, region=region,
                                   uploaded_by_id=job.uploaded_by_id, publish=False)
        elif job.batch_type == BatchType.RENT.value:
            r = ingest.ingest_rent(db, df, job.filename, region=region,
                                   uploaded_by_id=job.uploaded_by_id)
        elif job.batch_type == BatchType.FOR_SALE.value:
            r = ingest.load_for_sale(db, df, job.filename, region=region,
                                     uploaded_by_id=job.uploaded_by_id)
        else:
            raise StageError(f"unknown batch_type {job.batch_type}")

        ctx.finish(
            stage="loaded", batch_id=r.batch_id, rows_total=len(df),
            rows_processed=r.rows_inserted,
            rows_inserted=r.rows_inserted, rows_rejected=r.rows_rejected,
        )
    except Exception as e:
        ctx.fail(e)
    finally:
        _cleanup_temp(db, job_id)
        db.close()


def _cleanup_temp(db: Session, job_id: int) -> None:
    import os
    try:
        job = db.get(IngestJob, job_id)
        if job and job.file_path and os.path.exists(job.file_path):
            os.remove(job.file_path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Stage 2 — ENRICH (CoreLogic)
# ---------------------------------------------------------------------------
# Fields CoreLogic can supply → the column on PropertyForSale it fills.
_PV_FIELDS = (
    ("floor_area_m2", "floor_area_m2"),
    ("land_area_m2", "land_area_m2"),
    ("beds", "beds"),
    ("baths", "baths"),
    ("cv_numeric", "cv"),
    ("zoning", "zoning"),
)

# Consecutive misses after which we assume rate-limiting rather than a run of
# genuinely unresolvable addresses, and stop rather than burn the remaining hour.
_CIRCUIT_BREAK = 40


def _needs_lookup(p: PropertyForSale) -> bool:
    """Only spend a CoreLogic call when a pricing-critical size field is missing.
    Filling in cosmetic gaps is not worth 0.5s and a rate-limit slot."""
    return p.floor_area_m2 in (None, 0) or p.land_area_m2 in (None, 0)


def run_enrich(job_id: int, batch_id: int, region: str = "Auckland",
               *, delay: float = 0.5, cap: int = 5000) -> None:
    """Fill blank attributes on staged rows from CoreLogic, one row at a time,
    committing progress as it goes.

    Resumable: only rows with enrich_status='pending' are considered, and each
    row's outcome is written before moving on. A run that dies at 60% leaves the
    first 60% marked filled/missed/skipped, so re-running resumes at row 60% + 1
    rather than starting over.

    'missed' is not an error. CoreLogic returns nothing for plenty of addresses;
    those rows simply price on the numbers they already have, exactly as before.
    """
    from .propertyvalue import pv_lookup

    db = SessionLocal()
    ctx = JobCtx(db, job_id)
    try:
        pending = (
            db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch_id,
                    PropertyForSale.enrich_status == "pending")
            .order_by(PropertyForSale.id)
            .all()
        )
        total = len(pending)
        ctx.start(rows_total=total, stage="contacting CoreLogic")
        if total == 0:
            ctx.finish(stage="nothing left to enrich", rows_processed=0)
            return

        processed = filled = missed = skipped = 0
        looked = consec_fail = 0
        stopped_early: str | None = None

        for p in pending:
            if ctx.cancelled():
                stopped_early = "cancelled"
                break
            if looked >= cap:
                stopped_early = f"lookup cap of {cap:,} reached"
                break

            processed += 1

            if not _needs_lookup(p):
                p.enrich_status = "skipped"
                p.enriched_at = _now()
                skipped += 1
                ctx.progress(processed=processed, total=total, filled=filled,
                             missed=missed, skipped=skipped)
                if processed % _FLUSH_EVERY == 0:
                    db.commit()
                continue

            if not p.address:
                p.enrich_status = "skipped"
                p.enriched_at = _now()
                skipped += 1
                continue

            query = ", ".join(
                x for x in (p.address.strip(), (p.suburb or "").strip(), region)
                if x and x.lower() != "nan"
            )
            looked += 1
            try:
                pv = pv_lookup(query)
            except Exception:
                pv = None

            if not pv:
                p.enrich_status = "missed"
                p.enriched_at = _now()
                missed += 1
                consec_fail += 1
                if consec_fail >= _CIRCUIT_BREAK:
                    stopped_early = (
                        f"stopped after {consec_fail} consecutive misses — "
                        f"CoreLogic is most likely rate-limiting. Re-run to continue."
                    )
                    db.commit()
                    break
            else:
                consec_fail = 0
                cells = 0
                for col, pvkey in _PV_FIELDS:
                    if getattr(p, col, None) in (None, 0, "") and pv.get(pvkey):
                        setattr(p, col, pv.get(pvkey))
                        cells += 1
                p.enrich_cells_filled = cells
                p.enrich_status = "filled" if cells else "missed"
                p.enriched_at = _now()
                if cells:
                    filled += 1
                else:
                    missed += 1

            ctx.progress(processed=processed, total=total, filled=filled,
                         missed=missed, skipped=skipped)
            if processed % _FLUSH_EVERY == 0:
                db.commit()

            import time
            time.sleep(delay)

        db.commit()
        note = f"{filled:,} filled · {missed:,} missed · {skipped:,} needed nothing"
        if stopped_early == "cancelled":
            ctx.cancel(rows_processed=processed, rows_total=total, rows_filled=filled,
                       rows_missed=missed, rows_skipped=skipped,
                       error_message=f"Cancelled by operator after {processed:,} of {total:,} rows. {note}")
            return
        ctx.finish(
            stage=(stopped_early or note),
            rows_processed=processed, rows_total=total,
            rows_filled=filled, rows_missed=missed, rows_skipped=skipped,
        )
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        ctx.fail(e)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Stage 3 — PRICE
# ---------------------------------------------------------------------------
def run_price(job_id: int, batch_id: int, region: str = "Auckland") -> None:
    """Run the valuation pipeline over the staged rows, in place.

    Uses `reprice.reprice_batch`, which reconstructs the exact pipeline input
    from each stored row and writes back only the pipeline OUTPUT columns. That
    is the same code path the welded ingest ran, so separating the stages does
    not change any number.

    After pricing, the soft reject rules (the ones that depend on CV and floor
    area, which enrich may have supplied) are applied as HOLDS rather than
    deletions, so a rejected row stays inspectable in the grid instead of
    vanishing between stages.
    """
    from . import ingest
    from .release import hold_flagged_rows
    from .reprice import reprice_batch

    db = SessionLocal()
    ctx = JobCtx(db, job_id)
    try:
        total = (
            db.query(func.count(PropertyForSale.id))
            .filter(PropertyForSale.import_batch_id == batch_id).scalar() or 0
        )
        ctx.start(rows_total=total, stage="running valuation pipeline")
        if total == 0:
            raise StageError(f"batch {batch_id} has no rows to price")

        res = reprice_batch(db, batch_id, region=region, commit=True)
        if res.error:
            raise StageError(res.error)

        ctx.progress(processed=res.rows, total=total, stage="applying hold rules", force=True)

        recs = (
            db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch_id).all()
        )
        held = 0
        stamped = _now()
        for p in recs:
            p.priced_at = stamped
            payload = {
                "cv_numeric": p.cv_numeric,
                "floor_area_m2": p.floor_area_m2,
                "suburb": p.suburb,
                "region": p.region,
                "beds": p.beds,
            }
            reason = ingest._soft_reject_reason(payload, p.asking_price,
                                                property_type=p.property_type)
            if reason:
                p.is_held = True
                p.hold_reason = f"price stage: {reason}"
                held += 1
        db.commit()

        # The existing verification gate — extreme valuations, bad land areas.
        flagged = hold_flagged_rows(db, batch_id)

        ctx.finish(
            stage=f"{res.rows:,} priced · {held + flagged:,} held for review",
            rows_processed=res.rows, rows_total=total,
            rows_inserted=res.rows,
        )
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        ctx.fail(e)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Stage 4 — PUBLISH
# ---------------------------------------------------------------------------
def run_publish(job_id: int, batch_id: int, region: str = "Auckland") -> None:
    """Flip the staged batch live via the existing release gate.

    Refuses to publish a batch that was never priced. An unpriced row has no
    fair_value, so publishing one would put blank listings in front of users —
    the failure mode that separating the stages is specifically meant to prevent.
    """
    from .release import publish_release

    db = SessionLocal()
    ctx = JobCtx(db, job_id)
    try:
        ctx.start(stage="checking preconditions")
        unpriced = (
            db.query(func.count(PropertyForSale.id))
            .filter(PropertyForSale.import_batch_id == batch_id,
                    PropertyForSale.priced_at.is_(None)).scalar() or 0
        )
        if unpriced:
            raise StageError(
                f"{unpriced:,} rows in batch {batch_id} have never been priced. "
                f"Run the price stage before publishing."
            )
        ctx.progress(processed=0, total=1, stage="publishing", force=True)
        result = publish_release(db, region=region)
        ctx.finish(stage=f"published: {result}", rows_processed=1, rows_total=1)
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        ctx.fail(e)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Dispatch + status
# ---------------------------------------------------------------------------
_RUNNERS = {LOAD: run_load, ENRICH: run_enrich, PRICE: run_price, PUBLISH: run_publish}


def spawn(stage_name: str, job_id: int, batch_id: int | None, region: str) -> None:
    """Fire the stage on a daemon thread and return immediately.

    The HTTP request that triggered a stage must not hold open for its duration —
    that coupling is what made the old ingest fail whenever a browser dropped.
    Everything the operator needs to see afterwards is in the database.
    """
    fn = _RUNNERS[stage_name]
    args = (job_id, region) if stage_name == LOAD else (job_id, batch_id, region)
    threading.Thread(target=fn, args=args, daemon=True).start()


@dataclass
class StageState:
    stage: str
    status: str          # not_started | pending | running | completed | failed | cancelled
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


def _latest(db: Session, batch_id: int, stage_name: str) -> IngestJob | None:
    return (
        db.query(IngestJob)
        .filter(IngestJob.batch_id == batch_id, IngestJob.stage_name == stage_name)
        .order_by(desc(IngestJob.id))
        .first()
    )


def batch_stage_states(db: Session, batch_id: int) -> list[StageState]:
    """The four stages of one batch, with what each is allowed to do next.

    Called on every poll from the admin page, and reaps dead jobs on the way
    through so a killed worker surfaces as 'failed' rather than sitting at 62%.
    """
    reap_stale_jobs(db)

    counts = dict(
        db.query(PropertyForSale.enrich_status, func.count(PropertyForSale.id))
        .filter(PropertyForSale.import_batch_id == batch_id)
        .group_by(PropertyForSale.enrich_status).all()
    )
    rows_in_batch = sum(counts.values())
    unpriced = (
        db.query(func.count(PropertyForSale.id))
        .filter(PropertyForSale.import_batch_id == batch_id,
                PropertyForSale.priced_at.is_(None)).scalar() or 0
    )
    anything_running = any(
        (_latest(db, batch_id, s) or IngestJob()).status in ("pending", "running")
        for s in STAGES
    )

    out: list[StageState] = []
    for stage in STAGES:
        j = _latest(db, batch_id, stage)
        status = j.status if j else "not_started"

        can, blocked = True, None
        if anything_running and status not in ("pending", "running"):
            can, blocked = False, "another stage is running on this batch"
        elif status in ("pending", "running"):
            can, blocked = False, "already running"
        elif stage == ENRICH and rows_in_batch == 0:
            can, blocked = False, "load the batch first"
        elif stage == ENRICH and counts.get("pending", 0) == 0 and status == "completed":
            can, blocked = True, "nothing pending — re-running will be a no-op"
        elif stage == PRICE and rows_in_batch == 0:
            can, blocked = False, "load the batch first"
        elif stage == PUBLISH and unpriced:
            can, blocked = False, f"{unpriced:,} rows are unpriced — run price first"

        out.append(StageState(
            stage=stage,
            status=status,
            job_id=j.id if j else None,
            progress_pct=j.progress_pct if j else 0,
            rows_processed=(j.rows_processed or 0) if j else 0,
            rows_total=j.rows_total if j else None,
            rows_filled=(j.rows_filled or 0) if j else 0,
            rows_missed=(j.rows_missed or 0) if j else 0,
            rows_skipped=(j.rows_skipped or 0) if j else 0,
            detail=j.stage if j else None,
            error=j.error_message if j else None,
            started_at=j.started_at if j else None,
            completed_at=j.completed_at if j else None,
            heartbeat_at=j.heartbeat_at if j else None,
            can_run=can,
            blocked_reason=blocked,
        ))
    return out


def enrich_coverage(db: Session, batch_id: int) -> dict:
    """Per-row enrich outcomes for the batch, independent of any job row.

    This is the answer to 'did CoreLogic actually finish?' that survives
    everything — it's derived from the listings themselves, so it is still
    correct even if the job row was lost or reaped.
    """
    counts = dict(
        db.query(PropertyForSale.enrich_status, func.count(PropertyForSale.id))
        .filter(PropertyForSale.import_batch_id == batch_id)
        .group_by(PropertyForSale.enrich_status).all()
    )
    total = sum(counts.values())
    done = total - counts.get("pending", 0)
    return {
        "total": total,
        "pending": counts.get("pending", 0),
        "filled": counts.get("filled", 0),
        "missed": counts.get("missed", 0),
        "skipped": counts.get("skipped", 0),
        "complete": counts.get("pending", 0) == 0 and total > 0,
        "pct_done": round(100 * done / total, 1) if total else 0.0,
    }
