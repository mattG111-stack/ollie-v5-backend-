"""Grab propertyvalue.co.nz (CoreLogic) data for our deal listings.

For every deal (under- or over-priced vs our fair value) in the active batch,
look up the property on propertyvalue.co.nz and cache the full record — their
AVM, council CV, zoning, attributes and, especially, the LAST SALE — onto the
listing (pv_* columns + pv_data JSON). This is the "grab it for all our deals"
batch; individual non-deal listings still fill in on-demand as users view them.

Bounded and polite by design: deals only, one lookup per property, rate-limited,
resumable (skips rows already checked), and it stops itself if CoreLogic starts
refusing (a long run of failures) so we never hammer them.

  python scripts/enrich_propertyvalue.py               # active batch, deals
  python scripts/enrich_propertyvalue.py --limit 50    # a sample
  python scripts/enrich_propertyvalue.py --all         # every listing, not just deals
  python scripts/enrich_propertyvalue.py --refresh     # re-check even if already done
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import or_, text            # noqa: E402
from app.db import SessionLocal             # noqa: E402
from app.models import PropertyForSale      # noqa: E402
from app.propertyvalue import gaps, missing_fills, pv_lookup  # noqa: E402

STOP_AFTER_CONSECUTIVE_FAILURES = 40        # circuit-breaker: likely being blocked


def _active_batch(db) -> int | None:
    return db.execute(text(
        "SELECT id FROM import_batches WHERE batch_type='for_sale' AND is_active "
        "ORDER BY id DESC LIMIT 1")).scalar()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=None)
    ap.add_argument("--limit", type=int, default=5000, help="max lookups this run")
    ap.add_argument("--overpriced-margin", type=float, default=0.05)
    ap.add_argument("--all", action="store_true", help="every listing, not just deals")
    ap.add_argument("--refresh", action="store_true", help="re-check rows already done")
    ap.add_argument("--delay", type=float, default=0.8, help="seconds between lookups")
    args = ap.parse_args()

    db = SessionLocal()
    batch = args.batch or _active_batch(db)
    if not batch:
        raise SystemExit("No active for-sale batch.")

    q = db.query(PropertyForSale).filter(
        PropertyForSale.import_batch_id == batch,
        PropertyForSale.address.isnot(None),
    )
    if not args.refresh:
        q = q.filter(PropertyForSale.pv_checked_at.is_(None))   # resumable
    if not args.all:
        q = q.filter(or_(
            PropertyForSale.is_underpriced.is_(True),
            PropertyForSale.margin <= -args.overpriced_margin,
        ))
    q = q.order_by(PropertyForSale.opportunity_score_pct.desc().nullslast())
    rows = q.limit(args.limit).all()

    print(f"propertyvalue.co.nz enrichment · batch #{batch} · {len(rows)} listings "
          f"· {args.delay}s apart · circuit-break after {STOP_AFTER_CONSECUTIVE_FAILURES} misses")
    hits = last_sales = gaps_found = filled = 0
    consec_fail = 0
    for i, p in enumerate(rows, 1):
        addr = ", ".join(x for x in (p.address, p.suburb, "Auckland") if x)
        pv = None
        try:
            pv = pv_lookup(addr)
        except Exception:
            pv = None

        if pv:
            consec_fail = 0
            hits += 1
            p.pv_estimate_low = pv.get("estimate_low")
            p.pv_estimate_high = pv.get("estimate_high")
            p.pv_estimate_mid = pv.get("estimate_mid")
            p.pv_cv = pv.get("cv")
            p.pv_url = pv.get("url")
            p.pv_last_sale_price = pv.get("last_sale_price")
            p.pv_last_sale_date = pv.get("last_sale_date")
            p.pv_data = json.dumps(pv)
            if pv.get("last_sale_price"):
                last_sales += 1
            ours = {"land_area_m2": p.land_area_m2, "floor_area_m2": p.floor_area_m2,
                    "beds": p.beds, "baths": p.baths, "cv": p.cv_numeric,
                    "zoning": p.zoning, "year_built": None, "last_sale_price": None}
            gaps_found += len(gaps(ours, pv))
            # Fill our blank attributes from CoreLogic (floor/land/beds/baths/cv/zoning).
            for k, v in missing_fills({
                "floor_area_m2": p.floor_area_m2, "land_area_m2": p.land_area_m2,
                "beds": p.beds, "baths": p.baths, "cv_numeric": p.cv_numeric, "zoning": p.zoning,
            }, pv).items():
                setattr(p, k, v)
                filled += 1
        else:
            consec_fail += 1

        p.pv_checked_at = datetime.now(timezone.utc)            # stamp even on a miss
        if i % 20 == 0:
            db.commit()
            print(f"  {i}/{len(rows)} · {hits} matched · {last_sales} w/ last-sale · {gaps_found} gaps they fill")
        if consec_fail >= STOP_AFTER_CONSECUTIVE_FAILURES:
            db.commit()
            print(f"\nStopped early after {consec_fail} consecutive misses — likely rate-limited. "
                  f"Re-run later to resume (already-done rows are skipped).")
            db.close()
            return
        time.sleep(args.delay)

    db.commit()
    print(f"\nDone — {hits}/{len(rows)} matched · {last_sales} last-sales grabbed · "
          f"{filled} blank fields filled from CoreLogic.")
    db.close()


if __name__ == "__main__":
    main()
