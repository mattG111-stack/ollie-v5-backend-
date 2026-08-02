"""CLI ingest tool — bypasses the browser upload for large CSV files.

Usage:
  python scripts/ingest_csv.py for-sale "D:\\path\\to\\for_sale.csv"
  python scripts/ingest_csv.py sold "D:\\path\\to\\sold.csv"
  python scripts/ingest_csv.py rent "D:\\path\\to\\rent.csv"

For for-sale, the pricing pipeline needs a sold dataset. By default it uses the
currently-active sold batch from the DB. Override with --sold-csv if you want to
ingest both at once and use the new sold file as the comp set for the for-sale run.

  python scripts/ingest_csv.py for-sale "D:\\for_sale.csv" --sold-csv "D:\\sold.csv"
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal  # noqa: E402
from app.ingest import ingest_for_sale, ingest_rent, ingest_sold  # noqa: E402
from app.models import ImportBatch, PropertySold  # noqa: E402


def _load_active_sold(db):
    """Read the active sold batch from the DB back into a DataFrame for comp matching."""
    active = (
        db.query(ImportBatch)
        .filter(ImportBatch.batch_type == "sold", ImportBatch.is_active.is_(True))
        .order_by(ImportBatch.id.desc())
        .first()
    )
    if not active:
        raise SystemExit("No active sold batch in DB. Ingest sold first, or use --sold-csv.")
    print(f"  using sold batch #{active.id} from DB ({active.rows_inserted} rows)")
    rows = db.query(PropertySold).filter(PropertySold.import_batch_id == active.id).all()
    return pd.DataFrame([
        {
            "address": r.address, "name": r.name, "suburb": r.suburb,
            "district": r.district, "region": r.region,
            "property_type": r.property_type, "type_of_title": r.type_of_title,
            "zoning": r.zoning, "land_slope_contour": r.land_slope_contour,
            "key_bedrooms": r.beds, "key_bathrooms": r.baths, "key_carspaces": r.cars,
            "key_floor_area": r.floor_area_m2, "key_land_area": r.land_area_m2,
            "cv_numeric": r.cv_numeric, "land_value_numeric": r.land_value_numeric,
            "url": r.url, "slug_id": r.slug_id,
            "price_numeric": r.sale_price,
            "sold_listing_date": r.sold_date,
            "sale_method": r.sale_method,
        }
        for r in rows
    ])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=["for-sale", "sold", "rent"], help="Which dataset to ingest")
    ap.add_argument("csv_path", help="Path to the CSV file on disk")
    ap.add_argument("--sold-csv", help="(for-sale only) Use this CSV as the sold comp set instead of the active DB batch")
    ap.add_argument("--region", default="Auckland")
    args = ap.parse_args()

    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        raise SystemExit(f"File not found: {csv_path}")
    size_mb = csv_path.stat().st_size / 1024 / 1024

    print(f"Loading {csv_path.name} ({size_mb:.1f} MB) ...")
    t0 = time.time()
    df = pd.read_csv(csv_path, on_bad_lines="skip")
    print(f"  loaded {len(df):,} rows in {time.time() - t0:.1f}s (malformed lines auto-skipped)")

    db = SessionLocal()
    try:
        if args.kind == "sold":
            print("Ingesting as SOLD ...")
            t0 = time.time()
            r = ingest_sold(db, df, csv_path.name, region=args.region)
            print(f"  done in {time.time() - t0:.1f}s")
            print(f"  batch #{r.batch_id}: inserted={r.rows_inserted:,}, rejected={r.rows_rejected:,}")
            if r.notes:
                print(f"  note: {r.notes}")

        elif args.kind == "rent":
            print("Ingesting as RENT ...")
            t0 = time.time()
            r = ingest_rent(db, df, csv_path.name, region=args.region)
            print(f"  done in {time.time() - t0:.1f}s")
            print(f"  batch #{r.batch_id}: inserted={r.rows_inserted:,}, rejected={r.rows_rejected:,}")
            if r.notes:
                print(f"  note: {r.notes}")

        else:  # for-sale
            if args.sold_csv:
                print(f"Loading sold comp set from {args.sold_csv} ...")
                sold_df = pd.read_csv(args.sold_csv)
                print(f"  loaded {len(sold_df):,} sold rows")
            else:
                print("Loading sold comp set from active DB batch ...")
                sold_df = _load_active_sold(db)
            print(f"Ingesting as FOR-SALE ({len(df):,} rows) — this runs the pricing pipeline ...")
            t0 = time.time()
            r = ingest_for_sale(db, df, sold_df, csv_path.name, region=args.region)
            print(f"  done in {time.time() - t0:.1f}s")
            print(f"  batch #{r.batch_id}: inserted={r.rows_inserted:,}, rejected={r.rows_rejected:,}")
            if r.notes:
                print(f"  note: {r.notes}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
