"""v3.5 hedonic GLM + v3.8 CV-anchor blend.

Replaces comp-matching as the Market Value engine. Uses pre-trained
coefficients from the client's Excel — no on-the-fly training.

v3.5 formula (per the client's spec):
  raw = EXP( β1 + β2·ln(CV) + β3·ln(Floor) + β4·ln(Land+1) + β5·Beds
             + β6·Baths + β7·Cars + β8·Age + β10·[Title=2] + β11·[Title=3]
             + β12·[Title=4] + β13·[Title∈{0,blank}] + β14·[Method=A]
             + β15·[Method=T] )  ×  (1.029 if Pool=Y else 1)
  v3.5 = raw × SuburbCorr × SubTypeCorr × BldgCorr
  (Apartments skip SubTypeCorr per spec)

v3.8 blend:
  Z = n / (n + K)  where K=10, n = sales in Suburb×Type bucket
  v3.8 = Z × v3.5 + (1-Z) × CV × Area_SaleCV_Ratio
  (cascading: Suburb×Type → Suburb → Type → Global)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .v38_loader import TABLES, V38Tables

K = 10  # credibility constant for v3.8 blend
POOL_PREMIUM = 1.029
N_BETAS = 15

# Suburb -> District for the District-level β fallback. The trained tables
# only cover 8 Auckland districts so we build the suburb→district map at
# load time from whichever beta_suburb / beta_subtype entries we have.
# In practice we don't need this map for the predictor itself — the caller
# passes district directly. Kept here as documentation.
DISTRICTS = ("Rodney", "North Shore City", "Auckland City", "Papakura",
             "Waitakere City", "Manukau City", "Franklin", "Waiheke Island")


# ---------- canonical type normalisation ----------
# Client's training data uses these canonical Title-Case strings.
# Map the messy raw scraper values (English variants + Chinese) onto them.
_TYPE_MAP_EN = {
    "house": "House",
    "home": "House",
    "residential - dwelling": "House",
    "residential dwelling": "House",
    "single family residence": "House",
    "singlefamilyresidence": "House",
    "townhouse": "Townhouse",
    "residential - ownership home units": "Unit",
    "apartment": "Apartment",
    "residential - apartments": "Apartment",
    "unit": "Unit",
    "single house zone": "House",
    "residence": "Residence",
    "lifestyle property": "Lifestyle Property",
    "lifestyle - improved": "Lifestyle Property",
    "lifestyle section": "Lifestyle Section",
    "lifestyle - vacant": "Lifestyle Section",
    "lifestyle - bare land": "Lifestyle Section",
    "section": "Section",
    "residential - vacant": "Section",
    "residential - vacant land multiple housing": "Section",
    "residential section": "Section",
    "home and income": "Home and Income",
    "residential - home & income": "Home and Income",
    "carpark": "Carpark",
}
_TYPE_MAP_CN = {
    "独立屋": "House",
    "独立别墅": "House",
    "城市屋": "Townhouse",
    "排房": "Townhouse",
    "公寓": "Apartment",
    "单元房": "Unit",
    "单元": "Unit",
    "乡村别墅": "Lifestyle Property",
    "建地": "Section",
    "土地": "Section",
    "地皮": "Section",
    "乡村住宅建地": "Lifestyle Section",
    "自住投资": "House",
}


def canonical_type(raw: str | None) -> str:
    if not raw:
        return "House"
    s = str(raw).strip()
    # Direct Chinese map
    if s in _TYPE_MAP_CN:
        return _TYPE_MAP_CN[s]
    # Lowercase English match
    low = s.lower()
    if low in _TYPE_MAP_EN:
        return _TYPE_MAP_EN[low]
    # Substring fallback
    if any(k in low for k in ("apartment", "ownership home unit")):
        return "Apartment"
    if "townhouse" in low or "terrace" in low:
        return "Townhouse"
    if "unit" in low:
        return "Unit"
    if "lifestyle" in low and any(k in low for k in ("vacant", "section", "bare")):
        return "Lifestyle Section"
    if "lifestyle" in low:
        return "Lifestyle Property"
    if "vacant" in low or "section" in low:
        return "Section"
    if "carpark" in low:
        return "Carpark"
    if "home" in low and "income" in low:
        return "Home and Income"
    if "house" in low or "dwelling" in low or "home" in low:
        return "House"
    return "House"  # default


def title_code(raw) -> int:
    """Maps title-of-property to the integer codes used in training.
      1 = Freehold, 2 = Leasehold/?, 3 = Cross-Lease, 4 = Unit Title, 0 = Other
    """
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        try:
            v = int(raw)
            return v if v in (1, 2, 3, 4) else 0
        except (ValueError, TypeError):
            return 0
    s = str(raw).strip().lower()
    if s in ("freehold", "1"):
        return 1
    if s in ("leasehold", "2"):
        return 2
    if "cross" in s or s == "3":
        return 3
    if s in ("unit title", "unit-title", "4"):
        return 4
    return 0


def building_key(address: str | None) -> str | None:
    """Strip the unit prefix from an address to get a key shared by all
    units of the same building. Used for the BldgCorr lookup.
    """
    if not address:
        return None
    s = str(address).strip().lower()
    s = re.sub(r"^(\d+[a-z]?/|unit\s+\d+,?\s*|flat\s+\d+,?\s*)", "", s, count=1)
    return s.strip() or None


# ---------- feature vector ----------
def _features(*, cv: float, floor: float, land: float | None, beds: float, baths: float,
              cars: float, age: float, title: int, method: str) -> list[float]:
    """Build the 15-element feature vector in β1..β15 order.

    Layout (matches the extracted β coefficient columns):
      0  β1  intercept       = 1
      1  β2  ln(CV)
      2  β3  ln(Floor)
      3  β4  ln(Land + 1)
      4  β5  Beds
      5  β6  Baths
      6  β7  Cars
      7  β8  Age (years since built)
      8  β9  DISABLED        = 0   (was MonthsT in v3.5 spec; client disabled it)
      9  β10 [Title == 2]
     10  β11 [Title == 3]    (Cross-Lease)
     11  β12 [Title == 4]    (Unit Title)
     12  β13 [Title in {0, blank}]
     13  β14 [Method starts with 'A']
     14  β15 [Method starts with 'T']
    """
    m = str(method or "").strip().upper()[:1]
    return [
        1.0,                                          # β1
        math.log(cv) if cv and cv > 0 else 0.0,       # β2
        math.log(floor) if floor and floor > 0 else 0.0,  # β3
        math.log((land or 0) + 1.0),                  # β4
        float(beds or 0),                             # β5
        float(baths or 0),                            # β6
        float(cars or 0),                             # β7
        float(age or 0),                              # β8
        0.0,                                          # β9 disabled
        1.0 if title == 2 else 0.0,                   # β10
        1.0 if title == 3 else 0.0,                   # β11
        1.0 if title == 4 else 0.0,                   # β12
        1.0 if title in (0, None) else 0.0,           # β13
        1.0 if m == "A" else 0.0,                     # β14
        1.0 if m == "T" else 0.0,                     # β15
    ]


# ---------- output dataclass ----------
@dataclass
class Prediction:
    """Output of the v4 production AVM (two-tier: asking × 0.95 then v3.5 fallback).

    market_value is always the final number the user sees.
    range_low / range_high give an honest band — ±4% on the asking path,
    ±14% on the v3.5 fallback path (per client spec, June 2026).
    """
    pred_v35: float | None         # raw v3.5 prediction (always populated when computable)
    market_value: float | None     # final answer: asking × 0.95 OR v3.5 fallback
    pricing_path: str              # 'asking' | 'v35' | 'insufficient'
    range_low: float | None        # honest band low end
    range_high: float | None       # honest band high end
    beta_tier: str                 # 'subtype' | 'suburb' | 'district' | 'global' — only set on v3.5 path
    correction_used: str           # 'subtype' | 'suburb' | 'none'
    bldg_correction_applied: bool
    n_subtype: int                 # bucket count → drives confidence on the v3.5 path
    confidence: str                # 'high' (asking) | 'medium' (thick v3.5) | 'low' (thin v3.5) | 'insufficient'
    warnings: tuple[str, ...]

    # Independent fair-value reference for DEAL-FINDING (not the sale predictor).
    # = CV-bounded hedonic: the v3.5 estimate when it's within ±35% of CV,
    # else CV itself (thin-bucket hedonic predictions are untrustworthy).
    # market_value answers "what will it sell for"; fair_value answers
    # "what is it independently worth" so we can compare against asking.
    fair_value: float | None = None

    # Legacy diagnostic fields retained for backwards compatibility / dashboards.
    # These are NULL on the asking path and populated only when v3.5 fallback runs.
    pred_v38: float | None = None
    z_weight: float | None = None
    cv_anchor: float | None = None
    cv_ratio_tier: str = "none"


# Client-locked constants from v4 spec.
ASKING_DISCOUNT = 0.95      # median sold/ask = 0.951, MAPE-optimal = 0.949 → 0.95
BAND_LISTED = 0.04          # ±4% — std of sold/ask
BAND_UNLISTED = 0.14        # ±14% — std of sold/CV
LISTING_TYPES_WITH_ASKING = frozenset({"fixed"})  # all other types (auction/tender/negotiation/unknown) → fallback

# Typical Auckland floor area (m²) by canonical type, used to impute a missing
# floor so the hedonic stays sane instead of dropping the size term.
_TYPICAL_FLOOR_BY_TYPE = {
    "Apartment": 65.0,
    "Unit": 80.0,
    "Townhouse": 110.0,
    "House": 150.0,
    "Residence": 150.0,
}


# ---------- main predictor ----------
def predict(
    *,
    suburb: str | None,
    district: str | None,
    property_type: str | None,
    cv: float | None,
    floor: float | None,
    land: float | None,
    beds: float | None,
    baths: float | None,
    cars: float | None,
    age: float | None,
    title: int | None,
    method: str | None,
    pool: bool,
    address: str | None,
    asking_price: float | None = None,
    listing_type: str | None = None,
    tables: V38Tables = TABLES,
) -> Prediction:
    """Run the v4 production AVM (June 2026 spec).

    Two-tier:
      1. Primary path — if asking_price is present AND listing_type allows it
         (fixed-price, not auction/tender/negotiation):
           market_value = asking_price × 0.95
           confidence = high, band = ±4%
      2. Fallback — pure v3.5 hedonic with cascading β-tier + corrections
         (no v3.8 CV-anchor blend; client's "do not build" list).
           confidence = medium/low/insufficient by bucket-n, band = ±14%
    """
    warnings: list[str] = []
    ctype = canonical_type(property_type)

    def _safe_str(v) -> str:
        if v is None:
            return ""
        # pandas NaN is a float — protect against that
        if isinstance(v, float) and v != v:
            return ""
        return str(v).strip()

    suburb_s = _safe_str(suburb)
    district_s = _safe_str(district)
    subtype_key = f"{suburb_s}||{ctype}" if suburb_s else None
    asking_f = float(asking_price) if asking_price and asking_price > 0 else 0.0
    cv_f = float(cv) if cv and cv > 0 else 0.0

    # ================================================================
    # STEP A: compute the v3.5 hedonic — ALWAYS (when CV is available).
    # Needed both as the fallback predictor AND as the independent
    # fair-value reference for deal-finding, even on the asking path.
    # ================================================================
    pred_v35: float | None = None
    beta_tier = "n/a"
    correction_used = "none"
    bldg_applied = False
    n_subtype = int(tables.bucket_n.get(subtype_key, 0)) if subtype_key else 0

    # Impute a typical floor area when missing for a dwelling. Dropping the
    # ln(floor) term entirely (the old behaviour) distorted the estimate —
    # e.g. it inflated a thin-bucket West Harbour house to $1.09M. A typical
    # floor for the property type keeps the estimate sane and lets us still
    # value the ~6,600 listings that don't carry a floor area. Mirrors the
    # spec's "age → default 30 if missing" preprocessing.
    try:
        floor_used = float(floor) if floor is not None and float(floor) > 0 else 0.0
    except (TypeError, ValueError):
        floor_used = 0.0
    if floor_used <= 0:
        floor_used = _TYPICAL_FLOOR_BY_TYPE.get(ctype, 0.0)

    if cv_f > 0:
        betas = tables.beta_global.get("beta", [0.0] * N_BETAS)
        beta_tier = "global"
        if subtype_key and subtype_key in tables.beta_subtype:
            rec = tables.beta_subtype[subtype_key]
            beta_tier = "subtype"; betas = rec["beta"]
        elif suburb_s and suburb_s in tables.beta_suburb:
            rec = tables.beta_suburb[suburb_s]
            beta_tier = "suburb"; betas = rec["beta"]
        elif district_s and district_s in tables.beta_district:
            rec = tables.beta_district[district_s]
            beta_tier = "district"; betas = rec["beta"]

        x = _features(
            cv=cv_f, floor=floor_used, land=float(land or 0),
            beds=beds, baths=baths, cars=cars, age=age,
            title=title_code(title), method=method or "",
        )
        raw = math.exp(sum(b * xi for b, xi in zip(betas, x)))
        if pool:
            raw *= POOL_PREMIUM

        factor = 1.0
        if ctype != "Apartment" and subtype_key and subtype_key in tables.subtype_corr:
            factor *= tables.subtype_corr[subtype_key]; correction_used = "subtype"
        elif suburb_s and suburb_s in tables.suburb_corr:
            factor *= tables.suburb_corr[suburb_s]; correction_used = "suburb"
        elif ctype and ctype in tables.type_corr:
            factor *= tables.type_corr[ctype]; correction_used = "type"

        bk = building_key(address)
        if bk:
            bkey = f"{bk}||{suburb_s}".lower()
            if bkey in tables.building_corr:
                factor *= tables.building_corr[bkey]; bldg_applied = True

        candidate = raw * factor
        # Sanity: drop NaN / non-positive / blown-up (>15× CV) predictions.
        if candidate == candidate and candidate > 0 and candidate <= 15.0 * cv_f:
            pred_v35 = candidate

    # ================================================================
    # STEP B: independent fair_value = the MODEL's hedonic valuation.
    # This is the deal-finding reference — "what is it worth" to compare
    # against asking. It is the v3.5 hedonic estimate, NOT raw council CV
    # and NOT the asking × 0.95 sale predictor. Margin = (fair_value −
    # asking) / asking; positive = asking below the model's estimate = deal.
    #
    # When the hedonic can't be trusted we SUPPRESS it (no estimate, no
    # margin) rather than fall back to CV — the margin must always reflect
    # the model's own estimate, never a raw council valuation.
    # ================================================================
    fair_value: float | None = pred_v35

    if fair_value is not None and asking_f > 0:
        # Implausible vs asking → suppress. A model value above 1.6× asking is
        # almost never a real deal (a seller doesn't leave 38%+ on the table);
        # it's usually the hedonic over-valuing a unit/leasehold off a high CV,
        # or a bad CV. Genuine below-value deals top out around +50%, so 1.6×
        # keeps real deals while cutting the false ones. Below 0.5× = wildly
        # overpriced or a broken input → also suppress.
        if not (0.5 * asking_f <= fair_value <= 1.6 * asking_f):
            fair_value = None
            warnings.append("fair_value_implausible_vs_asking")
        elif cv_f > 2.5 * asking_f:
            # CV more than 2.5× asking is a scraping error that inflates the
            # hedonic — don't trust the resulting estimate.
            fair_value = None
            warnings.append("fair_value_bad_cv")

    # ================================================================
    # STEP C: pick the pricing PATH for the displayed sale estimate.
    # Spec pseudocode: if asking is a real number AND not an auction
    # → asking × 0.95. Only genuine auctions (and listings with no
    # usable number) fall back to v3.5.
    # ================================================================
    is_auction = (listing_type or "").lower() == "auction"
    if asking_f > 0 and not is_auction:
        market = asking_f * ASKING_DISCOUNT
        band = market * BAND_LISTED
        return Prediction(
            pred_v35=round(pred_v35) if pred_v35 is not None else None,
            market_value=round(market / 1000.0) * 1000,
            pricing_path="asking",
            range_low=round((market - band) / 1000.0) * 1000,
            range_high=round((market + band) / 1000.0) * 1000,
            fair_value=round(fair_value) if fair_value is not None else None,
            beta_tier=beta_tier, correction_used=correction_used,
            bldg_correction_applied=bldg_applied,
            # Confidence reflects the ASKING price being a real number, but it was
            # previously hardcoded "high" even with zero supporting comps, which
            # surfaced as "0 comps - high confidence" on the listing card. An
            # asking price alone is a decent anchor, not a well-evidenced one, so
            # without any subtype comps behind it we report medium.
            n_subtype=n_subtype,
            confidence="high" if n_subtype > 0 else "medium",
            warnings=tuple(warnings),
        )

    # ---- v3.5 fallback path (auction / no usable asking) ----
    if pred_v35 is None:
        return Prediction(
            pred_v35=None, market_value=None, pricing_path="insufficient",
            range_low=None, range_high=None, fair_value=round(fair_value) if fair_value is not None else None,
            beta_tier=beta_tier, correction_used=correction_used,
            bldg_correction_applied=bldg_applied,
            n_subtype=n_subtype, confidence="insufficient",
            warnings=tuple(warnings + ["no_usable_price_and_no_v35"]),
        )

    # Confidence is bucket-driven; band is wider (±14% — std of sold/CV).
    if n_subtype >= 8:
        confidence = "medium"
    elif n_subtype >= 2:
        confidence = "low"
    else:
        confidence = "insufficient"

    band = pred_v35 * BAND_UNLISTED
    return Prediction(
        pred_v35=round(pred_v35),
        market_value=round(pred_v35 / 1000.0) * 1000,
        pricing_path="v35",
        range_low=round((pred_v35 - band) / 1000.0) * 1000,
        range_high=round((pred_v35 + band) / 1000.0) * 1000,
        fair_value=round(fair_value) if fair_value is not None else None,
        beta_tier=beta_tier, correction_used=correction_used,
        bldg_correction_applied=bldg_applied,
        n_subtype=n_subtype, confidence=confidence,
        warnings=tuple(warnings),
    )
