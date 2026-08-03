"""What a renovation is actually worth, measured off sold data.

The naive way to answer "what does a 4th bedroom add" is to compare 3-bed and
4-bed sale prices in the suburb. That returns +24.9% and it is wrong: a 4-bed
house is usually a BIGGER house, so the comparison measures floor area, not the
bedroom. Controlling for floor area — comparing a 4-bed against a 3-bed of the
same size — the same data gives +6.2%.

Every figure here is size-controlled. Comparisons run inside a suburb, property
type and a +/-25% floor-area band, then widen to the district and finally all of
Auckland when a cell is too thin.

Measured Auckland-wide (size-controlled):
    bedroom 3 -> 4     n=147   +6.2%
    bedroom 2 -> 3     n= 35  +22.4%   thin, and 2-bed stock skews to units
    bathroom 1 -> 2    n=110   +7.4%
    bathroom 2 -> 3    n=161   -0.8%   a third bathroom returns nothing
    pool               n= 21  +19.9%   thin; pools cluster in premium homes,
                                       so this is association, not cause
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

import pandas as pd

# A cell needs this many sales on BOTH sides before its median means anything.
MIN_PER_SIDE = 3
# Floor-area band width — a "same size" house is within +25%.
BAND = 1.25
# Suburb-level needs a real sample before it beats the district. Only 29 of 272
# suburbs reach even 3 cells, and at that depth the spread runs -9.7% to +33.8%
# — noise, not local knowledge. Districts resolve on 7-12 cells and hold a
# genuine 15.7-point spread (North Shore +14.3% vs Auckland City -1.4%), so a
# thin suburb should fall through to its district rather than out-rank it.
MIN_CELLS_SUBURB = 6
MIN_CELLS = 5          # district / Auckland; below this the estimate is thin
FLOOR_MIN, FLOOR_MAX = 40, 600


@dataclass
class Uplift:
    label: str
    pct: float | None          # 0.062 = adds 6.2%
    dollars: float | None
    cells: int                 # how many suburb x size cells backed it
    scope: str                 # "suburb" | "district" | "auckland"
    is_thin: bool
    caveat: str | None = None
    # True when the number is an observed difference we cannot attribute to the
    # feature itself. Ranked below the real estimates and labelled differently.
    is_association: bool = False


def _cells(df: pd.DataFrame, col: str, lo, hi, pool: bool = False,
           hold: str | None = None) -> list[float]:
    """Median price ratio between two groups, inside each size band.

    `hold` pins the OTHER room count so a bathroom comparison is not quietly
    measuring bedrooms as well. Holding it both lowers the estimate and raises
    the cell count, because grouping produces more, tighter cells:

        bath 1->2   size only +10.1% (94 cells)  ->  size+beds  +8.4% (104)
        bath 2->3   size only  +0.8% (111)       ->  size+beds  -0.8% (161)
        bed  3->4   size only  +7.3% (102)       ->  size+baths +4.5% (140)
    """
    out: list[float] = []
    if df.empty:
        return out
    keys = ["property_type"] + ([hold] if hold else [])
    for _, g in df.dropna(subset=[hold] if hold else []).groupby(keys, dropna=True):
        floor = 60
        while floor < 340:
            band = g[(g.floor >= floor) & (g.floor < floor * BAND)]
            floor += 20
            if pool:
                a, b = band[~band.pool].price, band[band.pool].price
            else:
                a, b = band[band[col] == lo].price, band[band[col] == hi].price
            if len(a) >= MIN_PER_SIDE and len(b) >= MIN_PER_SIDE:
                am = a.median()
                if am and am > 0:
                    out.append(b.median() / am - 1)
    return out


def _resolve(sold: pd.DataFrame, suburb, district, col, lo, hi, pool=False, hold=None):
    """Suburb, then district, then all of Auckland — first scope with enough cells."""
    for scope, subset, need in (
        ("suburb", sold[sold.suburb == suburb] if suburb else sold.iloc[0:0], MIN_CELLS_SUBURB),
        ("district", sold[sold.district == district] if district else sold.iloc[0:0], MIN_CELLS),
        ("auckland", sold, MIN_CELLS),
    ):
        cells = _cells(subset, col, lo, hi, pool=pool, hold=hold)
        if len(cells) >= need:
            return median(cells), len(cells), scope, False
        if scope == "auckland":
            return (median(cells) if cells else None), len(cells), scope, True
    return None, 0, "auckland", True


def value_add(
    sold: pd.DataFrame,
    *,
    suburb: str | None,
    district: str | None,
    beds: float | None,
    baths: float | None,
    has_pool: bool,
    value: float | None,
) -> list[Uplift]:
    """Uplift options for one property, most valuable first."""
    df = sold[(sold.price > 50_000) & sold.floor.between(FLOOR_MIN, FLOOR_MAX)].dropna(subset=["floor"])
    out: list[Uplift] = []

    def add(label, col, lo, hi, pool=False, caveat=None, hold=None, association=False):
        pct, cells, scope, thin = _resolve(df, suburb, district, col, lo, hi, pool=pool, hold=hold)
        if pct is None:
            return
        out.append(Uplift(
            label=label, pct=pct,
            dollars=(value * pct) if value else None,
            cells=cells, scope=scope, is_thin=thin, caveat=caveat,
            is_association=association,
        ))

    if beds and 1 <= beds <= 5:
        add(f"Add a {int(beds) + 1}{_ord(int(beds) + 1)} bedroom", "beds", beds, beds + 1, hold="baths")
    if baths and 1 <= baths <= 3:
        cav = ("Measured at 0% Auckland-wide — a third bathroom does not pay for itself."
               if baths >= 2 else None)
        add(f"Add a {int(baths) + 1}{_ord(int(baths) + 1)} bathroom", "baths", baths, baths + 1, caveat=cav, hold="beds")
    if not has_pool:
        # Not presented as a renovation estimate. Controlling for suburb, type,
        # floor area, bedrooms AND land area the gap stays near +19% — a pool
        # costs nowhere near that, so what we are measuring is that pool houses
        # are better houses in ways these fields do not capture. Adding land as
        # a control moved it the WRONG way (+16.9% -> +18.9%), which rules out
        # section size as the explanation and means we cannot isolate the pool.
        add("Houses here with a pool sell for", "pool", 0, 1, pool=True, hold="beds",
            association=True,
            caveat="This is the gap between houses that have a pool and houses that don't — "
                   "not what building one would return. It survives controls for size, "
                   "bedrooms and land, so it is measuring the calibre of house that has a "
                   "pool. Do not read it as a renovation payback.")

    out.sort(key=lambda u: (u.is_association, -(u.pct or 0)))
    return out


def _ord(n: int) -> str:
    return {1: "st", 2: "nd", 3: "rd"}.get(n if n < 20 else n % 10, "th")


# --- district comparison table -------------------------------------------

DISTRICT_COMPARISONS = (
    ("bedroom", "beds", 3, 4, "baths", False),    # 3 -> 4 bed, bathrooms held
    ("bathroom", "baths", 1, 2, "beds", False),   # 1 -> 2 bath, bedrooms held
    ("pool", "pool", 0, 1, "beds", True),         # pool vs none, bedrooms held
)


def by_district(sold: pd.DataFrame) -> list[dict]:
    """The same size-controlled comparisons, per district.

    Fixed comparisons (3->4 bed, 1->2 bath, pool) so districts sit on the same
    axis and can be read against each other.
    """
    df = sold[(sold.price > 50_000) & sold.floor.between(FLOOR_MIN, FLOOR_MAX)].dropna(subset=["floor"])
    rows = []
    for district in sorted(x for x in df.district.dropna().unique()):
        sub = df[df.district == district]
        entry: dict = {"district": district}
        for name, col, lo, hi, hold, pool in DISTRICT_COMPARISONS:
            cells = _cells(sub, col, lo, hi, pool=pool, hold=hold)
            entry[name] = median(cells) if len(cells) >= MIN_CELLS else None
            entry[f"{name}_cells"] = len(cells)
        rows.append(entry)
    # Sort by the bedroom figure — it is the one people act on.
    rows.sort(key=lambda r: (r["bedroom"] is None, -(r["bedroom"] or 0)))
    return rows
