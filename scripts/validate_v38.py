"""Validate our v3.5/v3.8 port against the client's Full Score sheet.

For each of the ~2,100 sold properties in the client's Full Score sheet:
  - Build the property dict from the input columns
  - Run our predict() to get pred_v35
  - Compare against his published "Pred v3.5" column
  - Pass if 95% of rows match within +/- 5%

Also benchmarks the model against actual Sale Price (his stated MAPE 7.46%).
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pricing.glm import predict  # noqa: E402

XLSX = Path(r"C:\Users\hamza\Downloads\Algo data 17-05-2026.xlsx")


def main():
    print(f"Loading {XLSX.name} -> Full Score ...")
    df = pd.read_excel(XLSX, sheet_name="Full Score")
    print(f"  {len(df)} sold rows\n")

    print("Running our predictor on every row ...")
    our_v35 = []
    our_v38 = []
    for _, r in df.iterrows():
        p = predict(
            suburb=r.get("Suburb"),
            district=r.get("District"),
            property_type=r.get("Type"),
            cv=r.get("CV"),
            floor=r.get("Floor"),
            land=r.get("Land"),
            beds=r.get("Beds"),
            baths=r.get("Baths"),
            cars=r.get("Cars"),
            age=r.get("Age"),
            title=r.get("Title"),
            method=r.get("Method"),
            pool=bool(r.get("Pool") in ("Y", "y", True, 1)),
            address=r.get("Addr"),
        )
        our_v35.append(p.pred_v35)
        our_v38.append(p.pred_v38)
    df["our_v35"] = our_v35
    df["our_v38"] = our_v38

    # Compare to his Pred v3.5
    mask = df["our_v35"].notna() & df["Pred v3.5"].notna() & (df["Pred v3.5"] > 0)
    sub = df[mask].copy()
    sub["diff_pct"] = (sub["our_v35"] - sub["Pred v3.5"]) / sub["Pred v3.5"]
    sub["abs_diff_pct"] = sub["diff_pct"].abs()
    within_1 = (sub["abs_diff_pct"] <= 0.01).mean()
    within_5 = (sub["abs_diff_pct"] <= 0.05).mean()
    within_10 = (sub["abs_diff_pct"] <= 0.10).mean()

    print(f"\n=== Match: our v3.5 vs client's Pred v3.5 ===")
    print(f"  Rows compared: {len(sub):,}")
    print(f"  Median abs diff: {sub['abs_diff_pct'].median()*100:.2f}%")
    print(f"  Mean  abs diff: {sub['abs_diff_pct'].mean()*100:.2f}%")
    print(f"  Within +/-1%:  {within_1*100:.1f}%")
    print(f"  Within +/-5%:  {within_5*100:.1f}%   <-- target 95%")
    print(f"  Within +/-10%: {within_10*100:.1f}%")

    if within_5 < 0.95:
        print(f"\n  FAILED strict target. Showing top 10 worst rows:")
        worst = sub.nlargest(10, "abs_diff_pct")[["Addr", "Suburb", "Type", "CV", "Pred v3.5", "our_v35", "abs_diff_pct", "Tier"]]
        print(worst.to_string())

    # Compare both predictions to actual sale price (his stated 7.46% benchmark)
    print(f"\n=== Accuracy vs actual sale price (his MAPE benchmark 7.46%) ===")
    actual_mask = df["Sale Price"].notna() & (df["Sale Price"] > 0)
    a = df[actual_mask & df["Pred v3.5"].notna()].copy()
    a["his_v35_ape"] = (a["Pred v3.5"] - a["Sale Price"]).abs() / a["Sale Price"]
    print(f"  Client's Pred v3.5 MAPE: {a['his_v35_ape'].mean()*100:.2f}% (n={len(a)})")
    print(f"  Client's Pred v3.5 median APE: {a['his_v35_ape'].median()*100:.2f}%")

    b = df[actual_mask & df["our_v35"].notna()].copy()
    b["our_v35_ape"] = (b["our_v35"] - b["Sale Price"]).abs() / b["Sale Price"]
    print(f"  Our v3.5 MAPE: {b['our_v35_ape'].mean()*100:.2f}% (n={len(b)})")
    print(f"  Our v3.5 median APE: {b['our_v35_ape'].median()*100:.2f}%")

    c = df[actual_mask & df["our_v38"].notna()].copy()
    c["our_v38_ape"] = (c["our_v38"] - c["Sale Price"]).abs() / c["Sale Price"]
    print(f"  Our v3.8 MAPE: {c['our_v38_ape'].mean()*100:.2f}% (n={len(c)})")
    print(f"  Our v3.8 median APE: {c['our_v38_ape'].median()*100:.2f}%")

    # Tier breakdown
    print(f"\n=== Our predictions by beta tier used ===")
    df["_tier"] = df.apply(lambda r: "?" if pd.isna(r["our_v35"]) else "ok", axis=1)
    # Re-run for tier diagnostics
    tier_counts = {}
    for _, r in df.iterrows():
        p = predict(
            suburb=r.get("Suburb"), district=r.get("District"),
            property_type=r.get("Type"), cv=r.get("CV"),
            floor=r.get("Floor"), land=r.get("Land"),
            beds=r.get("Beds"), baths=r.get("Baths"),
            cars=r.get("Cars"), age=r.get("Age"),
            title=r.get("Title"), method=r.get("Method"),
            pool=bool(r.get("Pool") in ("Y", "y", True, 1)),
            address=r.get("Addr"),
        )
        tier_counts[p.beta_tier] = tier_counts.get(p.beta_tier, 0) + 1
    for tier, n in sorted(tier_counts.items(), key=lambda x: -x[1]):
        print(f"  {tier}: {n:,}")


if __name__ == "__main__":
    main()
