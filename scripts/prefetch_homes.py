"""Weekly pre-fetch of homes.co.nz estimates for the listings that matter.

Runs in the pre-publish pass, after verify_batch.py. Pulls the homes.co.nz
estimate for the deal listings (subdividable + underpriced first) so they're
ready — and cross-checkable — before anyone opens them. Everything else fills in
on-demand as users view it (endpoint /external-estimates).

Deliberately CAPPED and rate-limited: Brave's free search tier is ~2,000 queries
/month, so we spend it on the high-value listings, not all 10k. Bump --limit if
you move to a paid tier.

  python scripts/prefetch_homes.py                  # active batch, deals, cap 400
  python scripts/prefetch_homes.py --limit 200
  python scripts/prefetch_homes.py --all            # any listing, not just deals
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text, or_        # noqa: E402
from app.db import SessionLocal         # noqa: E402
from app.config import settings         # noqa: E402
from app.models import PropertyForSale  # noqa: E402
from app.external_estimates import homes_estimate  # noqa: E402


def _active_batch(db) -> int | None:
    return db.execute(text(
        "SELECT id FROM import_batches WHERE batch_type='for_sale' AND is_active "
        "ORDER BY id DESC LIMIT 1")).scalar()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=None)
    ap.add_argument("--limit", type=int, default=2500, help="max lookups this run")
    ap.add_argument("--overpriced-margin", type=float, default=0.05,
                    help="asking this far above fair value counts as overpriced")
    ap.add_argument("--all", action="store_true", help="every listing, not just mispriced")
    ap.add_argument("--delay", type=float, default=1.2, help="seconds between lookups (rate limit)")
    args = ap.parse_args()

    if not settings.brave_api_key:
        print("WARNING: no BRAVE_API_KEY set — falling back to free search (will throttle).")

    db = SessionLocal()
    batch = args.batch or _active_batch(db)
    if not batch:
        raise SystemExit("No batch to prefetch.")

    q = db.query(PropertyForSale).filter(
        PropertyForSale.import_batch_id == batch,
        PropertyForSale.address.isnot(None),
        PropertyForSale.homes_checked_at.is_(None),   # not already fetched
    )
    if not args.all:
        # Only the mispriced listings — under- or over-priced vs our fair value.
        # These are where a second opinion (homes.co.nz) actually matters; the
        # rest fill in on-demand if a user opens them.
        q = q.filter(or_(
            PropertyForSale.is_underpriced.is_(True),
            PropertyForSale.margin <= -args.overpriced_margin,
        ))
    q = q.order_by(PropertyForSale.opportunity_score_pct.desc().nullslast())
    rows = q.limit(args.limit).all()

    print(f"Pre-fetching homes.co.nz for {len(rows)} listings in batch #{batch} "
          f"(cap {args.limit}, {args.delay}s apart)…")
    hits = 0
    for i, p in enumerate(rows, 1):
        addr = ", ".join(x for x in (p.address, p.suburb, "Auckland") if x)
        est = None
        try:
            est = homes_estimate(addr)
        except Exception:
            est = None
        if est:
            p.homes_valuation = est.get("value")
            p.homes_valuation_low = est.get("low")
            p.homes_valuation_high = est.get("high")
            p.homes_cv = est.get("cv")
            p.homes_url = est.get("url")
            hits += 1
        p.homes_checked_at = datetime.now(timezone.utc)
        if i % 20 == 0:
            db.commit()
            print(f"  {i}/{len(rows)} · {hits} matched")
        time.sleep(args.delay)
    db.commit()
    print(f"\nDone — {hits}/{len(rows)} matched a homes.co.nz estimate.")
    db.close()


if __name__ == "__main__":
    main()
