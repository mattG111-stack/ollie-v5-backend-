"""Seed the database from existing local files.

Loads:
  - sold from Ollie Excel '2016 sold_output' tab (canonical training set)
  - for-sale from Ollie Excel 'for sale' tab
  - rentals from rent_input.csv

Runs the full pricing pipeline and writes everything to Supabase.
"""
from __future__ import annotations

import sys
import warnings as _w
from pathlib import Path

import pandas as pd

_w.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[3]
XLSX = ROOT / "Ollie property.xlsx"
RENT_CSV = ROOT / "rent_input.csv"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import SessionLocal  # noqa: E402
from app.ingest import ingest_for_sale, ingest_rent, ingest_sold  # noqa: E402


def main() -> int:
    print("Loading sold dataset ...")
    sold_df = pd.read_excel(XLSX, sheet_name="2016 sold_output")
    print(f"  {len(sold_df)} sold rows")

    print("Loading for-sale dataset ...")
    fs_df = pd.read_excel(XLSX, sheet_name="for sale")
    print(f"  {len(fs_df)} for-sale rows")

    print("Loading rentals ...")
    rent_df = pd.read_csv(RENT_CSV)
    print(f"  {len(rent_df)} rental rows")

    db = SessionLocal()
    try:
        print()
        print("Ingesting sold ...")
        r = ingest_sold(db, sold_df, "2016_sold_output.xlsx")
        print(f"  Batch #{r.batch_id}: inserted={r.rows_inserted}, rejected={r.rows_rejected}")

        print()
        print("Ingesting rent ...")
        r = ingest_rent(db, rent_df, "rent_input.csv")
        print(f"  Batch #{r.batch_id}: inserted={r.rows_inserted}, rejected={r.rows_rejected}")

        print()
        print("Ingesting for-sale (with pipeline) ...")
        r = ingest_for_sale(db, fs_df, sold_df, "ollie_for_sale.xlsx")
        print(f"  Batch #{r.batch_id}: inserted={r.rows_inserted}, rejected={r.rows_rejected}")
    finally:
        db.close()

    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
