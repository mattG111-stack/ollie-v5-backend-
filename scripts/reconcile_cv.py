"""Reconcile our council value against homes.co.nz's, using the market as referee.

CV drives Ollie's valuation, so a wrong CV quietly mis-prices a listing (40
Raurenga: a $2.1M CV with no comps to correct it printed a $2.1M value). homes.co.nz
carries its own CV; when the two disagree we let the MARKET decide which is
credible — not raw CV vs asking (biased, since homes sell below CV), but
CV × the suburb's sale/CV ratio vs the asking price.

  expected_from_cv = cv * suburb_sale_ratio       # what that CV implies it sells for
  the CV whose expected value is closer to the asking price is the credible one.

When the market clearly backs homes' CV, OURS is suspect: flagged, and the deal
signal it drove (is_underpriced) is dropped so a bad CV can't mint a fake deal.
Empirically our CV wins ~90% of disagreements, so this mostly confirms ours and
catches the ~10% where we're the stale one.

  python scripts/reconcile_cv.py            # active batch
"""
from __future__ import annotations

import argparse
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text                # noqa: E402
from app.db import SessionLocal            # noqa: E402
from app.models import PropertyForSale     # noqa: E402

CV_DIVERGENCE = 0.20     # only reconcile when the two CVs disagree by this much
SUSPECT_MARGIN = 0.05    # homes' CV must be clearly closer to the market to win


def _suburb_ratios(db, sold_batch: int) -> tuple[dict[str, float], float]:
    rows = db.execute(text(
        "SELECT suburb, sale_price, cv_numeric FROM properties_sold "
        "WHERE import_batch_id=:b AND sale_price>0 AND cv_numeric>0"), {"b": sold_batch}).fetchall()
    by_sub: dict[str, list[float]] = {}
    allr: list[float] = []
    for sub, sale, cv in rows:
        r = sale / cv
        allr.append(r)
        if sub:
            by_sub.setdefault(sub.strip(), []).append(r)
    ratios = {s: st.median(v) for s, v in by_sub.items() if len(v) >= 5}
    return ratios, (st.median(allr) if allr else 0.92)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=None)
    args = ap.parse_args()
    db = SessionLocal()

    batch = args.batch or db.execute(text(
        "SELECT id FROM import_batches WHERE batch_type='for_sale' AND is_active "
        "ORDER BY id DESC LIMIT 1")).scalar()
    sold = db.execute(text(
        "SELECT id FROM import_batches WHERE batch_type='sold' AND is_active "
        "ORDER BY id DESC LIMIT 1")).scalar()
    if not batch or not sold:
        raise SystemExit("Need an active for-sale and sold batch.")

    ratios, global_ratio = _suburb_ratios(db, sold)

    rows = (db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch,
                    PropertyForSale.homes_cv.isnot(None),
                    PropertyForSale.cv_numeric.isnot(None),
                    PropertyForSale.asking_price.isnot(None))
            .all())

    checked = ours = suspect = 0
    suspects = []
    for p in rows:
        if not p.cv_numeric or not p.homes_cv or not p.asking_price:
            continue
        if abs(p.homes_cv / p.cv_numeric - 1) < CV_DIVERGENCE:
            continue                         # CVs agree — nothing to reconcile
        checked += 1
        r = ratios.get((p.suburb or "").strip(), global_ratio)
        exp_ours = p.cv_numeric * r
        exp_homes = p.homes_cv * r
        err_ours = abs(p.asking_price / exp_ours - 1)
        err_homes = abs(p.asking_price / exp_homes - 1)
        if err_homes < err_ours - SUSPECT_MARGIN:
            p.cv_flag = "suspect"            # the market sides with homes → ours is off
            p.is_underpriced = False         # don't let a suspect CV mint a deal
            suspect += 1
            suspects.append((p.address, p.cv_numeric, p.homes_cv, p.asking_price))
        else:
            p.cv_flag = "ok"                 # our CV is backed
            ours += 1
    db.commit()

    print(f"Reconciled {checked} divergent-CV listings in batch #{batch}:")
    print(f"  our CV backed by the market: {ours}")
    print(f"  OUR CV suspect (market backs homes): {suspect}")
    if suspects:
        print("\n  Suspect — our CV likely wrong (ourCV → homesCV, asking):")
        for a, ocv, hcv, ask in suspects[:25]:
            print(f"    {a[:30]:<30} ${int(ocv/1000)}k → ${int(hcv/1000)}k  (asking ${int(ask/1000)}k)")
    db.close()


if __name__ == "__main__":
    main()
