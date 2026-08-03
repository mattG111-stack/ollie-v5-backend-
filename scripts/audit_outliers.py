"""Audit the live for-sale dataset for algorithm outliers.

Pulls the active for-sale batch from Postgres, flags listings where the
estimate looks broken, and groups them by likely root cause. Writes a
markdown report.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.db import DB_URL  # noqa: E402

OUT_REPORT = Path(__file__).resolve().parents[2] / "OUTLIER_AUDIT.md"


def main():
    engine = create_engine(DB_URL)
    print("Connecting to DB ...")
    with engine.connect() as c:
        active_id = c.execute(text(
            "SELECT id FROM import_batches WHERE batch_type='for_sale' AND is_active LIMIT 1"
        )).scalar()
        if not active_id:
            print("No active for-sale batch."); return
        print(f"Active for-sale batch: #{active_id}")
        df = pd.read_sql(text(
            """
            SELECT id, slug_id, address, suburb, district, region, property_type, zoning,
                   beds, baths, floor_area_m2, land_area_m2, cv_numeric,
                   asking_price, market_value, predicted_list, predicted_days,
                   comps_used, confidence, pred_vs_cv, pred_vs_listing,
                   min_lot_m2, max_addl_lots, total_subdivided_value, uplift_vs_asking,
                   est_weekly_rent, est_gross_yield, annual_cashflow, cash_on_cash,
                   opportunity_score, opportunity_score_pct, best_strategy, best_net_gain,
                   is_underpriced, is_cashflow_positive, is_subdividable
            FROM properties_for_sale WHERE import_batch_id = :b
            """
        ), c, params={"b": active_id})

    n = len(df)
    print(f"Loaded {n:,} for-sale rows.")

    # Derived helpers
    df["ratio_est_to_ask"] = df.apply(
        lambda r: r["market_value"] / r["asking_price"] if r.get("asking_price") and r.get("market_value") else None,
        axis=1,
    )
    df["ratio_est_to_cv"] = df.apply(
        lambda r: r["market_value"] / r["cv_numeric"] if r.get("cv_numeric") and r.get("market_value") else None,
        axis=1,
    )
    df["subdiv_uplift_x"] = df.apply(
        lambda r: r["total_subdivided_value"] / r["asking_price"] if r.get("asking_price") and r.get("total_subdivided_value") else None,
        axis=1,
    )

    # === Buckets of outliers ===
    buckets: dict[str, pd.DataFrame] = {}

    # 1. Estimate vastly above asking (>3x) - the "Halswell" bug class
    b1 = df[df["ratio_est_to_ask"].notna() & (df["ratio_est_to_ask"] > 3.0)]
    buckets["1. estimate > 3x asking"] = b1

    # 2. Estimate vastly below asking (<0.3x) - opposite anomaly
    b2 = df[df["ratio_est_to_ask"].notna() & (df["ratio_est_to_ask"] < 0.3)]
    buckets["2. estimate < 0.3x asking"] = b2

    # 3. Subdivision uplift > 5x asking - the $130M bug class
    b3 = df[df["subdiv_uplift_x"].notna() & (df["subdiv_uplift_x"] > 5.0)]
    buckets["3. subdivision uplift > 5x asking"] = b3

    # 4. Chinese property type labels (catch-all for non-Latin types)
    chinese_types = df[df["property_type"].astype(str).str.contains(r"[一-鿿]", regex=True, na=False)]
    buckets["4. non-Latin (Chinese) property types"] = chinese_types

    # 5. Non-Auckland districts (data from outside our sold-comp coverage)
    if "district" in df.columns:
        non_akl = df[df["district"].notna() & ~df["district"].str.contains("Auckland|Manukau|North Shore|Waitakere|Rodney|Papakura|Franklin", case=False, na=False)]
        buckets["5. non-Auckland district"] = non_akl

    # 6. Listings flagged "Underpriced" but with confidence < medium
    b6 = df[(df["is_underpriced"] == True) & (df["confidence"].isin(["low", "insufficient"]))]
    buckets["6. underpriced flag on low-confidence estimate"] = b6

    # 7. Subdivision uplift on Rural zones
    rural = df[df["zoning"].astype(str).str.contains("Rural", na=False) & (df["is_subdividable"] == True)]
    buckets["7. subdividable on Rural zone"] = rural

    # 8. Zero beds AND zero baths AND zero floor — totally feature-less
    b8 = df[(df["beds"].fillna(0) == 0) & (df["baths"].fillna(0) == 0) & df["floor_area_m2"].isna()]
    buckets["8. featureless rows (no beds/baths/floor)"] = b8

    # 9. Asking < $50k (probably scraper junk values like "2026")
    b9 = df[df["asking_price"].notna() & (df["asking_price"] < 50000)]
    buckets["9. asking < $50k (probable junk)"] = b9

    # === Top offenders worth eyeballing ===
    worst_overpriced = df.nlargest(10, "ratio_est_to_ask")[["id", "address", "suburb", "property_type", "asking_price", "market_value", "ratio_est_to_ask", "confidence", "comps_used"]]
    worst_subdivision = df.nlargest(10, "subdiv_uplift_x")[["id", "address", "suburb", "zoning", "land_area_m2", "min_lot_m2", "max_addl_lots", "asking_price", "total_subdivided_value", "subdiv_uplift_x"]]

    # === Build the report ===
    lines: list[str] = []
    out = lines.append
    out("# Outlier audit — live for-sale dataset")
    out("")
    out(f"Active batch: **#{active_id}** · {n:,} rows analysed.")
    out("")
    out("## Outlier buckets")
    out("")
    out("| # | Bucket | Count | % of total |")
    out("|---|---|---:|---:|")
    for label, sub in buckets.items():
        out(f"| | {label} | {len(sub):,} | {len(sub)/n*100:.1f}% |")
    out("")

    # Cross-tabulation: which buckets overlap?
    out("## Bucket overlap (top combinations)")
    out("")
    out("Most rows fall into multiple buckets. Examples:")
    out("")
    chinese_and_overpriced = chinese_types["id"].isin(b1["id"]).sum() if len(chinese_types) else 0
    rural_and_uplift = rural["id"].isin(b3["id"]).sum() if len(rural) else 0
    non_akl_and_overpriced = (non_akl["id"].isin(b1["id"]).sum() if "district" in df.columns and len(non_akl) else 0)
    out(f"- Chinese type AND estimate >3× asking: **{chinese_and_overpriced:,}**")
    out(f"- Rural zone AND subdivision uplift >5×: **{rural_and_uplift:,}**")
    out(f"- Non-Auckland district AND estimate >3× asking: **{non_akl_and_overpriced:,}**")
    out("")

    out("## Top 10 most overpriced estimates")
    out("")
    out("| ID | Address | Suburb | Type | Asking | Estimate | Ratio | Confidence | Comps |")
    out("|---|---|---|---|---:|---:|---:|---|---:|")
    for _, r in worst_overpriced.iterrows():
        out(f"| {int(r['id'])} | {r['address']} | {r['suburb']} | {str(r['property_type'])[:25]} | "
            f"${r['asking_price']:,.0f} | ${r['market_value']:,.0f} | {r['ratio_est_to_ask']:.1f}× | "
            f"{r['confidence']} | {int(r['comps_used']) if r['comps_used'] else 0} |")
    out("")

    out("## Top 10 most absurd subdivision uplifts")
    out("")
    out("| ID | Address | Suburb | Zone | Land m² | Min lot | Addl lots | Asking | Subdiv value | Uplift × |")
    out("|---|---|---|---|---:|---:|---:|---:|---:|---:|")
    for _, r in worst_subdivision.iterrows():
        out(f"| {int(r['id'])} | {r['address']} | {r['suburb']} | {str(r['zoning'])[:30]} | "
            f"{r['land_area_m2']:,.0f} | {r['min_lot_m2']:,.0f} | {r['max_addl_lots']:.0f} | "
            f"${r['asking_price']:,.0f} | ${r['total_subdivided_value']:,.0f} | {r['subdiv_uplift_x']:.1f}× |")
    out("")

    # Confidence breakdown
    out("## Confidence tier distribution")
    out("")
    out("| Tier | Count | % |")
    out("|---|---:|---:|")
    conf_counts = df["confidence"].fillna("none").value_counts()
    for tier, count in conf_counts.items():
        out(f"| {tier} | {count:,} | {count/n*100:.1f}% |")
    out("")

    # Property type distribution among bucket 1
    out("## Property types in the 'estimate > 3x asking' bucket")
    out("")
    out("(Reveals which type labels are causing the most damage)")
    out("")
    out("| Type label | Count |")
    out("|---|---:|")
    for pt, count in b1["property_type"].fillna("(blank)").value_counts().head(20).items():
        out(f"| `{pt}` | {count} |")
    out("")

    OUT_REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written: {OUT_REPORT}")
    for label, sub in buckets.items():
        print(f"  {label}: {len(sub):,}")


if __name__ == "__main__":
    main()
