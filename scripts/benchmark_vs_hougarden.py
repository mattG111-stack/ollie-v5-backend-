"""Benchmark Hougarden's valuation against actual sale prices — and ours.

`validate_against_excel.py` measures us against the CLIENT'S SPREADSHEET, which
answers "do we reproduce their tool" — not "are we right". This measures against
what properties ACTUALLY SOLD FOR, which is the only ground truth there is, and
puts Hougarden's own estimate next to ours on the identical rows.

Sold records carry `hg_valuation` (Hougarden's estimate) alongside `sale_price`,
so the comparison needs no scraping and no external call.

    python scripts/benchmark_vs_hougarden.py
    python scripts/benchmark_vs_hougarden.py --batch 12
    python scripts/benchmark_vs_hougarden.py --by-suburb

Read the paired figures, not the headline. Hougarden's coverage is partial, so
its MdAPE over the rows it chose to answer is not comparable to ours over every
row — the PAIRED section restricts both to the same properties, and that is the
only honest comparison.
"""
from __future__ import annotations

import argparse
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import desc                      # noqa: E402

from app.db import SessionLocal                  # noqa: E402
from app.models import BatchType, ImportBatch, PropertySold  # noqa: E402
from app.pricing.buyprice import CompEngine      # noqa: E402


def ape(estimate: float, actual: float) -> float:
    return abs(estimate - actual) / actual


def summarise(errors: list[float], label: str, *, biases: list[float] | None = None) -> None:
    if not errors:
        print(f"  {label:26} no data")
        return
    n = len(errors)
    within = lambda t: 100 * sum(1 for e in errors if e <= t) / n  # noqa: E731
    line = (f"  {label:26} n={n:6,}  MdAPE={st.median(errors) * 100:6.2f}%  "
            f"MAPE={st.mean(errors) * 100:6.2f}%  "
            f"<=10%={within(0.10):5.1f}%  <=20%={within(0.20):5.1f}%")
    if biases:
        line += f"  bias={st.mean(biases) * 100:+6.2f}%"
    print(line)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, help="sold batch id (default: newest)")
    ap.add_argument("--by-suburb", action="store_true", help="break the paired comparison down")
    ap.add_argument("--min-price", type=float, default=100_000)
    args = ap.parse_args()

    db = SessionLocal()
    try:
        if args.batch:
            batch = db.get(ImportBatch, args.batch)
        else:
            batch = (db.query(ImportBatch)
                     .filter(ImportBatch.batch_type == BatchType.SOLD.value)
                     .order_by(desc(ImportBatch.id)).first())
        if not batch:
            print("No sold batch found. Load one first.")
            return

        rows = (db.query(PropertySold)
                .filter(PropertySold.import_batch_id == batch.id,
                        PropertySold.sale_price.isnot(None),
                        PropertySold.sale_price >= args.min_price)
                .all())
        print(f"Sold batch #{batch.id} — {batch.filename}")
        print(f"{len(rows):,} sold records with a sale price\n")

        # --- Hougarden, over whatever it covers -----------------------------
        hg_errors, hg_bias = [], []
        for p in rows:
            hg = p.third_party_valuation
            if hg and hg > 0:
                hg_errors.append(ape(hg, p.sale_price))
                hg_bias.append((hg - p.sale_price) / p.sale_price)

        coverage = 100 * len(hg_errors) / len(rows) if rows else 0
        print("HOUGARDEN, over the rows it covers")
        summarise(hg_errors, "hougarden", biases=hg_bias)
        print(f"  coverage: {len(hg_errors):,} of {len(rows):,} sold records ({coverage:.1f}%)\n")

        # --- Ours, on the identical rows ------------------------------------
        # Priced holdout-style: each property valued against the OTHER sales in
        # the batch. Valuing a property using its own sale price would be
        # circular and would flatter the result enormously.
        import pandas as pd

        sold_df = pd.DataFrame([{
            "address": p.address, "suburb": p.suburb, "district": p.district,
            "property_type": p.property_type, "type_of_title": p.type_of_title,
            "key_bedrooms": p.beds, "key_bathrooms": p.baths,
            "key_floor_area": p.floor_area_m2, "key_land_area": p.land_area_m2,
            "price_numeric": p.sale_price, "cv_numeric": p.cv_numeric,
            "land_value_numeric": p.land_value_numeric,
            "sold_date": p.sold_date, "days_on_market": p.days_on_market,
        } for p in rows])

        engine = CompEngine(sold_df)

        ours_all, ours_bias_all = [], []
        paired_ours, paired_hg = [], []
        by_suburb: dict[str, list[tuple[float, float]]] = {}

        for p in rows:
            if not p.cv_numeric or p.cv_numeric <= 0:
                continue
            ratio, _src = engine.cv_ratio_for(
                suburb=p.suburb, district=p.district,
                property_type=p.property_type, beds=p.beds, baths=p.baths,
                land=p.land_area_m2, title=p.type_of_title)
            ours = p.cv_numeric * ratio
            e = ape(ours, p.sale_price)
            ours_all.append(e)
            ours_bias_all.append((ours - p.sale_price) / p.sale_price)

            hg = p.third_party_valuation
            if hg and hg > 0:
                paired_ours.append(e)
                paired_hg.append(ape(hg, p.sale_price))
                by_suburb.setdefault(p.suburb or "?", []).append((e, ape(hg, p.sale_price)))

        print("OURS, over every row with a CV")
        summarise(ours_all, "ollie", biases=ours_bias_all)
        print()

        print("PAIRED — identical properties, the only fair comparison")
        summarise(paired_hg, "hougarden")
        summarise(paired_ours, "ollie")
        if paired_ours and paired_hg:
            wins = sum(1 for a, b in zip(paired_ours, paired_hg) if a < b)
            print(f"\n  ollie closer on {wins:,} of {len(paired_ours):,} "
                  f"({100 * wins / len(paired_ours):.1f}%)")
            d = st.median(paired_ours) - st.median(paired_hg)
            verdict = "ahead" if d < 0 else "behind"
            print(f"  median gap: {abs(d) * 100:.2f} points {verdict}")

        if args.by_suburb and by_suburb:
            print("\nBY SUBURB (>=20 paired sales)")
            table = []
            for sub, pairs in by_suburb.items():
                if len(pairs) < 20:
                    continue
                table.append((sub, len(pairs),
                              st.median([a for a, _ in pairs]),
                              st.median([b for _, b in pairs])))
            table.sort(key=lambda t: t[2] - t[3], reverse=True)
            print(f"  {'suburb':24} {'n':>5} {'ollie':>8} {'hougarden':>10}  worst gap first")
            for sub, n, o, h in table[:25]:
                print(f"  {sub:24} {n:5} {o * 100:7.2f}% {h * 100:9.2f}%")
    finally:
        db.close()


if __name__ == "__main__":
    main()
