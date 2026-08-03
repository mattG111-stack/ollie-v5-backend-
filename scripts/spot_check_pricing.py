"""Spot-check: 5 rows from client's Excel 'for sale' tab vs our Python valuation.

If our numbers land within +/- 5% of client's Market_Value on these 5 rows,
the comp-matching logic is faithful and we can proceed to the full validation gate.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import warnings as _w

_w.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[3]
XLSX = ROOT / "Ollie property.xlsx"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.pricing.comps import SoldDataset, valuate  # noqa: E402


def main() -> int:
    print("Loading 2016 sold_output ...")
    sold_df = pd.read_excel(XLSX, sheet_name="2016 sold_output")
    print(f"  {len(sold_df)} sold rows")

    print("Loading for sale ...")
    fs_df = pd.read_excel(XLSX, sheet_name="for sale")
    print(f"  {len(fs_df)} for-sale rows")

    print("Building sold index ...")
    sold = SoldDataset(sold_df)

    # Pick 5 rows that have both Market_Value and the inputs we need.
    sample = fs_df.dropna(subset=["Market_Value", "suburb", "key_bedrooms", "key_floor_area"]).head(5)

    print()
    print(f"{'Address':<32} {'Suburb':<18} {'Type':<32} {'Beds':<5} {'Floor':<7} {'Theirs':>12} {'Ours':>12} {'Diff %':>8} {'Comps':>6} {'Method':<14}")
    print("-" * 165)

    all_within = True
    for _, row in sample.iterrows():
        v = valuate(
            suburb=row["suburb"],
            property_type=row.get("property_type"),
            beds=row["key_bedrooms"],
            baths=row.get("key_bathrooms"),
            floor_area=row.get("key_floor_area"),
            land_area=row.get("key_land_area"),
            cv=row.get("cv_numeric"),
            sold=sold,
        )
        their_mv = float(row["Market_Value"])
        our_mv = v.market_value
        diff_pct = (our_mv - their_mv) / their_mv * 100 if our_mv else None
        within = abs(diff_pct) <= 5 if diff_pct is not None else False
        if not within:
            all_within = False
        addr = str(row.get("address", "?"))[:30]
        sub = str(row["suburb"])[:16]
        typ = str(row.get("property_type", ""))[:30]
        print(
            f"{addr:<32} {sub:<18} {typ:<32} "
            f"{int(row['key_bedrooms']):<5} {int(row['key_floor_area']):<7} "
            f"${their_mv/1000:>10,.0f}k ${(our_mv or 0)/1000:>10,.0f}k "
            f"{diff_pct:>7.1f}%" + (" " if diff_pct is None else "") +
            f" {v.comps_used:>6} {v.method:<14}"
        )

    print()
    if all_within:
        print("All 5 rows within +/- 5% of client's Excel. Proceed to full validation.")
        return 0
    print("Some rows exceed +/- 5%. Investigate before proceeding.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
