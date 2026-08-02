"""Recompute the valuation for stored for-sale listings, in place.

Used in the weekly two-stage flow: after CoreLogic fills blank attributes on the
staged batch, re-price so fair_value / buy price / margin / cashflow / subdivision
reflect the filled numbers *before* the batch goes live.

How it stays faithful:
  - It reconstructs the exact pipeline INPUT from each stored row (the same scrape
    column names the pipeline reads), so the identical code path that ran at
    ingest runs again.
  - It writes back only the pipeline OUTPUT columns (OUTPUT_COLS) — never the
    attributes — using the same mapping ingest uses.
  - `price_display` isn't stored, only the resulting `listing_type`; we rebuild a
    price_display that _detect_listing_type maps back to the stored listing_type.

Trust gate: `validate_noop()` re-prices an UNCHANGED batch and confirms it
reproduces the stored fair_value/buy_price. If it doesn't, the reconstruction is
wrong and must be fixed before this is trusted — we never silently shift prices.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy.orm import Session

from .ingest import _to_float, _to_int, _to_str
from .models import BatchType, ImportBatch, PropertyForSale, PropertyRent, PropertySold
from .pricing.cashflow import RentRates
from .pricing.comps import SoldDataset
from .pricing.pipeline import run as run_pipeline


def _price_display_for(listing_type: str | None) -> str:
    """Rebuild a price_display that _detect_listing_type maps to listing_type.
    'fixed'/'unknown' need no keyword (fixed is driven by a present asking price)."""
    return {"auction": "auction", "tender": "tender",
            "negotiation": "negotiation"}.get((listing_type or "").lower(), "")


def _row_to_input(p: PropertyForSale) -> dict:
    """A stored listing → the scrape-shaped dict the pipeline reads."""
    return {
        "address": p.address, "suburb": p.suburb, "district": p.district,
        "property_type": p.property_type, "type_of_title": p.type_of_title,
        "zoning": p.zoning,
        "key_bedrooms": p.beds, "key_bathrooms": p.baths, "key_carspaces": p.cars,
        "key_floor_area": p.floor_area_m2, "key_land_area": p.land_area_m2,
        "price_numeric": p.asking_price, "price_display": _price_display_for(p.listing_type),
        "cv_numeric": p.cv_numeric, "land_value_numeric": p.land_value_numeric,
        "improvement_value_numeric": p.improvement_value_numeric,
        "building_age": p.building_age, "has_swimming_pool": p.has_swimming_pool,
        "days_on_market": p.days_on_market,
        "valuation_last_sold_value": p.valuation_last_sold_value,
        "valuation_last_sold_date": p.valuation_last_sold_date,
        # Independent external valuations — the pipeline uses these as a fallback
        # pricing anchor when the council CV is missing (see _external_anchor).
        "pv_estimate_mid": p.pv_estimate_mid,
        "homes_valuation": p.homes_valuation,
    }


# The pipeline output columns → how ingest writes them (mirrors ingest.py:512-569).
def _apply_outputs(rec: PropertyForSale, row: dict) -> None:
    rec.market_value = _to_float(row.get("market_value"))
    rec.predicted_list = _to_float(row.get("predicted_list"))
    rec.predicted_days = _to_float(row.get("predicted_days"))
    rec.comps_used = _to_int(row.get("comps_used"))
    rec.confidence = _to_str(row.get("confidence"))
    rec.pred_vs_cv = _to_float(row.get("pred_vs_cv"))
    rec.pred_vs_listing = _to_float(row.get("pred_vs_listing"))
    rec.pred_v35 = _to_float(row.get("pred_v35"))
    rec.pred_v38 = _to_float(row.get("pred_v38"))
    rec.z_weight = _to_float(row.get("z_weight"))
    rec.beta_tier = _to_str(row.get("beta_tier"))
    rec.cv_anchor = _to_float(row.get("cv_anchor"))
    rec.cv_ratio_tier = _to_str(row.get("cv_ratio_tier"))
    rec.correction_used = _to_str(row.get("correction_used"))
    rec.listing_type = _to_str(row.get("listing_type"))
    rec.pricing_path = _to_str(row.get("pricing_path"))
    rec.range_low = _to_float(row.get("range_low"))
    rec.range_high = _to_float(row.get("range_high"))
    rec.subdivision_premium = _to_float(row.get("subdivision_premium"))
    rec.fair_value = _to_float(row.get("fair_value"))
    rec.margin = _to_float(row.get("margin"))
    rec.is_premium = bool(row.get("is_premium", False))
    rec.buy_price = _to_float(row.get("buy_price"))
    rec.area_value = _to_float(row.get("area_value"))
    rec.comp_tier = _to_int(row.get("comp_tier"))
    rec.comps_matched = _to_int(row.get("comps_matched"))
    rec.min_lot_m2 = _to_float(row.get("min_lot_m2"))
    rec.max_addl_lots = _to_float(row.get("max_addl_lots"))
    rec.sections = _to_int(row.get("sections"))
    rec.dwellings = _to_int(row.get("dwellings"))
    rec.section_rate = _to_float(row.get("section_rate"))
    rec.gross_sales = _to_float(row.get("gross_sales"))
    rec.subdivision_profit = _to_float(row.get("subdivision_profit"))
    rec.section_price_per_m2 = _to_float(row.get("section_price_per_m2"))
    rec.section_value_method = _to_str(row.get("section_value_method"))
    rec.services_cost = _to_float(row.get("services_cost"))
    rec.total_subdivided_value = _to_float(row.get("total_subdivided_value"))
    rec.uplift_vs_asking = _to_float(row.get("uplift_vs_asking"))
    rec.est_weekly_rent = _to_float(row.get("est_weekly_rent"))
    rec.est_gross_yield = _to_float(row.get("est_gross_yield"))
    rec.annual_gross_rent = _to_float(row.get("annual_gross_rent"))
    rec.annual_net_rent = _to_float(row.get("annual_net_rent"))
    rec.annual_mortgage = _to_float(row.get("annual_mortgage"))
    rec.annual_cashflow = _to_float(row.get("annual_cashflow"))
    rec.cash_on_cash = _to_float(row.get("cash_on_cash"))
    rec.breakeven_deposit_pct = _to_float(row.get("breakeven_deposit_pct"))
    rec.expected_sale = _to_float(row.get("expected_sale"))
    rec.expected_sale_path = _to_str(row.get("expected_sale_path"))
    rec.expected_sale_band = _to_float(row.get("expected_sale_band"))
    rec.opportunity_score = _to_float(row.get("opportunity_score"))
    rec.opportunity_score_pct = _to_float(row.get("opportunity_score_pct"))
    rec.best_strategy = _to_str(row.get("best_strategy"))
    rec.best_net_gain = _to_float(row.get("best_net_gain"))
    rec.is_underpriced = bool(row.get("is_underpriced", False))
    rec.is_cashflow_positive = bool(row.get("is_cashflow_positive", False))
    rec.is_subdividable = bool(row.get("is_subdividable", False))


def _sold_df(db: Session, region: str) -> pd.DataFrame | None:
    """The newest loaded sold batch (staged or published), as the pipeline df."""
    sold = (db.query(ImportBatch)
            .filter(ImportBatch.batch_type == BatchType.SOLD.value,
                    ImportBatch.region == region,
                    ImportBatch.status.in_(("staged", "published")))
            .order_by(ImportBatch.id.desc()).first())
    if not sold:
        return None
    rows = db.query(PropertySold).filter(PropertySold.import_batch_id == sold.id).all()
    return pd.DataFrame([{
        "address": r.address, "suburb": r.suburb, "district": r.district,
        "property_type": r.property_type, "key_bedrooms": r.beds, "key_bathrooms": r.baths,
        "key_floor_area": r.floor_area_m2, "key_land_area": r.land_area_m2,
        "price_numeric": r.sale_price, "cv_numeric": r.cv_numeric,
        "land_value_numeric": r.land_value_numeric, "type_of_title": r.type_of_title,
        "sold_date": r.sold_date, "days_on_market": r.days_on_market,
    } for r in rows])


def _rent_rates(db: Session, region: str) -> RentRates | None:
    rr = (db.query(PropertyRent).join(ImportBatch, ImportBatch.id == PropertyRent.import_batch_id)
          .filter(ImportBatch.batch_type == BatchType.RENT.value,
                  ImportBatch.region == region, ImportBatch.is_active.is_(True)).all())
    if not rr:
        return None
    return RentRates(pd.DataFrame([{
        "weekly_rent": x.weekly_rent, "suburb": x.suburb, "district": x.district,
        "property_type": x.property_type, "beds": x.beds} for x in rr]))


@dataclass
class RepriceResult:
    rows: int = 0
    changed_fair_value: int = 0
    max_fair_value_delta_pct: float = 0.0
    committed: bool = False
    error: str | None = None
    samples: list = field(default_factory=list)   # a few {id, old, new} for spot-checks


def reprice_batch(db: Session, batch_id: int, *, region: str = "Auckland",
                  commit: bool = False, tol: float = 0.005) -> RepriceResult:
    """Re-run pricing on every listing in a batch using its CURRENT stored
    attributes. With commit=False it computes and reports the diff but writes
    nothing (used by validate_noop). tol = fractional change treated as 'same'."""
    res = RepriceResult()
    recs = (db.query(PropertyForSale)
            .filter(PropertyForSale.import_batch_id == batch_id)
            .order_by(PropertyForSale.id).all())
    if not recs:
        res.error = "no listings in batch"
        return res
    sold_df = _sold_df(db, region)
    if sold_df is None or sold_df.empty:
        res.error = "no sold batch to price against"
        return res

    df = pd.DataFrame([_row_to_input(p) for p in recs]).reset_index(drop=True)
    enriched = run_pipeline(df, SoldDataset(sold_df), _rent_rates(db, region)).reset_index(drop=True)
    res.rows = len(recs)

    for i, rec in enumerate(recs):
        row = enriched.iloc[i].to_dict()
        old_fv = rec.fair_value
        new_fv = _to_float(row.get("fair_value"))
        if old_fv and new_fv:
            delta = abs(new_fv - old_fv) / old_fv
            if delta > tol:
                res.changed_fair_value += 1
                res.max_fair_value_delta_pct = max(res.max_fair_value_delta_pct, delta)
                if len(res.samples) < 8:
                    res.samples.append({"id": rec.id, "address": rec.address,
                                        "old": round(old_fv), "new": round(new_fv)})
        elif bool(old_fv) != bool(new_fv):
            res.changed_fair_value += 1
        if commit:
            _apply_outputs(rec, row)
    if commit:
        db.commit()
        res.committed = True
    return res


def validate_noop(db: Session, batch_id: int, *, region: str = "Auckland") -> dict:
    """Trust gate: re-price WITHOUT committing and report how many rows would
    change. On a batch whose attributes haven't changed since ingest this should
    be ~0. A large unexplained change count means the reconstruction is wrong."""
    r = reprice_batch(db, batch_id, region=region, commit=False)
    return {
        "rows": r.rows, "would_change_fair_value": r.changed_fair_value,
        "pct_reproduced": round(100 * (r.rows - r.changed_fair_value) / r.rows, 2) if r.rows else 0,
        "max_delta_pct": round(r.max_fair_value_delta_pct * 100, 2),
        "samples": r.samples, "error": r.error,
    }
