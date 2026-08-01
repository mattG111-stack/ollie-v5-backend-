"""Functional tests for the four-stage ingest pipeline.

The properties these prove are the ones that failed in production:
  1. LOAD makes no external calls and produces an inspectable grid immediately.
  2. ENRICH progress is in the DATABASE, so it survives the client disconnecting.
  3. ENRICH is RESUMABLE — a run killed at 60% resumes, it does not restart.
  4. 'missed' is recorded distinctly from 'failed'.
  5. CANCEL is honoured, keeps completed work, and writes a terminal state.
  6. A worker killed without writing a terminal state is reaped, not left alive.
  7. PUBLISH refuses an unpriced batch.
"""
from __future__ import annotations

import os
import tempfile

import pandas as pd
import pytest

# app.db builds a pooled engine at import time; sqlite rejects those pool kwargs.
# Point the module-level engine at a postgres URL it never connects to, and give
# each test its own in-memory sqlite engine instead.
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/unused")
os.environ.setdefault("JWT_SECRET", "test-secret")


@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _csv(rows: int = 10) -> pd.DataFrame:
    """A for-sale frame where every row is missing floor area — i.e. every row
    is an enrich candidate."""
    return pd.DataFrame([{
        "address": f"{i} Test Road",
        "suburb": "Riverhead",
        "region": "Auckland",
        "property_type": "House",
        "key_bedrooms": 3,
        "key_bathrooms": 1,
        "key_floor_area": None,          # blank → needs CoreLogic
        "key_land_area": None,
        "cv_numeric": 1_000_000,
        "price_numeric": 1_050_000,
        "price_display": "$1,050,000",
        "slug_id": f"slug-{i}",
    } for i in range(rows)])


# --------------------------------------------------------------------------
def test_load_is_offline_and_immediate(db, monkeypatch):
    """LOAD must not touch CoreLogic. If it does, this blows up."""
    import app.propertyvalue as pv
    monkeypatch.setattr(pv, "pv_lookup", lambda *a, **k: pytest.fail("LOAD called CoreLogic"))

    from app import ingest
    r = ingest.load_for_sale(db, _csv(10), "test.csv", region="Auckland")

    assert r.rows_inserted == 10

    from app.models import PropertyForSale
    rows = db.query(PropertyForSale).filter(
        PropertyForSale.import_batch_id == r.batch_id).all()
    assert len(rows) == 10
    # Loaded but not enriched and not priced — the resting state between stages.
    assert all(p.enrich_status == "pending" for p in rows)
    assert all(p.priced_at is None for p in rows)
    assert all(p.fair_value is None for p in rows)
    # The attributes ARE there, so the grid is populated straight after load.
    assert all(p.address and p.suburb for p in rows)


def test_load_defers_soft_rejects_to_price(db):
    """A row with no floor area must survive LOAD — it is exactly the row ENRICH
    exists to repair. The old welded ingest could drop it only because CoreLogic
    had already run in the same function."""
    from app import ingest
    df = _csv(3)
    r = ingest.load_for_sale(db, df, "test.csv")
    assert r.rows_inserted == 3, "rows with blank floor area were dropped at load"


def test_load_still_applies_hard_rejects(db):
    """Rules no lookup can flip still fire at load."""
    from app import ingest
    df = _csv(4)
    df.loc[0, "price_numeric"] = 1          # placeholder asking
    df.loc[1, "suburb"] = None              # unmatchable
    df.loc[2, "region"] = "Canterbury"      # outside the sold dataset
    r = ingest.load_for_sale(db, df, "test.csv")
    assert r.rows_inserted == 1
    assert r.rows_rejected == 3


# --------------------------------------------------------------------------
def test_enrich_progress_is_durable_and_resumable(db, monkeypatch):
    """The core guarantee. Kill enrich part-way; the completed portion is on
    disk, and re-running resumes rather than re-paying for lookups."""
    from app import ingest, stages
    from app.models import IngestJob, PropertyForSale

    r = ingest.load_for_sale(db, _csv(10), "test.csv")

    calls = {"n": 0}

    def fake_lookup(q, **kw):
        calls["n"] += 1
        if calls["n"] > 6:                       # simulate the worker dying
            raise KeyboardInterrupt("container replaced")
        return {"floor_area_m2": 140.0, "land_area_m2": 600.0}

    monkeypatch.setattr("app.propertyvalue.pv_lookup", fake_lookup)
    monkeypatch.setattr(stages, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    job = stages.new_job(db, stage_name=stages.ENRICH, batch_type="for_sale",
                         filename="test.csv", batch_id=r.batch_id)

    with pytest.raises(KeyboardInterrupt):
        stages.run_enrich(job.id, r.batch_id, delay=0)

    db.expire_all()
    done = db.query(PropertyForSale).filter(
        PropertyForSale.import_batch_id == r.batch_id,
        PropertyForSale.enrich_status != "pending").count()
    assert done == 6, "completed rows were not persisted before the crash"

    # ---- resume ----
    calls["n"] = 0

    def ok_lookup(q, **kw):
        calls["n"] += 1
        return {"floor_area_m2": 140.0, "land_area_m2": 600.0}

    monkeypatch.setattr("app.propertyvalue.pv_lookup", ok_lookup)
    job2 = stages.new_job(db, stage_name=stages.ENRICH, batch_type="for_sale",
                          filename="test.csv", batch_id=r.batch_id)
    stages.run_enrich(job2.id, r.batch_id, delay=0)

    assert calls["n"] == 4, f"resume re-did work: {calls['n']} lookups for 4 remaining rows"

    db.expire_all()
    j2 = db.get(IngestJob, job2.id)
    assert j2.status == "completed"
    assert j2.rows_total == 4          # denominator is the REMAINING work
    assert j2.rows_filled == 4
    assert j2.completed_at is not None
    assert j2.started_at is not None

    cov = stages.enrich_coverage(db, r.batch_id)
    assert cov["complete"] is True
    assert cov["pending"] == 0
    assert cov["filled"] == 10


def test_enrich_records_missed_separately_from_failed(db, monkeypatch):
    """CoreLogic returning nothing is a normal outcome, not an error."""
    from app import ingest, stages
    from app.models import IngestJob

    r = ingest.load_for_sale(db, _csv(6), "test.csv")

    seq = [None, {"floor_area_m2": 100.0}, None,
           {"floor_area_m2": 120.0}, None, {"floor_area_m2": 90.0}]
    it = iter(seq)
    monkeypatch.setattr("app.propertyvalue.pv_lookup", lambda q, **k: next(it))
    monkeypatch.setattr(stages, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    job = stages.new_job(db, stage_name=stages.ENRICH, batch_type="for_sale",
                         filename="test.csv", batch_id=r.batch_id)
    stages.run_enrich(job.id, r.batch_id, delay=0)

    db.expire_all()
    j = db.get(IngestJob, job.id)
    assert j.status == "completed", "misses must not fail the stage"
    assert j.rows_filled == 3
    assert j.rows_missed == 3
    assert j.rows_processed == 6


def test_cancel_is_honoured_and_keeps_work(db, monkeypatch):
    from app import ingest, stages
    from app.models import IngestJob, PropertyForSale

    r = ingest.load_for_sale(db, _csv(10), "test.csv")
    monkeypatch.setattr(stages, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    job = stages.new_job(db, stage_name=stages.ENRICH, batch_type="for_sale",
                         filename="test.csv", batch_id=r.batch_id)

    n = {"i": 0}

    def lookup_then_cancel(q, **k):
        n["i"] += 1
        if n["i"] == 4:      # operator hits Cancel mid-run
            db.query(IngestJob).filter(IngestJob.id == job.id).update(
                {"cancel_requested": True})
            db.commit()
        return {"floor_area_m2": 130.0}

    monkeypatch.setattr("app.propertyvalue.pv_lookup", lookup_then_cancel)
    stages.run_enrich(job.id, r.batch_id, delay=0)

    db.expire_all()
    j = db.get(IngestJob, job.id)
    assert j.status == "cancelled"
    assert j.completed_at is not None, "cancelled jobs need a terminal timestamp"
    assert j.rows_processed == 4

    kept = db.query(PropertyForSale).filter(
        PropertyForSale.import_batch_id == r.batch_id,
        PropertyForSale.enrich_status == "filled").count()
    assert kept == 4, "cancelling threw away completed work"


def test_stale_running_job_is_reaped(db):
    """A worker killed without writing a terminal state must not sit at 62%
    looking alive. This is the case the old percentage bar could never cover."""
    from datetime import timedelta

    from app import stages
    from app.models import ImportBatch, IngestJob

    b = ImportBatch(batch_type="for_sale", region="Auckland", filename="x.csv")
    db.add(b); db.commit()

    j = IngestJob(stage_name=stages.ENRICH, batch_type="for_sale", filename="x.csv",
                  batch_id=b.id, status="running", progress_pct=62,
                  rows_processed=3100, rows_total=5000,
                  heartbeat_at=stages._now() - timedelta(minutes=45))
    db.add(j); db.commit()

    assert stages.reap_stale_jobs(db) == 1
    db.expire_all()
    j = db.get(IngestJob, j.id)
    assert j.status == "failed"
    assert j.completed_at is not None
    assert "No heartbeat" in j.error_message
    assert "3100 rows was saved" in j.error_message.replace(",", "")


def test_fresh_running_job_is_not_reaped(db):
    from app import stages
    from app.models import ImportBatch, IngestJob

    b = ImportBatch(batch_type="for_sale", region="Auckland", filename="x.csv")
    db.add(b); db.commit()
    j = IngestJob(stage_name=stages.ENRICH, batch_type="for_sale", filename="x.csv",
                  batch_id=b.id, status="running", heartbeat_at=stages._now())
    db.add(j); db.commit()
    assert stages.reap_stale_jobs(db) == 0


def test_publish_refuses_unpriced_batch(db, monkeypatch):
    """The gate that stops a +546% valuation batch reaching users."""
    from app import ingest, stages
    from app.models import IngestJob

    r = ingest.load_for_sale(db, _csv(5), "test.csv")
    monkeypatch.setattr(stages, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    job = stages.new_job(db, stage_name=stages.PUBLISH, batch_type="for_sale",
                         filename="test.csv", batch_id=r.batch_id)
    stages.run_publish(job.id, r.batch_id)

    db.expire_all()
    j = db.get(IngestJob, job.id)
    assert j.status == "failed"
    assert "never been priced" in j.error_message


def test_stage_states_gate_correctly(db):
    from app import ingest, stages

    r = ingest.load_for_sale(db, _csv(5), "test.csv")
    st = {s.stage: s for s in stages.batch_stage_states(db, r.batch_id)}

    assert st["enrich"].can_run is True
    assert st["price"].can_run is True
    assert st["publish"].can_run is False
    assert "unpriced" in st["publish"].blocked_reason
