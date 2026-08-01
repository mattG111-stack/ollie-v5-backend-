"""Weekly pre-publish verification of a for-sale batch.

Fetches every listing's own page, reads the land area it actually shows, and
compares it to the scraped figure. Writes the verdict back to each row
(land_area_listing_m2 + land_area_flag) and, for a hard MISMATCH, drops the
subdivision flag — a wrong land area is where bad data does the most damage
(139 Long Drive: stored 5,665 m², real 416 m², a $5M phantom "gem").

Run this AFTER ingesting the weekly batch and BEFORE marking it active/live:

  python scripts/verify_batch.py                     # active batch, all listings
  python scripts/verify_batch.py --subdividable-only # just the risky ones (fast)
  python scripts/verify_batch.py --batch 88 --limit 500
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text          # noqa: E402
from app.db import SessionLocal      # noqa: E402
from app.models import PropertyForSale  # noqa: E402
from app.verify import verify_many   # noqa: E402


def _active_batch(db) -> int | None:
    return db.execute(text(
        "SELECT id FROM import_batches WHERE batch_type='for_sale' AND is_active "
        "ORDER BY id DESC LIMIT 1")).scalar()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--subdividable-only", action="store_true")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--tol", type=float, default=0.10, help="allowed land-area gap")
    args = ap.parse_args()

    db = SessionLocal()
    batch = args.batch or _active_batch(db)
    if not batch:
        raise SystemExit("No batch to verify.")

    q = db.query(PropertyForSale.id, PropertyForSale.url, PropertyForSale.land_area_m2) \
        .filter(PropertyForSale.import_batch_id == batch,
                PropertyForSale.url.isnot(None))
    if args.subdividable_only:
        q = q.filter(PropertyForSale.is_subdividable.is_(True))
    if args.limit:
        q = q.limit(args.limit)
    items = [(r.id, r.url, r.land_area_m2) for r in q.all()]
    print(f"Verifying {len(items)} listings in batch #{batch} "
          f"(concurrency={args.concurrency}, tol={args.tol:.0%})…")

    t0 = time.time()
    checks = asyncio.run(verify_many(items, concurrency=args.concurrency, tol=args.tol))

    counts = {"ok": 0, "mismatch": 0, "unverified": 0}
    mismatches = []
    for c in checks:
        counts[c.status] += 1
        row = db.get(PropertyForSale, c.id)
        row.land_area_listing_m2 = c.listing
        row.land_area_flag = c.status
        if c.status == "mismatch":
            mismatches.append(c)
            row.is_subdividable = False        # bad area → not a credible subdivision
    db.commit()

    dt = time.time() - t0
    print(f"\nDone in {dt:.0f}s — ok={counts['ok']}  "
          f"MISMATCH={counts['mismatch']}  unverified={counts['unverified']}")
    if mismatches:
        print("\nFlagged mismatches (stored → listing):")
        for c in sorted(mismatches, key=lambda c: -(c.stored or 0))[:40]:
            row = db.get(PropertyForSale, c.id)
            print(f"  #{c.id} {row.address[:30]:<30} stored={c.stored} → listing={c.listing}")
    db.close()


if __name__ == "__main__":
    main()
