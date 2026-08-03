"""Opportunity Score — equal-weighted blend of three signals.

Signals (each in raw dollars to start with):
  underpriced  = max(fair_value - asking_price, 0)   # fair_value = sold-data valuation
  yield_signal = deposit * (cash_on_cash - worst_case_cash_on_cash)
  subdivision  = max(best_net_gain, 0)

Cashflow is negative for every Auckland listing under the current assumptions,
so the yield signal ranks by how *close to break-even* a listing gets rather
than by positive cashflow (which never occurs).

Raw score = W_u * underpriced + W_y * yield_signal + W_s * subdivision

After every listing in a batch has its raw score, we normalise to a 0-100
percentile rank for display on the Buy Score ring.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import assumptions as A
from . import cashflow as CF
from .buyprice import _title_bucket


@dataclass
class ScoreComponents:
    underpriced: float
    yield_signal: float
    subdivision: float
    raw_score: float
    is_underpriced: bool
    is_cashflow_positive: bool


def signals(
    *,
    asking_price: float | None,
    market_value: float | None,
    annual_cashflow: float | None,
    cash_on_cash: float | None,
    best_net_gain: float | None,
    confidence: str | None = None,
    fair_value: float | None = None,
    title_type: str | None = None,
) -> ScoreComponents:
    asking = asking_price or 0
    mv = market_value or 0

    # Deal-finding compares the asking price against our SOLD-DATA valuation. The old fallback to market_value compared the
    # asking price against itself (market_value = asking x 0.95), which can never
    # be a real discount. No sold-data value -> no deal signal.
    ref = fair_value if (fair_value and fair_value > 0) else 0
    underpriced = max(ref - asking, 0) if asking > 0 and ref > 0 else 0
    # Flag underpriced when asking is at least 5% below the independent fair
    # value — a meaningful discount, not noise.
    # Leasehold is excluded: only 41 leasehold sales exist in the whole sold set,
    # far too thin to price one against, and leasehold trades on terms (ground
    # rent, review dates) that none of our inputs capture.
    is_underpriced = (
        asking > 0 and ref > 0 and ref >= asking * 1.05
        and confidence in ("medium", "high")
        and _title_bucket(title_type) != "LH"
    )

    # Yield signal ranks by *least-negative* cashflow, not by positive cashflow.
    # Under these assumptions break-even needs ~7.1% gross yield while the top
    # tier is 5.5%, so cashflow is negative for every listing at every price —
    # the old `max(cf, 0)` therefore zeroed this pillar for all 14k rows, making
    # the "equal-weighted blend of three signals" a two-signal blend in practice.
    # Measure each listing against the worst-case tier at the same price so a
    # smaller shortfall scores higher, in dollars comparable to the other pillars.
    cf = annual_cashflow or 0
    coc = cash_on_cash or 0
    price = mv or asking
    # Same deposit the cashflow calc uses — A.LVR is the old fixed 65% and would
    # silently disagree with it once the deposit became a tunable assumption.
    deposit = price * CF.CashflowAssumptions().deposit_pct
    yield_signal = max(deposit * (coc - CF.worst_case_coc()), 0.0)
    is_cashflow_positive = cf > 0

    sub = max(best_net_gain or 0, 0)

    raw = (
        A.SCORE_WEIGHT_UNDERPRICED * underpriced
        + A.SCORE_WEIGHT_YIELD * yield_signal
        + A.SCORE_WEIGHT_SUBDIVISION * sub
    )

    return ScoreComponents(
        underpriced=underpriced,
        yield_signal=yield_signal,
        subdivision=sub,
        raw_score=raw,
        is_underpriced=is_underpriced,
        is_cashflow_positive=is_cashflow_positive,
    )


def percentile_rank_0_100(values: pd.Series) -> pd.Series:
    """Convert a series of raw scores to 0-100 percentile ranks.

    Rows with score==0 stay at 0 (no opportunity).  Rows with positive scores
    get rank-based 1-100. NaN stays NaN.
    """
    out = pd.Series(np.nan, index=values.index, dtype="float64")
    mask = values.notna() & (values > 0)
    if mask.any():
        ranked = values[mask].rank(method="average", pct=True) * 100
        out.loc[mask] = ranked.round(1)
    out.loc[values.notna() & (values <= 0)] = 0.0
    return out
