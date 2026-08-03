"""Constants from the client's Excel 'Assumptions' tab.

Single source of truth for every tunable parameter the model uses.
"""

# === Comp-matching defaults (from Pricing Tool tab) ===
# A comparable must be the SAME product: identical bed and bath count, within
# 25% on both floor and land. A 3-bed is not a 4-bed with a tolerance.
COMP_BEDS_TOL = 0
COMP_BATHS_TOL = 0
COMP_FLOOR_PCT = 0.25
COMP_LAND_PCT = 0.25
COMP_MIN_PRICE = 50_000
LIST_MULTIPLIER = 1.05

# === Rental yield tiers (from Assumptions tab) ===
YIELD_DEFAULT = 0.04
YIELD_PREMIUM_THRESHOLD = 2_000_000  # CV above -> 3% yield
YIELD_PREMIUM = 0.03
YIELD_AFFORDABLE_THRESHOLD = 800_000  # CV below -> 5.5% yield
YIELD_AFFORDABLE = 0.055

# === Operating expense ratios (% of gross rent) ===
OPEX_RATES = 0.08
OPEX_INSURANCE = 0.04
OPEX_MAINTENANCE = 0.05
OPEX_VACANCY = 0.04
OPEX_MANAGEMENT = 0.08
OPEX_TOTAL = OPEX_RATES + OPEX_INSURANCE + OPEX_MAINTENANCE + OPEX_VACANCY + OPEX_MANAGEMENT  # 0.29

# === Mortgage assumptions ===
# Superseded by CashflowAssumptions.deposit_pct (30% deposit = 70% LVR).
# Kept only so older scripts importing A.LVR keep working.
LVR = 0.65
MORTGAGE_RATE = 0.0675
LOAN_TERM_YEARS = 30

# === Subdivision ===
SUBDIVISION_YIELD_FACTOR = 0.75
DEMOLITION_COST = 30_000
SERVICES_COST_PER_LOT = 80_000
# Cap the practical lot count for any single property. Anything > 20 lots is a
# large-scale developer property that the residential-rate subdivision math
# doesn't price correctly (would need staged consents, infrastructure, etc).
MAX_PRACTICAL_LOTS_TOTAL = 20

# === Opportunity scoring weights (equal-weighted by default) ===
SCORE_WEIGHT_UNDERPRICED = 1.0
SCORE_WEIGHT_YIELD = 1.0
SCORE_WEIGHT_SUBDIVISION = 1.0

# === Section value fallback ($/m² when suburb not in bare-land table) ===
DEFAULT_SECTION_PRICE_PER_M2 = 1_000
SECTION_PRICE_MIN = 500

# Section $/m² used only when a suburb has neither bare-section sales nor
# council land values. A fallback, not a floor — a real suburb rate is used
# as-is even if it lands below this.
SECTION_RATE_FALLBACK = 850
SECTION_PRICE_MAX = 5_000

# === Property-type routing ===
# English + Chinese keywords. 建地 = vacant land, 乡村住宅建地 = rural residential land,
# 土地 = land, 地皮 = plot of land
VACANT_TYPE_KEYWORDS = ("vacant", "section", "bare land", "建地", "土地", "地皮")
COMMERCIAL_TYPE_KEYWORDS = ("commercial", "industrial", "other -", "商业", "工业")

# Estimate sanity gate — if our prediction is way off the asking price,
# the comps almost certainly weren't appropriate. Downgrade confidence.
ESTIMATE_VS_ASKING_MAX_RATIO = 3.0
ESTIMATE_VS_ASKING_MIN_RATIO = 0.33

# Asking price sanity at ingest — $1, $2 placeholder values from "by negotiation" listings
ASKING_PRICE_MIN = 10_000

# === Confidence tiers (based on comp count) ===
CONFIDENCE_HIGH_MIN = 10
CONFIDENCE_MEDIUM_MIN = 3
CONFIDENCE_LOW_MIN = 1


def expected_yield_for_cv(cv: float | None) -> float:
    """Yield tier based on Council Valuation."""
    if cv is None:
        return YIELD_DEFAULT
    if cv > YIELD_PREMIUM_THRESHOLD:
        return YIELD_PREMIUM
    if cv < YIELD_AFFORDABLE_THRESHOLD:
        return YIELD_AFFORDABLE
    return YIELD_DEFAULT


def confidence_tier(comps_count: int) -> str:
    if comps_count >= CONFIDENCE_HIGH_MIN:
        return "high"
    if comps_count >= CONFIDENCE_MEDIUM_MIN:
        return "medium"
    if comps_count >= CONFIDENCE_LOW_MIN:
        return "low"
    return "insufficient"


def is_vacant_type(property_type: str | None) -> bool:
    if not property_type:
        return False
    s = str(property_type).lower()
    return any(k in s for k in VACANT_TYPE_KEYWORDS)


def is_commercial_type(property_type: str | None) -> bool:
    if not property_type:
        return False
    s = str(property_type).lower()
    return any(k in s for k in COMMERCIAL_TYPE_KEYWORDS)
