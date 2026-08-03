"""Houses that already have the floor area for another bedroom.

Two facts, neither useful alone:

  1. What a bedroom is worth, per district, measured size-controlled against
     sold comparables (see valueadd.py). North Shore +10.7%, Franklin +0.3%.
  2. How much floor area a house of each bed count normally carries:
     2 bed 82 m² · 3 bed 121 m² · 4 bed 194 m² · 5 bed 248 m² · 6 bed 270 m²

Cross them and you get the listings where the space for another bedroom is
already inside the existing walls — no extension, no new roof — sitting in a
district that pays for it. 620 of 7,970 live listings qualify at a 5% threshold,
carrying $112M of uplift, median $152k each.

Deliberately conservative:
  * The house must carry at least the MEDIAN floor area of the next bed count
    up, not merely above-average for its own.
  * Districts under the threshold are dropped entirely — a bedroom returning
    +0.3% in Franklin is not an opportunity at any cost.
  * Uplift is resale value only. Conversion cost is not netted off, and floor
    area cannot tell us whether the layout, windows or egress actually permit
    the partition. It is a shortlist to inspect, not a guarantee.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from .valueadd import FLOOR_MAX, FLOOR_MIN, by_district

# Below this the bedroom does not pay enough to be worth the disruption.
MIN_UPLIFT_PCT = 0.05
MIN_BEDS, MAX_BEDS = 2, 5


@dataclass
class Conversion:
    id: int
    address: str | None
    suburb: str | None
    district: str | None
    beds: int | None
    floor_area_m2: float | None
    typical_floor_next: float | None   # median floor for one bedroom more
    asking_price: float | None
    fair_value: float | None
    uplift_pct: float
    uplift_dollars: float
    is_underpriced: bool
    margin: float | None
    image_url: str | None


def _num(v):
    """pandas NaN -> None. NaN is not JSON-serialisable and reaches the API as a 500."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _typical_floor(sold: pd.DataFrame) -> dict[float, float]:
    d = sold[sold.floor.between(FLOOR_MIN, FLOOR_MAX)]
    return d.groupby("beds").floor.median().to_dict()


def conversion_opportunities(sold: pd.DataFrame, live: pd.DataFrame) -> list[Conversion]:
    """Live listings with the floor area for another bedroom, worth doing."""
    uplift = {r["district"]: r["bedroom"] for r in by_district(sold) if r["bedroom"] is not None}
    typical = _typical_floor(sold)

    out: list[Conversion] = []
    for r in live.itertuples():
        beds, floor = _num(r.beds), _num(r.floor_area_m2)
        if beds is None or floor is None or not (MIN_BEDS <= beds <= MAX_BEDS):
            continue
        if not (FLOOR_MIN <= floor <= FLOOR_MAX):
            continue
        need = typical.get(beds + 1)
        if need is None or floor < need:
            continue
        pct = uplift.get(r.district)
        if pct is None or pct < MIN_UPLIFT_PCT:
            continue
        value = _num(r.fair_value)
        if not value or value <= 0:
            continue
        out.append(Conversion(
            id=r.id, address=r.address, suburb=r.suburb, district=r.district,
            beds=int(beds), floor_area_m2=float(floor), typical_floor_next=float(need),
            asking_price=_num(r.asking_price), fair_value=_num(value),
            uplift_pct=float(pct), uplift_dollars=float(value * pct),
            is_underpriced=bool(r.is_underpriced), margin=_num(r.margin),
            image_url=r.image_url,
        ))

    # Biggest dollar uplift first; the double plays surface naturally near the top.
    out.sort(key=lambda c: -c.uplift_dollars)
    return out
