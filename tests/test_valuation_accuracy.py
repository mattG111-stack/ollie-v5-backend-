"""Holdout backtest: the valuation must beat quoting the raw council CV.

The valuation exists to be better than the number anyone can read off the
council website. For a long time it was not: a tight comp cascade (same suburb
+ beds + baths + land +/-15%) scored 10.06% median error against actual sale
prices, while raw CV scored 9.39%. It resolved down to 2-5 sales and reported
their noise as signal.

The current estimator groups broadly — what the same TYPE of house sells for
against CV in that AREA — and shrinks each estimate toward the wider area in
proportion to its sale count. That scores 8.99%.

These tests are slow-ish (they build a CompEngine per split) but they are the
only thing standing between a plausible-looking refactor and a silent accuracy
regression. Run: .venv/bin/pytest tests/test_valuation_accuracy.py -q
"""
from __future__ import annotations

import os
import pathlib

import numpy as np
import pandas as pd
import pytest

from app.pricing.buyprice import CompEngine
from app.pricing.comps import SoldDataset


def _resolve_data() -> pathlib.Path | None:
    """Locate the sold-comps CSV without hardcoding one machine's home directory.

    The old absolute path (/Users/matthewgrant/…) existed on exactly one laptop,
    so every one of these accuracy tests skipped silently everywhere else — CI
    included — and the guard against valuation drift was never actually running.

    Resolution order:
      1. $OLLIE_SOLD_COMPS_CSV — explicit override (used in CI).
      2. tests/fixtures/sold_comps.csv — a committed sample, if present.
      3. <repo>/data/1_sold_comps/sold_comps.csv — the local full dataset.
    """
    env = os.environ.get("OLLIE_SOLD_COMPS_CSV")
    if env:
        return pathlib.Path(env)
    here = pathlib.Path(__file__).resolve().parent
    for cand in (
        here / "fixtures" / "sold_comps.csv",
        here.parent / "data" / "1_sold_comps" / "sold_comps.csv",
    ):
        if cand.exists():
            return cand
    return None


DATA = _resolve_data()
pytestmark = pytest.mark.skipif(
    DATA is None or not DATA.exists(),
    reason="sold dataset not present — set OLLIE_SOLD_COMPS_CSV or add "
           "tests/fixtures/sold_comps.csv",
)

SEEDS = (1, 7, 13, 42, 99)


def _split(seed: int):
    sold = pd.read_csv(DATA, low_memory=False)
    idx = np.random.default_rng(seed).permutation(len(sold))
    cut = int(0.8 * len(idx))
    return sold.iloc[idx[:cut]].copy(), sold.iloc[idx[cut:]].copy()


def _errors(seed: int) -> tuple[float, float]:
    """(median % error of the model, median % error of raw CV) on held-out sales."""
    train, test = _split(seed)
    ce = CompEngine(SoldDataset(train).df)
    model, rawcv = [], []
    for _, r in test.iterrows():
        actual = pd.to_numeric(r.get("price_numeric"), errors="coerce")
        cv = pd.to_numeric(r.get("cv_numeric"), errors="coerce")
        if not (actual and actual > 50_000 and cv and cv > 0):
            continue
        ratio, _src = ce.cv_ratio_for(
            suburb=str(r.get("suburb")).strip(),
            district=str(r.get("district")).strip(),
            property_type=r.get("property_type"),
        )
        model.append(abs(cv * ratio - actual) / actual)
        rawcv.append(abs(cv - actual) / actual)
    return float(np.median(model)) * 100, float(np.median(rawcv)) * 100


@pytest.mark.parametrize("seed", SEEDS)
def test_valuation_beats_raw_cv(seed):
    """If the model is worse than the council figure, it is not earning its place."""
    model, rawcv = _errors(seed)
    assert model < rawcv, (
        f"seed {seed}: model {model:.2f}% vs raw CV {rawcv:.2f}% — "
        "the valuation is adding error, not removing it"
    )


def test_valuation_error_stays_under_ceiling():
    """Absolute guard: mean median error across splits must stay below 9.5%.

    Measured at 8.99% when the shrunk estimator landed. The ceiling leaves a
    little headroom for data refreshes without letting a real regression through
    — the old tight-comp cascade scored 10.06% and would fail this.
    """
    errs = [_errors(s)[0] for s in SEEDS]
    mean_err = float(np.mean(errs))
    assert mean_err < 9.5, f"mean median error {mean_err:.2f}% across {SEEDS}"


def test_shrinkage_reports_how_local_the_estimate_is():
    """The source string must distinguish a well-sampled suburb from a shrunk one."""
    train, _ = _split(7)
    ce = CompEngine(SoldDataset(train).df)
    _, src = ce.shrunk_cv_ratio(suburb="Nowhere At All", district="Nowhere",
                                property_type="House")
    assert src == "global"
    ratio, src = ce.shrunk_cv_ratio(suburb="Papakura", district="Papakura",
                                    property_type="House")
    assert 0.5 < ratio < 1.5
    assert src.startswith("suburb") or src in ("district", "global")


def test_ratio_is_bounded_for_every_suburb_in_the_data():
    """No area may produce a ratio that would invent or destroy value wholesale."""
    train, test = _split(7)
    ce = CompEngine(SoldDataset(train).df)
    seen = {(str(r.get("suburb")).strip(), str(r.get("district")).strip(),
             r.get("property_type")) for _, r in test.head(800).iterrows()}
    for suburb, district, ptype in seen:
        ratio, _ = ce.shrunk_cv_ratio(suburb=suburb, district=district,
                                      property_type=ptype)
        assert 0.5 <= ratio <= 1.5, f"{suburb}/{ptype} ratio {ratio}"
