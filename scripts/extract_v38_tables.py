"""Extract trained v3.5/v3.8 reference tables from the client's Excel workbook
into JSON files the runtime can load.

Source: Algo data 17-05-2026.xlsx (the older workbook where v3.5 was developed)
Output: ollie/backend/app/pricing/v38_tables/*.json

What this produces:
  beta_subtype.json   — beta coefficients keyed by 'Suburb||Type'    (~85 entries)
  beta_suburb.json    — beta coefficients keyed by Suburb            (~130 entries)
  beta_district.json  — beta coefficients keyed by District          (~8 entries)
  beta_global.json    — single fallback beta row
  suburb_corr.json    — Suburb-level correction factor (dict)
  subtype_corr.json   — Suburb||Type correction factor (dict)
  type_corr.json      — Type-only correction factor (dict)
  building_corr.json  — Building||Suburb correction factor (dict)
  district_type_corr.json — District||Type correction factor (dict)
  cv_ratios.json      — { subtype: {..}, suburb: {..}, type: {..}, global: float }
  bucket_n.json       — { 'Suburb||Type': n } - drives the Z blend weight

All beta rows hold exactly 15 floats: beta1..beta15 per the algorithm spec.
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "app" / "pricing" / "v38_tables"
OUT_DIR.mkdir(parents=True, exist_ok=True)

XLSX = Path(r"C:\Users\hamza\Downloads\Algo data 17-05-2026.xlsx")
N_BETAS = 15  # beta1..beta15


def _f(v) -> float | None:
    try:
        f = float(v)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


def _s(v) -> str | None:
    if v is None or (isinstance(v, float) and v != v):
        return None
    return str(v).strip() or None


def extract_coef_backup():
    """Coef_Backup_2026-05-17 has 4 side-by-side tables.

    Column layout (verified):
      cols 0-19  : SubTypeT — Suburb, Type, n, beta1..beta15, Key, ?
      cols 20-37 : SubT     — Suburb, n, beta1..beta15
      cols 38-53 : DistT    — District, n, beta1..beta15
      cols 54-71 : GlobBeta — Label, n, beta1..beta15
      col  72    : SubTypeKey
    """
    df = pd.read_excel(XLSX, sheet_name="Coef_Backup_2026-05-17", header=None)
    print(f"  Coef_Backup_2026-05-17 shape: {df.shape}")

    subtype_betas: dict[str, dict] = {}
    suburb_betas: dict[str, dict] = {}
    district_betas: dict[str, dict] = {}
    global_betas: dict | None = None

    # SubTypeT — cols 0,1,2 (suburb, type, n), 3..17 (beta1..beta15)
    for i in range(1, len(df)):
        suburb = _s(df.iat[i, 0])
        type_ = _s(df.iat[i, 1])
        n = _f(df.iat[i, 2])
        if not suburb or not type_ or n is None:
            continue
        betas = [_f(df.iat[i, 3 + k]) or 0.0 for k in range(N_BETAS)]
        if all(b == 0.0 for b in betas):
            continue  # all-zero row, skip
        subtype_betas[f"{suburb}||{type_}"] = {"n": int(n), "beta": betas}

    # SubT — cols 20 (suburb), 21 (n), 22..36 (beta1..beta15)
    for i in range(1, len(df)):
        suburb = _s(df.iat[i, 20])
        n = _f(df.iat[i, 21])
        if not suburb or n is None:
            continue
        betas = [_f(df.iat[i, 22 + k]) or 0.0 for k in range(N_BETAS)]
        if all(b == 0.0 for b in betas):
            continue
        suburb_betas[suburb] = {"n": int(n), "beta": betas}

    # DistT — cols 38 (district), 39 (n), 40..54 (beta1..beta15)
    for i in range(1, len(df)):
        district = _s(df.iat[i, 38])
        n = _f(df.iat[i, 39])
        if not district or n is None:
            continue
        betas = [_f(df.iat[i, 40 + k]) or 0.0 for k in range(N_BETAS)]
        if all(b == 0.0 for b in betas):
            continue
        district_betas[district] = {"n": int(n), "beta": betas}

    # GlobBeta — col 54 (label), 55 (n), 56..70 (beta1..beta15)
    for i in range(1, len(df)):
        label = _s(df.iat[i, 54])
        n = _f(df.iat[i, 55])
        if label and n is not None:
            betas = [_f(df.iat[i, 56 + k]) or 0.0 for k in range(N_BETAS)]
            global_betas = {"n": int(n), "beta": betas}
            break  # only one global row

    return subtype_betas, suburb_betas, district_betas, global_betas


def extract_corrections():
    """SuburbCorr, SubTypeCorr (has 2 side-by-side tables: Suburb||Type and Type),
    BuildingCorr, DistTypeCorr."""

    # SuburbCorr — Suburb, Median Sale/Pred Ratio, n
    df = pd.read_excel(XLSX, sheet_name="SuburbCorr")
    suburb_corr: dict[str, float] = {}
    for _, r in df.iterrows():
        sub = _s(r.iloc[0])
        ratio = _f(r.iloc[1])
        if sub and ratio is not None:
            suburb_corr[sub] = ratio
    print(f"  SuburbCorr: {len(suburb_corr)} entries")

    # SubTypeCorr — has two side-by-side tables:
    #   Suburb, Type, Key (Suburb||Type), n, Factor   |   Type, n, Factor
    df = pd.read_excel(XLSX, sheet_name="SubTypeCorr", header=None)
    subtype_corr: dict[str, float] = {}
    type_corr: dict[str, float] = {}
    # First table starts at row 1, cols 0..4
    for i in range(1, len(df)):
        sub = _s(df.iat[i, 0])
        type_ = _s(df.iat[i, 1])
        factor = _f(df.iat[i, 4])
        if sub and type_ and factor is not None:
            subtype_corr[f"{sub}||{type_}"] = factor
    # Second table at cols 6..8
    for i in range(1, len(df)):
        type_ = _s(df.iat[i, 6])
        factor = _f(df.iat[i, 8])
        if type_ and factor is not None:
            type_corr[type_] = factor
    print(f"  SubTypeCorr: {len(subtype_corr)} suburb×type, {len(type_corr)} type")

    # BuildingCorr — Building||Suburb, Factor, n, Notes
    df = pd.read_excel(XLSX, sheet_name="BuildingCorr")
    building_corr: dict[str, float] = {}
    for _, r in df.iterrows():
        key = _s(r.iloc[0])
        factor = _f(r.iloc[1])
        if key and factor is not None:
            building_corr[key.lower()] = factor
    print(f"  BuildingCorr: {len(building_corr)} entries")

    # DistTypeCorr — District, Type, n, Factor, Key
    df = pd.read_excel(XLSX, sheet_name="DistTypeCorr")
    dist_type_corr: dict[str, float] = {}
    for _, r in df.iterrows():
        district = _s(r.iloc[0])
        type_ = _s(r.iloc[1])
        factor = _f(r.iloc[3])
        if district and type_ and factor is not None:
            dist_type_corr[f"{district}||{type_}"] = factor
    print(f"  DistTypeCorr: {len(dist_type_corr)} entries")

    return suburb_corr, subtype_corr, type_corr, building_corr, dist_type_corr


def extract_cv_ratios():
    """CV_Ratios — 4 side-by-side tables: Suburb×Type, Suburb, Type, Global."""
    df = pd.read_excel(XLSX, sheet_name="CV_Ratios", header=None)
    print(f"  CV_Ratios shape: {df.shape}")

    subtype: dict[str, float] = {}
    suburb: dict[str, float] = {}
    type_: dict[str, float] = {}
    global_ratio: float | None = None
    bucket_n: dict[str, int] = {}

    # Header row 0: Suburb, Type, n, Median Sale/CV, Key | Suburb.1, n.1, Median Sale/CV.1 |
    #               Type.1, n.2, Median Sale/CV.2 | Global Median, n.3
    # Layout from earlier:
    #   cols 0-4: Suburb | Type | n | Median Sale/CV | Key (Suburb||Type)
    #   cols 6-8: Suburb | n | Median Sale/CV
    #   cols 10-12: Type | n | Median Sale/CV
    #   cols 14-15: Global Median | n
    for i in range(1, len(df)):
        # Suburb×Type table
        sub = _s(df.iat[i, 0])
        typ = _s(df.iat[i, 1])
        n = _f(df.iat[i, 2])
        ratio = _f(df.iat[i, 3])
        if sub and typ and ratio is not None:
            key = f"{sub}||{typ}"
            subtype[key] = ratio
            if n is not None:
                bucket_n[key] = int(n)
        # Suburb table
        sub_only = _s(df.iat[i, 6])
        sub_ratio = _f(df.iat[i, 8])
        if sub_only and sub_ratio is not None:
            suburb[sub_only] = sub_ratio
        # Type table
        type_only = _s(df.iat[i, 10])
        type_ratio = _f(df.iat[i, 12])
        if type_only and type_ratio is not None:
            type_[type_only] = type_ratio
        # Global (only first row)
        if global_ratio is None:
            gr = _f(df.iat[i, 14])
            if gr is not None:
                global_ratio = gr

    print(f"  CV_Ratios: {len(subtype)} subtype, {len(suburb)} suburb, {len(type_)} type, global={global_ratio}")
    print(f"  bucket_n: {len(bucket_n)} entries")
    return subtype, suburb, type_, global_ratio, bucket_n


def write_json(name: str, data):
    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    size_kb = path.stat().st_size / 1024
    print(f"  wrote {name}.json ({size_kb:.1f} KB)")


def main():
    print(f"Source: {XLSX}")
    if not XLSX.exists():
        sys.exit(f"File not found: {XLSX}")
    print(f"Output dir: {OUT_DIR}\n")

    print("Extracting beta tables ...")
    subtype_betas, suburb_betas, district_betas, global_betas = extract_coef_backup()
    print(f"  SubTypeT: {len(subtype_betas)} entries")
    print(f"  SubT:     {len(suburb_betas)} entries")
    print(f"  DistT:    {len(district_betas)} entries")
    print(f"  GlobBeta: {global_betas is not None}\n")

    print("Extracting correction tables ...")
    suburb_corr, subtype_corr, type_corr, building_corr, dist_type_corr = extract_corrections()
    print()

    print("Extracting CV ratios + bucket counts ...")
    cv_st, cv_sub, cv_type, cv_global, bucket_n = extract_cv_ratios()
    print()

    print("Writing JSON files ...")
    write_json("beta_subtype", subtype_betas)
    write_json("beta_suburb", suburb_betas)
    write_json("beta_district", district_betas)
    write_json("beta_global", global_betas)
    write_json("suburb_corr", suburb_corr)
    write_json("subtype_corr", subtype_corr)
    write_json("type_corr", type_corr)
    write_json("building_corr", building_corr)
    write_json("district_type_corr", dist_type_corr)
    write_json("cv_ratios", {
        "subtype": cv_st, "suburb": cv_sub, "type": cv_type, "global": cv_global,
    })
    write_json("bucket_n", bucket_n)

    print("\nDone.")


if __name__ == "__main__":
    main()
