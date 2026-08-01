"""Rental + cashflow calculations.

Mirrors the client's 'Assumptions' tab numbers:
  Yield tiers   : premium (CV > $2M) 3% / default 4% / affordable (CV < $800k) 5.5%
  Operating exp : 29% of gross rent
  Mortgage      : 30% deposit (70% LVR), rate 6.75%, 30 yr amortising
  Cashflow runs off the BUY PRICE, not the asking price.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from . import assumptions as A
from .glm import canonical_type


@dataclass
class Cashflow:
    est_weekly_rent: float | None
    est_gross_yield: float | None
    annual_gross_rent: float | None
    annual_net_rent: float | None
    annual_mortgage: float | None
    annual_cashflow: float | None
    cash_on_cash: float | None  # cashflow / deposit
    yield_tier: str  # premium / default / affordable
    rent_source: str | None = None  # which rent tier was used; None = yield tier
    # Deposit fraction at which this property breaks even. More useful than a
    # yes/no at one assumed deposit: it says how much cash the deal actually
    # needs. 0.0 = positive even fully financed; None = net rent <= 0, so no
    # deposit works.
    breakeven_deposit_pct: float | None = None


@dataclass(frozen=True)
class CashflowAssumptions:
    """Every tunable behind the cashflow figure. Overridable per property.

    deposit_pct is the cash you put in; the rest is borrowed. A smaller deposit
    means a bigger loan, so cashflow gets harder even though cash-on-cash is
    measured against less cash. Break-even gross yield is ~7.7% at the 30%
    default, ~9.9% at a 10% deposit and ~7.1% at 35%.
    """
    deposit_pct: float = 0.30
    mortgage_rate: float = A.MORTGAGE_RATE
    loan_term_years: int = A.LOAN_TERM_YEARS
    opex_pct: float = A.OPEX_TOTAL
    weekly_rent: float | None = None   # override the observed/estimated rent
    buy_price: float | None = None     # override the modelled acquisition price


def annual_mortgage_payment(loan_amount: float, rate: float | None = None,
                            term_years: int | None = None) -> float:
    """Standard fixed-rate amortising annual payment."""
    if loan_amount <= 0:
        return 0.0
    r = (A.MORTGAGE_RATE if rate is None else rate) / 12.0
    n = (A.LOAN_TERM_YEARS if term_years is None else term_years) * 12
    if r == 0:
        monthly = loan_amount / n
    else:
        monthly = loan_amount * (r * (1 + r) ** n) / ((1 + r) ** n - 1)
    return monthly * 12


MIN_RENTS_FOR_MEDIAN = 3


class RentRates:
    """Observed weekly rent, looked up by area → bedrooms → property type.

    Built once per ingest from the active rental batch. Without it, rent is
    price × a flat yield tier, which is circular: rent derives from price, so
    cashflow can tell you nothing price did not already. Real rentals make the
    yield an observation instead.

    Cascade, tightest first — each tier needs MIN_RENTS_FOR_MEDIAN listings:
        1. suburb + type + beds
        2. suburb + beds            (type is the noisiest of the three)
        3. suburb                   (any type/beds — better than leaving the suburb)
        4. district + type + beds
        5. district + beds
        6. None  -> caller falls back to the yield tier
    """

    def __init__(self, rent_df: "pd.DataFrame | None"):
        self._t: dict[tuple, float] = {}
        if rent_df is None or len(rent_df) == 0:
            return
        df = rent_df.copy()
        rent = pd.to_numeric(df.get("weekly_rent"), errors="coerce")
        beds = pd.to_numeric(df.get("beds"), errors="coerce")
        ok = rent.notna() & (rent >= 100) & (rent <= 10_000)
        df = df[ok]
        if df.empty:
            return
        work = pd.DataFrame({
            "suburb": df.get("suburb").astype(str).str.strip(),
            "district": df.get("district").astype(str).str.strip(),
            "ctype": df.get("property_type").map(canonical_type),
            "beds": beds[ok].round(),
            "rent": rent[ok],
        })

        def _add(prefix: str, keys: list[str]) -> None:
            sub = work.dropna(subset=keys + ["rent"])
            if sub.empty:
                return
            g = sub.groupby(keys)["rent"]
            med, cnt = g.median(), g.size()
            for k in med.index:
                if cnt[k] >= MIN_RENTS_FOR_MEDIAN:
                    key = (prefix,) + (k if isinstance(k, tuple) else (k,))
                    self._t[key] = float(med[k])

        _add("sub_type_beds", ["suburb", "ctype", "beds"])
        _add("sub_beds", ["suburb", "beds"])
        _add("sub", ["suburb"])
        _add("dist_type_beds", ["district", "ctype", "beds"])
        _add("dist_beds", ["district", "beds"])

    def weekly_rent_for(
        self, *, suburb=None, district=None, property_type=None, beds=None
    ) -> tuple[float | None, str | None]:
        """Returns (weekly rent, which tier supplied it) or (None, None)."""
        ct = canonical_type(property_type) if property_type else None
        b = None
        try:
            b = round(float(beds)) if beds is not None and float(beds) == float(beds) else None
        except (TypeError, ValueError):
            b = None
        s = str(suburb).strip() if suburb else None
        d = str(district).strip() if district else None

        for key, tier in (
            ((("sub_type_beds"), s, ct, b), "suburb_type_beds"),
            ((("sub_beds"), s, b), "suburb_beds"),
            ((("sub"), s), "suburb"),
            ((("dist_type_beds"), d, ct, b), "district_type_beds"),
            ((("dist_beds"), d, b), "district_beds"),
        ):
            if None in key:
                continue
            hit = self._t.get(key)
            if hit:
                return hit, tier
        return None, None


def coc_for_yield(gross_yield: float) -> float:
    """Cash-on-cash for a given gross yield. Independent of price.

    Every term in the cashflow calc is proportional to price, so price cancels:
    CoC is a pure function of the yield tier. Used to establish the worst-case
    floor the yield signal is measured against (see scoring.signals).
    """
    ca = CashflowAssumptions()
    net_yield = gross_yield * (1 - ca.opex_pct)
    deposit_per_dollar = ca.deposit_pct
    mortgage_per_dollar = annual_mortgage_payment(
        1 - ca.deposit_pct, ca.mortgage_rate, ca.loan_term_years)
    return (net_yield - mortgage_per_dollar) / deposit_per_dollar


def worst_case_coc() -> float:
    """Lowest CoC any listing can have — the premium (lowest-yield) tier."""
    return coc_for_yield(min(A.YIELD_PREMIUM, A.YIELD_DEFAULT, A.YIELD_AFFORDABLE))


def breakeven_deposit(annual_net_rent: float, price: float,
                      rate: float, term_years: int) -> float | None:
    """Deposit fraction where net rent exactly covers the mortgage.

    Net rent must service the loan, so at break-even:
        annual_net = price x (1 - deposit) x k     (k = annual payment per $1)
    Rearranged: deposit = 1 - annual_net / (price x k). Clamped at 0 — a
    property that services itself fully financed needs no deposit to break even.
    Returns None when net rent is non-positive, where no deposit can help.
    """
    if price <= 0 or annual_net_rent <= 0:
        return None
    k = annual_mortgage_payment(1.0, rate, term_years)
    if k <= 0:
        return 0.0
    return max(0.0, 1.0 - annual_net_rent / (price * k))


def yield_tier_for(cv: float | None) -> tuple[float, str]:
    if cv is None:
        return A.YIELD_DEFAULT, "default"
    if cv > A.YIELD_PREMIUM_THRESHOLD:
        return A.YIELD_PREMIUM, "premium"
    if cv < A.YIELD_AFFORDABLE_THRESHOLD:
        return A.YIELD_AFFORDABLE, "affordable"
    return A.YIELD_DEFAULT, "default"


def _num(x) -> float | None:
    try:
        f = float(x)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


def compute(
    *,
    asking_price: float | None,
    market_value: float | None,
    cv: float | None,
    observed_weekly_rent: float | None = None,
    rent_source: str | None = None,
    buy_price: float | None = None,
    assumptions: "CashflowAssumptions | None" = None,
) -> Cashflow:
    """Run the full cashflow calc. Uses market_value if available, else asking_price.

    When an observed weekly rent is supplied (from the rental scrape, via
    RentRates) it drives the yield. Otherwise rent falls back to price x the CV
    yield tier, which is circular and cannot make cashflow vary independently of
    price -- see RentRates.
    """
    ca = assumptions or CashflowAssumptions()
    mv = _num(market_value)
    ap = _num(asking_price)
    cv = _num(cv)
    # Cashflow is what YOU pay, so the buy price leads; market value and asking
    # are only fallbacks when no acquisition price could be modelled.
    price = _num(ca.buy_price) or _num(buy_price) or mv or ap
    if not price or price <= 0:
        return Cashflow(None, None, None, None, None, None, None, "default", None)

    gross_yield, tier = yield_tier_for(cv)
    obs = _num(ca.weekly_rent) or _num(observed_weekly_rent)
    if _num(ca.weekly_rent):
        rent_source = "manual"
    if obs and obs > 0:
        weekly_rent = obs
        annual_gross = obs * 52
        gross_yield = annual_gross / price
    else:
        rent_source = None
        annual_gross = price * gross_yield
        weekly_rent = annual_gross / 52
    annual_net = annual_gross * (1 - ca.opex_pct)
    deposit = price * ca.deposit_pct
    loan = price - deposit
    annual_mort = annual_mortgage_payment(loan, ca.mortgage_rate, ca.loan_term_years)
    annual_cf = annual_net - annual_mort
    coc = annual_cf / deposit if deposit > 0 else None

    return Cashflow(
        est_weekly_rent=round(weekly_rent),
        est_gross_yield=round(gross_yield, 4),
        annual_gross_rent=round(annual_gross),
        annual_net_rent=round(annual_net),
        annual_mortgage=round(annual_mort),
        annual_cashflow=round(annual_cf),
        cash_on_cash=round(coc, 4) if coc is not None else None,
        yield_tier=tier,
        rent_source=rent_source,
        breakeven_deposit_pct=(
            round(bd, 4) if (bd := breakeven_deposit(
                annual_net, price, ca.mortgage_rate, ca.loan_term_years)) is not None
            else None),
    )
