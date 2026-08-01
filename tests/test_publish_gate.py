"""Nothing goes live unless it is both well-evidenced and sane.

Two gates, because neither catches what the other does:

  confidence  — how many comps stood behind the number
  divergence  — how far the number sits from the asking price

The second matters more. `confidence` is a comp COUNT, not an accuracy measure:
501/125 Customs Street West carried HIGH confidence and was 8.5x the asking
price, because it had plenty of comps and they were the wrong tenure. A
confidence floor on its own would have published it.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/unused")
os.environ.setdefault("JWT_SECRET", "test-secret")


@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _row(**kw):
    from app.models import PropertyForSale

    base = dict(
        import_batch_id=1, address="1 Test Road", suburb="Riverhead",
        property_type="House", floor_area_m2=140.0, land_area_m2=600.0,
        cv_numeric=1_000_000.0, asking_price=1_000_000.0,
        fair_value=980_000.0, confidence="high",
    )
    base.update(kw)
    return PropertyForSale(**base)


def test_clean_row_publishes():
    from app.release import _hold_reason
    assert _hold_reason(_row()) is None


def test_thin_comps_do_not_block_a_sane_number():
    """A low comp count is not itself a fault. Only 'insufficient' — no comps at
    all — blocks, and only because there is nothing behind the number."""
    from app.release import _hold_reason
    assert _hold_reason(_row(confidence="low")) is None
    assert _hold_reason(_row(confidence="medium")) is None
    assert _hold_reason(_row(confidence="insufficient")) is not None


def test_no_valuation_is_held():
    """The pipeline declining to value a row is a decision, not a gap to fill."""
    from app.release import _hold_reason
    assert _hold_reason(_row(fair_value=None, market_value=None)) is not None


def test_confidently_wrong_row_is_still_held():
    """The Customs Street case: HIGH confidence, 8.5x the asking price.

    This is the one a confidence floor alone would miss, and the reason the
    divergence gate exists.
    """
    from app.release import _hold_reason

    reason = _hold_reason(_row(
        address="501/125 Customs Street West", suburb="Auckland Central",
        property_type="Residential - Apartments",
        asking_price=79_000.0, fair_value=673_000.0, confidence="high",
    ))
    assert reason is not None, "a high-confidence 8.5x error was published"
    assert "8.5x" in reason


def test_wildly_undervalued_row_is_held():
    """Under a third of asking is our error, not a bargain."""
    from app.release import _hold_reason
    reason = _hold_reason(_row(asking_price=1_000_000.0, fair_value=200_000.0))
    assert reason is not None and "asking price" in reason


def test_real_margins_are_not_eaten():
    """The band must be wide enough to leave the actual product intact."""
    from app.release import _hold_reason
    for fv in (1_150_000.0, 1_500_000.0, 2_000_000.0, 2_900_000.0):
        assert _hold_reason(_row(asking_price=1_000_000.0, fair_value=fv)) is None, \
            f"a {fv / 1e6:.1f}x valuation was held"
    for fv in (700_000.0, 500_000.0, 400_000.0):
        assert _hold_reason(_row(asking_price=1_000_000.0, fair_value=fv)) is None, \
            f"a {fv / 1e6:.1f}x valuation was held"


def test_band_edges():
    """3x and 1/3 are the boundaries."""
    from app.release import _hold_reason
    assert _hold_reason(_row(asking_price=1_000_000.0, fair_value=2_999_000.0)) is None
    assert _hold_reason(_row(asking_price=1_000_000.0, fair_value=3_100_000.0)) is not None
    assert _hold_reason(_row(asking_price=1_000_000.0, fair_value=340_000.0)) is None
    assert _hold_reason(_row(asking_price=1_000_000.0, fair_value=300_000.0)) is not None


def test_row_without_asking_price_relies_on_confidence_alone():
    """Auction and tender listings have no asking price to diverge from."""
    from app.release import _hold_reason
    assert _hold_reason(_row(asking_price=None, confidence="high")) is None
    assert _hold_reason(_row(asking_price=None, confidence="insufficient")) is not None


def test_hold_flagged_rows_marks_the_batch(db):
    from app.models import ImportBatch
    from app.release import hold_flagged_rows

    b = ImportBatch(batch_type="for_sale", region="Auckland", filename="x.csv")
    db.add(b)
    db.commit()

    good = _row(import_batch_id=b.id)
    bad_conf = _row(import_batch_id=b.id, confidence="insufficient")
    bad_div = _row(import_batch_id=b.id, asking_price=79_000.0, fair_value=673_000.0)
    db.add_all([good, bad_conf, bad_div])
    db.commit()

    assert hold_flagged_rows(db, b.id) == 2
    db.expire_all()
    assert good.is_held is False
    assert bad_conf.is_held is True
    assert bad_div.is_held is True
    assert bad_div.hold_reason is not None
