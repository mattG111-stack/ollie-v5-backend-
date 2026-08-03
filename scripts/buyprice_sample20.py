"""Worked buy-price examples on 20 real listings, with source URLs for verification.

For each subject (a Hougarden for-sale listing) it runs the full cascade comp
search against the realestate.co.nz sold set, computes the area value and buy
price, and writes a CSV the client can cross-check against both websites.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, text  # noqa: E402
from app.db import DB_URL  # noqa: E402
from app.pricing.glm import predict, canonical_type  # noqa: E402

e = create_engine(DB_URL)
FB = "(SELECT id FROM import_batches WHERE batch_type='for_sale' AND is_active LIMIT 1)"
SB = "(SELECT id FROM import_batches WHERE batch_type='sold' AND is_active LIMIT 1)"

DISCOUNT = 0.95


def v4_value(row) -> float | None:
    """v4 model value (hedonic + corrections) for a sold comp."""
    yb = row.get("building_age")
    age = None
    try:
        yb = float(yb)
        age = 2026 - int(yb) if 1800 <= yb <= 2030 else (yb if 0 < yb < 200 else None)
    except (TypeError, ValueError):
        pass
    p = predict(
        suburb=row.get("suburb"), district=row.get("district"),
        property_type=row.get("property_type"), cv=row.get("cv_numeric"),
        floor=row.get("floor_area_m2"), land=row.get("land_area_m2"),
        beds=row.get("beds"), baths=row.get("baths"), cars=row.get("cars"),
        age=age, title=row.get("type_of_title"), method=None, pool=False,
        address=row.get("address"),
    )
    # pure model value = pred_v35 (no asking path)
    return p.pred_v35


def main() -> None:
    fs = pd.read_sql(text(f"""
        SELECT id, address, suburb, district, property_type, beds, baths, cars,
               land_area_m2, floor_area_m2, cv_numeric, asking_price, fair_value,
               hg_valuation AS third_party_valuation, url
        FROM properties_for_sale WHERE import_batch_id={FB}
          AND asking_price > 0 AND cv_numeric > 0 AND fair_value IS NOT NULL
        ORDER BY id LIMIT 400
    """), e)

    sd = pd.read_sql(text(f"""
        SELECT address, suburb, district, property_type, beds, baths, cars,
               land_area_m2, floor_area_m2, sale_price, cv_numeric, type_of_title,
               building_age, sold_date, url
        FROM properties_sold WHERE import_batch_id={SB} AND sold_date LIKE '%2026'
    """), e)
    sd["ct"] = sd["property_type"].map(canonical_type)
    # arm's-length: drop freehold houses sold under 75% of CV
    excl = (sd["type_of_title"].astype(str).str.lower().str.contains("freehold")) & \
           (sd["cv_numeric"] > 0) & (sd["sale_price"] < 0.75 * sd["cv_numeric"])
    sd = sd[~excl].copy()
    fs["ct"] = fs["property_type"].map(canonical_type)

    gsub = {k: g for k, g in sd.groupby(["suburb", "ct"])}
    gdist = {k: g for k, g in sd.groupby(["district", "ct"])}

    def find_comps(r):
        b, ba, la = r["beds"], r["baths"], r["land_area_m2"]
        g = gsub.get((r["suburb"], r["ct"]))
        if g is not None:
            if pd.notna(b) and pd.notna(ba) and pd.notna(la):
                m = g[(g["beds"].sub(b).abs() <= 1) & (g["baths"].sub(ba).abs() <= 1) & (g["land_area_m2"].between(0.85*la, 1.15*la))]
                if len(m) >= 2: return 1, m
            if pd.notna(b) and pd.notna(la):
                m = g[(g["beds"].sub(b).abs() <= 1) & (g["land_area_m2"].between(0.85*la, 1.15*la))]
                if len(m) >= 2: return 2, m
            if pd.notna(b):
                m = g[g["beds"].sub(b).abs() <= 1]
                if len(m) >= 2: return 3, m
            if len(g) >= 2: return 4, g
        gd = gdist.get((r["district"], r["ct"]))
        if gd is not None:
            if pd.notna(b):
                m = gd[gd["beds"].sub(b).abs() <= 1]
                if len(m) >= 2: return 5, m
            if len(gd) >= 2: return 6, gd
        return 0, None

    rows, comp_rows = [], []
    picked = 0
    for _, r in fs.iterrows():
        if picked >= 20:
            break
        tier, comps = find_comps(r)
        if comps is not None and len(comps) > 12:
            comps = comps.head(12)
        # area value
        if comps is not None:
            ratios = []
            for _, c in comps.iterrows():
                cv4 = v4_value(c)
                if cv4 and cv4 > 0:
                    ratio = c["sale_price"] / cv4
                    # Guard: a sale/v4 ratio outside [0.5, 2.0] means the comp's
                    # own v4 is unreliable (garbage CV in the sold record) — skip it.
                    if 0.5 <= ratio <= 2.0:
                        ratios.append(ratio)
            area = r["fair_value"] * (sum(ratios) / len(ratios)) if ratios else r["fair_value"]
        else:
            area = r["fair_value"]
        buy = DISCOUNT * min(r["asking_price"], area)
        rows.append({
            "address": r["address"], "suburb": r["suburb"],
            "asking": round(r["asking_price"]), "CV": round(r["cv_numeric"]),
            "HG_estimate": round(r["third_party_valuation"]) if pd.notna(r["third_party_valuation"]) else "",
            "Ollie_valuation_v4": round(r["fair_value"]),
            "comp_tier": tier if tier else "v4 fallback",
            "n_comps": len(comps) if comps is not None else 0,
            "area_value": round(area),
            "BUY_PRICE": round(buy),
            "hougarden_url": r["url"],
        })
        # record up to 3 comps for verification
        if comps is not None:
            for _, c in comps.head(3).iterrows():
                comp_rows.append({
                    "subject": r["address"], "comp_address": c["address"],
                    "comp_suburb": c["suburb"], "comp_beds": c["beds"], "comp_baths": c["baths"],
                    "comp_land": c["land_area_m2"], "comp_sale_price": round(c["sale_price"]),
                    "comp_sold_date": c["sold_date"], "realestate_url": c["url"],
                })
        picked += 1

    out_dir = Path(r"D:\scraping scripts\property site")
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "buyprice_20_examples.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(comp_rows).to_csv(out_dir / "buyprice_20_comps_used.csv", index=False, encoding="utf-8-sig")
    print(f"Wrote {len(df)} subject rows -> buyprice_20_examples.csv")
    print(f"Wrote {len(comp_rows)} comp rows  -> buyprice_20_comps_used.csv")
    print()
    # console preview
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    for r in rows:
        a = str(r["address"]).encode("ascii", "replace").decode("ascii")[:32]
        print(f"  {a:<32} ask={r['asking']/1000:>6,.0f}k CV={r['CV']/1000:>6,.0f}k "
              f"v4={r['Ollie_valuation_v4']/1000:>6,.0f}k tier={str(r['comp_tier']):<11} "
              f"n={r['n_comps']:>2} area={r['area_value']/1000:>6,.0f}k BUY={r['BUY_PRICE']/1000:>6,.0f}k")


if __name__ == "__main__":
    main()
