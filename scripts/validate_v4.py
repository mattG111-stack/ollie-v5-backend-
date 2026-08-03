"""Validation harness for v4 production AVM.

Mirrors the client's 'Backtest' scoreboard contract from
Algo data 25-06-2026.xlsx:
  - Sheet10!D = sold price (truth)
  - Sheet10!AK = council CV
  - Sheet11!AM = asking price
  - Sheet11!K = address match key

Reports MAPE / MdAPE / %within 8% / %over 20% by segment (listed vs unlisted).
Per-spec accuracy targets:
  - Listed (asking path): ~3% MAPE (3.2% measured on n=72 in his harness)
  - Unlisted (v3.5 path): ~6-8% MAPE
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Make app importable when running as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.pricing.glm import predict  # noqa: E402

XLSX = Path(r"D:\scraping scripts\property site\Algo data 25-06-2026.xlsx")


def _norm_address(s: object) -> str:
    """Normalise per v4 spec: lowercase, strip non-alphanumeric."""
    if s is None or (isinstance(s, float) and s != s):
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


def _load_sold() -> pd.DataFrame:
    df = pd.read_excel(XLSX, sheet_name="Sheet10")
    keep = {
        "address", "suburb", "district", "property_type", "type_of_title",
        "key_bedrooms", "key_bathrooms", "key_carspaces",
        "key_floor_area", "key_land_area", "building_age",
        "price_numeric", "cv_numeric",
    }
    cols = [c for c in df.columns if c.strip() in keep]
    df = df[cols].copy()
    df.columns = [c.strip() for c in df.columns]
    df["sold_price"] = pd.to_numeric(df["price_numeric"], errors="coerce")
    df["cv"] = pd.to_numeric(df["cv_numeric"], errors="coerce")
    df = df[(df["sold_price"] > 0) & (df["cv"] > 0)].copy()
    df["addr_key"] = df["address"].map(_norm_address)
    return df


def _load_asking() -> pd.DataFrame:
    df = pd.read_excel(XLSX, sheet_name="Sheet11")
    # match key (col K, idx 10 = "location") + asking price (col AM, idx 38 = " price ")
    df = df.iloc[:, [10, 38]].copy()
    df.columns = ["match_key", "asking_price"]
    df["asking_price"] = pd.to_numeric(df["asking_price"], errors="coerce")
    df["addr_key"] = df["match_key"].map(_norm_address)
    df = df[(df["asking_price"] > 0) & (df["addr_key"] != "")].copy()
    # Dedupe — keep first asking per address
    df = df.drop_duplicates("addr_key", keep="first")
    return df


def _parse_area(v) -> float | None:
    """Strip units like '317 sqm' / '0.49 ha'. Per spec: VALUE(LEFT(txt,FIND(' ',txt)-1))."""
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    s = str(v).strip().lower()
    if not s:
        return None
    # Try direct float first
    try:
        return float(s) if float(s) > 0 else None
    except ValueError:
        pass
    # Strip first token before space
    m = re.match(r"^([0-9.,]+)\s*(ha|sqm|m2)?", s)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if m.group(2) == "ha":
        val *= 10000
    return val if val > 0 else None


def _parse_age(yb) -> float | None:
    try:
        yb = float(yb)
    except (TypeError, ValueError):
        return None
    if 1800 <= yb <= 2030:
        return 2026 - int(yb)
    if 0 < yb < 200:
        return yb
    return None


def _predict_row(row: pd.Series) -> tuple[float | None, str]:
    """Run v4 predict and return (predicted_sale_price, pricing_path)."""
    asking = row.get("asking_price")
    listing_type = "fixed" if pd.notna(asking) and asking > 0 else "unknown"
    p = predict(
        suburb=row.get("suburb"),
        district=row.get("district"),
        property_type=row.get("property_type"),
        cv=row.get("cv"),
        floor=_parse_area(row.get("key_floor_area")),
        land=_parse_area(row.get("key_land_area")),
        beds=row.get("key_bedrooms"),
        baths=row.get("key_bathrooms"),
        cars=row.get("key_carspaces"),
        age=_parse_age(row.get("building_age")),
        title=row.get("type_of_title"),
        method=None,
        pool=False,
        address=row.get("address"),
        asking_price=asking if pd.notna(asking) else None,
        listing_type=listing_type,
    )
    return p.market_value, p.pricing_path


def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    ape = np.abs(y_pred - y_true) / y_true
    return {
        "n": len(ape),
        "MAPE": ape.mean() * 100,
        "MdAPE": np.median(ape) * 100,
        "%within_8": (ape <= 0.08).mean() * 100,
        "%over_20": (ape > 0.20).mean() * 100,
    }


def main() -> int:
    print("Loading sold truth (Sheet10) ...")
    sold = _load_sold()
    print(f"  {len(sold):,} sold rows with price + CV")

    print("Loading asking-price truth (Sheet11) ...")
    asking = _load_asking()
    print(f"  {len(asking):,} asking rows with address + price")

    print("Joining sold + asking on normalised address ...")
    merged = sold.merge(asking[["addr_key", "asking_price"]], on="addr_key", how="left")
    print(f"  matched asking: {merged['asking_price'].notna().sum():,} of {len(merged):,}")
    print()

    print("Running v4 predictor over the test set ...")
    preds, paths = [], []
    for _, row in merged.iterrows():
        mv, path = _predict_row(row)
        preds.append(mv)
        paths.append(path)
    merged["pred"] = preds
    merged["path"] = paths

    # Drop rows without a prediction
    scored = merged[merged["pred"].notna() & (merged["pred"] > 0)].copy()
    print(f"  predictions: {len(scored):,} of {len(merged):,}")
    print()

    # Per his ML Build Spec: drop non-market sales (sale/CV beyond +/- 40%)
    # before reporting metrics. These are family transfers, mortgagee sales,
    # etc. — they distort accuracy numbers without saying anything about model
    # quality. ARMS_LOW / ARMS_HIGH = 0.60 / 1.40 (his constants).
    scored["sale_to_cv"] = scored["sold_price"] / scored["cv"]
    market = scored[(scored["sale_to_cv"].between(0.60, 1.40))].copy()
    print(f"  market-only filter: dropped {len(scored)-len(market):,} non-market sales (sale/CV outside [0.6, 1.4])")
    print()
    scored = market

    y = scored["sold_price"].values
    p = np.asarray(scored["pred"].values, dtype=float)
    print("=== OVERALL (v4 production AVM, market-only) ===")
    for k, v in _metrics(y, p).items():
        print(f"  {k}: {v:.2f}" if isinstance(v, float) else f"  {k}: {v:,}")
    print()
    for path_name in ("asking", "v35"):
        seg = scored[scored["path"] == path_name]
        if len(seg) == 0:
            continue
        print(f"=== Path = {path_name!r}  (target: {'~3% MAPE' if path_name=='asking' else '~6-8% MAPE'}) ===")
        ys = seg["sold_price"].values
        ps = np.asarray(seg["pred"].values, dtype=float)
        for k, v in _metrics(ys, ps).items():
            print(f"  {k}: {v:.2f}" if isinstance(v, float) else f"  {k}: {v:,}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
